from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image


MINERU_API_BASE_DEFAULT = "https://mineru.net"
MINERU_MODEL_VERSION_DEFAULT = "vlm"
MINERU_POLL_INTERVAL_DEFAULT = 5
MINERU_POLL_TIMEOUT_DEFAULT = 900
MINERU_UPLOAD_TIMEOUT = 120
MINERU_DOWNLOAD_TIMEOUT = 180
MINERU_REQUEST_TIMEOUT = 30

WEBP_QUALITY = 82

FIGURE_CAPTION_RE = re.compile(r"(?:Figure|Fig\.?)\s*(\d+)\s*[.:]?", re.IGNORECASE)
TABLE_CAPTION_RE = re.compile(r"Table\s*(\d+)\s*[.:]?", re.IGNORECASE)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _warn(message: str) -> None:
    print(f"[WARN] MinerU: {message}", flush=True)


def _info(message: str) -> None:
    print(f"[INFO] MinerU: {message}", flush=True)


def _save_webp_from_path(src_path: str, dst_path: str) -> Tuple[int, int]:
    with Image.open(src_path) as img:
        img.load()
        return _save_webp_from_image(img, dst_path)


def _save_webp_from_image(img: Image.Image, dst_path: str) -> Tuple[int, int]:
    width, height = img.size
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        export_img = bg
    elif img.mode != "RGB":
        export_img = img.convert("RGB")
    else:
        export_img = img.copy()
    export_img.save(dst_path, format="WEBP", quality=WEBP_QUALITY, method=6)
    return width, height


def _caption_number(caption: str, kind: str) -> int:
    text = str(caption or "").strip()
    if not text:
        return 0
    pattern = FIGURE_CAPTION_RE if kind == "figure" else TABLE_CAPTION_RE
    m = pattern.search(text)
    return int(m.group(1)) if m else 0


