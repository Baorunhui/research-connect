# Report Hub 多安装接入

多个用户可以在自己的 Windows/Linux 电脑上运行 Connect Hub，共用一个公网 Report Hub。论文处理、Docling、LLM 调用和 API 配置都留在用户电脑；公网服务器只负责邀请注册、token 隔离、静态站点与运行快照、文件存储和通用命令邮箱。

不需要邮件注册、用户账号、VPN、内网穿透、PostgreSQL 或 Redis。

## 凭证边界

| 凭证 | 持有人 | 用途 |
|---|---|---|
| 邀请码 | 一批测试用户 | 在有效期和次数内创建安装 |
| 安装 token | 每个 Connect Hub 安装 | 操作本安装的站点、文件、运行记录和命令 |
| 网页随机链接 | 对应用户 | 浏览和操作该安装发布的页面 |

服务端只保存邀请码和安装 token 的 SHA-256 哈希。邀请码原文只在创建时显示；安装 token 原文只在注册响应或轮换时出现。

## 管理员创建邀请码

在公网服务器执行：

```bash
cd research-connect/apps/report-hub
docker compose exec report-hub report-hub \
  --issue-invite "demo-2026-09" \
  --max-uses 30 \
  --expires-in 30d
```

把输出的 `REPORT_HUB_INVITE_CODE` 通过私密渠道发给测试用户。它最多注册 30 个不同的飞书 App ID，30 天后自动失效。

查看或主动注销邀请码：

```bash
docker compose exec report-hub report-hub --list-invites
docker compose exec report-hub report-hub --revoke-invite INVITE_ID
```

`--expires-in` 支持 `30m`、`24h`、`30d`。注销只阻止后续注册，不会停用已经注册的安装。

## 用户自助注册

先在 `apps/connect-hub/.env` 填好自己的 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`，再执行：

Linux：

```bash
.venv/bin/connect-hub --env-file apps/connect-hub/.env register \
  --server https://report.sinksilk.com:58443 \
  --invite 'rhi_inv_xxx'
```

Windows PowerShell：

```powershell
.venv\Scripts\connect-hub.exe --env-file apps\connect-hub\.env register `
  --server https://report.sinksilk.com:58443 `
  --invite 'rhi_inv_xxx'
```

注册成功后，命令会把公网地址和安装 token 原子写入 `.env`，不会在终端回显完整 token。随后运行 `doctor` 和 `start` 即可。

同一个飞书 App ID 只能注册一次，避免多人误用同一个机器人后混入同一安装。每位测试用户必须创建自己的飞书应用。

## 安装运维

```bash
# 查看安装，不显示 token
docker compose exec report-hub report-hub --list-installs

# 怀疑泄漏时轮换；旧 token 立即失效，站点与历史文件保留
docker compose exec report-hub report-hub --rotate-install INSTALL_ID

# 停用安装
docker compose exec report-hub report-hub --revoke-install INSTALL_ID

# 查看指定安装占用的站点和空间
docker compose exec report-hub report-hub --show-install-data INSTALL_ID

# 清空站点与文件，但保留安装和 token
docker compose exec report-hub report-hub --clear-install-data INSTALL_ID --yes

# 删除全部数据和安装记录，token 立即永久失效
docker compose exec report-hub report-hub --delete-install INSTALL_ID --yes
```

手工 `--issue-install` 仍保留为管理员应急入口，正常新增用户应使用邀请码注册。

普通用户也可在自己的电脑执行：

```bash
connect-hub --env-file apps/connect-hub/.env remote-data list
connect-hub --env-file apps/connect-hub/.env remote-data delete-site SITE_ID
connect-hub --env-file apps/connect-hub/.env remote-data clear --yes
```

这些命令只使用当前安装 token，只能看到和删除本安装的数据。`clear` 保留 token，重新启动 Connect Hub 后会重新发布三个稳定页面。

## 配置与隔离

- LLM、Embedding、Reranker、Supabase 和 Citation API Key 的正式副本在用户电脑的 `providers.json`。
- `/config` 页面由本地 Connect Hub 打包上传，Provider 目录、探测和模块配置转换也在本机执行。
- 用户从手机保存配置时，请求会短暂经过 Report Hub 命令邮箱，但不会写入公网配置表；返回内容会脱敏。
- 安装 token 只能操作本安装拥有的站点、运行快照和命令，跨安装访问返回 403。
- 公网 HTTP API 只接受安装 token，不提供管理员绕过。管理员管理操作只在服务器 shell/Docker CLI 中开放。
- 网页链接是 bearer link，拿到链接即可访问对应页面，仍需当作私密链接保存。

当前设计适合少量可信 demo 用户。若以后开放公共注册，再增加账号登录、速率限制、磁盘配额和审计后台。
