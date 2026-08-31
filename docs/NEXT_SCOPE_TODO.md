# 下一阶段待办：尚不符合既定使用原则的部分

本文件只记录下一轮范围。本次统一环境、公共 LLM 运行时、数据根目录和缓存索引工作不顺带修改下列业务链路，避免在模块负责人代码尚未确认时扩大改动。

## P0：模块接入与任务边界

- [x] Daily Paper 适配最新版 `/api/local/workflows/dispatch`，补齐 submit、事件、artifact、错误回调和进程树取消。
- [x] CitationClaw 保留 Connect Hub adapter 和原版网页发布能力；当前产品入口不向 LLM 注册查引用工具，飞书菜单仅返回固定网页，由用户在网页内操作。
- [x] Connect Hub 成为唯一对外任务生命周期所有者；模块状态仅作为内部执行句柄。
- [x] Linux/Windows 子进程树取消已实现，模块轮询持续检查 Hub 取消标志。
- [x] 把 Daily Paper 已有的“论文总结”异步接口（`/api/paper/summarize`）注册为飞书固定工具，接入统一任务、进度、取消、产物与固定日报站点刷新；支持论文 URL 和飞书 PDF 上传。
- [x] 把 Daily Paper 已有的“论文综述”异步接口（`/api/survey`）注册为飞书固定工具，定义主题、候选数、回溯范围、粗筛量与种子论文等稳定参数，并接入统一任务生命周期和联网主题富化。
- [x] 增加统一配置预检：飞书与 Python CLI 只检查公网配置记录是否存在；不存在时返回模块原版公网网页。配置保存后由本机 Connect Hub 在任务启动前同步，不探测 LLM/API 连通性。

## P0：符合轻量部署原则

- [x] Daily Paper 默认使用 Supabase/远程 embedding，关闭本地 embedding 自动降级与模块自带 scheduler。
- [x] 增加一次性 Docling 入口；PaperCropper/DocLayout-YOLO 不再进入运行路径，CitationClaw 本地 MinerU 默认关闭。
- [x] 移除硬编码 embedding token。
- [x] 统一 `serve` 只常驻轻量 HTTP 壳和飞书连接；模型/流水线按任务启动并退出。

## P0：LLM 精筛与外部 API

- [x] 按本轮要求不做学校 API 压测和 Provider 额度调研；保留共享运行时已有的 4 并发与 429/`Retry-After` 重试能力。
- [x] 模块调用次数、耗时、状态码及 CitationClaw 费用摘要写入 Connect Hub `usage_records`。
- [x] Daily Paper 本地聊天代理复用 shared LLM transport；CitationClaw 由 Connect Hub 环境覆盖统一 LLM 配置。
- [x] 外部 API 故障映射为统一错误码，写入 SQLite、Report Hub 事件并通过飞书提示。
- [x] 修复论文综述凭据透传：Connect Hub 将已配置的 `DAILY_PAPER_EMBED_API_KEY` 作为仅内存运行时凭据传给 `/api/survey`，Daily Paper 在创建公开 job 前剥离凭据，再显式传给 embedding client；凭据不进入 job input、事件、日志或产物。

## P0：统一 Provider 与凭据管理

### Demo 版已完成（2026-08-31）

- [x] Report Hub 提供统一 HTTPS 配置中心，按实际功能流水线展示 Provider 用途、启用状态和论文源选择。
- [x] 公网读取接口只返回配置状态与空白密钥；密钥留空保存表示保留旧值。
- [x] 每个 Provider 提供用户主动点击的最小可用性探测，不在任务预检时自动消耗额度。
- [x] Connect Hub 启动时导入现有配置，运行时从 Report Hub 拉取，并写入权限为 `0600` 的本机 `providers.json` 缓存。
- [x] 飞书增加 `/config`（兼容 `/settings`、`/配置`），同一飞书用户首次使用时只提醒一次配置入口。
- [x] 统一配置可下发给 LLM Gateway、Daily Paper 和 CitationClaw；Daily Paper 的局部配置接口已允许更新 `source_backends`/`supabase`。

