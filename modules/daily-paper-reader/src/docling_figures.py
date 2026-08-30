from __future__ import annotations

"""Docling 整图提取器：把 figure_pipeline（Docling）接入日报图片提取主链路。

figure_pipeline（见仓库 figure_pipeline/）用 Docling 版面分析，以编号图注
（Figure N:）为锚点，把同页同栏的多个 PictureItem union 合并成整图并纳入图注，
避免 DocLayout-YOLO 那种把 composite figure 拆成多个子图的问题。

本模块提供两层接入：
- parse_docling_result(): 纯转换。读 figure_pipeline 产出的 result.json + figures/*.png，
  转成与 paper_figures 同构的 figures/tables dict，并落 webp + meta.json。
  可用于离线批处理产物接入，不依赖 Docling/PyTorch。
- extract_media_with_docling(): 一站式。子进程调用 figure_pipeline/extract_paper_figures.py
  跑单篇 PDF，再交给 parse_docling_result() 转出。失败返回空，与 paper_figures 的降级链一致。

Docling 只在子进程里运行（隔离模型下载与转换器），本模块自身除 PIL/paper_figures 外无重依赖。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from paper_figures import _first_existing, _load_image_size, _save_webp_from_path
except Exception:  # 允许测试/直接运行时不依赖 paper_figures
    def _first_existing(candidates: List[str]) -> str:
        for candidate in candidates:
            path = str(candidate or "").strip()
            if path and os.path.exists(path):
                return path
        return ""

    def _load_image_size(path: str) -> tuple[int, int]:  # type: ignore[no-redef]
        from PIL import Image

        with Image.open(path) as img:
            img.load()
            return img.size

    def _save_webp_from_path(  # type: ignore[no-redef]
        src_path: str, dst_path: str
    ) -> tuple[int, int]:
        from PIL import Image

        with Image.open(src_path) as img:
            img.load()
            width, height = img.size
            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                export_img = bg
            elif img.mode != "RGB":
                export_img = img.convert("RGB")
            else:
                export_img = img.copy()
            export_img.save(dst_path, format="WEBP", quality=82, method=6)
            return width, height


WEBP_QUALITY = 82
FIGURE_META_VERSION = 2

DOCLING_PIPELINE_SCRIPT_ENV = "DOCLING_PIPELINE_SCRIPT"
DOCLING_PYTHON_ENV = "DOCLING_PYTHON"
DOCLING_DISABLE_ENV = "DPR_DISABLE_DOCLING"
DOCLING_TIMEOUT_ENV = "DOCLING_TIMEOUT_SECONDS"
DOCLING_PIPELINE_FILENAME = "extract_paper_figures.py"
DOCLING_LOG_LIMIT = 1200

# 子进程只设置跨平台稳定性选项。模型源默认使用 Hugging Face 官方站；如部署
# 环境确实需要镜像，使用 DPR_DOCLING_HF_ENDPOINT 单独配置，避免继承到一个
# 与 huggingface_hub 不兼容的全局 HF_ENDPOINT。
_DOCLING_SUBPROCESS_ENV = {
    "HF_HUB_DISABLE_XET": "1",
    "HF_HUB_DISABLE_SYMLINKS": "1",
    "TORCH_COMPILE_DISABLE": "1",
    # Windows GBK 控制台默认编码会读崩模型里的非 ASCII 文件，强制 UTF-8 模式
    "PYTHONUTF8": "1",
}

# result.json 里 anchor 的 image_path 都是 figN.png，落在 <out>/<slug>/figures/ 下。
# caption 以 Table/Figure 开头的分离为 table/figure。
TABLE_CAPTION_RE = re.compile(r"^\s*(?:Table|Tbl\.?)\s*\d+", re.IGNORECASE)


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _warn(message: str) -> None:
    print(f"[WARN] Docling 提取降级：{message}", flush=True)


def docling_enabled() -> bool:
    """开关：DPR_DISABLE_DOCLING 置 1/off 关闭；缺省开启。"""
    if _truthy_env(DOCLING_DISABLE_ENV):
        return False
    return True


def resolve_docling() -> Tuple[str, str]:
    """定位 figure_pipeline 的脚本与解释器，返回 (python, script)，任一缺失返回 ("","")。"""
    if not docling_enabled():
        return "", ""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    configured_python = str(os.getenv(DOCLING_PYTHON_ENV) or "").strip()
    python_candidates = [
        configured_python,
        os.path.join(project_root, ".venv-docling", "bin", "python"),
        os.path.join(project_root, ".venv-docling", "Scripts", "python.exe"),
        sys.executable,
    ]
    python_path = _first_existing(python_candidates)
    script_candidates = [
        str(os.getenv(DOCLING_PIPELINE_SCRIPT_ENV) or "").strip(),
        os.path.join(
            project_root,
            "figure_pipeline",
            DOCLING_PIPELINE_FILENAME,
        ),
        os.path.join(os.getcwd(), "figure_pipeline", DOCLING_PIPELINE_FILENAME),
    ]
    script_path = _first_existing(script_candidates)
    if not script_path or not os.path.exists(python_path):
        return "", ""
    return python_path, script_path


def parse_docling_result(
    result_path: str,
    figure_dir: str,
    figure_rel_prefix: str,
    table_dir: str,
    table_rel_prefix: str,
    *,
    result_root: str = "",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把 figure_pipeline 的 result.json 转成 paper_figures 同构的 figures/tables dict。

    result.json 结构（figure_pipeline 产出，schema_version 1）：
      anchors: [{key, caption, page_no, section, image_path, ...}]
      auxiliary_picture_indices: [int] —— 未被编号图注消费的原始 PictureItem 序号
      raw_figures: [{index, image_path, caption, ...}]

    映射到 paper_figures 契约字段：url/caption/page/index/width/height/label。
    index 用图号（fig 序号），caption 用图注全文，page 用 page_no。
    辅助候选（未编号图）用 index=1000+aux 序号并入，避免漏图。
    """
    if not result_path or not os.path.exists(result_path):
        return [], []
    try:
        with open(result_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        _warn(f"读取 result.json 失败：{e}")
        return [], []
    if not isinstance(payload, dict):
        return [], []

    paper = payload.get("paper") or {}
    slug = str(paper.get("slug") or "").strip()
    if result_root and slug:
        base_dir = os.path.join(result_root, slug)
    else:
        base_dir = os.path.dirname(result_path)

    figures: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []

    def _emit(anchor: Dict[str, Any]) -> None:
        key = str(anchor.get("key") or "").strip()
        image_path = str(anchor.get("image_path") or "").strip()
        if not image_path or not os.path.isabs(image_path):
            src = os.path.join(base_dir, image_path) if image_path else ""
        else:
            src = image_path
        if not src or not os.path.exists(src):
            _warn(f"anchor 图片缺失：{image_path or key}")
            return
        caption = str(anchor.get("caption") or "").strip()
        # 图号：key 形如 "fig1"/"figa3" → 取数字部分
        m = re.search(r"(\d+)", key)
        num = int(m.group(1)) if m else len(figures) + len(tables) + 1
        page = max(0, int(anchor.get("page_no") or 0))
        section = str(anchor.get("section") or "body").strip()

        is_table = bool(TABLE_CAPTION_RE.match(caption or "")) or key.startswith(("table", "tab"))
        out_dir = table_dir if is_table else figure_dir
        rel = table_rel_prefix if is_table else figure_rel_prefix
        os.makedirs(out_dir, exist_ok=True)
        file_name = f"{'table' if is_table else 'fig'}-{num:03d}.webp"
        abs_path = os.path.join(out_dir, file_name)
        try:
            width, height = _save_webp_from_path(src, abs_path)
        except Exception as e:
            _warn(f"转 webp 失败 {src}: {e}")
            return
        item: Dict[str, Any] = {
            "url": "/".join([rel.strip("/"), file_name]),
            "caption": caption,
            "page": page,
            "index": num,
            "width": width,
            "height": height,
            "label": "Table" if is_table else "Figure",
            "section": section,
            "extractor": "docling",
        }
        if is_table:
            tables.append(item)
        else:
            figures.append(item)

    # 1) 编号图注锚点（主图，含整图 + 图注）
    anchors = payload.get("anchors")
    if isinstance(anchors, list):
        for anchor in anchors:
            if isinstance(anchor, dict):
                _emit(anchor)

    # 2) 辅助候选（未编号图，防漏）
    aux_indices = payload.get("auxiliary_picture_indices") or []
    raw_figures = payload.get("raw_figures") or []
    if isinstance(aux_indices, list) and isinstance(raw_figures, list):
        raw_by_index = {}
        for rf in raw_figures:
            if isinstance(rf, dict):
                raw_by_index[rf.get("index")] = rf
        for idx in aux_indices:
            rf = raw_by_index.get(idx)
            if not isinstance(rf, dict):
                continue
            image_path = str(rf.get("image_path") or "").strip()
            src = os.path.join(base_dir, image_path) if image_path and not os.path.isabs(image_path) else image_path
            if not src or not os.path.exists(src):
                continue
            caption = str(rf.get("caption") or "").strip()
            num = 1000 + int(idx)
            file_name = f"fig-{num:03d}.webp"
            abs_path = os.path.join(figure_dir, file_name)
            os.makedirs(figure_dir, exist_ok=True)
            try:
                width, height = _save_webp_from_path(src, abs_path)
            except Exception:
                continue
            figures.append(
                {
                    "url": "/".join([figure_rel_prefix.strip("/"), file_name]),
                    "caption": caption,
                    "page": 0,
                    "index": num,
                    "width": width,
                    "height": height,
                    "label": "Figure",
                    "section": "auxiliary",
                    "extractor": "docling",
                }
            )

    if figures or tables:
        os.makedirs(figure_dir, exist_ok=True)
        with open(os.path.join(figure_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"version": FIGURE_META_VERSION, "extractor": "docling", "figures": figures},
                f,
                ensure_ascii=False,
                indent=2,
            )
        if tables:
            os.makedirs(table_dir, exist_ok=True)
            with open(os.path.join(table_dir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {"version": FIGURE_META_VERSION, "extractor": "docling", "tables": tables},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
    return figures, tables


def _stub_main(real_cmd: List[str]) -> List[str]:
    """构造可移植的启动命令：注入 resource stub 再 runpy 运行 figure_pipeline 脚本。

    extract_paper_figures.py 顶部 import resource（Windows 无此模块）。这里用
    python -c 预先向 sys.modules 塞一个 resource stub，再 runpy.run_path 脚本。
    返回完整的 argv 中第一个命令（子进程用 python -c <code> script... 不合适跨参数）。
    采用 runpy 方案：把"脚本路径 + 剩余参数"拼进注入代码。
    """
    # real_cmd 形如 [script, arg1, arg2...]（不含 python 本身）
    script = real_cmd[0]
    args_json = json.dumps(real_cmd[1:])
    stub = (
        "import sys,types,json,runpy\n"
        "script=sys.argv[1]\n"
        "args=json.loads(sys.argv[2])\n"
        "_stub=types.ModuleType('resource')\n"
        "_stub.RUSAGE_SELF=0\n"
        "class _R:\n ru_maxrss=0\n"
        "_stub.getrusage=lambda who:_R()\n"
        "_stub.getpagesize=lambda:4096\n"
        "sys.modules['resource']=_stub\n"
        "sys.argv=[script]+args\n"
        "runpy.run_path(script,run_name='__main__')\n"
    )
    return ["-c", stub, script, args_json]


def extract_media_with_docling(
    pdf_path: str,
    figure_dir: str,
    figure_rel_prefix: str,
    table_dir: str,
    table_rel_prefix: str,
    *,
    scale: float = 2.0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """一站式：子进程跑 figure_pipeline 提取单篇 PDF，再转成 paper_figures 契约。

    失败返回空列表并打印警告，不抛异常（与 paper_figures 风格一致）。
    """
    if not pdf_path or not os.path.exists(pdf_path):
        _warn(f"PDF 不存在：{pdf_path}")
        return [], []
    python_path, script_path = resolve_docling()
    if not python_path or not script_path:
        if docling_enabled():
            _warn("未定位到 figure_pipeline 脚本或解释器，改用备用提取器。")
        return [], []

    timeout = int(float(os.getenv(DOCLING_TIMEOUT_ENV) or "1500"))
    tmp_root = tempfile.mkdtemp(prefix="docling_run_")
    try:
        cmd = [
            script_path,
            str(pdf_path),
            "--output",
            tmp_root,
            "--scale",
            str(float(scale)),
        ]
        launch = [python_path] + _stub_main(cmd)
        env = dict(os.environ)
        env.update(_DOCLING_SUBPROCESS_ENV)
        env["HF_ENDPOINT"] = str(
            os.getenv("DPR_DOCLING_HF_ENDPOINT") or "https://huggingface.co"
        ).strip()
        try:
            proc = subprocess.run(
                launch,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(int(timeout), 60),
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            _warn(f"执行超时（>{int(timeout)}s），改用备用提取器。")
            return [], []
        except Exception as e:
            _warn(f"子进程启动失败：{e}，改用备用提取器。")
            return [], []
        if proc.returncode != 0:
            detail = _tail(proc.stdout or "")
            _warn(f"执行失败 returncode={proc.returncode}；输出:{detail}")
            return [], []

        # 定位 result.json（output/<slug>/result.json 或嵌套任意一层）
        result_path = _find_result_json(tmp_root)
        if not result_path:
            detail = _tail(proc.stdout or "")
            _warn(f"未产生产物（result.json 缺失）{detail}")
            return [], []
        return parse_docling_result(
            result_path,
            figure_dir,
            figure_rel_prefix,
            table_dir,
            table_rel_prefix,
            result_root=tmp_root,
        )
    finally:
        import shutil

        shutil.rmtree(tmp_root, ignore_errors=True)


def _find_result_json(root: str) -> str:
    for dirpath, _dirs, files in os.walk(root):
        if "result.json" in files:
            return os.path.join(dirpath, "result.json")
    return ""


def _tail(text: str, limit: int = DOCLING_LOG_LIMIT) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(compact) <= limit:
        return compact
    return "..." + compact[-limit:]