class MinerUClient:
    """MinerU 云端 API 客户端（mineru.net）。

    流程：申请上传 URL → PUT 上传 PDF → 轮询 batch_id → 下载 zip → 解压。
    token 从环境变量 MINERU_API_TOKEN 读取，也可在构造时显式传入。
    """

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        model_version: Optional[str] = None,
        poll_interval: Optional[int] = None,
        poll_timeout: Optional[int] = None,
    ) -> None:
        self.token = str(token or os.getenv("MINERU_API_TOKEN") or "").strip()
        if not self.token:
            raise RuntimeError(
                "MinerU API token 未配置：请在 .env 中设置 MINERU_API_TOKEN "
                "（在 https://mineru.net/apiManage 生成）。"
            )
        self.base_url = str(base_url or os.getenv("MINERU_API_BASE") or MINERU_API_BASE_DEFAULT).rstrip("/")
        self.model_version = str(
            model_version or os.getenv("MINERU_MODEL_VERSION") or MINERU_MODEL_VERSION_DEFAULT
        ).strip() or MINERU_MODEL_VERSION_DEFAULT
        self.poll_interval = int(
            poll_interval or os.getenv("MINERU_POLL_INTERVAL") or MINERU_POLL_INTERVAL_DEFAULT
        )
        self.poll_timeout = int(
            poll_timeout or os.getenv("MINERU_POLL_TIMEOUT") or MINERU_POLL_TIMEOUT_DEFAULT
        )

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

    def submit_batch(self, pdf_paths: List[str]) -> str:
        """申请一批上传 URL 并上传所有 PDF，返回 batch_id。"""
        if not pdf_paths:
            raise ValueError("pdf_paths 不能为空")
        if len(pdf_paths) > 200:
            raise ValueError("单批最多 200 个文件")

        files_payload = []
        for idx, path in enumerate(pdf_paths):
            name = os.path.basename(path)
            files_payload.append({"name": name, "data_id": f"dpr-{idx}-{int(time.time())}"})

        url = f"{self.base_url}/api/v4/file-urls/batch"
        resp = requests.post(
            url,
            headers=self._headers(),
            json={"files": files_payload, "model_version": self.model_version},
            timeout=MINERU_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("code") or -1) != 0:
            raise RuntimeError(f"申请上传 URL 失败：{data.get('msg') or data}")

        batch_id = str(data["data"]["batch_id"])
        upload_urls = data["data"]["file_urls"]
        if len(upload_urls) != len(pdf_paths):
            raise RuntimeError(
                f"上传 URL 数量({len(upload_urls)})与文件数({len(pdf_paths)})不匹配"
            )

        for path, upload_url in zip(pdf_paths, upload_urls):
            with open(path, "rb") as f:
                put_resp = requests.put(upload_url, data=f, timeout=MINERU_UPLOAD_TIMEOUT)
            if put_resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"上传 {os.path.basename(path)} 失败：HTTP {put_resp.status_code}"
                )
        _info(f"已提交 {len(pdf_paths)} 个 PDF，batch_id={batch_id}")
        return batch_id

    def poll_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        """轮询 batch 直到全部 done 或超时，返回 extract_result 列表。"""
        url = f"{self.base_url}/api/v4/extract-results/batch/{batch_id}"
        deadline = time.time() + self.poll_timeout
        last_progress = ""
        while time.time() < deadline:
            resp = requests.get(url, headers=self._headers(), timeout=MINERU_REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if int(data.get("code") or -1) != 0:
                raise RuntimeError(f"轮询失败：{data.get('msg') or data}")
            results = data.get("data", {}).get("extract_result", []) or []
            pending = [r for r in results if str(r.get("state") or "") not in ("done", "failed")]
            progress_parts = []
            for r in results:
                state = r.get("state") or "?"
                if state == "running":
                    p = r.get("extract_progress") or {}
                    progress_parts.append(
                        f"{r.get('file_name','?')}:{p.get('extracted_pages','?')}/{p.get('total_pages','?')}"
                    )
                else:
                    progress_parts.append(f"{r.get('file_name','?')}:{state}")
            progress = " | ".join(progress_parts)
            if progress != last_progress:
                _info(f"进度：{progress}")
                last_progress = progress
            if not pending:
                failed = [r for r in results if r.get("state") == "failed"]
                if failed:
                    names = ", ".join(r.get("file_name", "?") for r in failed)
                    _warn(f"{len(failed)} 个文件解析失败：{names}")
                _info(f"批次完成：done={len(results)-len(failed)} failed={len(failed)}")
                return results
            time.sleep(self.poll_interval)
        raise TimeoutError(f"轮询超时（>{self.poll_timeout}s），batch_id={batch_id}")

    def download_zip(self, zip_url: str, extract_dir: str) -> str:
        """下载 zip 并解压到 extract_dir，返回解压后的根目录。"""
        os.makedirs(extract_dir, exist_ok=True)
        resp = requests.get(zip_url, timeout=MINERU_DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            zf.extractall(extract_dir)
        # zip 内通常是 <pdf_name>/ 目录，找到含 content_list.json 的那一层
        for root, _dirs, files in os.walk(extract_dir):
            if any(name.endswith("_content_list.json") for name in files):
                return root
        return extract_dir

    def find_content_list(self, doc_dir: str) -> Optional[str]:
        """在解压目录里找 *_content_list.json（优先非 v2）。"""
        candidates = [
            p for p in Path(doc_dir).rglob("*_content_list.json")
            if not p.name.endswith("_v2.json")
        ]
        if candidates:
            return str(candidates[0])
        v2 = list(Path(doc_dir).rglob("*_content_list_v2.json"))
        return str(v2[0]) if v2 else None


def parse_content_list(
    content_list_path: str,
    doc_dir: str,
    output_dir: str,
    relative_prefix: str,
    *,
    table_output_dir: Optional[str] = None,
    table_relative_prefix: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把 MinerU 的 content_list.json 转成和 paper_figures 同构的 figures/tables dict。

    输出 dict 字段：url / caption / page / index / width / height / label，
    另带额外字段 table_html / bbox / extractor 供对照评估使用。
    """
    if not content_list_path or not os.path.exists(content_list_path):
        return [], []

    try:
        with open(content_list_path, "r", encoding="utf-8") as f:
            blocks = json.load(f)
    except Exception as e:
        _warn(f"读取 content_list 失败：{e}")
        return [], []
    if not isinstance(blocks, list):
        return [], []

    figure_dir = output_dir
    table_dir = table_output_dir or output_dir
    fig_rel = relative_prefix
    tbl_rel = table_relative_prefix or relative_prefix
    os.makedirs(figure_dir, exist_ok=True)
    if table_dir != figure_dir:
        os.makedirs(table_dir, exist_ok=True)

    figures: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []
    fig_fallback_idx = 0
    table_fallback_idx = 0

    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").lower()
        if btype not in ("image", "table", "chart"):
            continue

        # chart 也归入 figure（MinerU 把图形化的 chart 单独分类）
        is_table = btype == "table"
        kind = "table" if is_table else "figure"
        label = "Table" if is_table else "Figure"

        img_path_rel = str(block.get("img_path") or "").strip()
        if not img_path_rel:
            continue
        src_img = os.path.join(doc_dir, img_path_rel) if not os.path.isabs(img_path_rel) else img_path_rel
        if not os.path.exists(src_img):
            _warn(f"图片缺失：{img_path_rel}")
            continue

        # caption
        if is_table:
            caption_list = block.get("table_caption") or []
        else:
            caption_list = block.get("image_caption") or block.get("chart_caption") or []
        caption = " ".join(str(c) for c in caption_list).strip() if isinstance(caption_list, list) else str(caption_list or "").strip()

        # index：优先从 caption 提取编号，否则递增
        num = _caption_number(caption, kind)
        if num:
            index = num
        else:
            if is_table:
                table_fallback_idx += 1
                index = table_fallback_idx
            else:
                fig_fallback_idx += 1
                index = fig_fallback_idx

        file_name = f"{'table' if is_table else 'fig'}-{index:03d}.webp"
        out_dir = table_dir if is_table else figure_dir
        rel = tbl_rel if is_table else fig_rel
        abs_path = os.path.join(out_dir, file_name)
        try:
            width, height = _save_webp_from_path(src_img, abs_path)
        except Exception as e:
            _warn(f"转 webp 失败 {img_path_rel}: {e}")
            continue

        item: Dict[str, Any] = {
            "url": "/".join([rel.strip("/"), file_name]),
            "caption": caption,
            "page": int(block.get("page_idx") or 0) + 1,
            "index": index,
            "width": width,
            "height": height,
            "label": label,
            "bbox": block.get("bbox") or [],
            "extractor": "mineru",
        }
        if is_table:
            table_body = str(block.get("table_body") or "").strip()
            if table_body:
                item["table_html"] = table_body
            tables.append(item)
        else:
            figures.append(item)

    _info(f"解析完成：figures={len(figures)} tables={len(tables)}")
    return figures, tables


def extract_media_with_mineru(
    pdf_path: str,
    output_dir: str,
    relative_prefix: str,
    *,
    table_output_dir: Optional[str] = None,
    table_relative_prefix: Optional[str] = None,
    client: Optional[MinerUClient] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """一站式：提交单篇 PDF 到 MinerU 云端，轮询，下载，解析为 figures/tables dict。

    失败返回空列表并打印警告，不抛异常（与 paper_figures 风格一致）。
    """
    if not pdf_path or not os.path.exists(pdf_path):
        _warn(f"PDF 不存在：{pdf_path}")
        return [], []

    try:
        cli = client or MinerUClient()
    except Exception as e:
        _warn(str(e))
        return [], []

    try:
        batch_id = cli.submit_batch([pdf_path])
        results = cli.poll_batch(batch_id)
    except Exception as e:
        _warn(f"提交/轮询失败：{e}")
        return [], []

    # 单文件提交，取第一个结果
    if not results:
        _warn("未返回任何结果")
        return [], []
    result = results[0]
    if result.get("state") != "done":
        _warn(f"解析未完成：state={result.get('state')} err={result.get('err_msg')}")
        return [], []
    zip_url = str(result.get("full_zip_url") or "").strip()
    if not zip_url:
        _warn("缺少 full_zip_url")
        return [], []

    extract_dir = os.path.join(output_dir, "_mineru_raw")
    try:
        doc_dir = cli.download_zip(zip_url, extract_dir)
    except Exception as e:
        _warn(f"下载/解压 zip 失败：{e}")
        return [], []

    content_list_path = cli.find_content_list(doc_dir)
    if not content_list_path:
        _warn(f"未找到 content_list.json：{doc_dir}")
        return [], []

    return parse_content_list(
        content_list_path,
        doc_dir=doc_dir,
        output_dir=output_dir,
        relative_prefix=relative_prefix,
        table_output_dir=table_output_dir,
        table_relative_prefix=table_relative_prefix,
    )
