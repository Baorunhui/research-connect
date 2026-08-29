#!/usr/bin/env bash
set -euo pipefail

HOST="${DPR_LOCAL_HOST:-127.0.0.1}"
PORT="${DPR_LOCAL_PORT:-8567}"

cd "$(dirname "$0")/.."
# 解释器优先用 dpr conda 环境
# shellcheck source=scripts/_resolve_python.sh
source scripts/_resolve_python.sh
if [ -z "${DPR_RESOLVED_PYTHON:-}" ]; then
  echo "未解析到可用的 Python 解释器" >&2
  exit 1
fi
exec "$DPR_RESOLVED_PYTHON" src/local_debug_server.py --host "$HOST" --port "$PORT"
