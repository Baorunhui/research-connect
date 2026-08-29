# Research Bot 完整部署与开发规划

> 状态：2026-08-19 方案基线。本文面向 Connect Hub、论文日报、小红书三个模块的开发者，用于统一目标、接口、优先级和验收标准。模块内部可以独立开发，但不得绕过本文定义的集成边界。

## 1. 最终部署形态

```text
飞书用户（单用户）
        │
        │ 飞书 WebSocket 长连接
        ▼
┌─────────────────────────────────────────────────────────┐
│ Connect Hub：唯一常驻的轻量主服务                       │
│                                                         │
│  对话/指令理解 ─ LLM Gateway ─ 联网搜索 Provider        │
│          │              │                │              │
│          └──── Orchestrator / Tool Registry ────────────┤
│                         │                               │
│  SQLite：会话、任务、进度、取消状态、成本、缓存索引     │
│  本地文件：PDF、Figure、小红书图片、Markdown 日报       │
└─────────────────────────┬───────────────────────────────┘
                          │ 统一模块接口 v1
            ┌─────────────┼────────────────┐
            ▼             ▼                ▼
      论文日报任务     小红书任务       后续查引用任务
      按任务启动进程   按任务启动进程   按同一接口接入
      完成后退出       完成后退出
            │
            ├─ Supabase：远程论文池/向量数据
            ├─ 远程 Embedding/Rerank API
            ├─ LLM API：必须保留的论文精筛
            ├─ Jina API：角色经测试后确定
            └─ Docling：本地 PDF 解析与 Figure 提取

日报 Markdown/静态资源 ──发布──> GitHub Pages + Docsify
                                      │
                                      └──公网 URL 回发飞书
```

### 1.1 常驻与非常驻边界

- **常驻**：Connect Hub、飞书 WebSocket、轻量定时器、SQLite。
- **按任务启动并在完成后退出**：论文日报流水线、Docling、小红书生成进程。
- **不使用**：PaperCropper、DocLayout-YOLO、常驻 PDF Worker、Worker 保温池。
- Python 虚拟环境只是磁盘上的依赖目录，不等于常驻进程，也不会在空闲时占用计算资源。

### 1.2 基础设施边界

- 第一阶段使用 Python 模块化单体，不引入 Redis、Celery、Kafka、Kubernetes、本地 PostgreSQL 或自托管 Supabase。
- 本地控制面统一使用 SQLite；大文件统一放本地文件目录，SQLite 只保存任务、路径、哈希和状态。
- Supabase 是远程托管 PostgreSQL，只作为远程论文数据源使用，不要求用户本地安装数据库。
- 各模块允许使用独立 `venv` 避免依赖冲突，但由同一套安装/检查/启动脚本管理。
- Linux 与 Windows 都假设用户已安装 Python；不要求制作 exe/MSI 安装包。

## 2. 统一需求与使用场景

### 2.1 部署要求

1. 默认部署不能要求 GPU。
2. Embedding、Supabase、Jina 均使用远程服务。
3. 本地仅保留 Docling 所需依赖，不安装 PaperCropper/DocLayout-YOLO。
4. Linux 与 Windows 使用相同 Python 代码和配置结构。
5. 安装过程应收口为少量命令，提供配置向导和连通性检查。
6. 不要求所有模块共用一个虚拟环境，但不能各自引入重复的数据库、任务队列、配置中心和日志系统。
7. 外部 API 失败时不实现复杂自动降级；必须返回明确、可操作的错误，并通过统一事件/回调通知飞书。

推荐用户入口：

```text
python -m connect_hub setup    # 创建配置并检查必填项
python -m connect_hub doctor   # 检查飞书、LLM、Supabase、Jina/Embedding、搜索 API
python -m connect_hub serve    # 启动唯一常驻服务
```

### 2.2 论文日报使用场景

#### A. 定时日报

