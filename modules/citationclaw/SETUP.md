# CitationClaw 快速上手

## 环境要求
- Python 3.10+
- 依赖: `pip install -r requirements.txt`

## 启动
```bash
# 方式一: 用启动脚本（自动设置数据库路径）
./run.sh

# 方式二: 手动指定数据库路径后启动
export CITATIONCLAW_DATA_DIR="$(pwd)/dot_citationclaw"
python -m citationclaw --no-browser --host 127.0.0.1 --port 8000
```
浏览器打开 `http://127.0.0.1:8000`

## 目录说明
- `citationclaw/` — 源代码
- `dot_citationclaw/` — 两个 SQLite 数据库（知名学者库 + arXiv 论文库）
- `config.json` — API 密钥与配置（已预填可用 key）
- `data/` — 运行输出目录（首次运行为空，自动创建）
- `test/` — 测试（`python -m pytest test/ -q`）

## 详细文档
- `docs/pipeline-and-api-keys.md` — 完整流水线说明 + API 密钥清单
- `docs/TODO.md` — 待办事项
