# Docker 部署（可选）

Docker 版把 Connect Hub、Daily Paper、CitationClaw、XHS Agent、Docling 和
Playwright Chromium 放进同一个镜像。运行时只有 Connect Hub 常驻；它仍按现有方式在容器内部
按需管理读论文和查引用服务，不会额外引入 PostgreSQL、Redis 或常驻模型服务。

这是一种客户端部署方式，不是公网 Report Hub 的部署方式。用户电脑仍通过飞书长连接和
Report Hub 主动向外连接，所以无需公网 IP，也无需在路由器或 Docker 中开放端口。

## 1. 准备配置

要求 Docker Engine 24+ 或 Docker Desktop，以及 Compose v2。

Linux：

```bash
git clone https://github.com/Baorunhui/research-connect.git
cd research-connect
./scripts/docker-setup.sh
```

Windows PowerShell：

```powershell
git clone https://github.com/Baorunhui/research-connect.git
cd research-connect
powershell -ExecutionPolicy Bypass -File .\scripts\docker-setup.ps1
```

编辑 `docker/config/connect-hub.env`，至少填写飞书 App ID、App Secret 和统一 LLM：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
LLM_BASE_URL=https://your-openai-compatible-endpoint/v1
LLM_API_KEY=xxx
LLM_MODEL=your-model
```

准备脚本还会创建 `docker/config/daily-paper.yaml`，以便 Daily Paper 网页内保存的订阅和运行
配置在容器重建后继续存在。`docker/config/` 已被 Git 和 Docker 构建上下文排除，不会进入
提交或镜像。

## 2. 构建

```bash
docker compose build
```

默认镜像基于 Python 3.11 slim，并包含 Docling 及 Chromium，因此首次构建下载量和镜像体积
会明显大于 Python 安装版。镜像只安装 Chromium 必需系统库和中英文字体，不包含本地运行记录、
模型缓存或 CUDA 运行库。
默认安装 PyTorch CPU wheel，避免普通用户被迫下载数 GB 的 CUDA 运行库。首次执行某些
Docling 能力还可能把模型下载到持久化的 `/data/cache`。

确实需要 NVIDIA GPU 时，可以指定与宿主机驱动兼容的 PyTorch wheel 源并为 Compose
配置 GPU 设备，例如先设置 `DOCLING_TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128`
再构建。GPU 容器还要求宿主机安装 NVIDIA Container Toolkit；CPU 镜像是默认和兜底路径。

## 3. 注册 Report Hub

使用公网管理员提供的邀请码完成一次注册：

```bash
docker compose run --rm research-connect register \
  --server https://report.sinksilk.com:58443 \
  --invite 'rhi_inv_xxx' \
  --username '你的名字'
```

注册得到的安装 token 会写回 `docker/config/connect-hub.env`。一个飞书 App ID 只注册一次。

## 4. 检查并启动

```bash
docker compose run --rm research-connect doctor
docker compose up -d
docker compose logs -f research-connect
```

看到飞书 WebSocket 连接成功后即可私聊机器人。退出日志查看不会停止服务。

常用管理命令：

```bash
docker compose ps
docker compose restart research-connect
docker compose stop
docker compose down
```

`docker compose down` 只删除容器和网络，不删除数据卷。不要附加 `-v`，除非明确要永久删除
SQLite、配置索引、论文历史、日报网页和缓存。

## 5. 更新

```bash
git pull --ff-only
docker compose build
docker compose up -d
```

配置与历史数据保留在宿主机配置目录和命名卷中，重建镜像不会清空。

## 数据与端口

| 内容 | 容器位置 | 保存方式 |
|---|---|---|
| 飞书、LLM、Report Hub 配置 | `/config/connect-hub.env` | `docker/config/` 绑定目录 |
| Daily Paper 原版配置 | `/app/modules/daily-paper-reader/config.yaml` | `docker/config/daily-paper.yaml` 绑定文件 |
| Connect Hub/CitationClaw 数据 | `/data` | `research-connect-data` 卷 |
| Daily Paper 站点和报告 | `/app/modules/daily-paper-reader/docs` | `daily-paper-docs` 卷 |
| Daily Paper 运行记录 | `/app/modules/daily-paper-reader/.local-runs` | `daily-paper-runs` 卷 |
| Daily Paper 原始缓存 | `/app/modules/daily-paper-reader/archive` | `daily-paper-archive` 卷 |

Compose 不发布任何端口。`8567`、`8000` 和 `8791` 只用于同一容器内的模块通信；手机和其他
设备看到的网页仍由 Report Hub 提供。

Linux 准备脚本会把当前账号的 UID/GID 写入 Git 忽略的根目录 `.env`，因此注册命令可以安全地
原子更新配置文件，生成结果也不会变成宿主机上无法管理的 root 文件。Windows Docker Desktop
使用容器默认的 1000:1000 映射。

## 排查

```bash
docker compose logs --tail 200 research-connect
docker compose run --rm research-connect doctor
docker compose config
```

- 配置文件不存在：重新执行准备配置中的 `cp`，不要把单个文件挂载到 `/config`；注册过程需要原子更新该文件。
- 飞书无响应：检查日志中的 WebSocket 连接、飞书应用版本、事件订阅和可用范围。
- 浏览器或 Docling 首次较慢：检查磁盘空间和网络，模型缓存会在命名卷中复用。
- 想查看卷：使用 `docker volume ls | grep research-connect`，不要直接修改卷内数据库。
