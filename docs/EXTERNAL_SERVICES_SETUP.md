# 外部论文服务申请与配置

本文说明 Research Connect 当前使用的四项论文服务如何取得凭据，以及在统一配置中心中填写哪些字段：

- Semantic Scholar API；
- arXiv Supabase 论文池；
- 论文 Embedding；
- 论文 Reranker。

启动机器人后发送 `/config`，在返回的 HTTPS 统一配置中心中填写。保存后点击对应卡片的“测试可用性”；无需同时修改 Daily Paper 和 CitationClaw 自己的配置文件。

## 快速选择

| 服务 | Demo 推荐取得方式 | 是否必须 | 用途 |
| --- | --- | --- | --- |
| Semantic Scholar | 用户从官方网站自行申请 | CitationClaw 强烈推荐 | 作者论文、引用关系和论文元数据 |
| arXiv Supabase 论文池 | 向 Research Connect 服务维护者领取只读连接 | 日报混合召回需要 | 近期论文元数据、BM25 和向量召回 |
| 论文 Embedding | 向 zwwen/项目服务维护者领取 | Supabase 向量召回和综述需要 | 将查询编码为 384 维向量 |
| 论文 Reranker | 默认使用项目公共服务；需要独立额度时自行申请 SiliconFlow | 日报和综述默认需要 | 对融合后的候选论文重新排序 |

> “注册一个 Supabase 账号”不等于获得 arXiv 论文池。论文池还必须包含论文数据、384 维向量、索引、只读权限和项目约定的 RPC 函数。

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

### Demo 推荐方式：领取现有论文池的只读凭据

向论文池维护者领取：

```text
Project URL
Publishable Key 或旧版 anon key
论文表名
BM25 RPC 名
向量 RPC 名
```

统一配置中心的推荐填写值：

```text
API 端点：维护者提供的 https://<project-ref>.supabase.co
Anon / Publishable Key：维护者提供的只读 Key
论文表名：arxiv_papers
BM25 RPC：match_arxiv_papers_bm25
向量 RPC：match_arxiv_papers_exact
```

Supabase 官方将 Publishable/anon Key 定义为低权限客户端 Key，真正的数据权限仍由 Postgres RLS 控制；维护者必须为论文表和 RPC 配置只读策略。参见 [Supabase API Keys 文档](https://supabase.com/docs/guides/getting-started/api-keys)。用户电脑绝不能填写 `service_role`、Secret Key 或数据库密码。

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
API Key：向 zwwen/项目服务维护者领取
```

它必须和 Supabase 论文池中已有向量使用同一个模型及维度；随意换成另一个 embedding 模型，即使接口返回 200，向量召回结果也会失真。因此普通用户不应单独注册一个任意 embedding 服务替换它。

在统一配置中心打开 **论文日报/论文综述 → 论文 Embedding**，填写端点、模型和 Key 后测试。当前没有公开自助申请页面；维护者应为每个测试用户签发或分发可轮换的访问凭据。

## 4. 论文 Reranker

### 默认公共服务

Demo 默认使用：

```text
API 端点：https://zwwen.online/rerank
模型：Qwen/Qwen3-Reranker-0.6B
```

公共服务的可用性和额度由项目服务维护者负责。如果统一配置中心测试返回 401，需要领取对应 Key；如果返回 429，应稍后重试或改用自己的 SiliconFlow 凭据。

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

## 5. 向项目维护者申请 Demo 凭据

可以直接发送以下模板：

```text
用途：Research Connect 个人研究 Demo
安装标识/测试人员：<姓名或 install_id>
需要：arXiv Supabase 只读连接、zwwen Embedding Key、公共 Reranker Key
预计频率：日报每天 1 次，按需调研偶尔使用
承诺：不公开转发、不提交 Git；泄漏后立即申请轮换
```

收到凭据后只在 `/config` 的 HTTPS 页面填写。测试成功即可运行，不需要把 Key 写入 README、截图、群聊或仓库内的 `.env.example`。