- 用户设置每天运行时间，例如 `23:30`。
- 到点后启动一次论文日报进程；完成或失败后退出。
- 不检测键鼠、CPU 或所谓系统“空闲状态”，不引入复杂空闲判定。
- 一天通常一次，允许用更长时间完成精筛和 PDF 处理。
- 机器关机时任务不会执行；第一版不要求补跑或恢复。

#### B. 按需调研

- 用户随时在飞书中提出论文名、研究方向、关键词或时间范围。
- 机器人先识别并补齐关键参数，再启动日报/调研任务。
- 用户希望尽快看到结果，因此必须持续汇报召回、重排、LLM 精筛、PDF、网页发布等进度。
- 可能短时连续运行多次，但不保留常驻 Worker；优化重点放在 API 并发、缓存和减少重复工作。

### 2.3 小红书使用场景

- 短时间连续生成多份内容。
- 本地运行环境轻，按任务启动即可。
- 用户可以选择是否启用视觉模型进行版面审查；默认不启用，避免额外费用和延迟。

### 2.4 飞书机器人使用场景

- 随时可能收到消息，因此 Connect Hub 必须后台常驻并自动重连。
- 当前只服务单用户，不实现多租户、用户配额和复杂权限模型。
- 用户输入可能极简，例如“调研这篇论文”或“做一个 AIGC 方向日报”；机器人需要通过一轮简短对话确认和富化需求。
- 联网搜索为可选工具，只在需要实时或外部信息时调用，不默认搜索每一条消息。

## 3. 已确认的产品与技术决策

### 3.1 必须保留 LLM 精筛

论文日报的 LLM 精筛不能跳过。现有真实运行曾对大量候选产生约 86 次聊天调用、约 27 万 token、约 42 分钟耗时并触发 429，因此它是论文日报的首要性能优化项。

目标不是删除精筛，而是：

- 在召回和专用 reranker 后只把高价值候选送入 LLM；
- 按 token 数而不是固定论文篇数分批；
- 控制并发并根据 429 动态降速；
- 每批保存结果，失败后只重试失败批次；
- 严格限制重试层级，避免内外层重试相乘；
- 向飞书汇报 `已精筛 x/y 篇` 和预计剩余时间。

### 3.2 PDF 处理只使用 Docling

- 删除 PaperCropper/DocLayout-YOLO 安装入口、配置开关和降级分支。
- Docling 按任务/批次启动；一次任务内可以复用同一个 converter 处理多篇论文，任务完成后退出。
- Docling 失败时记录单篇失败并继续处理其他论文；是否允许无 Figure 继续生成摘要由日报任务参数决定。

### 3.3 简单定时调度

- 配置项只需要 `enabled`、`local_time`、`timezone` 和日报模板/订阅 ID。
- 第一版不做操作系统空闲检测、任务优先级和重启恢复。
- 启动时若发现旧任务仍为 `running`，统一标记为 `interrupted` 并通知用户，不自动续跑。

### 3.4 公网日报

- 日报静态网页直接公开到公网，不实现访问鉴权和隐私分级。
- 默认方案为 Docsify + GitHub Pages。
- 发布目录中必须排除 `.env`、日志、原始 API 响应和密钥；“网页公开”不等于“可以公开运行配置”。

## 4. 跨模块统一原则

### 4.1 统一基础设施与数据边界

各模块不得为同类数据分别引入独立的数据库、ORM、任务队列、配置体系或日志体系。跨模块共享的会话、任务、状态、进度、取消状态、成本和产物索引统一由 Connect Hub 数据层管理；模块私有领域数据通过稳定接口访问。第一阶段本地控制面统一使用 SQLite 和文件目录，Supabase 仅作为远程论文数据源。最终整合通过适配器收口现有模块，不要求各模块立即重写内部实现。

### 4.2 开发原则

