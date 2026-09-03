# Connect Hub

Connect Hub 是 Research Connect 的本机核心，负责飞书长连接、LLM 工具编排、统一任务生命周期、配置同步、模块启动与 Report Hub 客户端通信。

普通用户不要从本目录单独安装。统一安装、注册、启动和更新流程见仓库根目录 [README](../../README.md)，飞书开放平台配置见 [飞书机器人配置教程](../../docs/FEISHU_BOT_SETUP.md)，常见故障见 [FAQ](../../docs/FAQ.md)。

## 开发入口

```bash
pip install -e 'apps/connect-hub[dev]'
connect-hub --env-file apps/connect-hub/.env doctor
connect-hub --env-file apps/connect-hub/.env serve
pytest -q apps/connect-hub/tests
```

主要目录：

```text
src/connect_hub/
  adapters/          Daily Paper 与 CitationClaw 适配器
  connectors/        飞书 WebSocket 与附件处理
  llm/               OpenAI 兼容 LLM Gateway
  tools/             固定工具注册与业务工具
  static/            客户端上传的统一配置页面
  cli.py             doctor/register/serve 等命令
  service.py         单 Agent 对话与确定性斜杠命令
  storage.py         本机 SQLite 状态
  reporting.py       Report Hub 安装级客户端
```

架构、对话护栏、`connect.job.v1`、错误码、取消语义和安全边界统一记录在 [作品设计说明书](../../docs/DESIGN_DOCUMENT.md)。外部 Provider 配置见 [外部论文服务申请与配置](../../docs/EXTERNAL_SERVICES_SETUP.md)。后续工作只维护 [下一阶段待办](../../docs/NEXT_SCOPE_TODO.md)。

## 边界

- LLM 只能调用静态 `ToolRegistry` 中的工具，不能执行任意 shell；
- 每条消息限制模型、联网与耗时业务工具调用次数；
- 长任务、事件、产物、用量和取消状态写入本机 SQLite；
- Daily Paper/CitationClaw 重任务由各模块执行，Connect Hub 负责适配和进度转发；
- API Key 正式副本保存在用户本机；Report Hub 只保存发布副本并转发受限命令。
