#!/bin/bash
# CitationClaw 启动脚本 — 自动定位项目目录和数据库，无需修改任何路径
# 用法: ./run.sh [端口号]   默认端口 8000
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CITATIONCLAW_DATA_DIR="$SCRIPT_DIR/dot_citationclaw"
cd "$SCRIPT_DIR"

PORT="${1:-8000}"

echo "CitationClaw v2"
echo "  项目目录: $SCRIPT_DIR"
echo "  数据库:   $CITATIONCLAW_DATA_DIR"
echo "  端口:     $PORT"
echo ""

exec python -m citationclaw --no-browser --host 127.0.0.1 --port "$PORT"
