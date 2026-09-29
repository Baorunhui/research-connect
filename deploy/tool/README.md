# 浏览器独立部署（tool.sinksilk.com）

把三个研究模块作为独立 Docker 服务运行，通过一个域名的不同路径在浏览器中使用，
不需要飞书机器人、Connect Hub 或 Report Hub。

| 路径 | 服务 | 容器 | 本机端口 |
|---|---|---|---|
| `/` | 入口页（gateway，负责路径路由和登录） | `rc-tool-gateway` | `127.0.0.1:58888` |
| `/papers/` | Daily Paper Reader：论文日报、论文阅读、领域综述 | `rc-daily-paper` | `127.0.0.1:58889` |
| `/citations/` | CitationClaw：查引用、学者主页引用画像 | `rc-citationclaw` | `127.0.0.1:58890` |
| `/xhs/` | XHS Agent：小红书内容包 | `rc-xhs` | `127.0.0.1:58891` |

端口只绑定 `127.0.0.1`；公网入口是宿主机反向代理 → `127.0.0.1:58888`。
整个站点使用 HTTP Basic Auth，因为各模块页面可以读取/修改模型 API Key 并发起付费任务。

## 启动

```bash
cd deploy/tool
./init.sh                  # 首次：生成 .env，填好 LLM_* 后再运行一次
docker compose up -d --build
docker compose ps
```

`init.sh` 只创建不存在的文件：

- `state/htpasswd`、`state/credentials.txt`：登录账号（密码为空时随机生成）；
- `state/daily-paper/config.yaml`：Daily Paper 配置，`local.chat` 预填 `LLM_*`。

`state/` 与 `.env` 均被 Git 忽略，不要提交。

## 环境变量（`deploy/tool/.env`）

| 变量 | 说明 |
|---|---|
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 三个模块共用的 OpenAI 兼容 LLM（必填） |
| `XHS_MODEL` | 小红书流水线使用的模型，默认同 `LLM_MODEL` |
| `RESEARCH_CONNECT_UID` / `RESEARCH_CONNECT_GID` | 容器用户，应与 `state/` 的属主一致 |
| `GATEWAY_PORT` 等 `*_PORT` | 本机端口 |
| `TOOL_BASIC_AUTH_USER` / `TOOL_BASIC_AUTH_PASSWORD` | 登录账号 |
| `OUTBOUND_PROXY` | 容器访问 Semantic Scholar / OpenAlex / arXiv 的 HTTP 代理（可选） |
| `BUILD_HTTP_PROXY` | 构建镜像时的代理（可选，构建使用宿主机网络） |
| `LEGACY_CITATIONCLAW_DATA` | 旧版独立 CitationClaw 的数据目录（可选，只读挂载，缓存一次性导入） |
| `KAGGLE_ARXIV_INDEX_DIR` | Kaggle arXiv 元数据索引目录（默认 `./state/kaggle-arxiv`，只读挂载到 `/kaggle-arxiv`） |

Semantic Scholar、OpenAlex、ScraperAPI、MinerU 等引用数据源 Key 在 `/citations/`
页面的配置区填写；Daily Paper 的订阅和模型在 `/papers/` 的设置面板中修改。

## 查引用缓存

CitationClaw 的学者主页流水线（输入 Google Scholar 主页 URL 或上传保存的主页 HTML）
完成后，会按学者记录结果：键为 `gs:<Google Scholar user id>`，上传 HTML 时另记
`name:<学者姓名>`。缓存与其他 CitationClaw 缓存一样保存在数据卷
`/data/modules/citationclaw/cache/`（单条 JSON 文件 + SQLite 索引，命名空间
`scholar-profile`），结果文件仍在 `/data/modules/citationclaw/result-*/`。

再次查询同一学者时，只要缓存指向的结果文件仍在，就直接返回并在页面展示，不访问
Google Scholar、Semantic Scholar 或 LLM；API 请求中传 `force_refresh=true` 才会重新查询。
已缓存的学者列表：`GET /citations/api/profile/cache`。

## 本地论文元数据（Kaggle arXiv 快照）

`/citations/` 与 `/papers/` 查单篇论文的标题、作者、年份、venue（journal-ref）、
摘要、DOI 时，先查本地 Kaggle `Cornell-University/arxiv` 快照索引（约 318 万篇），
命中即返回，不再为这篇论文请求 Semantic Scholar / OpenAlex / arXiv；未命中才走外部接口。
学者缓存（`scholar-profile`）照旧优先，本地索引只加速论文级 metadata。
引用关系（谁引用了谁）和被引数不在快照里，仍来自 Semantic Scholar。

索引由快照 JSON 生成（约 5 分钟，含 FTS），放在 `state/kaggle-arxiv/index.sqlite3`：

```bash
cd research-connect
python3 modules/daily-paper-reader/scripts/build_kaggle_arxiv_index.py --no-download --no-vacuum \
  --json-path /home/cs/paper_agent/data/arxiv/arxiv-metadata-oai-snapshot.json \
  --db-path deploy/tool/state/kaggle-arxiv/index.sqlite3
docker compose -f deploy/tool/compose.yaml restart daily-paper citationclaw
```

快照刷新后重跑同一命令即可（先写 `.tmp` 再原子替换）。验证：
`GET /citations/api/paper/meta?arxiv_id=1706.03762` 返回 `"source": "kaggle_arxiv"`。

## 宿主机 Nginx

```nginx
server {
    server_name tool.sinksilk.com;
    listen 8443 ssl;
    listen [::]:8443 ssl;
    client_max_body_size 100m;
    proxy_read_timeout 3600s;
    location / {
        proxy_pass http://127.0.0.1:58888;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
    }
}
```
