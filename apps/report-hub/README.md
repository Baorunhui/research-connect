# Report Hub

Report Hub 是 Research Connect 的轻量公网配置、任务进度和静态报告托管服务。它不运行论文任务或本地模型，也不持有飞书 Secret；用户选择在公网原版设置页保存的模块 API Key 会进入其 SQLite 数据卷，并只通过 HTTPS 和受保护的 Agent API 同步给本机 Connect Hub。

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/report-hub --init-config
.venv/bin/report-hub
```

健康检查：`curl http://127.0.0.1:58787/healthz`。完整接口和部署方式见仓库 `docs/REPORT_HUB_V1.md` 与 `docs/PUBLIC_SERVER_HANDOFF.md`。

模块页面的本机 API 中继采用按站点存储的动态 `command_policy`。策略由拥有该站点
的 Connect Hub 安装在启动时登记；未登记的操作默认返回 403。模块新增网页操作时
不需要修改或重建 Report Hub。