- **轻部署优先**：新增依赖前先判断能否通过已有 Python 标准库或远程 API 完成。
- **接口先行**：模块只依赖统一请求/响应/事件契约，不读取其他模块内部表或目录。
- **可取消**：所有长任务必须保存底层 PID/进程句柄，取消时终止整个子进程树。
- **有界重试**：默认最多 2 次重试；429 优先尊重 `Retry-After`，不能无间隔重放。
- **缓存优先**：PDF、Docling Figure、论文摘要命中缓存时不重复下载和计算。
- **错误透明**：不要让 LLM 猜测错误原因；模块返回结构化错误，飞书使用确定性模板回复。
- **跨平台**：使用 `pathlib`、`tempfile`、参数数组形式的 `subprocess`，禁止 `shell=True` 和硬编码 `/tmp`。
- **凭据隔离**：真实 Key 只进入环境变量或私有配置文件，示例配置只保留占位符。
- **实测而非假设**：外部 API 的配额、429、延迟和中国大陆网络可达性必须用真实任务测量。

## 5. 通用模块接口 v1

### 5.1 接口目标

论文日报、小红书和未来查引用可以继续使用 HTTP、子进程或 MCP 实现，但 Connect Hub 内部只看到同一种 `ModuleAdapter`：

```python
class ModuleAdapter(Protocol):
    manifest: ModuleManifest

    def submit(self, request: JobRequest, events: EventSink) -> JobAccepted: ...
    def cancel(self, job_id: str) -> CancelResult: ...
    def health(self) -> HealthResult: ...
```

### 5.2 版本规则

- 每个请求和响应都携带 `schema_version`，初版固定为 `connect.job.v1`。
- 每个模块暴露 `module_name`、`module_version`、`supported_job_types` 和 `schema_versions`。
- Connect Hub 启动时执行兼容性握手；不支持 `connect.job.v1` 时拒绝启动该模块并给出明确配置错误。
- v1 内只允许增加可选字段；删除字段、修改字段语义或新增必填字段必须发布 v2。
- 暂不记录每份产物使用的模型、Prompt 和 PDF 版本；模块接口版本仍必须记录，以防模块升级破坏集成。

建议 Manifest：

```json
{
  "module_name": "daily-paper",
  "module_version": "0.1.0",
  "schema_versions": ["connect.job.v1"],
  "supported_job_types": ["daily_report", "paper_research"],
  "capabilities": ["progress", "cancel", "artifacts", "cost"]
}
```

### 5.3 任务请求

```json
{
  "schema_version": "connect.job.v1",
  "job_id": "job-uuid",
  "job_type": "daily_report",
  "requested_at": "ISO-8601",
  "input": {},
  "options": {
    "publish_web": true,
    "force_refresh": false
  },
  "callback": {
    "url": "http://127.0.0.1:PORT/internal/events",
    "secret_ref": "local-callback-secret"
  }
}
```

### 5.4 统一事件/回调

事件类型至少包括：

- `job.accepted`
- `job.started`
- `job.progress`
- `job.artifact`
- `job.cost`
- `job.completed`
- `job.failed`
- `job.cancelled`

`job.failed` 必须提供：

```json
{
  "code": "PROVIDER_RATE_LIMITED",
  "stage": "llm_refine",
  "provider": "provider-name",
  "retryable": true,
  "user_message": "LLM 服务触发频率限制，任务已停止；请稍后重试。",
  "technical_message": "sanitized diagnostic without secret"
}
```

Connect Hub 收到事件后负责持久化并回发飞书。HTTP 回调仅监听本机地址并使用共享密钥签名；同一事件通过 `event_id` 去重。

### 5.5 取消语义

- 用户在飞书发送“取消任务”后，Connect Hub 将任务标记为 `cancelling`。
- Adapter 必须终止整个子进程树，而不只是取消 Python Future。
- Linux 使用独立 process group；Windows 使用新的 process group，并在必要时使用系统进程树终止能力。
- 终止后模块清理临时文件，保留已完成的可复用缓存，并发送 `job.cancelled`。
- 取消超过设定时间仍未结束时标记 `cancel_failed`，向飞书提示需要人工检查 PID。

