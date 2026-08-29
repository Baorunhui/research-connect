#!/usr/bin/env bash
# 共享：解析项目应该使用的 Python 解释器路径。
# 被 run_local.sh / local_debug.sh 等脚本 source 使用。
#
# 优先级（从高到低）：
#   1. $DPR_PYTHON       （显式指定，最高）
#   2. dpr conda 环境    （miniforge/conda 的 envs/dpr/python，推荐：内置 Docling+torch）
#   3. 项目 .venv        （bootstrap_local.sh 建的 pyvenv）
#   4. python3 / python  （回退到 PATH）
#
# 输出：全局变量 DPR_RESOLVED_PYTHON
# 无需显式 source 返回；调用前确保已 source 本文件。

# 避免重复 source 时重复探测
if [ -n "${DPR_RESOLVED_PYTHON:-}" ]; then
  : # 已解析
fi

_resolve_project_python() {
  # 1) 显式指定
  if [ -n "${DPR_PYTHON:-}" ] && command -v "$DPR_PYTHON" >/dev/null 2>&1; then
    DPR_RESOLVED_PYTHON="$DPR_PYTHON"; return
  fi

  # 2) dpr conda 环境（miniforge/conda）
  local base=""
  if command -v conda >/dev/null 2>&1; then
    base="$(conda info --base 2>/dev/null | tr -d '\r')"
  elif [ -n "${CONDA_PREFIX:-}" ]; then
    base="$(dirname "${CONDA_PREFIX%/envs/*}")"
  fi
  # 常见安装路径兜底（Windows/Linux 通用）
  local home="${HOME:-$USERPROFILE}"
  for candidate in \
    "$base/envs/dpr/python" \
    "$base/envs/dpr/bin/python" \
    "$home/miniforge3/envs/dpr/python" \
    "$home/miniforge3/envs/dpr/bin/python" \
    "$home/miniforge3/envs/dpr/Scripts/python.exe" \
    "$home/miniconda3/envs/dpr/python" \
    "$home/miniconda3/envs/dpr/bin/python"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      DPR_RESOLVED_PYTHON="$candidate"; return
    fi
  done

  # 3) 项目 .venv
  local root d="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  for candidate in "$d/.venv/Scripts/python.exe" "$d/.venv/bin/python"; do
    if [ -x "$candidate" ]; then
      DPR_RESOLVED_PYTHON="$candidate"; return
    fi
  done

  # 4) PATH 回退
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      DPR_RESOLVED_PYTHON="$cand"; return
    fi
  done

  DPR_RESOLVED_PYTHON=""
}

if [ -z "${DPR_RESOLVED_PYTHON:-}" ]; then
  _resolve_project_python
fi