本 demo 暂不实现 OS Keyring、SQLite `secret_ref` 元数据、严格的 RuntimeCredentials 白名单信封和多级 Provider 优先级；飞书 App Secret 与 Report Hub Agent Token 仍是安装启动凭据，保留在本机 `.env`。

### 当前问题

同一个凭据目前可能从系统环境变量、模块 `.env`、模块 YAML/JSON 配置、网页接口请求和任务 `secret` 五处进入。不同入口的优先级由各模块自己决定，已经造成“日报收到 embedding key、综述没有收到”的真实故障。CitationClaw 还会把 Connect Hub 的 LLM 环境变量同步进自己的配置对象，Daily Paper 网页则能把聊天 key 写入 `config.yaml`，职责边界不一致。

- [ ] 由 Connect Hub 新增唯一的 `ProviderRegistry` 和 `CredentialStore`，统一管理 LLM、embedding、reranker、学术数据 API、飞书和 Report Hub 凭据；业务模块只使用稳定的 provider ID，不再自行解析多套 key 名。
- [ ] 第一版采用跨平台私有文件保存秘密（建议 `~/.research-connect/config/secrets.json`，Linux 权限 `0600`，Windows 限当前用户），SQLite 只保存 provider 元数据、`secret_ref` 和“是否已配置”，不保存明文 key。后续可选接 OS Keyring，但不把它作为部署前置条件。
- [ ] `.env` 只保留为首次安装/旧配置导入入口：启动时导入统一存储并提示迁移结果，运行期不再参与多层覆盖；模块自己的 `.env` 和 `config.yaml` 不再保存 API key。
- [ ] 定义稳定的凭据引用，例如 `llm.primary`、`llm.fallback`、`embedding.paper`、`rerank.paper`、`supabase.arxiv`、`citation.semantic_scholar`、`citation.scraperapi`、`feishu.bot`、`report_hub.agent`。
- [ ] 定义 `RuntimeCredentials` 白名单信封：Hub 只把一次任务实际需要的最小凭据传给 adapter；HTTP 服务在创建公开 job 前剥离，子进程只在进程环境中接收，所有日志、事件、SQLite 和产物统一脱敏。
- [x] 增加统一配置页：按 Provider 展示用途、端点、模型、配置状态和“测试连接”按钮；读取接口只返回状态，永不回传完整 key。
- [ ] 明确唯一优先级：任务显式选择的 provider → 统一 ProviderRegistry 默认项；不再允许模块 `.env`、模块配置和请求字段暗中互相覆盖。
- [x] 配置页连接探测报告 `ready / missing_credential / disabled / unreachable`；命令行 `doctor providers` 暂留后续。联网探测只由用户点击触发。

### API/凭据使用盘点

