# Research Connect Hub

独立的飞书连接层与 OpenAI-compatible LLM 中台。论文日报、查引用和小红书生成均作为后续外部工具适配器接入，不侵入各自项目实现。

## 为什么不直接把 cc-connect 当核心

`cc-connect` 的飞书通道设计值得复用：它通过 WebSocket 长连接接收飞书事件，因此不需要公网 IP、域名或 webhook。它的主要运行对象是 Claude Code、Codex 等本地 Agent CLI，而本项目需要稳定调用领域功能和统一 LLM API，所以这里只复用其通道思想，不把 Claude Code CLI 作为生产依赖。

无显示器服务器不需要扫码。二维码只是 `cc-connect feishu new/setup` 自动代创建应用的快捷方式；我们采用飞书开放平台手动创建应用，然后用 `App ID + App Secret` 建立长连接。

## 当前实现

```text
飞书客户端
  ↕ WebSocket 长连接（官方 lark-oapi SDK）
FeishuConnector
  ↓ 快速入队，避免阻塞飞书 3 秒确认窗口
ChatService
  ├─ /ping /help /model /reset /jobs /job /cancel /web /config
  ├─ 单 Agent 生成式编排（自然追问或固定工具调用）
  ├─ 模型按需调用 Exa MCP 搜索 + Jina Reader
  ├─ 每消息程序护栏与 agent run/step 审计
  ├─ SQLite 会话历史
  └─ LLMGateway
       ├─ primary OpenAI-compatible provider
       └─ optional fallback provider

External adapters
  ├─ Daily Paper（最新版本地 workflow HTTP）
  ├─ CitationClaw（外部 job_id + 进度/取消/报告）
  └─ Xiaohongshu Generator（独立子进程）
```

当前已实现文字消息、普通 LLM 对话、受约束工具调用，以及飞书图片/文件附件发送。

已注册或可配置的工具：

- `system_status`：无副作用的服务状态检查。
- `generate_xhs_package`：通过 `xhs_agent` 独立虚拟环境生成文案和卡片，不自动发布。
- `generate_daily_paper_report`：调用最新版 Daily Paper workflow，实时转发步骤，并把原生 Daily Paper 整站发布到该安装实例的固定公网地址。
- `summarize_paper`：按原版 `/api/paper/summarize` 流程总结论文 URL 或飞书上传的 PDF，实时回传解析、图表和落盘进度，完成后刷新固定站点并返回论文页。
- `generate_paper_survey`：按原版 `/api/survey` 综述流水线执行召回、精选、抽取、聚类、深读、写作与审校；支持联网富化主题，以及 URL/PDF 种子论文。

飞书入站 PDF 需要机器人具备已发布的 `im:resource` 权限。附件限制为 PDF、最大 30 MiB；文件保存到统一 data root 的 `modules/connect-hub/artifacts/inbound/`，不会写入 Git 仓库。
- `lookup_citations`：调用 CitationClaw，转发进度、支持取消，并发布最终 Dashboard。

工具只能来自静态注册表。LLM不能执行任意 shell、动态导入代码或调用未注册工具；每次工具调用的参数、状态、结果/错误和时间都会审计到 SQLite `tool_runs` 表。

长任务统一使用 `connect.job.v1`。任务、事件、产物、缓存索引和 API 用量写入 SQLite，结构化事件由 Connect Hub 转成确定性的飞书进度/错误消息。服务重启时遗留任务标记为 `interrupted`，第一版不自动恢复。

用户可发送 `/cancel`、`取消任务` 或`取消当前任务`，取消当前会话最近的运行中任务。本地长子进程必须通过受控进程运行器启动，支持 Linux/Windows 整棵进程树取消。`/jobs` 查看最近任务，`/job <短ID>` 查询事件、错误、产物和调用统计。完整模块接入说明见 [`docs/connect-job-v1.md`](docs/connect-job-v1.md)。

对话入口不再使用固定槽位表、`task_drafts` 草稿或“两轮后终止”的状态机。每条用户消息都把最近的完整对话片段交给同一个模型；模型可以直接回答、结合上一问理解短回复、自然追问、按需搜索，或调用静态注册的业务工具。用户可以跨任意多条消息澄清，系统不会因为达到固定轮数丢弃任务语境。

