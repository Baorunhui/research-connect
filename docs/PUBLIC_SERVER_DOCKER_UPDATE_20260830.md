# Report Hub Docker 更新单（2026-08-30）

这份文档可直接交给 `report.sinksilk.com` 的服务器维护人员。

## 本次更新目标

服务器当前 `/healthz` 正常，但 `POST /api/v1/sites` 返回 `404`，说明容器运行的是旧版 Report Hub。新版至少需要包含以下历史提交，并更新到 `main` 最新提交：

```text
60a43eb fix: make Report Hub Docker image standalone
16671c9 fix: proxy Daily Paper setup checks and update Docling extraction
87bc3c0 feat: configure modules from stable public sites
```

其中公网服务器实际使用的改动包括：

- Daily Paper 与 CitationClaw 的固定原版网页站点 `/s/{token}/`；
- SQLite 中的站点、运行快照和模块配置记录；
- 公网设置页面的配置读取与保存；
- Daily Paper 设置页手动“获取模型列表”和“测试连接”的受限代理接口；
- Agent API：Connect Hub 在任务开始前读取配置并上传网页/进度。
- 安装级唯一配置：统一配置页、Daily Paper、CitationClaw 共享一条 SQLite 配置记录；
- CitationClaw `site_commands` 受限命令队列，本机 Connect Hub 主动拉取并执行；
- CitationClaw 公网页的状态、取消、结果浏览和下载中继，以及上次输入参数预填。
- 每个 Connect Hub 安装独立的哈希 token 与资源归属校验；管理员 token 不再分发给普通用户。

Docling 抽图代码属于用户电脑上的 Daily Paper，不在公网 Report Hub 容器内运行。

## Docker 更新命令

在服务器的 `research-connect` 仓库执行：

```bash
git fetch --all --prune
git switch main
git pull --ff-only
git rev-parse --short HEAD

cd apps/report-hub
docker compose build --pull --no-cache report-hub
docker compose up -d --force-recreate report-hub
docker compose ps
docker compose logs --tail=100 report-hub
```

`git rev-parse --short HEAD` 必须包含 `60a43eb`，并应为该提交或更新提交。`60a43eb` 移除了 Report Hub 未使用、会让独立 Docker 构建失败的本地包依赖。

不要执行 `docker compose down -v`，否则会删除保存 SQLite、配置和网页的 `report-hub-data` 命名卷。

## 环境变量

`apps/report-hub/.env` 至少需要：

```dotenv
REPORT_HUB_HOST=0.0.0.0
REPORT_HUB_PORT=58787
REPORT_HUB_PUBLIC_BASE_URL=https://report.sinksilk.com:58443
REPORT_HUB_AGENT_TOKEN=<双方已经约定的 token，不要发到公开仓库或日志>
REPORT_HUB_MAX_UPLOAD_MB=256
REPORT_HUB_MAX_EXPANDED_MB=1024
```

Compose 已把持久化目录固定为 `/data`，对应 `report-hub-data` 命名卷，不需要在 `.env` 中另改宿主机路径。

Nginx 应把 `https://report.sinksilk.com:58443` 的全部路径原样反向代理到 Report Hub 的 `58787` 端口，不能只代理 `/healthz` 或 `/api/v1/jobs`。请使用 [nginx-report-hub.conf](../apps/report-hub/deploy/nginx-report-hub.conf) 中的关键设置，尤其是 `client_max_body_size 256m`。论文日报原版站点会保留历史图片，实际压缩包可能超过 50 MiB；Nginx 和 `REPORT_HUB_MAX_UPLOAD_MB` 两层限制必须同时放大。

修改 Nginx 后执行：

```bash
nginx -t
nginx -s reload
```

如果 Nginx 也运行在 Docker 中，请在对应容器内检查并 reload，而不是在宿主机盲目执行上述命令。

## 升级验收

```bash
BASE=https://report.sinksilk.com:58443
AGENT_TOKEN='<REPORT_HUB_AGENT_TOKEN>'

curl -fsS "$BASE/healthz"

curl -fsS -X POST "$BASE/api/v1/sites" \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"site_id":"deploy-check","module_name":"other","title":"Deploy Check"}'

curl -fsS "$BASE/api/v1/sites/deploy-check/config" \
  -H "Authorization: Bearer $AGENT_TOKEN"
```

预期：

- `/healthz` 返回 `status: ok`；
- 创建站点返回 `public_url`，不再是 404；
- 配置查询返回 `{"configured":false,"config":...}`，不再是 404。

完成后把上述三条命令的状态结果发回即可，不要发送 Agent Token。

服务器验收通过后，用户电脑只需重启一次 `connect-hub serve`。Connect Hub 会在启动阶段自动创建并上传 Daily Paper 与 CitationClaw 的原版网页；这不是公网容器的第二次更新，也不需要服务器管理员手工复制网页文件。

给新用户签发凭证时使用：

```bash
docker compose exec report-hub report-hub --issue-install "用户姓名或设备备注"
```

把输出的 `REPORT_HUB_API_URL` 和安装级 `REPORT_HUB_AGENT_TOKEN` 私下发给对应用户。不要把服务器 `.env` 中的管理员 token 发给用户。查看、轮换和停用命令见 [`REPORT_HUB_MULTI_INSTALL.md`](REPORT_HUB_MULTI_INSTALL.md)。

如果日报已经生成、但曾收到 `413 Request Entity Too Large`，不需要重新运行日报。先完成上述两层上传限制更新，再重启用户电脑上的 Connect Hub；启动阶段会把本机现有整站补同步到公网。

## 它是不是“动态网络”或内网穿透？

不是通用内网穿透。它是一个轻量的公网中转、配置与静态网页托管服务：

```text
飞书 → 用户电脑 Connect Hub → 本机运行 Daily Paper/CitationClaw
                         │
                         └─ 主动发起 HTTPS 请求
                              ├─ 上传原版网页和结果
                              ├─ 上报实时进度
                              ├─ 读取用户已保存的统一配置
                              └─ 拉取 CitationClaw 待执行命令并回传响应

手机/电脑浏览器 ──HTTPS──> 公网 Report Hub
```

- 公网服务器不会主动连接用户电脑；
- 不需要用户有公网 IP，不需要路由器端口映射、VPN、FRP 或 Cloudflare Tunnel；
- 用户电脑只需能主动访问公网 HTTPS；
- Report Hub 不运行论文流水线、Docling 或其他模型；CitationClaw 网页请求只进入受限队列，由用户电脑执行；
- 网页和配置保存在公网服务器的 SQLite/磁盘卷中，因此关掉用户电脑后历史页面仍可访问。

类似“代理”的地方有两处：Daily Paper 设置页的模型列表/连接测试由 Report Hub 发起；CitationClaw 的白名单接口通过命令队列交给本机 Connect Hub。后者只有代码列出的固定路径，不能访问任意本机地址，也不是通用 HTTP 隧道。

## 安全边界

- 正式环境必须使用 HTTPS；
- 模块 API Key 会保存在 Report Hub 的 SQLite 中，公开读取接口会把密钥字段清空，只有携带 Agent Token 的本机 Connect Hub 能取回完整配置；
- 固定站点链接当前采用“持有随机链接即可访问/修改配置”的单用户设计，不适合公开传播；
- 服务器管理员 token 权限较高，只留给服务器管理员；普通用户使用各自的安装 token。两者都不写入 Git、群聊或普通日志；
- 需要备份的是 `.env` 和 `report-hub-data` 卷。
