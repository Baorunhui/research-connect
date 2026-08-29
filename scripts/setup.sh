#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
VENV_DIR="${RESEARCH_CONNECT_VENV:-$ROOT_DIR/.venv}"
CONSTRAINTS_FILE="$ROOT_DIR/constraints.txt"
DATA_DIR="${RESEARCH_CONNECT_DATA_DIR:-$HOME/.research-connect/data}"
export RESEARCH_CONNECT_DATA_DIR="$DATA_DIR"
export PLAYWRIGHT_BROWSERS_PATH="$DATA_DIR/browsers"

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 14) else "Research Connect requires Python 3.11-3.13")'
if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python"
"$VENV_PY" -m pip install --upgrade pip setuptools wheel
"$VENV_PY" -m pip install -c "$CONSTRAINTS_FILE" -e "$ROOT_DIR/packages/research-connect-core"
"$VENV_PY" -m pip install -c "$CONSTRAINTS_FILE" -e "$ROOT_DIR[dev]"
"$VENV_PY" -m pip install --no-deps \
  -e "$ROOT_DIR/apps/connect-hub" \
  -e "$ROOT_DIR/apps/report-hub" \
  -e "$ROOT_DIR/modules/citationclaw" \
  -e "$ROOT_DIR/modules/xhs-agent"

if [ "${RESEARCH_CONNECT_INSTALL_DOCLING:-0}" = "1" ]; then
  "$VENV_PY" -m pip install -c "$CONSTRAINTS_FILE" -e "$ROOT_DIR[docling]"
fi

"$VENV_PY" -m playwright install chromium
"$VENV_PY" -m pip check
printf 'Research Connect environment ready: %s\n' "$VENV_DIR"
printf 'Shared data root: %s\n' "$DATA_DIR"
