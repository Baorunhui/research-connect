# Research Connect

Research Connect 是一套可自行部署的本地优先研究工具。每位用户在自己的 Windows 电脑、Linux 工作站或服务器上运行一份，通过飞书机器人在手机或电脑上调用研究功能。

当前 Python demo 包含：

- 论文日报：按主题检索近期论文，经过 BM25、向量召回、RRF、Reranker 和 LLM 精筛生成日报；
- 论文阅读：总结论文 URL/PDF，或围绕主题和种子论文生成领域综述；
- 查引用：打开 CitationClaw 原版网页，查询施引论文、作者和引用语境；
- 小红书：短时多次生成图文内容包；
- 飞书 Connect Hub：自然语言理解、联网富化、任务进度、取消、缓存和用量记录；
- 公网 Report Hub：把原版网页和历史报告提供给手机/其他电脑访问，不要求用户机器有公网 IP。

论文处理和任务进程运行在用户自己的机器；Embedding、Reranker、Supabase、Exa、Jina 和 LLM 可使用远程服务。默认不下载本地 embedding/reranker 模型，不需要 PostgreSQL、Redis 或 Docker。

> 当前是面向少量测试用户的 Python demo。每个安装只运行一个 Connect Hub；公网网页采用随机 bearer 链接，不应公开转发。

## 安装前准备

- Windows 10/11 或常见 x86-64 Linux；
- Python 3.11～3.13（推荐 3.11）和 Git；
- 可以访问 PyPI、GitHub、飞书和所配置 API 的网络；
- 自己创建的中国版飞书企业自建应用；
- Report Hub 管理员私下发放的安装 token；
- 一个 OpenAI 兼容 LLM 的端点、模型名和 API Key。
- 流程中使用到的第三方 API Key，包括：
    - arXiv Supabase 论文池
    - 论文 Embedding
    - 论文 Reranker
    - CitationClaw Search LLM

每位用户应使用自己的飞书 App ID 和 Report Hub 安装 token。不要把服务器管理员 token 放进用户电脑。

## 1. 下载与安装

Linux：

```bash
git clone https://github.com/Baorunhui/research-connect.git
cd research-connect
./scripts/setup.sh  --with-docling
```

Windows PowerShell：

```powershell
git clone https://github.com/Baorunhui/research-connect.git
cd research-connect
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1  -WithDocling
```

安装脚本会：

1. 创建仓库根目录下的 `.venv`；
2. 安装 Connect Hub、Daily Paper、CitationClaw、XHS Agent 和共享运行时；
3. 安装 Playwright Chromium；
4. 安装 Docling 模型提取 pdf 图片和表格
5. 首次运行时创建本机配置 `apps/connect-hub/.env`。

## 2. 配置飞书机器人
飞书开放平台需要添加机器人能力、权限、长连接事件和固定菜单，完整步骤见 [飞书机器人配置教程](docs/FEISHU_BOT_SETUP.md)。

## 3. 填写配置

编辑 `apps/connect-hub/.env`，至少填写：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx

LLM_BASE_URL=https://your-openai-compatible-endpoint/v1
LLM_API_KEY=xxx
LLM_MODEL=your-model

REPORT_HUB_API_URL=https://report.sinksilk.com:58443
REPORT_HUB_AGENT_TOKEN=rhi_xxx
```

REPORT_HUB_AGENT_TOKEN 由 Report Hub 管理员为每个安装单独签发。LLM 配置首次启动后会导入统一配置中心；以后也可以在飞书 `/config` 返回的 HTTPS 页面修改。

## 4. 检查并启动

Linux：

```bash
./scripts/doctor.sh
./scripts/start.sh
```

Windows PowerShell：

```powershell
.\scripts\doctor.ps1
.\scripts\start.ps1
```

看到飞书 WebSocket 连接成功后即可私聊机器人。终端需要保持运行，按 `Ctrl+C` 停止。`start` 会常驻轻量 Connect Hub，并自动管理本机 Daily Paper/CitationClaw HTTP 服务；重型 PDF 和论文流水线只在任务执行时运行。

## 5. 使用

可以直接自然语言输入，例如：

```text
帮我生成一份最近 15 天的 3D visual grounding 论文日报，使用 skims 模式
总结这篇论文：https://arxiv.org/abs/xxxx.xxxxx
围绕 embodied visual reasoning 生成领域综述，回溯半年，精选 8 篇
把这份材料做成 5 页、面向研究生的小红书帖子
```

常用命令：

```text
/config          打开统一配置中心
/paper_reader    打开论文日报/论文阅读原版网页
/citationclaw    打开查引用原版网页
/jobs            查看最近任务
/cancel          取消当前任务
/help            查看帮助
```

飞书菜单提供“读论文”“查引用”“使用帮助”。机器人默认可以联网富化含糊的研究主题；联网由 Exa MCP 搜索和 Jina URL 获取完成，不运行本地搜索模型。

也可以不启动飞书，只运行单个原版模块：

```bash
.venv/bin/connect-hub --env-file apps/connect-hub/.env daily-paper
.venv/bin/connect-hub --env-file apps/connect-hub/.env citationclaw
```

Windows 将 `.venv/bin/connect-hub` 换成 `.venv\Scripts\connect-hub.exe`。

## 更新

```bash
git pull --ff-only
./scripts/setup.sh                    # Linux
```

```powershell
git pull --ff-only
.\scripts\setup.ps1                  # Windows
```

## 数据与安全

- 默认数据根：`~/.research-connect/data`；可用 `RESEARCH_CONNECT_DATA_DIR` 修改；
- SQLite 只保存任务、事件、索引和小型元数据；PDF、JSON 大对象、图片和网页仍保存为文件；
- `.env`、本地数据库、缓存、PDF、生成结果和模型不会提交 Git；
- Report Hub 安装 token 只允许操作本安装的资源；服务端只保存 token 哈希；
- 公网配置读取不会回显 API Key，但拿到配置页随机链接的人可以修改配置，因此链接也应保密；
- 一个飞书 App ID 不应在两台机器上同时运行 Connect Hub。

## 仓库结构

```text
apps/connect-hub/                 飞书、LLM 网关、任务与统一事件中心
apps/report-hub/                  公网页面、配置和受限命令中继
packages/research-connect-core/   共享 LLM、数据目录、缓存和 CLI 事件运行时
modules/daily-paper-reader/       论文日报、论文总结与领域综述
modules/citationclaw/             查引用与引用画像
modules/xhs-agent/                小红书内容生成
scripts/                          Windows/Linux 安装、检查和启动入口
docs/                             部署、协议和开发文档
```

进一步阅读：[Python 安装与故障排查](docs/PYTHON_INSTALL.md)、[飞书配置](docs/FEISHU_BOT_SETUP.md)、[多安装公网接入](docs/REPORT_HUB_MULTI_INSTALL.md)、[自托管架构](docs/SELF_HOSTING_ARCHITECTURE.md) 和 [下一阶段待办](docs/NEXT_SCOPE_TODO.md)。
