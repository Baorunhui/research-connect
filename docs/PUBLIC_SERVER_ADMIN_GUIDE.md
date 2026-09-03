# Report Hub 公网服务器部署与运维

本文只面向 Report Hub 公网服务器管理员。普通用户请阅读仓库根目录的 [README](../README.md)。

## 1. 服务边界

Report Hub 是一个轻量公网中转与静态文件服务，只负责：

- 使用限时、限次邀请码注册 Connect Hub 安装；
- 为每个安装签发独立 token，并按 `INSTALL_ID` 隔离数据；
- 保存客户端上传的配置页、Daily Paper/CitationClaw 页面和报告副本；
- 保存 Daily Paper 运行快照；
- 通过受限命令邮箱转发浏览器与用户本机服务之间的请求。

它不运行论文任务、Docling 或 LLM，不保存飞书 Secret，也不保存 Provider/API Key 的正式副本。服务器没有全局管理员 HTTP token；管理员操作只能在服务器 shell 或 Docker 容器内执行。

## 2. 准备条件

- 一台安装了 Git、Docker Engine 和 Docker Compose v2 的 Linux 服务器；
- 一个解析到服务器的域名；
- HTTPS 证书，或能够自动申请证书的 Caddy；
- 公网开放 TCP `58443`；
- 服务器本机可使用 TCP `58787`，但不要向公网开放该 HTTP 端口。

约定端口：

| 用途 | 端口 |
|---|---:|
| Report Hub 容器及宿主机 HTTP | `58787` |
| Nginx/Caddy 对外 HTTPS | `58443` |

## 3. 首次部署

```bash
git clone https://github.com/Baorunhui/research-connect.git
cd research-connect/apps/report-hub
cp .env.example .env
```

编辑 `.env`：

```dotenv
REPORT_HUB_HOST=0.0.0.0
REPORT_HUB_PORT=58787
REPORT_HUB_PUBLIC_BASE_URL=https://report.example.com:58443
REPORT_HUB_DATA_DIR=/data
REPORT_HUB_MAX_UPLOAD_MB=256
REPORT_HUB_MAX_EXPANDED_MB=1024
```

不要添加 `REPORT_HUB_AGENT_TOKEN`。用户安装 token 由注册接口签发，服务器管理员不使用 HTTP token。

构建并启动：

```bash
docker compose build report-hub
docker compose up -d report-hub
docker compose ps
docker compose logs --tail=100 report-hub
curl -fsS http://127.0.0.1:58787/healthz
```

数据保存在 Compose 命名卷 `report-hub-data` 的 `/data` 目录。不要执行 `docker compose down -v`。

Compose 默认发布宿主机 `58787`。请通过云安全组或主机防火墙限制该端口只供服务器本机/反向代理访问，公网用户只访问 HTTPS `58443`。

## 4. HTTPS 反向代理

Nginx 示例位于 [`apps/report-hub/deploy/nginx-report-hub.conf`](../apps/report-hub/deploy/nginx-report-hub.conf)。关键配置：

```nginx
server {
    listen 58443 ssl;
    server_name report.example.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    client_max_body_size 256m;
    proxy_request_buffering off;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;

    location / {
        proxy_pass http://127.0.0.1:58787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

应用并验证：

```bash
nginx -t
nginx -s reload
curl -fsS https://report.example.com:58443/healthz
```

Caddy 用户可参考 [`apps/report-hub/deploy/Caddyfile`](../apps/report-hub/deploy/Caddyfile)。必须代理全部路径，不能只代理 `/healthz`。

## 5. 创建和管理邀请码

创建一个最多使用 30 次、30 天有效的邀请码：

```bash
docker compose exec report-hub report-hub \
  --issue-invite "demo-30-users" \
  --max-uses 30 \
  --expires-in 30d
```

输出的 `REPORT_HUB_INVITE_CODE` 只显示一次，应通过私密渠道发给用户。`--expires-in` 支持 `30m`、`24h`、`30d`。

```bash
docker compose exec report-hub report-hub --list-invites
docker compose exec report-hub report-hub --revoke-invite INVITE_ID
```

注销邀请码只阻止新注册，不影响已经注册的用户。

## 6. 用户注册

用户在自己的电脑执行：

```bash
connect-hub --env-file apps/connect-hub/.env register \
  --server https://report.example.com:58443 \
  --invite 'rhi_inv_xxx' \
  --username '用户自定义名称'
