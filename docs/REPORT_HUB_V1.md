# Report Hub v1 接入协议

## 目标

Report Hub 只提供安装注册、稳定站点、运行快照、文件托管和受限命令邮箱。任务生命周期和进度事件保存在用户本机 Connect Hub，并由飞书文字消息或模块原版网页展示。

客户端先使用邀请码注册，注册命令自动写入：

```env
REPORT_HUB_API_URL=https://reports.example.com
REPORT_HUB_AGENT_TOKEN=<注册接口一次性签发>
```

所有 Agent 写接口使用 `Authorization: Bearer <token>`。这里只接受安装 token，且只能操作该安装拥有的站点；服务器管理员通过本机 CLI 运维，不存在全局 HTTP token。

## 稳定站点

Connect Hub 为配置中心、Daily Paper 和 CitationClaw 分别调用 `POST /api/v1/sites`。Report Hub 返回高熵 `/s/{public_token}/` URL；同一 `site_id` 重复注册会沿用原 URL，并更新本地客户端声明的 `command_policy`。

客户端将包含根目录 `index.html` 的 ZIP 上传到：

```http
PUT /api/v1/sites/{site_id}/report
Authorization: Bearer ...
Content-Type: application/zip
```

大站点使用 `/api/v1/sites/{site_id}/uploads/{upload_id}/parts/{part_number}` 分片上传。服务端校验大小、校验和、路径穿越和符号链接，全部分片完成后原子替换站点。

## 运行快照

Daily Paper 可将无密钥的运行元数据和日志镜像到：

```http
PUT /api/v1/sites/{site_id}/runs/{run_id}
```

原版网页通过以下公开接口读取：

- `GET /s/{public_token}/api/local/runs`
- `GET /s/{public_token}/api/local/runs/{run_id}/log`

这只是模块稳定站点的历史/进度数据，不是独立公网任务页。飞书仍直接接收本机 Connect Hub 的结构化事件播报。

## 通用命令邮箱

每个站点注册一组只允许精确路径或末尾 `/*` 前缀的 `command_policy`。浏览器请求 `/s/{public_token}/api/...` 时，Report Hub 校验策略并放入 `site_commands`；用户本机 Connect Hub 主动拉取、调用 `127.0.0.1` 模块 API，再回传响应。

Report Hub 不理解具体模块业务、不连接用户内网，也不保存 Provider 配置。新增网页按钮或模块 API 时，由新版客户端重新上传站点和策略即可。

## 注册协议

管理员先创建限时限次邀请码：

```bash
report-hub --issue-invite demo --max-uses 30 --expires-in 30d
```

客户端调用公开的 `POST /api/v1/installations/register`，提交邀请码、飞书 App ID 和设备备注。服务端验证邀请状态、次数、有效期和 App ID 唯一性，返回一次性安装 token；数据库只保存哈希。

详细命令见 [Report Hub 多安装接入](REPORT_HUB_MULTI_INSTALL.md)。

## 安装数据管理

安装 token 可以调用：

- `GET /api/v1/installations/current/storage`：列出自己的站点、运行数量和磁盘占用；
- `DELETE /api/v1/installations/current/sites/{site_id}`：删除自己的一个站点；
- `DELETE /api/v1/installations/current/data`：清空自己的全部公网内容，但保留安装 token。

跨安装操作返回 403。管理员按 `install_id` 清理或彻底删除用户时使用 `report-hub --clear-install-data` / `--delete-install` CLI。
