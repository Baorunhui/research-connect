# 常见问题

本文汇总普通用户安装和运行 Research Connect 时最常见的问题。标准安装流程以仓库根目录 [README](../README.md) 为准。

## 安装

### 支持哪些系统和 Python 版本？

支持 Windows 10/11 和常见 x86-64 Linux，Python 3.11～3.13，推荐 3.11。macOS 和 ARM 尚未作为当前 Demo 的正式验收平台。

### Python 版本不支持怎么办？

Windows 使用 `py -0p` 查看解释器；Linux 使用 `python3 --version`。可以显式指定：

```bash
PYTHON=/path/to/python3.11 ./scripts/setup.sh --with-docling
```

```powershell
.\scripts\setup.ps1 -Python "C:\Path\To\Python311\python.exe" -WithDocling
```

### Linux 无法创建虚拟环境

Debian/Ubuntu 安装 `python3-venv`，然后重新运行安装脚本。只有确认 `.venv` 是失败安装留下的空目录时才删除它。

### PowerShell 禁止执行脚本

使用一次性绕过命令即可，无需修改系统全局策略：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -WithDocling
```

### Windows 路径过长

优先把仓库放到短路径，例如 `C:\src\research-connect`。只有 Git 明确报告 long path 错误时，再启用 Git long path 支持。

### Chromium 缺失

```bash
.venv/bin/python -m playwright install chromium
```

```powershell
.venv\Scripts\python.exe -m playwright install chromium
```

Linux 如果缺少动态库，可根据 Playwright 输出安装系统依赖；有 sudo 权限时可执行 `playwright install --with-deps chromium`。

### Docling 或 pypdfium2 缺失

重新运行带 Docling 的安装：

```bash
./scripts/setup.sh --with-docling
```

```powershell
.\scripts\setup.ps1 -WithDocling
```

Docling 支持 CPU；有兼容 GPU 时会利用 GPU。第一次运行可能下载模型，因此会明显更慢。

## 配置与注册

### 最少需要配置什么？

需要飞书 App ID/Secret、一个 OpenAI 兼容 LLM，以及管理员提供的 Report Hub 邀请码。论文池、Embedding 和 Reranker 有 Demo 公开默认值；其他服务见 [外部论文服务申请与配置](EXTERNAL_SERVICES_SETUP.md)。

### `REPORT_HUB_AGENT_TOKEN` 填什么？

普通用户不要手填。执行 `connect-hub register --username ...` 后，命令会自动把用户自己的安装 token 写入 `.env`。它不是服务器管理员 token；公网服务器不存在全局管理员 HTTP token。

### 邀请码注册失败

- 403：邀请码无效、过期、已耗尽或已被管理员撤销；
- 409：这个飞书 App ID 已注册；
- 422：飞书 App ID 格式或用户名不合法。

用户名支持中文、英文和重复名称，但不能是纯空白或包含换行/制表符。

### Report Hub 返回 401/403

401 通常表示安装 token 已轮换、安装已撤销或删除。403 表示正在访问其他安装的资源，或网页操作不在允许的接口策略中。不要尝试使用其他用户 token，应联系管理员核对 `INSTALL_ID`。

## 启动与飞书

### `doctor` 有 FAIL

`FAIL` 是启动前需要解决的配置或依赖；`WARN` 表示可选能力不可用；`OK` 表示已就绪。`doctor` 不会显示 Key，也不会调用付费 LLM。配置中心的“测试可用性”按钮才会实际访问对应 API。

### 飞书机器人没有回复

确认：

- 飞书应用已发布；
- 已启用机器人能力和长连接事件；
- 应用可用范围包含当前用户；
- `start` 终端显示 WebSocket connected；
- 同一个飞书 App ID 没有在另一台电脑同时运行。

逐步配置见 [飞书机器人配置教程](FEISHU_BOT_SETUP.md)。

### 本地端口被占用

关闭旧的 Research Connect、Daily Paper 或 CitationClaw 进程，再运行 `start`。不要同时手工启动模块服务和 `connect-hub serve`。

### 如何停止？

前台运行时按 `Ctrl+C`。正在执行的任务可先在飞书发送 `/cancel`。强行关闭终端可能使模块来不及写入最终状态，重启后系统会尽量把遗留运行标为中断。

## 网页与 Report Hub

### 公网页面没有更新

本机结果通常仍在。先查看启动终端中的发布错误，再检查 Report Hub 地址、安装 token 和网络。更新本机代码并重启 Connect Hub 会重新上传配置中心、Daily Paper 和 CitationClaw 页面，通常不需要公网管理员更新容器。

### 页面能打开，但按钮一直等待

网页静态副本保存在公网，但动态操作需要用户电脑上的 Connect Hub 在线。确认本机服务仍运行，并能主动访问 Report Hub。

### 大站点上传失败或返回 413

客户端会把大于 4 MiB 的站点 ZIP 分片上传。如果仍返回 413，公网管理员需要检查反向代理和 Report Hub 的上传限制。日报结果不会因此从本机丢失。

### 如何管理或删除公网内容？

飞书发送 `/storage`，然后选择：

```text
1. 刷新并查看站点
2. 删除指定站点
3. 清空本安装全部公网内容
0. 退出
```

选 `3` 后还需要回复“确认清空”。清空只删除公网副本并保留安装 token；重启服务后必要页面会重新发布。

## 论文功能

### 为什么论文日报是 0 篇？

先看网页实时进度或飞书步骤统计，确认候选在哪一步归零：时间窗抓取、BM25/向量召回、Reranker、LLM 精筛或最终数量限制。再在 `/config` 测试 Supabase、Embedding、Reranker 和 LLM，不要只根据最终结果猜测。

### 为什么日报最后阶段很慢？

召回和筛选只处理标题摘要，最终文档阶段可能逐篇下载原文、用 Docling/Jina 提取并多次调用 LLM。几十篇论文会需要较长时间，应减少精读/速读数量，而不是简单扩大总超时。

### 为什么论文总结拿不到 arXiv 内容？

可能是 arXiv、Jina 或网络出口限流。论文 PDF 获取和元数据获取是不同路径；遇到 429 时稍后重试，或直接上传 PDF。

### CitationClaw 为什么没有引用描述综合分析？

该部分属于更高服务档位的引文描述搜索，不是基础服务结果。Semantic Scholar Key 主要影响作者/论文/施引文献查询速率和完整性；具体配置见外部服务文档。

## 数据与更新

### 本地数据保存在哪里？

- Linux：`~/.research-connect/data`
- Windows：`%USERPROFILE%\.research-connect\data`

可用 `RESEARCH_CONNECT_DATA_DIR` 修改。`.env`、SQLite、PDF、图片、缓存、模型和生成报告不会提交 Git。

### 如何更新？

先停止服务，再执行：

```bash
git pull --ff-only
./scripts/setup.sh --with-docling
```

```powershell
git pull --ff-only
.\scripts\setup.ps1 -WithDocling
```

没有安装 Docling 的用户去掉对应参数。安装脚本不会覆盖 `.env` 或本地数据目录。

### 可以直接删除 `.venv` 或数据目录吗？

`.venv` 可以在停止服务后重建，但数据目录包含任务、配置、缓存和生成结果，删除前必须备份。安装脚本不会主动删除数据目录。