这不是无限循环 Agent。程序在每条用户消息内硬限制最多 3 次模型调用、1 次 Exa 搜索、1 次 Jina URL 读取和1次耗时业务工具调用；相同参数的重复调用会被拒绝。日报、小红书等有副作用或高成本工具还必须能从近期用户原话中找到明确请求，联网检索和业务执行也不能在同一批并行启动。模型和工具的每一步、token、耗时与拦截结果记录到 SQLite `agent_runs` / `agent_steps`。

联网采用 Cherry Studio 同类轻量路线：匿名远程 Exa MCP 负责搜索，免 Key Jina Reader 负责读取模型选中的公开 URL。每条消息最多搜索一次、读取一次，结果缓存 24 小时；搜索内容按不可信外部数据处理，回复中保留实际使用的来源 URL。

飞书不能由机器人向原生输入框工具栏注入自定义图标。联网现在默认开启，不再占用机器人自定义菜单；仍保留命令作为临时控制和故障排查入口：

```text
/web auto   # 模糊任务或时效问题才联网
/web on     # 默认；后续消息始终允许联网
/web off    # 禁止联网
/web        # 查看当前状态
```

也支持“自动联网”“开启联网”“关闭联网”。设置持久化到 SQLite。机器人聊天页底部提供“读论文”“查引用”“使用帮助”三个快捷菜单，点击后机器人分别主动发送原版 Paper Reader 网页、CitationClaw 网页和斜杠命令帮助。菜单点击事件只包含用户 `open_id`，不包含当前会话 ID，因此回复会发送到该用户与机器人的单聊。

## 你需要在飞书网页完成的配置

服务器不需要浏览器，以下操作可以在你自己的 Windows 电脑或任何有浏览器的设备完成。

