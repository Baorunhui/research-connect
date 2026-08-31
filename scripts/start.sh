#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${RESEARCH_CONNECT_VENV:-$ROOT_DIR/.venv}"
ENV_FILE="${RESEARCH_CONNECT_ENV_FILE:-$ROOT_DIR/apps/connect-hub/.env}"

if [ ! -x "$VENV_DIR/bin/connect-hub" ]; then
  printf 'Research Connect is not installed. Run ./scripts/setup.sh first.\n' >&2
  exit 2
fi
if [ ! -f "$ENV_FILE" ]; then
  printf 'Missing configuration: %s\n' "$ENV_FILE" >&2
  exit 2
fi

exec "$VENV_DIR/bin/connect-hub" --env-file "$ENV_FILE" serve
