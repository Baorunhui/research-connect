#!/bin/bash
# CitationClaw 启动脚本 — 使用仓库统一环境和数据根目录
# 用法: ./run.sh [端口号]   默认端口 8000
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
export RESEARCH_CONNECT_DATA_DIR="${RESEARCH_CONNECT_DATA_DIR:-$HOME/.research-connect/data}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$RESEARCH_CONNECT_DATA_DIR/browsers}"
cd "$SCRIPT_DIR"

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="${PYTHON:-python3}"
fi

PORT="${1:-8000}"

echo "CitationClaw v2"
echo "  项目目录: $SCRIPT_DIR"
echo "  数据根目录: $RESEARCH_CONNECT_DATA_DIR"
echo "  端口:     $PORT"
echo ""

exec "$PYTHON_BIN" -m citationclaw --no-browser --host 127.0.0.1 --port "$PORT"
