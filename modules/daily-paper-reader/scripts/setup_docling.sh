#!/usr/bin/env bash
# 一键搭建 Docling 整图提取环境，并默认启用。
#
# 做什么：
#   1. 在项目根建独立 venv（默认 .venv-docling，避免污染 dpr 主环境——
#      docling 依赖链会强制 torch==2.13.0 CPU 版，与 dpr 的 CUDA torch 冲突）
#   2. 装 CPU 版 PyTorch + Docling + pypdfium2 + Pillow
#   3. 把 DOCLING_PYTHON 写进 .env（setdefault 语义，不覆盖已有值）
#   4. 之后项目启动（load_local_env 加载 .env）即默认启用 Docling 整图提取；
#      日报 ensure_paper_media 会先走 Docling，空/失败只回落 PyMuPDF。
#
# 用法：
#   bash scripts/setup_docling.sh
#
# 可覆盖的环境变量：
#   DPR_DOCLING_VENV     venv 目录（默认 <root>/.venv-docling）
#   DPR_PYTHON           用于建 venv 的基础解释器（默认依次尝试 python3 / python）
#   DPR_TORCH_INDEX_URL  torch 下载源（默认 https://download.pytorch.org/whl/cpu
#                        国内网络不稳时可设 https://mirrors.aliyun.com/pytorch-wheels/cpu）
#   DPR_PIP_INDEX_URL    其余依赖 PyPI 源（默认 https://pypi.org/simple
#                        国内可设 https://mirrors.aliyun.com/pypi/simple/）
#   DPR_SKIP_INSTALL=1   只建 venv + 写 .env，不装依赖（复用已装好的环境）
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${DPR_DOCLING_VENV:-$ROOT_DIR/.venv-docling}"
TORCH_INDEX_URL="${DPR_TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}"
PIP_INDEX_URL="${DPR_PIP_INDEX_URL:-}"
SKIP_INSTALL="${DPR_SKIP_INSTALL:-0}"

log() { printf '[setup-docling] %s\n' "$*"; }
fail() { printf '[setup-docling] ERROR: %s\n' "$*" >&2; exit 1; }

# 选择一个可用的基础 python（Windows 的 python3 可能是 MS Store stub，须实测能出版本）
PY=""
if [ -n "${DPR_PYTHON:-}" ]; then
  if command -v "$DPR_PYTHON" >/dev/null 2>&1; then PY="$DPR_PYTHON"; else fail "指定的 python 不存在：$DPR_PYTHON"; fi
else
  for cand in python3 python py; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" --version >/dev/null 2>&1; then
      PY="$cand"; break
    fi
  done
fi
[ -n "$PY" ] || fail "未找到可用的 Python（可设置 DPR_PYTHON 指向你的 python3）"

log "基础解释器：$PY $("$PY" --version 2>&1)"

# 1) 建 venv
if [ ! -d "$VENV_DIR" ]; then
  log "创建 venv：$VENV_DIR"
  "$PY" -m venv "$VENV_DIR"
else
  log "venv 已存在：$VENV_DIR"
fi
if [ -x "$VENV_DIR/Scripts/python.exe" ]; then
  VENV_PY="$VENV_DIR/Scripts/python.exe"
elif [ -x "$VENV_DIR/bin/python" ]; then
  VENV_PY="$VENV_DIR/bin/python"
elif [ -x "$VENV_DIR/python.exe" ]; then
  # conda env 布局：解释器在环境根目录
  VENV_PY="$VENV_DIR/python.exe"
else
  fail "找不到可用的解释器（预期 Scripts/python.exe、bin/python 或 python.exe）"
fi