## 6. 论文日报任务规划

### P0：部署前必须完成

#### P0-1 恢复并优化 LLM 精筛

**为什么做**：精筛是最终推荐质量的必要环节，也是当前 42 分钟长耗时和 429 的主要来源。

**实现方向**：

1. BM25、Embedding、RRF、专用 reranker 之后，按阈值和 Top-N 控制送入 LLM 的候选量。
2. 根据估算 token 数动态分批，设置 `max_batch_tokens`，不再只按篇数切批。
3. 实现 Provider 级共享限流器：`max_concurrency`、RPM、TPM 三类上限。
4. 初始并发建议从 2 开始，通过真实压测确定，不直接追求最大并发。
5. 429 时读取 `Retry-After`；没有该字段时使用带抖动的指数退避。
6. 采用“加性升速、乘性降速”：连续成功再缓慢加并发，一次 429 即降低并发。
7. 每批落盘/写 SQLite 检查点；只重试失败批次，最多 2 次。
8. 对 JSON 输出做 schema 校验；缺论文 ID、截断或非法 JSON 时拆小批次，不重跑全部候选。
9. 进度事件包含完成数、总数、成功数、429 次数、预计剩余时间。

**验收**：

- 用同一历史日期和同一候选集做并发 1/2/3/4 对照实验。
- 记录总耗时、首个结果时间、总 token、API 次数、429 次数和推荐一致性。
- 选择“429 可控且耗时最短”的默认并发，而不是理论最大并发。
- 任一批失败不会丢失已完成批次。

#### P0-2 远程 Embedding、Supabase、Jina 角色和配额验证

**为什么做**：远程服务减轻本地部署，但模型不匹配、免费额度耗尽或中国大陆网络不稳定都会直接中断日报。

**必须先确认的架构问题**：

- Supabase 中已有论文向量由哪个 embedding 模型生成、维度是多少。
- 查询向量必须与库内论文向量使用兼容模型和相同维度；不能把查询端直接切换为 Jina 而不重建论文向量。
- 明确 Jina 在主链中的用途：Embedding、Rerank、Reader/PDF 读取或联网搜索。一个服务可以承担多种角色，但要分别统计用量。
- Docling 是本地 PDF 主解析器；Jina Reader 不应在没有明确收益的情况下重复解析同一 PDF。

**截至 2026-08-19 的官方配额基线**：

| 服务 | 免费/起步配额 | 对本项目的判断 | 申请难度 |
|---|---|---|---|
| Supabase Free | API 请求不限量；数据库 500 MB；每月 5 GB 非缓存 + 5 GB 缓存出口；项目连续一周无活动可能暂停 | 单用户查询流量大概率够用；如果自己存全量论文和向量，500 MB 很可能不够 | 注册账号、创建项目、取 URL/Key 很容易；自建论文库和迁移向量不容易 |
| Jina Search Foundation API | 新用户 10M 一次性免费 tokens；新 API 文档列 Free 500 RPM、1M TPM、并发 5 | 单次/单日吞吐通常够；10M 是否能覆盖长期使用取决于实际输入 token，不能当永久月度额度 | 注册后在 Dashboard 创建 Key，无人工审批；超额付费和大陆网络需实测 |
| Jina 产品页旧/并行口径 | 页面仍显示免费 Key 约 100 RPM、100k TPM | 官方页面口径存在差异，必须以账号 Dashboard 和响应限额头为准 | 同上 |

