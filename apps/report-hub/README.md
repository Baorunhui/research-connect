# Report Hub

Report Hub 是 Research Connect 的通用公网注册、隔离、文件/运行记录托管和命令邮箱。它不运行论文任务或模型，不持有飞书 Secret，不保存 Provider 配置，也不理解 Daily Paper/CitationClaw 的业务结构。

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/report-hub --init-config
.venv/bin/report-hub
```

健康检查：`curl http://127.0.0.1:58787/healthz`。完整部署、注册、用户管理、备份和更新方式见 [Report Hub 公网服务器部署与运维](../../docs/PUBLIC_SERVER_ADMIN_GUIDE.md)。

模块页面的本机 API 中继采用按站点存储的动态 `command_policy`。策略由拥有该站点
的 Connect Hub 安装在启动时登记；未登记的操作默认返回 403。模块新增网页操作时
不需要修改或重建 Report Hub。

创建一个可供 30 个测试安装使用、30 天有效的邀请码：

```bash
report-hub --issue-invite "demo" --max-uses 30 --expires-in 30d
```

可用 `--list-invites` 查看使用量，用 `--revoke-invite INVITE_ID` 提前注销。公网 HTTP API 不设置管理员 token；管理员通过 `--show-install-data`、`--clear-install-data` 和 `--delete-install` 等本机 CLI 管理用户数据。