# 写进 .env 的路径：Windows(Git Bash/MSYS) 下统一转成 Windows 可执行路径，
# 保证 docling_figures 用 subprocess 直接跑；本脚本内的命令调用继续用 posix $VENV_PY。
VENV_PY_DOTENV="$VENV_PY"
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*)
    if command -v cygpath >/dev/null 2>&1; then
      VENV_PY_DOTENV="$(cygpath -w "$VENV_PY")"
    else
      VENV_PY_DOTENV="$(printf '%s' "$VENV_PY" | sed 's#/#\\#g')"
    fi
    ;;
esac

log "Docling 解释器：$VENV_PY"
log "写入 .env 的路径：$VENV_PY_DOTENV"

# 2) 装依赖
if [ "$SKIP_INSTALL" = "1" ]; then
  log "DPR_SKIP_INSTALL=1，跳过依赖安装"
else
  log "升级 pip"
  "$VENV_PY" -m pip install --upgrade pip

  log "安装 CPU 版 PyTorch（$TORCH_INDEX_URL）"
  "$VENV_PY" -m pip install --index-url "$TORCH_INDEX_URL" "torch" "torchvision"

  log "安装 Docling 及提图依赖"
  PIP_ARGS=()
  if [ -n "$PIP_INDEX_URL" ]; then
    PIP_ARGS+=(--index-url "$PIP_INDEX_URL")
    PIP_ARGS+=(--trusted-host "$(printf '%s' "$PIP_INDEX_URL" | sed -E 's#^https?://##; s#/.*##')")
  fi
  "$VENV_PY" -m pip install "${PIP_ARGS[@]}" \
    "docling==2.119.0" "pypdfium2==5.12.1" "Pillow" "dill"
fi

# 3) 校验能 import docling
if ! "$VENV_PY" -c "import docling" >/dev/null 2>&1; then
  fail "docling 未能导入：$VENV_PY 请检查安装"
fi
log "校验通过：$VENV_PY 可导入 docling（$("$VENV_PY" -c 'import docling;print(docling.__version__)' 2>/dev/null)）"

# 4) 把 DOCLING_PYTHON 写入 .env（setdefault：不覆盖已有）
ENV_FILE="$ROOT_DIR/.env"
[ -f "$ENV_FILE" ] || { touch "$ENV_FILE"; log "已创建空 .env"; }
# 写入 .env 的 DOCLING_PYTHON：用 Windows 可执行路径（subprocess 可直接跑）
PY_VALUE="$VENV_PY_DOTENV"
if grep -qE "^DOCLING_PYTHON=" "$ENV_FILE"; then
  existing="$(grep -E '^DOCLING_PYTHON=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
  log "检测到已有 DOCLING_PYTHON=$existing，不覆盖"
else
  {
    echo ""
    echo "# Docling 整图提取解释器（scripts/setup_docling.sh 写入；失败时回落 PyMuPDF）"
    echo "# DPR_DISABLE_DOCLING=1 可整体关闭 Docling"
    echo "DOCLING_PYTHON=$PY_VALUE"
  } >> "$ENV_FILE"
  log "已写入 .env: DOCLING_PYTHON=$PY_VALUE"
fi

# 5) 确认 figure_pipeline 脚本存在
FPP="figure_pipeline/extract_paper_figures.py"
if [ -f "$ROOT_DIR/$FPP" ]; then
  log "figure_pipeline 脚本就绪：$FPP"
else
  fail "未找到 $FPP（期望 figure_pipeline/extract_paper_figures.py）"
fi

cat <<EOF

[setup-docling] 完成，Docling 已默认启用。
- 开启日报图提取时，ensure_paper_media 将先走 Docling 整图提取；
  空/失败只回落 PyMuPDF，不安装或调用 PaperCropper/DocLayout-YOLO/OpenCV。
- 关闭：在 .env 设 DPR_DISABLE_DOCLING=1。
- 换解释器：改 .env 的 DOCLING_PYTHON。
- 模型首次运行会从 HuggingFace 下载（适配层已自动走 hf-mirror、关闭 Xet）。
验证：python -m pytest tests/test_docling_figures.py tests/test_paper_figures.py
EOF
