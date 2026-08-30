# Report Hub

Report Hub 是 Research Connect 的轻量公网配置、任务进度和静态报告托管服务。它不运行论文任务或本地模型，也不持有飞书 Secret；用户选择在公网原版设置页保存的模块 API Key 会进入其 SQLite 数据卷，并只通过 HTTPS 和受保护的 Agent API 同步给本机 Connect Hub。

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/report-hub --init-config
.venv/bin/report-hub
```

健康检查：`curl http://127.0.0.1:8787/healthz`。完整接口和部署方式见仓库 `docs/REPORT_HUB_V1.md` 与 `docs/PUBLIC_SERVER_HANDOFF.md`。