| Provider / 凭据 | 当前使用步骤 | 使用模块 | 是否必需 | 统一后的建议 ID |
|---|---|---|---|---|
| OpenAI 兼容 LLM endpoint/key/model | 飞书意图理解与工具调用、联网结果富化；日报关键词扩充、LLM 精筛和文档生成；单篇总结；综述查询规划、逐篇抽取、聚类解释、写作审校；CitationClaw 作者分析与报告；小红书文案 | Hub、Daily Paper、CitationClaw、XHS | 必需 | `llm.primary`；可选 `llm.fallback`、`llm.light` |
| zwwen embedding endpoint/key | 日报 Step 2.2 查询向量；综述 Supabase BM25+向量召回的查询向量 | Daily Paper | 日报语义召回/综述必需 | `embedding.paper` |
| Reranker endpoint/key/profile | 日报 Step 3；综述候选精选 | Daily Paper | 默认需要，可配置无 rerank 降级 | `rerank.paper` |
| Supabase URL + anon/publishable key | 日报 BM25、向量召回、RRF 前候选池；综述近 180 天主召回；各论文源远程表/RPC | Daily Paper | 当前默认路线必需 | `supabase.<source_id>` |
| Supabase service key | 维护脚本同步、清理远程论文池 | Daily Paper 维护工具 | 普通用户不需要 | `supabase.<source_id>.admin`，默认不在 UI 展示 |
| arXiv 公共 API/摘要页/PDF | 单篇总结元数据与 PDF；综述种子及种子引文；本地召回模式增量抓取 | Daily Paper | 免 key | 论文源 `arxiv` 的 direct fetcher |
| Exa MCP | 飞书“自动联网”的检索与主题富化 | Connect Hub | 默认免 key，可关闭 | `web_search.exa` |
| Jina Reader | Hub URL 正文获取；日报深度总结获取结构化正文，失败回退 PyMuPDF | Hub、Daily Paper | 默认免 key，可关闭 | `url_fetch.jina` |
| DeepXiv token | 综述可选外部语义召回、被引数和新鲜论文补充 | Daily Paper | 可选，默认关闭 | `academic_search.deepxiv` |
| OpenReview 登录 | 会议论文源抓取/维护 | Daily Paper | 仅相应源需要 | `paper_source.openreview` |
| ScraperAPI keys | Google Scholar 被引列表、学者主页抓取 | CitationClaw | 当前完整查引用链的重要来源 | `citation.scraperapi` |
| Semantic Scholar key | 引用抓取兜底、论文/作者元数据、PDF 定位；无 key 时额度更低 | CitationClaw | 可选但推荐 | `citation.semantic_scholar` |
| OpenAlex email | 作者与论文元数据礼貌池访问 | CitationClaw | 可选 | `citation.openalex`（非秘密配置） |
| Web of Science key | 结构化作者信息提取 | CitationClaw | 可选 | `citation.wos` |
| MinerU token | CitationClaw 大文件解析备用；Daily Paper 旧备用路线 | CitationClaw / Daily Paper | 默认关闭；当前主路线用 Docling | `document.mineru` |
| Feishu App ID/Secret | 长连接机器人、消息和附件下载 | Connect Hub | 使用飞书时必需 | `feishu.bot` |
| Report Hub agent token | 原版网页整站上传、分片同步和固定公网地址 | Connect Hub | 需要公网报告时必需 | `report_hub.agent` |
| 中转站 access token/user ID | 查询额度/费用快照，不参与模型请求 | CitationClaw | 可选 | `billing.llm_gateway` |

Docling、PyMuPDF、SQLite 和本地 HTML 渲染不是外部 API，不进入 CredentialStore，只进入运行能力检查。

## P0：论文源目录与订阅管理

### 当前配置实况

- 真正配置并启用的远程论文后端只有 `arxiv`：Supabase 的 `arxiv_papers` 表、BM25 RPC 和向量 RPC。
- 代码声明支持但当前没有 backend 的预印本源：`biorxiv`、`medrxiv`、`chemrxiv`。
- 代码声明支持但当前没有 backend 的会议源：`neurips`、`iclr`、`icml`、`acl`、`emnlp`、`aaai`、`cvpr`、`eccv`、`ijcai`、`osdi`、`sosp`、`ieee_sp`、`ndss`。
- 现有订阅编辑器能编辑主题、关键词和 intent，但没有论文源选择控件；保存时只是静默保留旧 `paper_sources`，缺失时自动填 `arxiv`，因此用户无法从界面判断某个源是“代码支持”“后端已配置”还是“当前订阅已选中”。
- DeepXiv 是综述召回 Provider，Exa 是联网富化 Provider，Jina 是正文获取 Provider；三者都不应伪装成订阅论文库。
- Kaggle 全量快照不部署并默认关闭，只保留为高级可选插件，不进入默认安装、默认配置和常规故障提示。

### 目标设计

