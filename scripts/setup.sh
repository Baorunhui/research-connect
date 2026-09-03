#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -n "${PYTHON:-}" ]; then
  PYTHON_BIN="$PYTHON"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  printf 'Python was not found on PATH. Activate a Conda environment or install Python 3.11-3.13.\n' >&2
  exit 2
fi
VENV_DIR="${RESEARCH_CONNECT_VENV:-$ROOT_DIR/.venv}"
CONSTRAINTS_FILE="$ROOT_DIR/constraints.txt"
DATA_DIR="${RESEARCH_CONNECT_DATA_DIR:-$HOME/.research-connect/data}"
export RESEARCH_CONNECT_DATA_DIR="$DATA_DIR"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$DATA_DIR/browsers}"
WITH_DOCLING="${RESEARCH_CONNECT_INSTALL_DOCLING:-0}"
WITH_DEV=0
SKIP_BROWSER=0

usage() {
  cat <<'EOF'
Usage: ./scripts/setup.sh [--with-docling] [--dev] [--skip-browser]
  --with-docling  Install Docling PDF/figure extraction support (large optional dependency).
  --dev           Install pytest and developer dependencies.
  --skip-browser  Do not install Playwright Chromium (disables browser rendering paths).
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --with-docling) WITH_DOCLING=1 ;;
    --dev) WITH_DEV=1 ;;
    --skip-browser) SKIP_BROWSER=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 14) else "Research Connect requires Python 3.11-3.13")'
if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PY" ]; then
  printf 'Could not locate Python in environment %s. Set RESEARCH_CONNECT_VENV to a valid environment.\n' "$VENV_DIR" >&2
  exit 2
fi
"$VENV_PY" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 14) else "The existing virtual environment does not use Python 3.11-3.13")'
printf 'Using Python: %s\n' "$PYTHON_BIN"
printf 'Installing into environment: %s\n' "$VENV_DIR"
"$VENV_PY" -m pip install --upgrade pip setuptools wheel
"$VENV_PY" -m pip install -c "$CONSTRAINTS_FILE" -e "$ROOT_DIR/packages/research-connect-core"
ROOT_SPEC="$ROOT_DIR"
if [ "$WITH_DEV" = "1" ]; then ROOT_SPEC="$ROOT_DIR[dev]"; fi
"$VENV_PY" -m pip install -c "$CONSTRAINTS_FILE" -e "$ROOT_SPEC"
"$VENV_PY" -m pip install --no-deps \
  -e "$ROOT_DIR/apps/connect-hub" \
  -e "$ROOT_DIR/modules/citationclaw" \
  -e "$ROOT_DIR/modules/xhs-agent"

if [ "$WITH_DOCLING" = "1" ]; then
  "$VENV_PY" -m pip install -c "$CONSTRAINTS_FILE" -e "$ROOT_DIR[docling]"
fi

if [ "$SKIP_BROWSER" != "1" ]; then
  "$VENV_PY" -m playwright install chromium
fi
"$VENV_PY" -m pip check

ENV_FILE="$ROOT_DIR/apps/connect-hub/.env"
if [ ! -f "$ENV_FILE" ]; then
  cp "$ROOT_DIR/apps/connect-hub/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  printf 'Created local configuration: %s\n' "$ENV_FILE"
fi
printf 'Research Connect environment ready: %s\n' "$VENV_DIR"
printf 'Shared data root: %s\n' "$DATA_DIR"
printf 'Next: edit %s, then run ./scripts/doctor.sh\n' "$ENV_FILE"
