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
| `CITATIONCLAW_LIGHT_BASE_URL` / `_API_KEY` / `_MODEL` | CitationClaw 轻量模型档（二次筛选/自引/格式化、画像报告、引文语境、PDF 作者抽取），覆盖页面里保存的轻量档；联网搜索和作者校验仍用 `LLM_*`。宿主机本地服务写 `http://host.docker.internal:<端口>/v1` |
| `HONOR_LIST_DIR` | 荣誉名单目录（默认 `./state/honor-list`，只读挂载到 `/honor-list`） |

Semantic Scholar、OpenAlex、MinerU 等引用数据源 Key 在 `/citations/` 页面的配置区填写；
ScraperAPI 可选，本机部署默认不需要。Daily Paper 的订阅和模型在 `/papers/` 的设置面板中修改。

## 查他引快查（默认）

学者主页入口默认是「快查」：施引关系到 OpenAlex 拉全，题录用本地 Kaggle arXiv 快照核对，
不下载 PDF / 落地页 / 全文，不走 Google Scholar 施引、Semantic Scholar 或 ScraperAPI。

1. 学者缓存（`scholar-profile`）命中 → 直接展示已有结果。
2. 论文列表：上传的主页 HTML 完全本地解析；输入 URL 时只做一次身份解析（本机 Google 常 403，
   建议上传 HTML）。取引用量最高的 N 篇（`top_n`，0 = 全部）。
3. OpenAlex 施引：每篇目标先按 DOI / arXiv DOI / 精确标题找 work（预印本与正式版同名的一并纳入），
   再 `filter=cites:W…` + `cursor` 翻页，每页 200，`select=id,display_name,publication_year,doi,ids,authorships`。
   被引超过 2000 的按 `publication_year` 分区并行翻页。带 `mailto`（`CITATIONCLAW_OPENALEX_MAILTO`），
   令牌桶限速（`CITATIONCLAW_OPENALEX_RPS`，默认 8 次/秒），429/5xx 按 `Retry-After` 或指数退避重试。
   线路 `CITATIONCLAW_OPENALEX_ROUTE=auto|direct|proxy`，auto 先直连、不通再走代理。
   原始施引缓存在数据卷 `/data/cache/openalex_citing/`（按论文、按年份分区的 gzip JSON），同一篇论文不重拉。
4. 题录核对：目标与施引论文的 arXiv id / DOI / 标题查 Kaggle 本地索引，补 arXiv id、标题、年份；
   OpenAlex 没给作者时用 Kaggle 作者补。Kaggle 没有引用边，不能替代第 3 步。
5. 自引跳过；荣誉匹配：施引作者（OpenAlex 机构名 / 原始单位串里的邮箱）对
   `state/honor-list/honors.sqlite3`，姓名一致且（邮箱完整地址 / 邮箱域名 / 规范化单位）至少一项一致才算命中；
   只有同名的计为候选、不计入。

报告（`*_fast_report.{html,json,xlsx}`）标明「OpenAlex 施引 + Kaggle 题录核对，不是谷歌学术全量爬取」，
列出荣誉、匹配依据、每篇目标的 OpenAlex 被引数与已拉取条数。需要 LLM/PDF 全流程时勾选「全量施引」或 API 传 `mode=full`。

荣誉名单不进 Git、不进镜像，在宿主机生成（需要 `pypinyin` 生成中文名拼音键，抓取来源走代理）：

```bash
cd research-connect
PYTHONPATH=packages/research-connect-core/src:modules/citationclaw \
python3 -m citationclaw.core.honor_list build --db deploy/tool/state/honor-list/honors.sqlite3 \
  --wikidata-csv /home/cs/CitationClaw/他引/data/scholars.csv --sources aaai changjiang ieee_cs
docker compose -f deploy/tool/compose.yaml restart citationclaw
```

`--sources` 另可加 `jieqing`（LetPub，约 1 小时）。当前统计：`GET /citations/api/honor-list/stats`。

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