- [ ] 新增统一 `PaperSourceCatalog`，每个源至少包含：`source_id`、名称、类别（预印本/会议/期刊/外部检索）、`enabled`、支持的能力、backend/provider 引用、credential 引用和新鲜度说明。
- [ ] 明确三个彼此独立的状态：`supported`（代码有适配器）、`configured`（端点/RPC/凭据完整）、`enabled`（用户允许任务使用）。任务只能选择 configured + enabled 的源。
- [ ] 给论文源声明能力：`daily_recall`、`survey_recall`、`metadata_fetch`、`pdf_fetch`、`citation_graph`。例如 Supabase arXiv 支持日报/综述召回，arXiv direct fetcher 支持元数据/PDF/种子引文，不能用一个模糊的 `source=arxiv` 隐藏两条完全不同的链路。
- [ ] 配置页增加“论文源”区域：总开关、状态、数据范围/最近同步时间、用途勾选、连接测试；缺少 backend 的源显示“未配置”，不能显示成可选成功状态。
- [ ] 订阅词条增加多选源控件，只列出已配置且启用、具备 `daily_recall` 能力的源；默认选择安装级默认源（当前为 `arxiv`），不再静默回填不可见值。
- [ ] 综述单独选择 recall providers：默认 `Supabase arXiv`，可选 DeepXiv 和种子引文；Kaggle 默认不显示，只有安装相应插件并检测到索引后才出现。
- [ ] 单篇论文总结不绑定订阅源：接受 URL/PDF 后由 resolver 判断 arXiv、bioRxiv、会议网页或普通网页，并在结果中记录实际 metadata/PDF resolver。
- [ ] 任务启动事件必须列出本次实际启用与跳过的源及理由，并按源报告输入/命中数量；不能再把“认证失败、未配置、0 命中”合并成同一个“所有召回为空”。
- [ ] 将 `source_config.py` 中大量逐源环境变量分支改为数据驱动 catalog + backend schema；保留一次性迁移器，把旧 `DPR_*` 和 `source_backends` 导入新配置。

建议的非秘密配置形态：

```yaml
paper_sources:
  arxiv_supabase:
    label: arXiv（远程论文池）
    enabled: true
    capabilities: [daily_recall, survey_recall]
    provider: supabase.arxiv
  arxiv_direct:
    label: arXiv（元数据/PDF）
    enabled: true
    capabilities: [metadata_fetch, pdf_fetch, citation_graph]
    provider: arxiv.public

subscriptions:
  - id: 3dvg
    enabled: true
    source_ids: [arxiv_supabase]
```

其中 `provider` 只引用 ProviderRegistry；任何 key 都不出现在这份论文源配置里。

## P1：网页与跨设备体验

- [ ] Daily Paper 和 CitationClaw 前端移除固定轮询，统一消费 Connect/Report Hub WebSocket；创建任务时立即返回手机可打开的固定 URL。
- [ ] 排查 Daily Paper 静态页面依赖本机 API/CDN 的部分，发布报告必须在任务结束后仍可独立查看。
- [ ] 优化 CitationClaw Dashboard 手机布局。
- [ ] Report Hub 不可达时继续飞书进度，完成后以附件或本地链接降级。

## P1：小红书质量链

- [ ] 把 Pipeline 当前输出解析重试与 shared LLM 的传输重试明确分层，并把降级原因写入统一事件/usage。
- [ ] 增加可选视觉审查；不开启时不引入视觉 API 或本地视觉模型。
- [ ] 补齐每页疏密和布局自适应回归样例。

## P1：发行与清理

- [ ] 为 Windows/Linux 增加 `doctor` 与统一 `serve`，检查 Python、字体、浏览器、端口和外部 Provider。
- [ ] 增加 Windows 与 Linux CI；之后再制作 Docker CPU 镜像。
- [ ] 清理三个上游模块中不再使用的实验脚本、旧缓存实现和冗余测试夹具；删除前逐项核对模块负责人仍在使用的入口。

### 已知的 Daily Paper 上游测试债务

统一环境验证时，Daily Paper 核心测试通过 540 项；完整上游测试另有 8 个失败、6 个初始化错误，暂不在本轮修改：

- 6 个会议数据初始化测试在模块导入阶段强制 `import torch`，而默认轻量环境有意不安装 torch；应改成按实际执行路径延迟导入或归入可选维护测试。
- 3 个 workflow/UI 合约仍断言旧 reranker 和 OpenCV/PaperCropper 依赖，与当前上游 workflow 及“远程 embedding + Docling”路线不一致。
- 3 个测试依赖 Daily Paper 目录作为当前工作目录，应改成相对测试文件定位 fixture/config/workflow。
- 1 个同步 embedding 测试假设 torch 永远存在，应在可选依赖缺失时跳过。
- 1 个生成文档测试依赖仓库中不存在的历史报告 fixture。
