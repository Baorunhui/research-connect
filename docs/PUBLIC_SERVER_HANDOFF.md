# 公网 Report Hub：技术路线与服务器交付说明

## 一句话技术路线

每位用户的 Windows/Linux 电脑负责执行论文日报、查引用和小红书任务；共同的公网 Report Hub 只接收进度/运行快照和最终静态网页。Daily Paper 按安装实例发布一个固定整站地址，保留其原生 `index.html + app/ + docs/` 界面、历史日报和实时步骤；CitationClaw 仍按任务返回独立链接。任务开始时链接就发到飞书，手机和电脑均可查看，任务结束后内容继续保存在公网服务器。

```text
用户飞书 → 用户电脑上的 Connect Hub → 本机领域模块
                         │
                         ├─ HTTPS 上报进度/运行快照 → 公网 Report Hub(SQLite)
                         └─ HTTPS 上传原生静态站点/报告 → 公网磁盘

手机/电脑浏览器 ← Daily Paper 固定整站链接 / CitationClaw 单任务链接
```

这不是反向代理用户电脑，也不把本机 FastAPI 暴露到公网。因此用户不需要公网 IP、内网穿透账号或路由器配置，公网服务器也不接触飞书 Secret、LLM Key 和学校 API Key。Daily Paper 公网站点中的 `/api/local/runs` 是 Report Hub 保存的只读镜像，不会把“发起任务”接口暴露到公网。

## 需要服务器管理员提供

- 一台有公网入口的 Linux 服务器，Python 3.10+ 或 Docker；
- 一个域名（如 `reports.example.edu.cn`），A 记录指向服务器；
- 放行 TCP 80/443；测试阶段可临时放行 TCP 8787；
- 建议至少 1 CPU、1 GB 内存；磁盘容量取决于报告和图片留存，建议从 20 GB 起；
- 决定备份与清理周期。Python 部署建议每日备份 `apps/report-hub/data/`；Docker 部署备份 `report-hub-data` 命名卷。报告建议保留 90 天以上。

## 测试部署（Python）

```bash
cd research-connect/apps/report-hub
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/report-hub --init-config
.venv/bin/report-hub
curl http://127.0.0.1:8787/healthz
```

编辑 `.env`：测试时 `REPORT_HUB_PUBLIC_BASE_URL=http://211.86.155.100:8787`；有域名后改成 `https://reports.example.edu.cn`。`.env` 内生成的 `REPORT_HUB_AGENT_TOKEN` 只分发给可信的 Connect Hub 安装者，不提交 Git。

长期运行可把 `deploy/report-hub.service` 中用户和路径换成实际值后安装到 systemd。

## Docker 部署

```bash
cd research-connect/apps/report-hub
cp .env.example .env
# 修改公网地址，并把 token 替换为至少 32 字符的随机值
docker compose up -d --build
```

## 域名与 HTTPS

安装 Caddy，把 `deploy/Caddyfile` 的域名换成实际域名；Caddy 会自动申请证书并支持 WebSocket 反向代理。正式环境只把 Caddy 的 80/443 暴露公网，Report Hub 的 8787 可只监听内网或防火墙限制访问。

## 验收

1. `https://域名/healthz` 返回 `status: ok`；
2. 运行 `scripts/smoke_report_hub.py`，它会创建任务、上报三条进度并上传一份静态报告；
3. 从非校园 Wi-Fi 的手机打开脚本输出链接；
4. 重启本地客户端，确认旧报告仍能访问；
5. 重启 Report Hub，确认 SQLite 与报告目录仍在。

## 安全与运维边界

- 公开链接是“持有链接即可查看”，token 足够长但不是账号权限系统；日报按当前需求可公开，敏感材料以后再加登录层。
- 写接口必须带上传 token；应使用 HTTPS，避免 token 明文传输。
- ZIP 上传会限制大小，并拒绝路径穿越和符号链接。
- 服务首版单进程、SQLite，无 PostgreSQL/Redis/本地模型，部署和迁移成本较低。
- 备份的最小集合只有 `.env` 与 `data/`；其中 `.env` 应加密保存。