Supabase 官方价格：[Pricing](https://supabase.com/pricing)；Jina 当前 API 文档：[Search Foundation API](https://api.jina.ai/docs)。

**用量估算方法**：

- Supabase：记录每次召回响应字节数；按 `单次 egress × 每日任务数 × 30` 估算月流量。
- Embedding：分别统计 query tokens 和 document tokens。若论文向量已预计算，每次日报通常只需生成少量查询向量；禁止每天重新向量化整个候选库。
- Rerank：记录每次传入的候选数、标题/摘要 token 和返回数。
- Jina：读取 `X-RateLimit-Remaining-*` 响应头并在 `doctor` 中展示；余额低于阈值时提前告警。

**上线前配额门槛**：

- 连续运行 7 天定时日报，并额外模拟一天 5 次按需调研。
- 所有 API 记录调用数、token、响应字节、P50/P95 延迟、429 和 5xx。
- 月度估算低于免费额度 70% 才可把免费层视为“足够”；超过 70% 就明确付费预算或减少请求。

#### P0-3 Docling 单一路径与缓存

**为什么做**：删除多套 Figure 模型可以显著减轻安装和维护；缓存可以避免短时多次调研重复下载和解析。

**实现方向**：

- PDF 缓存键：优先 `arxiv_id + version`，没有稳定 ID 时使用文件 SHA-256。
- Docling 结果缓存：PDF 缓存键 + `docling` 处理类型；不记录模型/Prompt 版本。
- 摘要缓存：论文稳定 ID + 摘要类型；提供 `force_refresh=true` 手动刷新。
- 保存 `cache_index`：路径、大小、创建时间、最后访问时间和状态。
- 同一任务内批量处理多篇时只初始化一次 Docling converter；任务结束立即退出进程。
- 设置缓存大小上限和手动清理命令，不做自动云存储。

**验收**：同一论文第二次调研不得重新下载 PDF、不得重新跑 Docling；强制刷新时可以绕过缓存。

#### P0-4 可取消任务与孤儿进程治理

**为什么做**：当前上层超时不一定能停止底层日报，可能继续消耗 LLM 配额。

**实现方向**：按通用取消语义保存 PID/进程组；取消、超时和主服务退出时终止子进程树。暂不实现重启续跑。

#### P0-5 统一进度、错误回调和成本小计

**为什么做**：长任务需要让飞书用户知道卡在哪一步；外部 API 故障不能由 LLM自行猜测。

**至少汇报**：

- 参数确认完成；
- BM25 完成；
- Embedding 完成；
- RRF 完成；
- Rerank 完成；
- LLM 精筛 `x/y`；
- PDF/Docling `x/y`；
- 网页发布；
- 完成、失败或取消。

成本作为小功能记录：总耗时、各阶段耗时、LLM token、LLM/API 调用次数、429 次数。第一版只展示，不做预算拦截。

### P1：完成 P0 后增强

#### P1-1 固定时间日报

- SQLite 保存单条/少量 schedule。
- 每分钟检查是否到用户配置时间，当日只触发一次。
- 提供飞书指令：“设置日报每天 23:30 运行”“暂停定时日报”“立即运行”。
- 第一版机器关机错过后不补跑，服务重启后不恢复未完成任务。

#### P1-2 Docsify + GitHub Pages 发布

- 将 Connect Hub 生成的 Markdown 和静态资源写入独立发布目录。
- 使用 GitHub Actions 或受控 `git push` 发布 Pages。
- 发布成功后产生 `job.artifact`，包含公网 URL，飞书发送摘要、Markdown 文件和网页链接。
- 不依赖旧日报 Step 5.5/6 才能生成网页；网页发布应接受统一日报产物。

#### P1-3 会议信息查询优化

- Step 5.5 不允许全表分页扫描。
- 改为 DOI、标准标题或论文 ID 精确/索引查询，并缓存查询结果。
- 查不到会议不是整份日报失败条件，标记“未确认”即可。

### P2：后续评估

- 对不同 LLM 模型做精筛质量/成本对照。
- 对 Jina Reader 与本地 Docling 的 PDF 文本质量做抽样对照；没有显著收益则不接主链。
- 更智能的精筛候选阈值，但不能以牺牲关键论文召回为代价。

## 7. 飞书与 Connect Hub 任务规划

### P0：部署前必须完成

#### P0-1 持久化任务、事件与真正取消

SQLite 增加：

- `jobs`：任务类型、状态、模块、PID、开始/结束时间、错误码；
- `job_events`：进度、回调、去重 ID；
- `artifacts`：文件/URL、类型、大小；
- `cache_index`：论文缓存索引；
- `usage_records`：provider、调用次数、token、耗时、状态码。

不实现重启恢复。启动时把遗留 `queued/running/cancelling` 标记为 `interrupted`，清理仍存在的已知子进程，并向用户提供查询结果。

#### P0-2 配置傻瓜化和单命令运行

`setup` 应完成：

1. 选择中国版飞书；
2. 填入 App ID/App Secret；
3. 填入 LLM Base URL、Key、模型；
4. 填入 Supabase URL/Key；
5. 选择远程 Embedding/Rerank/Jina Provider；
6. 选择联网搜索 Provider；
7. 设置日报时间和 GitHub Pages 信息；
8. 生成私有配置并执行 `doctor`。

`doctor` 不只检查“字段非空”，还要发送最小真实请求，并显示配额头、延迟和错误建议。日志必须遮盖 Key。

Linux 提供 systemd 单元；Windows 提供 PowerShell 安装脚本和任务计划模板，保证登录/开机后启动机器人。Python venv 在目标机器创建，不能跨系统复制。

#### P0-3 指令理解与一轮需求富化

采用受约束状态机，不引入可无限自主循环的 Agent：

```text
识别意图 → 提取槽位 → 判断关键缺失项 → 最多追问 1～2 轮
        → 展示规范化任务 → 用户确认/直接执行 → 调用固定工具
```

论文任务的关键槽位：主题/论文名、关键词、时间范围、模式、是否联网、是否发布网页。小红书任务的关键槽位：主题、受众、目的、页数/风格、材料来源、是否视觉审查。

对高置信、参数完整的命令直接执行；不要为了形式强制确认。

#### P0-4 联网搜索 Provider

参考 Cherry Studio 的两条技术路径：

1. 模型服务商原生 Web Search；
2. 外部搜索 API 返回结构化结果，再作为上下文交给 LLM。

Cherry Studio 当前支持 Tavily、博查、Exa、智谱、SearXNG 等 Provider，工作机制是“搜索 → 返回网页摘要 → 注入 LLM 上下文”，并不需要本地搜索模型。参考：[Cherry Studio 联网模式](https://github.com/CherryHQ/cherry-studio-docs/blob/main/pre-basic/websearch/README.md)。

本项目第一版实现统一接口：

```python
class WebSearchProvider(Protocol):
    def search(self, query: str, *, max_results: int,
               include_domains: list[str] | None = None,
               freshness_days: int | None = None) -> SearchResponse: ...
```

统一结果字段：`title`、`url`、`snippet`、`published_at`、`score`、`provider`。LLM 只接收选中的结果，并在回复中保留来源 URL。

**当前费用/配额调研**：

| Provider | 免费额度与费用 | 注册/使用难度 | 建议 |
|---|---|---|---|
| Tavily | 每月 1,000 credits，无需信用卡；basic/fast 搜索 1 credit，advanced 2 credits；超额按量约 $0.008/credit | 邮箱/第三方账号注册并复制 Key，最简单 | 第一版默认候选；单用户每天 5～10 次 basic 搜索约 150～300 credits/月，免费层充足 |
| Exa | 注册赠送 $20，免费层每月 $10；基础 Search 约 $7/1000 请求，内容/高级 Agent 另计 | 创建 Key 容易，费用项比 Tavily 多 | 作为学术/技术搜索对照，不作为首个默认 |
| 博查 | 当前官网宣称个人/小团队免费，但未公开稳定的数字调用上限；国内访问和中文结果更友好 | 手机号/邮箱注册、创建 Key；需在账号后台确认额度 | 必须做中国大陆网络与真实额度测试，可作为国内优先 Provider |
| Jina Search | 免费 Key 页面显示约 100 RPM；搜索请求有固定 token 消耗，可能与其他 Jina 能力共享余额 | 已使用 Jina 时接入方便，但用量口径需核实 | 作为可选 Provider，不与 Embedding/Rerank 无上限共用 |

Tavily 官方价格：[Credits & Pricing](https://docs.tavily.com/documentation/api-credits)；Exa 官方价格：[API Pricing](https://exa.ai/pricing?tab=api)；博查当前价格页：[Pricing](https://open.bochaai.com/pricing)。

**上线前必须调研**：

- 从实际部署的 Linux 和 Windows 网络分别测试 DNS、TLS、P50/P95 延迟和成功率。
- 中文、英文、论文名、最新新闻四类查询各建 20 条小测试集，对比 Tavily/博查/Exa。
- 检查注册是否需要手机、信用卡、企业认证或人工审批。
- 检查免费额度是一次性还是每月重置，是否共享给同账号其他 API。
- 保存 provider 返回的计费字段和 429 响应，但不保存无关网页全文。
- 默认每次对话最多 3 次搜索，避免 LLM 自主循环烧配额。

#### P0-5 长连接与服务守护

- 保持飞书事件回调快速返回，将实际工作放入任务执行层。
- 自动重连并记录最近一次连接、断开原因和重连次数。
- `system_status` 返回飞书连接、各模块 health、外部 API 最近状态和当前任务。
- 正式部署必须由 systemd/Windows 任务计划守护，不能依赖临时终端。

### P1：完成 P0 后增强

- 飞书卡片展示阶段进度和“取消任务”按钮。
- 支持“查看当前任务”“查看最近任务”“重新发送日报链接”。
- 联网搜索黑名单和学术域名白名单。
- GitHub Pages 发布失败时仍发送飞书 Markdown，不让发布错误否定日报本身。

### P2：后续评估

- 模型原生 Web Search 与外部 Provider 的质量/费用自动选择。
- 更丰富的飞书交互卡片；第一版文本进度足够时不阻塞上线。

## 8. 小红书任务规划

### P0：部署前必须完成

- 按统一模块接口 v1 封装现有 CLI，支持进度、取消、产物和结构化错误。
- 保持独立轻量 venv，由 Connect Hub 安装脚本自动创建。
- 固化 Windows/Linux 字体、路径、浏览器渲染和临时目录行为。
- 输出正文、标签、卡片图片和完整内容包；附件发送失败与内容生成失败分开报告。
- 为短时连续调用增加输入/素材缓存和确定性请求 ID，避免误重复执行。

### P1：排版自适应

**为什么做**：比直接加入 VLM 更便宜、更稳定，能解决大部分“每页太挤/太空、文字溢出、版式不好看”问题。

**实现方向**：

- 根据字数、标题行数、图片占比动态分页；
- 设定最小字号、最大行数、最小留白和安全边距；
- 渲染后进行 DOM/像素级溢出检查；
- 对比度、图片裁切和中文断行使用确定性规则；
- 规则失败时自动重排一次，仍失败则返回可操作提示。

### P2：可选 VLM 视觉审查

- 用户显式选择“视觉审查”时才调用远程视觉模型。
- VLM 只做审稿人，输出结构化评分：拥挤度、层级、可读性、对比度、裁切、总体建议。
- 不让 VLM 直接自由生成页面；根据评分触发确定性模板参数调整。
- 每个内容包最多审查和重排一次，并记录额外调用次数与 token。
- 上线前用固定样例评估“人工偏好提升是否值得额外成本”。

## 9. 跨平台实现清单

### Linux

- `python3 -m venv` 创建各模块环境。
- systemd 守护 `connect_hub serve`。
- 子进程使用独立 process group 以支持整组取消。
- Docsify/GitHub Pages 发布使用非交互 Git 凭据。

### Windows

- `py -3.11 -m venv`，解释器位于 `.venv\Scripts\python.exe`。
- 使用 PowerShell 安装/检查脚本。
- 使用任务计划程序在登录或开机后启动 Connect Hub。
- 子进程取消必须覆盖子孙进程，不能只结束启动器进程。
- 不使用 `resource`、Unix signal、Bash、`/tmp` 或软链接作为必需能力。

### CI/验收

- Ubuntu 与 Windows 都运行离线单元测试。
- 两个平台都完成：安装、`doctor`、模拟飞书消息、模拟日报、离线小红书、取消子进程树。
- Docling 至少各完成一次真实 PDF 冒烟；若 Windows CI 太重，可在固定 Windows 测试机定期执行。

## 10. 推荐实施顺序与负责人拆分

### 阶段 A：统一接口和任务底座

**Connect Hub 负责人**：

1. 定稿 `connect.job.v1`、事件和错误码。
2. 实现 `jobs/job_events/artifacts/cache_index/usage_records`。
3. 实现跨平台子进程取消。
4. 实现事件到飞书的确定性通知。

**各模块负责人**：只需提供 Manifest、Adapter 和进度事件，不自行建立新任务库。

### 阶段 B：论文日报核心优化

**论文日报负责人**：

1. 恢复 LLM 精筛。
2. 完成动态分批、并发、429、自适应降速和批次检查点。
3. 删除 PaperCropper/DocLayout-YOLO，只保留 Docling。
4. 完成 PDF/Figure/摘要缓存。
5. 输出阶段进度、成本和结构化错误。

**Connect Hub 负责人**：实现 Adapter、取消、回调、飞书进度和错误映射，不修改日报领域算法。

### 阶段 C：部署和联网

**Connect Hub/部署负责人**：

1. `setup/doctor/serve`。
2. systemd 与 Windows 任务计划模板。
3. Tavily Provider + 至少一个国内 Provider 对照。
4. Supabase/Jina/搜索 API 的 7 天用量测试和月度估算。

### 阶段 D：网页和产品体验

- Docsify/GitHub Pages 发布。
- 一轮需求富化。
- 小红书确定性排版自适应。
- 可选 VLM 视觉审查。

## 11. 第一版完成定义

以下条件全部满足才算“可部署第一版”：

- Linux 可通过一套命令安装、检查并由 systemd 常驻运行。
- Windows 可通过 PowerShell 安装，并由任务计划自动启动。
- 飞书长连接断开后可自动恢复。
- 定时日报可按用户设置的本地时间运行。
- 按需调研保留 LLM 精筛，并在 429 下有界等待、降并发或明确失败。
- 飞书能看到各阶段进度、最终 Markdown、附件和公网网页 URL。
- 用户可以取消任务，取消后没有继续消耗 API 的孤儿进程。
- 同一论文再次运行能复用 PDF、Docling Figure 和摘要缓存。
- PaperCropper/DocLayout-YOLO 不再是安装或运行依赖。
- Supabase、Embedding、Jina、搜索 API 均完成真实配额/延迟测试，并有月度用量估算。
- 外部 API 失败返回结构化错误并通知飞书，不由 LLM 编造原因。
- 小红书能在 Windows/Linux 生成并发送完整内容包。
- 三个模块都通过 `connect.job.v1` 兼容性检查。

## 12. 当前明确不做

- 多用户和多租户权限/配额。
- 交互任务与闲时任务优先级。
- 服务重启后的任务续跑。
- 复杂外部 API 自动降级链。
- 操作系统空闲检测。
- 常驻或保温 PDF Worker。
- PaperCropper/DocLayout-YOLO。
- 本地 Embedding 模型。
- 本地 PostgreSQL、Redis、Celery、Kafka、Kubernetes。
- 日报网页鉴权和隐私分级。
- 每份产物的模型、Prompt、PDF 版本追踪。

这些项目只有在真实使用证明有必要时再重新进入规划。
