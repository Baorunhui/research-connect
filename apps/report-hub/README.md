# Report Hub

Report Hub 是 Research Connect 的轻量公网任务页和静态报告托管服务。它不运行论文任务，不持有飞书或 LLM 密钥。

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/report-hub --init-config
.venv/bin/report-hub
```

健康检查：`curl http://127.0.0.1:8787/healthz`。完整接口和部署方式见仓库 `docs/REPORT_HUB_V1.md` 与 `docs/PUBLIC_SERVER_HANDOFF.md`。