```

用户名支持中文、英文和重名，仅用于管理员识别；唯一身份是 `INSTALL_ID`。同一个飞书 App ID 只能注册一次。注册成功后，安装 token 自动写入用户本机 `.env`，服务器只保存 token 哈希。

## 7. 用户与数据管理

查看全部安装：

```bash
docker compose exec report-hub report-hub --list-installs
```

输出包括：

```text
INSTALL_ID  STATE    REGISTERED_AT              USERNAME
38b305...   enabled  2026-09-03T13:15:44+00:00  测试用户
```

用户名可以重复，所有运维命令必须使用唯一的 `INSTALL_ID`。

```bash
# 查看指定安装的站点、运行数量和磁盘占用
docker compose exec report-hub report-hub --show-install-data INSTALL_ID

# token 泄漏时轮换；旧 token 失效，站点和数据保留
docker compose exec report-hub report-hub --rotate-install INSTALL_ID

# 暂停安装；数据保留，token 暂时不能使用
docker compose exec report-hub report-hub --revoke-install INSTALL_ID

# 清空站点和文件，但保留安装记录及 token
docker compose exec report-hub report-hub --clear-install-data INSTALL_ID --yes

# 删除安装、全部数据和 token
docker compose exec report-hub report-hub --delete-install INSTALL_ID --yes
```

用户也可在飞书发送 `/storage`，管理自己安装的公网内容；安装 token 无法查看或删除其他用户的数据。

## 8. 更新

先备份，再执行：

```bash
cd research-connect
git pull --ff-only
cd apps/report-hub
docker compose build --no-cache report-hub
docker compose up -d --force-recreate report-hub
docker compose ps
docker compose logs --tail=100 report-hub
curl -fsS http://127.0.0.1:58787/healthz
curl -fsS https://report.example.com:58443/healthz
```

更新普通客户端网页、Provider 列表或模块接口时，通常只需用户更新本机代码并重启。只有 Report Hub 的注册、隔离、存储协议或服务器运维逻辑变化时才需要更新公网容器。

## 9. 备份与恢复

需要备份的是 `.env` 和完整 `/data` 数据卷。为了得到一致快照，先短暂停止服务：

```bash
mkdir -p backups
docker compose stop report-hub
docker cp "$(docker compose ps -aq report-hub):/data" "backups/report-hub-data-$(date +%F-%H%M%S)"
docker compose start report-hub
```

确认备份目录中包含 `report-hub.sqlite3` 和 `sites/`。恢复前必须再次备份当前数据，停止容器，并整体恢复 `/data`；不要只复制 SQLite 而遗漏站点文件。

## 10. 日常检查

```bash
curl -fsS http://127.0.0.1:58787/healthz
curl -fsS https://report.example.com:58443/healthz
docker compose ps
docker compose logs --tail=200 report-hub
docker compose exec report-hub report-hub --list-invites
docker compose exec report-hub report-hub --list-installs
docker system df
```

建议定期备份数据卷并监控磁盘空间。当前单进程 SQLite 方案面向少量 Demo 用户，不需要 PostgreSQL、Redis 或 Kubernetes。

## 11. 常见故障

- 公网健康检查失败：先检查本机 `58787`，再检查反向代理、证书、防火墙和 `58443`。
- 上传返回 413：同时检查 Nginx/Caddy 请求体限制和 `REPORT_HUB_MAX_UPLOAD_MB`。客户端会对大站点进行 4 MiB 分片上传。
- 用户返回 401：安装 token 无效、已轮换、已撤销或安装已删除。
- 用户返回 403：该 token 正在访问其他安装拥有的站点，或网页请求不在站点声明的命令策略中。
- 网页仍是旧版本：用户需要更新本机代码并重启 Connect Hub，让客户端重新上传静态页面；通常不需要重建公网容器。
- 页面能打开但本机操作超时：用户电脑上的 Connect Hub 未运行，或其命令邮箱轮询无法访问公网服务器。

完整用户侧排查见 [常见问题](FAQ.md)。
