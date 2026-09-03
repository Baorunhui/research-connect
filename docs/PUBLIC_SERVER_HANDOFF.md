# 公网 Report Hub 交付与更新

## 本次升级结论

这是一次不兼容升级。现有测试用户需要拉取新版、使用邀请码重新注册并重新启动。完成这次公网 Docker 更新后，日常新增 Provider、修改配置页面、调整 Daily Paper/CitationClaw 接口时通常只需更新用户本机代码并重启 Connect Hub，不再更新公网服务器。

Report Hub 只保留以下通用职责：

- 邀请码注册、安装 token 签发与撤销；
- 按安装隔离站点、运行快照、文件和命令；
- 托管客户端上传的静态站点与报告；
- 提供浏览器到本机 Connect Hub 的通用受限命令邮箱；
- SQLite 持久化和上传大小限制。

它不再保存统一 Provider 配置，不再探测外部 API，不再理解 Daily Paper/CitationClaw 配置结构，也不再内置这些产品页面的业务接口名单。命令策略和配置页面由用户本机 Connect Hub 上传。

## 端口与环境

容器监听 `58787`，公网 HTTPS 入口为 `58443`。服务器 `.env` 示例：

```dotenv
REPORT_HUB_HOST=0.0.0.0
REPORT_HUB_PORT=58787
REPORT_HUB_PUBLIC_BASE_URL=https://report.sinksilk.com:58443
REPORT_HUB_AGENT_TOKEN=至少32字符的服务器管理员密钥
REPORT_HUB_DATA_DIR=/data
REPORT_HUB_MAX_UPLOAD_MB=256
REPORT_HUB_MAX_EXPANDED_MB=1024
```

`REPORT_HUB_AGENT_TOKEN` 只供服务器运维，不分发给普通用户。Caddy/Nginx 将公网 `58443` 反向代理到容器 `58787`。

## 一次性更新

先备份服务器 `.env` 和 `report-hub-data` 数据卷，再执行：

```bash
cd research-connect
git pull --ff-only
cd apps/report-hub
docker compose build --no-cache report-hub
docker compose up -d report-hub
docker compose ps
curl -fsS http://127.0.0.1:58787/healthz
curl -fsS https://report.sinksilk.com:58443/healthz
```

不要运行 `docker compose down -v`，否则会删除 SQLite、站点和报告数据。

## 创建测试邀请码

```bash
docker compose exec report-hub report-hub \
  --issue-invite "demo-30-users" \
  --max-uses 30 \
  --expires-in 30d
```

记录输出的 `invite_id`，将 `REPORT_HUB_INVITE_CODE` 私下发给测试用户。管理命令：

```bash
docker compose exec report-hub report-hub --list-invites
docker compose exec report-hub report-hub --revoke-invite INVITE_ID
docker compose exec report-hub report-hub --list-installs
docker compose exec report-hub report-hub --revoke-install INSTALL_ID
docker compose exec report-hub report-hub --rotate-install INSTALL_ID
```

邀请码注销后，已注册安装不受影响。安装注销后，该安装 token 立即不能再写入或读取受保护资源。

## 用户注册验收

用户先在本机 `.env` 填写自己的飞书 App ID/Secret，然后执行：

```bash
.venv/bin/connect-hub --env-file apps/connect-hub/.env register \
  --server https://report.sinksilk.com:58443 \
  --invite '管理员提供的邀请码'
```

Windows 使用 `.venv\Scripts\connect-hub.exe`。注册成功后 token 自动写入 `.env`。同一个飞书 App ID 重复注册应返回 409；过期、耗尽或已注销的邀请码应返回 403。

## 验收清单

1. `/healthz` 返回 `status: ok`。
2. 邀请码能按最大次数注册，列表中的 `used_count` 正确增加。
3. 同一飞书 App ID 不能重复注册。
4. 注销邀请码后，新注册被拒绝；既有安装仍可访问。
5. 两个安装创建的站点和任务彼此返回 403。
6. 用户启动 Connect Hub 后，`/config`、`/paper_reader`、`/citationclaw` 返回各自稳定页面。
7. 更新用户本机配置页面后重新启动，公网稳定 URL 内容更新且响应带 `no-cache`。
8. 配置保存、Provider 探测和模块操作通过命令邮箱在用户本机执行。

## 运维边界

- 网页链接是 bearer link，持有链接即可访问；正式使用必须启用 HTTPS。
- API Key 的正式副本在用户电脑。浏览器提交配置时数据会短暂经过通用命令队列，但不会保存到公网配置表。
- Report Hub 单进程使用 SQLite，适合当前少量 demo 用户；建议每日备份数据卷。
- 新增本地网页/API 只需客户端上传新站点和 `command_policy`。只有注册、隔离、存储协议或公网运维本身改变时，才需要再次更新 Report Hub。
