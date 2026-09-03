# Research Connect

Research Connect 是一套可自行部署的本地优先研究工具。每位用户在自己的 Windows 电脑、Linux 工作站或服务器上运行一份，通过飞书机器人在手机或电脑上调用研究功能。专为青椒打造！

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
- Report Hub 管理员提供的邀请码；
- 一个 OpenAI 兼容 LLM 的端点、模型名和 API Key。
- 流程中使用到的第三方服务：Demo 已预置 Daily Paper 上游公开的 arXiv Supabase
  论文池、论文 Embedding 和论文 Reranker；查引用建议用户自行申请 Semantic Scholar Key。

公开服务开箱可用，但共享额度、没有可用性保证；可在 `/config` 中测试或换成自己的兼容服务。默认值、申请/替换教程和安全边界见 [外部论文服务申请与配置](docs/EXTERNAL_SERVICES_SETUP.md)。

每位用户应使用自己的飞书 App ID，并通过邀请码注册自己的 Report Hub 安装。公网服务器不配置全局管理员 HTTP token；服务器运维通过 Docker 内的 `report-hub` CLI 完成。

## 1. 下载与安装

Linux：

```bash
git clone https://github.com/Baorunhui/research-connect.git
cd research-connect
./scripts/setup.sh --with-docling
```

Windows PowerShell：

```powershell
git clone https://github.com/Baorunhui/research-connect.git
cd research-connect
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -WithDocling
```

安装选项：

| Linux | Windows | 作用 |
|---|---|---|
| `--with-docling` | `-WithDocling` | 安装 Docling、pypdfium2 和 PDF 图表提取依赖 |
| `--skip-browser` | `-SkipBrowser` | 不安装 Playwright Chromium |
| `--dev` | `-Dev` | 安装 pytest 等开发依赖 |

Docling 支持 CPU，并会在环境可用时使用 GPU；首次使用可能下载模型。不需要 PDF 深度解析时可以去掉 Docling 参数。

如果使用 Conda，请先激活目标环境再运行上述命令。Windows 和 Linux 安装脚本
都会优先使用当前 `PATH` 中的 `python`（即 Conda 环境的 Python），并用它创建项目专属的 `.venv`，
不会把依赖直接装入 Conda 环境：

```text
conda activate your-env
# Linux: ./scripts/setup.sh --with-docling
# Windows: powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -WithDocling
```

也可以通过 Linux 的 `PYTHON` / `RESEARCH_CONNECT_VENV` 或 Windows 的
`-Python` / `-Venv` 显式指定解释器及目标环境。

安装脚本会：

1. 创建仓库根目录下的 `.venv`（若已激活 Conda，则使用 Conda 的 Python 创建）；
2. 安装 Connect Hub、Daily Paper、CitationClaw、XHS Agent 和共享运行时；
3. 安装 Playwright Chromium；
4. 按选项安装 Docling，用于提取 PDF 图片和表格；
5. 首次运行时创建本机配置 `apps/connect-hub/.env`。

## 2. 配置飞书机器人
飞书开放平台需要添加机器人能力、权限、长连接事件和固定菜单，完整步骤见 [飞书机器人配置教程](docs/FEISHU_BOT_SETUP.md)。

## 3. 填写配置并注册

编辑 `apps/connect-hub/.env`，至少填写：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx

LLM_BASE_URL=https://your-openai-compatible-endpoint/v1
LLM_API_KEY=xxx
LLM_MODEL=your-model

```

然后使用管理员提供的邀请码注册。Linux：

```bash
.venv/bin/connect-hub --env-file apps/connect-hub/.env register \
  --server https://report.sinksilk.com:58443 \
  --invite 'rhi_inv_xxx' \
  --username '你的名字'
```

Windows PowerShell：

```powershell
.venv\Scripts\connect-hub.exe --env-file apps\connect-hub\.env register `
  --server https://report.sinksilk.com:58443 `
  --invite 'rhi_inv_xxx' `
  --username '你的名字'
```

注册成功后，安装 token 和公网地址会自动写入 `.env`，无需管理员逐个生成或传递 token。一个飞书 App ID 只能注册一次；换电脑时应由管理员轮换或注销原安装。

这里写入客户端 `.env` 的 `REPORT_HUB_AGENT_TOKEN` 是该用户自己的安装 token，不是服务器管理员密钥。用户可以查看或清理自己在公网服务器保存的内容：

```bash
.venv/bin/connect-hub --env-file apps/connect-hub/.env remote-data list
.venv/bin/connect-hub --env-file apps/connect-hub/.env remote-data delete-site SITE_ID
.venv/bin/connect-hub --env-file apps/connect-hub/.env remote-data clear --yes
```

Windows 将 `.venv/bin/connect-hub` 换成 `.venv\Scripts\connect-hub.exe`。

LLM 配置首次启动后会导入本机统一配置；以后也可以在飞书 `/config` 返回的 HTTPS 页面修改。配置和 API Key 的正式副本保存在用户电脑，公网服务器只临时转发页面请求，不保存 Provider 配置。

统一配置中心已为论文池、Embedding 和 Reranker 填入上游公开配置，普通 Demo
用户无需再申请这三项。公开 Key 会以“已配置”显示而不回显原文；需要独立额度或自建服务时可覆盖并点击“测试可用性”。

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
/storage         查看或删除本安装保存在公网服务器上的内容
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

- 默认数据根：Linux 为 `~/.research-connect/data`，Windows 为 `%USERPROFILE%\.research-connect\data`；可用 `RESEARCH_CONNECT_DATA_DIR` 修改；
- SQLite 只保存任务、事件、索引和小型元数据；PDF、JSON 大对象、图片和网页仍保存为文件；
- `.env`、本地数据库、缓存、PDF、生成结果和模型不会提交 Git；
- Report Hub 安装 token 只允许操作本安装的资源；服务端只保存 token 哈希；
- 公网配置读取不会回显 API Key；浏览器提交的配置通过通用命令邮箱转发到本机，因此配置页随机链接仍应保密；
- 一个飞书 App ID 不应在两台机器上同时运行 Connect Hub。

## 仓库结构

```text
apps/connect-hub/                 飞书、LLM 网关、任务、本地配置与客户端网页
apps/report-hub/                  通用公网文件/运行存储、注册、隔离和命令邮箱
packages/research-connect-core/   共享 LLM、数据目录、缓存和 CLI 事件运行时
modules/daily-paper-reader/       论文日报、论文总结与领域综述
modules/citationclaw/             查引用与引用画像
modules/xhs-agent/                小红书内容生成
scripts/                          Windows/Linux 安装、检查和启动入口
docs/                             部署、协议和开发文档
```

## 文档导航

- [常见问题](docs/FAQ.md)：安装、注册、飞书、网页、论文任务和数据排查；
- [外部论文服务申请与配置](docs/EXTERNAL_SERVICES_SETUP.md)：LLM、论文源、Embedding、Reranker 和引用服务；
- [飞书机器人配置教程](docs/FEISHU_BOT_SETUP.md)：飞书开放平台逐步配置；
- [Report Hub 公网服务器部署与运维](docs/PUBLIC_SERVER_ADMIN_GUIDE.md)：公网管理员专用；
- [作品设计说明书](docs/DESIGN_DOCUMENT.md)：设计思路、架构、模块和技术难点；
- [下一阶段待办](docs/NEXT_SCOPE_TODO.md)：唯一的后续开发清单。
