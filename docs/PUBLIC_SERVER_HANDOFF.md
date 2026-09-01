# 公网 Report Hub：技术路线与服务器交付说明

## 2026-08-31：统一配置与本机命令中继更新

本次更新需要重新构建并滚动重启 Report Hub Docker 容器。无需新增端口、数据库或环境变量；仍使用现有 HTTPS 域名、SQLite 数据卷和服务器管理员 `REPORT_HUB_AGENT_TOKEN`。管理员 token 不再分发给普通用户；每个用户使用单独签发的安装 token，详见 [REPORT_HUB_MULTI_INSTALL.md](REPORT_HUB_MULTI_INSTALL.md)。

- `PUT /api/v1/sites/{site_id}/config` 供 Connect Hub 使用 Agent Token 初始化安装级配置；
- `/configure/{public_token}/` 是带随机 bearer token 的手机兼容配置页；
- 页面可保存、查看脱敏状态，并主动测试 LLM、Embedding、Reranker、Supabase、Exa、Jina 和 Citation Provider；
- 配置仍保存在 Report Hub 数据卷内的 SQLite，不进入 Git 或模块静态网页。
- `connect-config-*`、`daily-paper-*`、`citationclaw-*` 使用相同安装 ID 后缀，映射到同一条安装级配置；三个页面不再各存一份配置，也没有覆盖优先级；
- Daily Paper 的聊天模型与 CitationClaw 的轻量模型都映射到 `llm.primary`；CitationClaw Search LLM 单独映射到 `citation.search_llm`；
- `site_commands` 出站命令队列同时服务 CitationClaw 与 Daily Paper。公网原版页面把运行、问答、总结、综述、状态、取消和结果读取请求写入队列，本机 Connect Hub 主动拉取后调用 `127.0.0.1` 模块接口，再回传响应；
- 用户电脑不需要公网 IP、端口映射或额外穿透软件。旧版每模块配置会在首次访问时一次性导入安装级配置。

部署后先检查 `/healthz`。本机 Connect Hub 重启时会自动创建 `connect-config-*` 稳定站点；配置页链接本身具有管理权限，不应转发或公开。

## 一句话技术路线

每位用户的 Windows/Linux 电脑负责执行论文日报、查引用和小红书任务；共同的公网 Report Hub 接收配置、进度/运行快照和最终静态网页。Daily Paper 与 CitationClaw 按安装实例发布固定原版网页地址；任务开始时链接就发到飞书，手机和电脑均可查看，任务结束后内容继续保存在公网服务器。

```text
用户飞书 → 用户电脑上的 Connect Hub → 本机领域模块
                         │
                         ├─ HTTPS 上报进度/运行快照 → 公网 Report Hub(SQLite)
                         └─ HTTPS 上传原生静态站点/报告 → 公网磁盘

手机/电脑浏览器 ← Daily Paper / CitationClaw 固定原版网页链接
```

这不是让公网服务器主动反向代理用户电脑，也不把本机 FastAPI 暴露到公网。因此用户不需要公网 IP、内网穿透账号或路由器配置。三个配置界面读写同一份安装级配置，本机 Connect Hub 主动拉取配置和待执行命令；飞书 Secret 仍只留在本机。由于配置包含 LLM/API Key，正式服务必须启用 HTTPS，并妥善保护 Report Hub 数据目录、页面 bearer 链接和 Agent Token。

公网 CitationClaw 与 Daily Paper 的任务操作是一条动态“邮箱”链路：浏览器投递请求，用户电脑上的 Connect Hub 取件并执行，再把响应放回公网。它不是通用 HTTP 隧道，只允许代码内分别列出的模块 API 路径；单次请求最多等待 140 秒，日报、总结和综述等长任务本身仍是异步启动，之后通过状态轮询取得进度。Report Hub 容器无需连接用户内网。

## 需要服务器管理员提供

- 一台有公网入口的 Linux 服务器，Python 3.10+ 或 Docker；
- 一个域名（如 `reports.example.edu.cn`），A 记录指向服务器；
- 放行 TCP 80/443；测试阶段可临时放行 TCP 8787；
- 建议至少 1 CPU、1 GB 内存；磁盘容量取决于报告和图片留存，建议从 20 GB 起；
- 决定备份与清理周期。Python 部署建议每日备份 `apps/report-hub/data/`；Docker 部署备份 `report-hub-data` 命名卷。报告建议保留 90 天以上。

## 测试部署（Python）

```bash
cd research-connect/apps/report-hub
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/report-hub --init-config
.venv/bin/report-hub
curl http://127.0.0.1:8787/healthz
```

编辑 `.env`：测试时 `REPORT_HUB_PUBLIC_BASE_URL=http://211.86.155.100:8787`；有域名后改成 `https://reports.example.edu.cn`。`.env` 内生成的 `REPORT_HUB_AGENT_TOKEN` 只分发给可信的 Connect Hub 安装者，不提交 Git。

长期运行可把 `deploy/report-hub.service` 中用户和路径换成实际值后安装到 systemd。

## Docker 部署

```bash
cd research-connect/apps/report-hub
cp .env.example .env
# 修改公网地址，并把 token 替换为至少 32 字符的随机值
docker compose up -d --build
```

`report.sinksilk.com` 的本次升级步骤与验收命令见 [`PUBLIC_SERVER_DOCKER_UPDATE_20260830.md`](PUBLIC_SERVER_DOCKER_UPDATE_20260830.md)。

## 域名与 HTTPS

安装 Caddy，把 `deploy/Caddyfile` 的域名换成实际域名；Caddy 会自动申请证书并支持 WebSocket 反向代理。正式环境只把 Caddy 的 80/443 暴露公网，Report Hub 的 8787 可只监听内网或防火墙限制访问。

## 验收

1. `https://域名/healthz` 返回 `status: ok`；
2. 运行 `scripts/smoke_report_hub.py`，它会创建任务、上报三条进度并上传一份静态报告；
3. 从非校园 Wi-Fi 的手机打开脚本输出链接；
4. 重启本地客户端，确认旧报告仍能访问；
5. 重启 Report Hub，确认 SQLite 与报告目录仍在。
6. 在统一配置页修改统一 LLM 模型，刷新 Daily Paper 设置页和 CitationClaw 首页，确认两边同时显示新模型且密钥显示“已配置”；
7. 在 CitationClaw 公网页提交一个测试任务，确认本机 Connect Hub 日志出现命令中继请求，公网状态与结果列表可以刷新。
8. 在 Daily Paper 公网页检查论文问答模型，并提交一条论文链接总结；确认立即返回 `job_id`，随后能轮询查看实时进度。

## 安全与运维边界

- 公开链接是“持有链接即可查看”，token 足够长但不是账号权限系统；日报按当前需求可公开，敏感材料以后再加登录层。
- 写接口必须带 token；普通安装 token 只能操作自己的资源，服务器管理员 token 可操作全部资源。应使用 HTTPS，避免 token 明文传输。
- ZIP 上传会限制大小，并拒绝路径穿越和符号链接。
- 服务首版单进程、SQLite，无 PostgreSQL/Redis/本地模型，部署和迁移成本较低。
- 备份的最小集合只有 `.env` 与 `data/`；其中 `.env` 应加密保存。
