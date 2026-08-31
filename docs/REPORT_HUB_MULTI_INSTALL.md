# Report Hub 多安装接入（简化版）

## 目标

多个用户可以各自在 Windows/Linux 电脑或服务器上运行 Connect Hub，共用一个公网 Report Hub。论文处理、Docling 和 LLM 调用仍发生在用户自己的机器或其配置的外部 API；公网服务器只负责 HTTPS 页面、统一配置、运行快照、文件和受限命令队列。

不需要用户注册公网账号，不需要公网 IP、VPN、FRP、Cloudflare Tunnel、PostgreSQL 或 Redis。

## 凭证分为三类

| 凭证 | 持有人 | 用途 |
|---|---|---|
| 服务器管理员 token | 仅服务器管理员 | 部署验收和紧急运维；不得分发给普通用户 |
| 安装 token | 每个 Connect Hub 安装一个 | 上传自己的站点、读取自己的配置和命令队列 |
| 网页随机链接 | 由 Report Hub 自动生成并由机器人返回 | 在手机/电脑访问该安装的配置页、日报页和查引用页 |

安装 token 只在签发或轮换时显示一次，Report Hub SQLite 只保存 SHA-256 哈希。网页链接本身是 bearer link，应当像私人共享链接一样保管。

## 服务器管理员：签发一个安装

在公网服务器仓库中执行：

```bash
cd apps/report-hub
docker compose exec report-hub report-hub --issue-install "用户姓名或设备备注"
```

命令输出如下两项供用户填写：

```dotenv
REPORT_HUB_API_URL=https://report.sinksilk.com:58443
REPORT_HUB_AGENT_TOKEN=rhi_...
```

通过私人飞书消息、当面传递或其他私密渠道发给该用户，不要放进群聊、Git、截图和公开文档。用户不需要拿到服务器 `.env` 中的管理员 token。

## 用户：本机配置

把管理员发来的两行写入 `apps/connect-hub/.env`，然后按正常方式启动：

```bash
.venv/bin/connect-hub --env-file apps/connect-hub/.env serve
```

Connect Hub 会使用该 token 创建自己的 Daily Paper、CitationClaw 和配置中心站点。用户通过飞书 `/config`、`/paper_reader`、`/citationclaw` 或机器人菜单获得自己的网页链接，不需要管理员再分发网页地址。

从旧版共享管理员 token 升级时，管理员先为原用户签发安装 token。用户替换本机 `.env` 并第一次重启后，服务会把该用户原有、尚未归属任何安装的三个稳定站点认领给这个安装；网页地址和历史配置不变。认领只适用于升级前 `owner_install_id` 为空的站点，已属于其他安装的站点始终返回 403。

每位用户应创建自己的飞书应用。站点稳定 ID 会结合飞书 App ID 生成；多人共用同一个飞书 App ID 会被视为同一个安装，不属于支持用法。

## 查看、停用和轮换

```bash
cd apps/report-hub

# 只显示安装 ID、状态、备注和创建时间，不显示 token
docker compose exec report-hub report-hub --list-installs

# 丢失或怀疑泄漏时轮换；历史站点和配置不变，旧 token 立即失效
docker compose exec report-hub report-hub --rotate-install INSTALL_ID

# 停用一个安装
docker compose exec report-hub report-hub --revoke-install INSTALL_ID
```

轮换后只把新输出的 `REPORT_HUB_AGENT_TOKEN` 发给对应用户，并重启该用户的 Connect Hub。

## 隔离边界

- 安装 token 只能操作由该安装创建的站点、任务、配置和命令队列；跨安装访问返回 403。
- 管理员 token 可以操作所有记录，用于兼容既有单用户数据和运维，因此必须只留在服务器管理员手中。
- 各用户的 LLM、Embedding、Reranker、Supabase 和 Citation API Key 保存在各自的安装级配置中，公开 GET 只返回“已配置”状态，不回显密钥。
- 公网服务器仍共用磁盘和 SQLite；当前适合少量可信 demo 用户。单次上传已有大小限制，但暂不实现用户计费、账号系统和精细磁盘配额。
- 备份 `report-hub-data` Docker 卷和服务器 `.env`；不要运行 `docker compose down -v`。

## 当前无需再增加的组件

- 不需要用户数据库或 OAuth 登录；
- 不需要邮件邀请系统，管理员命令签发即可；
- 不需要为每个用户启动一个 Report Hub 容器；
- 不需要公网服务器替用户运行模型；
- 不需要把用户电脑的 FastAPI 端口暴露到公网。

如果以后从少量可信用户扩大到公开注册，再增加磁盘配额、请求限流、用户自助登录和审计后台；这些不作为当前 demo 的部署前置条件。
