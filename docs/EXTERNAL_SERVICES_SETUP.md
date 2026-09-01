# 外部论文服务申请与配置

本文说明 Research Connect 当前使用的四项论文服务默认如何配置，以及何时需要自行申请凭据：

- Semantic Scholar API；
- arXiv Supabase 论文池；
- 论文 Embedding；
- 论文 Reranker。

启动机器人后发送 `/config`，在返回的 HTTPS 统一配置中心中填写。保存后点击对应卡片的“测试可用性”；无需同时修改 Daily Paper 和 CitationClaw 自己的配置文件。

## 快速选择

| 服务 | Demo 推荐取得方式 | 是否必须 | 用途 |
| --- | --- | --- | --- |
| Semantic Scholar | 用户从官方网站自行申请 | CitationClaw 强烈推荐 | 作者论文、引用关系和论文元数据 |
| arXiv Supabase 论文池 | 已预置上游公开只读连接 | 日报混合召回需要 | 近期论文元数据、BM25 和向量召回 |
| 论文 Embedding | 已预置上游公开服务 | Supabase 向量召回和综述需要 | 将查询编码为 384 维向量 |
| 论文 Reranker | 已预置上游公开服务；需要独立额度时自行申请 SiliconFlow | 日报和综述默认需要 | 对融合后的候选论文重新排序 |

> 三项预置配置来自 [ziwenhahaha/daily-paper-reader](https://github.com/ziwenhahaha/daily-paper-reader) 公开仓库。它们是共享 Demo 服务，不是每位用户的专属额度，也没有 SLA；遇到 401/429/5xx 时先在 `/config` 测试，必要时换成自己的兼容服务。Supabase 使用的是可公开分发的 publishable key，不是 `service_role`。

## 1. Semantic Scholar API

Semantic Scholar 的多数 Academic Graph 接口可以匿名调用，但匿名请求共享公共限额，高峰期可能频繁返回 `429 Too Many Requests`。CitationClaw 的学者主页分析会连续查询作者论文和施引文献，因此建议每位用户申请自己的 Key。

1. 打开 [Semantic Scholar API 官方页面](https://www.semanticscholar.org/product/api)。
2. 点击 **Request an API Key**，填写联系邮箱、项目名称和用途。
3. 用途可以说明：个人学术研究工具；按作者读取论文及引用关系；客户端限制为约 1 request/second；不转售数据。
4. 等待邮件发放 Key。官方说明新 Key 的初始限额通常为 1 RPS。
5. 在统一配置中心打开 **查引用 CitationClaw → Semantic Scholar**：
   - 启用 Provider；
   - 填写 `API Key`；
   - 保存并测试。

Key 通过 `x-api-key` 请求头发送。不要多人共享、提交 Git 或放进截图；旧 Key 持续返回 429 时应暂停任务，必要时联系 Semantic Scholar 或重新申请，而不是使用 ScraperAPI 绕过限流。

## 2. arXiv Supabase 论文池

### Demo 默认公开论文池

统一配置中心已预填：

```text
Project URL：https://lyucdwgefyfbmaiopjbk.supabase.co
Publishable Key：已内置（配置页只显示“已配置”，不回显）
论文表名：arxiv_papers
BM25 RPC：match_arxiv_papers_bm25
向量 RPC：match_arxiv_papers_exact
```

Supabase 官方将 Publishable/anon Key 定义为低权限客户端 Key，真正的数据权限仍由 Postgres RLS 控制；维护者必须为论文表和 RPC 配置只读策略。参见 [Supabase API Keys 文档](https://supabase.com/docs/guides/getting-started/api-keys)。用户电脑绝不能填写 `service_role`、Secret Key 或数据库密码。

该公共池主要服务近期论文，不应把它当成完整历史学术索引。若要替换，不能只新建一个空 Supabase 项目：必须准备相同表结构、384 维向量、BM25/向量 RPC 和只读 RLS，然后在 `/config` 覆盖上述字段。

### 自建方式：当前不作为普通用户安装步骤

空白 Supabase 项目没有任何论文。自建者还需要创建 `arxiv_papers` 表、启用 pgvector、导入和持续同步 arXiv 元数据、生成与查询端相同模型的向量，并安装 BM25/向量 RPC 和 RLS。仓库已有部分 RPC 与同步代码，但尚未提供一键初始化完整论文池，因此当前发行版不把它作为普通用户的配置路线。

## 3. 论文 Embedding

当前远程适配器不是通用 OpenAI `/v1/embeddings` 协议，而是项目约定的接口：

```http
POST /embed
Authorization: Bearer <key>
Content-Type: application/json

{"texts":["query 1","query 2"]}
```

响应需要包含：

```json
{"embeddings":[[0.1, 0.2], [0.3, 0.4]]}
```

当前推荐服务：

```text
API 端点：https://zwwen.online/embed
模型：BAAI/bge-small-en-v1.5
API Key：已内置上游公开 Key（配置页只显示“已配置”）
```

它必须和 Supabase 论文池中已有向量使用同一个模型及维度；随意换成另一个 embedding 模型，即使接口返回 200，向量召回结果也会失真。因此普通用户不应单独注册一个任意 embedding 服务替换它。

在统一配置中心打开 **论文日报/论文综述 → 论文 Embedding** 可直接测试。当前这个自定义 `/embed` 协议没有通用的自助申请平台；要使用私有服务，需要部署一个兼容接口，并确保模型和公共论文池的向量维度完全一致。

## 4. 论文 Reranker

### 默认公共服务

Demo 默认使用：

```text
API 端点：https://zwwen.online/rerank
模型：Qwen/Qwen3-Reranker-0.6B
API Key：已内置上游公开 Key（配置页只显示“已配置”）
```

公共服务由上游维护者提供，共享额度且没有 SLA。如果统一配置中心测试返回 401，说明公开凭据已被轮换；如果返回 429，应稍后重试或改用自己的 SiliconFlow 凭据。

### 自行申请 SiliconFlow

1. 注册并登录 [SiliconFlow](https://cloud.siliconflow.cn/)。
2. 在 API Keys 页面创建 Key；官方步骤见 [SiliconFlow Quick Start](https://docs.siliconflow.com/en/userguide/quickstart)。
3. 在统一配置中心的 **论文 Reranker** 填写：

```text
API 端点：https://api.siliconflow.cn/v1/rerank
模型：Qwen/Qwen3-Reranker-0.6B
API Key：账户创建的 Key
```

SiliconFlow 的 rerank 接口采用 Bearer Key，并返回每个候选的 `relevance_score`；接口格式参见 [Create Rerank](https://siliconflow-4a6a0801.mintlify.app/en/api-reference/rerank/create-rerank)。调用会消耗账户余额，具体价格和赠送额度以注册时模型页面为准。

`https://api.sinksilk.com:58443/v1/rerank` 是项目维护者提供的另一条兼容中转路线，不是 Report Hub 安装 token 自动附带的服务；使用它时需要维护者另行提供 Reranker Key，模型名填写 `qwen3-reranker`。

## 5. 默认值、覆盖与安全边界

- 新安装会自动得到三项公开配置；已有安装中对应端点为空或仍指向上游公开服务、但 Key 为空的，也会自动补齐。
- `/config` 出于安全设计不会把任何 Key 原文返回浏览器，只显示“已配置”；留空保存表示保留当前值。
- 自己申请的 Semantic Scholar、SiliconFlow 或 LLM Key 仍是私有凭据，不要提交 Git、截图或转发。
- 公共 Key 已由上游主动发布，因此可以随客户端分发；这不代表可以滥用，也不保证永久有效。维护者轮换后需要随版本更新默认值。