1. 登录[飞书开放平台](https://open.feishu.cn/)，进入开发者后台。
2. 创建“企业自建应用”。这不是群里的“自定义 Webhook 机器人”。
3. 在“应用能力”中添加并启用“机器人”。
4. 在“凭证与基础信息”复制：
   - `App ID`，通常以 `cli_` 开头；
   - `App Secret`。
5. 在“权限管理”添加最小权限：
   - `im:message.p2p_msg:readonly`：接收发给机器人的单聊消息；
   - `im:message.group_at_msg:readonly`：接收群里 @机器人的消息；
   - `im:message:send_as_bot`：以机器人身份回复。
   - `im:resource`：上传并发送小红书卡片图片和论文日报文件。
6. 先把 `App ID/App Secret` 写进服务器 `.env` 并启动长连接。
7. 回到“事件与回调 → 事件配置”，选择“使用长连接接收事件”。
8. 添加事件 `im.message.receive_v1`（接收消息）。
9. 添加事件 `application.bot.menu_v6`（机器人自定义菜单事件）。
10. 在“应用能力 → 机器人 → 机器人自定义菜单”添加三个一级菜单项，类型均选择“推送事件”：
    - 菜单名称：`读论文`；事件键：`connect_paper_reader`；
    - 菜单名称：`查引用`；事件键：`connect_citationclaw`；
    - 菜单名称：`使用帮助`；事件键：`connect_help`。
11. 创建新版本、发布应用，并设置应用可用范围。企业租户可能需要管理员审批；已经发布过的应用在新增事件或菜单后也要再次发布。
12. 单聊搜索机器人，或者将机器人添加到群聊后 @它。菜单发布后会出现在机器人聊天页底部，点击后机器人会在单聊中主动回复对应内容。

第一版不使用交互卡片，因此不需要 `card.action.trigger`、Encrypt Key、Verification Token或公网回调地址。自定义菜单事件同样通过当前 WebSocket 长连接接收。

## 需要提供给开发端的信息

要完成真实连接，只需要：

```text
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_DOMAIN=feishu
```

此外请确认：

- 使用中国版飞书还是国际版 Lark；中国版填 `feishu`，国际版填 `lark`。
- 机器人先只允许你本人使用，还是允许指定群成员使用。
- 应用版本是否已发布/审批，可用范围是否包含测试账号。

最初可以不提供用户 `open_id`。首次消息到达后日志里能看到发送人的 `open_id`，再把它加入 `FEISHU_ALLOWED_OPEN_IDS` 做第二层白名单。飞书应用的“可用范围”是第一层边界。

## Linux 安装与运行

```bash
cd connect-hub
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

编辑 `.env`，先检查配置：

```bash
connect-hub check
```

先在本地终端测试 LLM 中台：

```bash
connect-hub chat
```

部署时用一个命令启动两个轻量模块 API 和飞书长连接：

```bash
connect-hub serve
```

连接成功后应看到 `connected to wss://...`。进程需要持续运行，后续可交给 systemd 管理。

## Windows 安装与运行

```powershell
cd connect-hub
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
connect-hub check
connect-hub chat
connect-hub serve
```

该连接层没有 Linux 专属依赖。Windows后续可用“任务计划程序”或服务包装器保持进程运行。

## 配置说明

- `LLM_BASE_URL/API_KEY/MODEL`：主模型服务。
- `LLM_FALLBACK_*`：可选回退模型；主服务失败才调用。
- `CONNECT_HUB_DB_PATH`：SQLite会话库。
- `CONNECT_HUB_HISTORY_MESSAGES`：每次发送给 LLM 的最近历史消息数；它是上下文成本上限，不是澄清轮数上限。
- `FEISHU_ALLOWED_OPEN_IDS`：逗号分隔的用户白名单，空值表示仅依赖飞书应用可用范围。
- `FEISHU_REQUIRE_MENTION=true`：群聊必须 @机器人；私聊不受影响。
- `XHS_AGENT_DIR`：已有 `xhs_agent` 项目路径；由独立子进程调用。
- `DAILY_PAPER_TRANSPORT/ENDPOINT`：默认 `local_http` / `http://127.0.0.1:8567`。
- `DAILY_PAPER_TIMEOUT_SECONDS/POLL_SECONDS`：日报长任务的总等待时间和轮询间隔。
- `CITATIONCLAW_ENDPOINT`：默认 `http://127.0.0.1:8000`。
- `REPORT_HUB_API_URL/AGENT_TOKEN`：公网托管；Daily Paper 与 CitationClaw 各有一个固定的原版网页地址，模块配置也在该网页填写。部署见 [`../../docs/PUBLIC_SERVER_HANDOFF.md`](../../docs/PUBLIC_SERVER_HANDOFF.md)。
- `DAILY_PAPER_EMBED_API_URL/API_KEY`：远程 embedding 服务，按任务注入 Daily Paper 子进程；默认不启用本地模型回退。
- `DAILY_PAPER_SKIP_LLM_REFINE=false`：默认保留逐篇 LLM 精筛；后续通过动态分批、并发限流和 429 退避优化耗时。仅诊断时才临时设为 `true`。
- `DAILY_PAPER_RERANK_*`：每次任务注入的重排服务配置，不改日报仓库自己的 `.env`。默认使用日报项目已有的 `public-zwwen-rerank`；需要私有服务时再填写 API Key。
- `WEB_SEARCH_PROVIDER=exa_mcp`：启用匿名 Exa Hosted MCP；填 `disabled` 可全局关闭。
- `WEB_SEARCH_ENDPOINT`：默认只开放只读的 `web_search_exa` 工具。

也可以只启动一个模块（两条命令都先检查公网配置记录；不存在时打印原版配置网页并退出，不测试 API 连通性）：

```bash
python -m connect_hub.cli daily-paper
python -m connect_hub.cli citationclaw
```
- `WEB_SEARCH_MAX_RESULTS/CACHE_HOURS`：默认 5 条、缓存 24 小时。
- `URL_FETCH_PROVIDER=jina`：选中 URL 的免 Key Jina Reader；不加载任何本地搜索模型。

## 论文日报边界

`connect_hub.adapters.daily_paper.DailyPaperAdapter` 调用日报最新版 `/api/local/workflows/dispatch`。Connect Hub 的 job ID 是唯一对外任务 ID；日报内部 run ID 仅作为执行句柄。

默认运行日报的逐篇 LLM 精筛。Reranker 仍使用独立 `/rerank` 接口，普通聊天模型 API 不能直接替代；connect-hub 会把主 LLM provider 作为 `SUMMARY/DEEPSEEK` 配置随请求注入。

飞书会在管线进入 BM25、Embedding、RRF、Rerank 和 Top-K 选择时分别发送一次进度消息。相同步骤只汇报一次。

`serve` 常驻的只是 HTTP 壳和飞书 WebSocket，不预加载 PDF/embedding 模型。流水线与 Docling 按任务启动并退出；最终 `docs/` 静态站点上传到 Report Hub，用户电脑关机后旧链接仍可访问。
