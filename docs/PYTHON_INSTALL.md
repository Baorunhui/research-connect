# Python 版安装与故障排查

本文面向在个人 Windows 电脑、Linux 工作站或服务器上部署 Research Connect 的测试用户。当前发行版不要求 Docker、PostgreSQL、Redis 或本地 embedding/reranker 模型。

## 1. 准备账号与配置

安装前准备：

- Python 3.11～3.13，推荐 3.11；
- Git；
- 一个中国版飞书企业自建应用的 App ID 和 App Secret；
- Report Hub 管理员单独发放的安装 token；
- 一个 OpenAI 兼容 LLM 的 API 端点、模型名和 Key。

每个安装应使用自己的飞书应用和 Report Hub token。不要使用或转发服务器管理员 token，也不要把 `.env` 提交到 Git。

## 2. Linux 安装

先确认版本：

```bash
python3 --version
git --version
```

Debian/Ubuntu 如果缺少虚拟环境组件，可安装：

```bash
sudo apt update
sudo apt install git python3 python3-venv
```

然后安装项目：

```bash
git clone https://github.com/Baorunhui/research-connect.git
cd research-connect
./scripts/setup.sh
```

需要用 Docling 做 PDF 解析和图表提取时，使用：

```bash
./scripts/setup.sh --with-docling
```

脚本会创建 `.venv`，安装全部业务模块和 Playwright Chromium，并在首次安装时创建 `apps/connect-hub/.env`。重复执行不会覆盖已有配置。

脚本会优先使用当前 `PATH` 中的 `python`，因此激活 Conda 后会用该环境的 Python 创建项目专属的 `.venv`，不会把依赖直接装入 Conda 环境。显式设置 `PYTHON` 仍具有最高优先级。

## 3. Windows 安装

安装 64 位 Python 和 Git。安装 Python 时建议选中 `Add python.exe to PATH`；也可以直接使用 Windows Python Launcher `py`。

在 PowerShell 中运行：

```powershell
git clone https://github.com/Baorunhui/research-connect.git
cd research-connect
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

安装 Docling：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -WithDocling
```

脚本会优先使用当前 `PATH` 中的 `python`（包括已激活 Conda 环境中的 Python），找不到时才回退到 `py -3`。如机器上有多个 Python，也可以显式指定：

```powershell
.\scripts\setup.ps1 -Python "C:\Path\To\Python311\python.exe"
```

## 4. 安装选项

| Linux | Windows | 作用 |
|---|---|---|
| `--with-docling` | `-WithDocling` | 安装 Docling、pypdfium2 及相关 PDF 依赖；体积较大 |
| `--skip-browser` | `-SkipBrowser` | 不安装 Chromium；会禁用需要浏览器渲染的路径 |
| `--dev` | `-Dev` | 安装 pytest 等开发依赖 |

Docling 可在 CPU 上运行，并会在环境可用时使用 GPU。第一次使用可能下载模型，因此耗时和磁盘占用会明显高于基础安装。

## 5. 配置

编辑 `apps/connect-hub/.env`，最低配置如下：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx

LLM_BASE_URL=https://your-openai-compatible-endpoint/v1
LLM_API_KEY=xxx
LLM_MODEL=your-model

