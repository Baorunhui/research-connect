#!/usr/bin/env bash
# 启动本地化部署后端（src/local_server.py）。
# 解释器优先使用 dpr conda 环境（见 scripts/_resolve_python.sh），回退 .venv / python。
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/_resolve_python.sh
source scripts/_resolve_python.sh
if [ -z "${DPR_RESOLVED_PYTHON:-}" ]; then
  echo "未解析到可用的 Python 解释器" >&2
  exit 1
fi
exec "$DPR_RESOLVED_PYTHON" src/local_server.py --serve