REPORT_HUB_API_URL=https://report.sinksilk.com:58443
REPORT_HUB_AGENT_TOKEN=rhi_xxx
```

飞书应用必须添加机器人能力、必要权限、长连接事件和机器人菜单。逐步操作见 [飞书机器人配置教程](FEISHU_BOT_SETUP.md)。

首次运行后，可以在飞书中输入 `/config` 打开统一配置页。配置页可填写 LLM、Embedding、Reranker、论文源和各模块配置；同一配置也会同步给 Daily Paper 和 CitationClaw 原版网页。

## 6. 环境检查

Linux：

```bash
./scripts/doctor.sh
```

Windows：

```powershell
.\scripts\doctor.ps1
```

`doctor` 不调用付费 LLM，也不会显示 Key。它检查 Python 版本、飞书/LLM/Report Hub 配置、核心依赖、源码目录、数据目录写权限、Docling、Chromium 和 Report Hub 健康状态。

- `FAIL`：启动前必须解决；命令返回非零状态；
- `WARN`：可选能力不可用，例如没有安装 Docling；
- `OK`：该项已满足。

配置中心里的 Provider “测试连接”按钮会实际访问对应 API；只有用户点击时才执行。

## 7. 启动与停止

Linux：

```bash
./scripts/start.sh
```

Windows：

```powershell
.\scripts\start.ps1
```

看到飞书 WebSocket 连接成功后，就可以私聊机器人。保持终端运行，按 `Ctrl+C` 停止。不要让同一个飞书 App ID 在两台机器上同时运行。

Linux 服务器可先用 `tmux` 或 `systemd` 保持后台运行；demo 阶段不强制提供特定进程管理器。

## 8. 单独运行网页模块

无需飞书也能直接启动原版模块：

Linux：

```bash
.venv/bin/connect-hub --env-file apps/connect-hub/.env daily-paper
.venv/bin/connect-hub --env-file apps/connect-hub/.env citationclaw
```

Windows：

```powershell
.venv\Scripts\connect-hub.exe --env-file apps\connect-hub\.env daily-paper
.venv\Scripts\connect-hub.exe --env-file apps\connect-hub\.env citationclaw
```

如果统一配置尚未建立，命令会给出配置页，而不是直接启动昂贵任务。

## 9. 更新

先停止 Connect Hub，再执行：

```bash
git pull --ff-only
./scripts/setup.sh --with-docling
```

Windows：

```powershell
git pull --ff-only
.\scripts\setup.ps1 -WithDocling
```

没有安装 Docling 的用户可去掉对应选项。更新脚本不会覆盖 `.env` 和数据目录。

## 10. 数据位置

默认数据根目录：

- Linux：`~/.research-connect/data`
- Windows：`%USERPROFILE%\.research-connect\data`

可在运行前设置 `RESEARCH_CONNECT_DATA_DIR` 修改。SQLite 只保存任务、事件、索引和小型元数据；PDF、图片、网页、缓存和较大的 JSON 保存在数据根下的文件目录中。

本地数据、`.env`、模型、浏览器、日志和报告均已排除在 Git 之外。卸载或清理前请先备份数据根；安装脚本不会主动删除它。

## 11. 常见问题

### Python 版本不支持

确认实际执行的是 Python 3.11～3.13。Windows 可运行 `py -0p` 查看已安装版本；Linux 可通过 `PYTHON=/path/to/python3.11 ./scripts/setup.sh` 指定解释器。

### Linux 无法创建虚拟环境

安装发行版对应的 `python3-venv` 包，然后删除创建失败的空 `.venv` 再重试。不要删除包含正常安装的 `.venv`。

### PowerShell 禁止执行脚本

使用文档中的一次性 `powershell -ExecutionPolicy Bypass -File ...` 命令即可，无需修改全局执行策略。

### Chromium 缺失

Linux：

```bash
.venv/bin/python -m playwright install chromium
```

Windows：

```powershell
.venv\Scripts\python.exe -m playwright install chromium
```

Linux 如果提示缺少系统动态库，可根据 Playwright 输出安装所需系统包；有 sudo 权限时可使用 `playwright install --with-deps chromium`。

### Docling 未启用或缺少 pypdfium2

重新运行带 `--with-docling` / `-WithDocling` 的安装命令，再执行 `doctor`。不需要安装 PaperCropper、DocLayout-YOLO 或 OpenCV 路线。

### 飞书消息没有响应

确认应用已发布、长连接事件已启用、应用可用范围包含本人，并查看终端是否显示 WebSocket 已连接。同一 App ID 只能保留一个运行中的 Connect Hub。

### Report Hub 返回 401/403

通常表示安装 token 错误、已被轮换，或该 token 正在访问其他安装的资源。向管理员索取本人的安装 token；不要改用管理员 token。

### 本地端口被占用

Daily Paper 和 CitationClaw 本地服务默认使用各自配置的端口。先关闭旧的 Research Connect 进程，再启动新实例；不要同时手工启动模块和 `connect-hub serve`。

### 公网页面未更新

本机结果不会丢失。先查看飞书进度中的发布错误，再检查 Report Hub 地址、安装 token 和网络。大型站点会分片上传，不要手工把运行结果提交到 Git。

### Windows 路径过长

优先把仓库放在较短路径，例如 `C:\src\research-connect`。只有 Git 明确报告 long path 错误时，再以管理员身份启用 Git 的 long path 支持。
