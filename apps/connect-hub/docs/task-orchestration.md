# 单 Agent 对话编排与联网富化

Connect Hub 使用一个轻量生成式编排器，不再预先执行意图分类、固定槽位提取或 `task_drafts` 草稿状态机。每条普通消息的处理路径是：

```text
飞书命令处理
  -> 最近对话历史 + 当前消息 + 工具说明
  -> LLM 自然回答 / 追问 / tool call
       ├─ Exa 搜索或 Jina URL 读取（可选）
       └─ 固定 ToolRegistry 业务工具（条件满足后）
  -> 工具结果回到同一个 LLM，或确定性返回业务产物
```

用户的澄清轮数没有写死。例如“生成论文日报 → OCC → 对，是 3D occupancy”会作为连续对话理解，不会把 `OCC` 当成一条全新的任务，也不会在第二轮自动作废。模型只在缺失信息会实质改变结果时追问；能安全采用的日报默认值是最近 30 天、`standard`、不发布网页。

## 为什么仍然有程序护栏

自然语言理解交给模型，但权限、成本和副作用不能只靠提示词保证。当前每条用户消息的内部预算为：

- 最多 3 次 LLM 调用；
- 最多 1 次 Exa 搜索；
- 最多 1 次 Jina URL 读取；
- 最多 1 次耗时业务工具调用；
- 相同工具和参数不能重复调用；
- 检索与业务工具不能在同一批并行启动；
- 日报、小红书等业务工具必须能从近期用户原话中找到明确请求。

这些限制只约束一条消息内部的自动 tool-calling，不限制用户继续对话。工具仍来自静态 `ToolRegistry`，模型不能执行 shell、动态加载模块或自行创造工具。

每轮运行写入 SQLite `agent_runs`，每次模型和工具步骤写入 `agent_steps`，包括 provider、模型、token、耗时、状态和截断后的审计载荷。原数据库中的 `task_drafts` 表仅为已有 SQLite 文件的向后兼容保留，运行路径不再读写它。

## 联网模式

- `auto`：模型只在术语含糊、问题有时效性、用户要求搜索，或公开事实材料不足时联网。
- `on`：在上述场景更积极地联网，但不是每句闲聊都强制搜索。
- `off`：联网工具不提供给模型；模型不能声称已经搜索。

飞书命令为 `/web auto`、`/web on`、`/web off` 和 `/web`。单个机器人自定义菜单项使用事件键 `connect_web_toggle`：当前为 `on` 时切换到 `off`，其他状态切换到 `on`。飞书菜单没有持久勾选态，因此机器人每次回复当前状态。偏好保存在 `conversation_settings`。

## Provider 边界

`ExaMCPWebSearchProvider` 直接使用 MCP Streamable HTTP 调用远程只读 `web_search_exa`，不安装 Node、浏览器或本地搜索模型。`JinaReaderProvider` 只读取公开 HTTP(S) URL，并拒绝 localhost、环回和 `.local` 地址。

两个 Provider 作为模型可选择的工具暴露：搜索摘要足够时模型不必读取正文；需要核对选中页面时才调用 Jina。搜索结果统一为 `title`、`url`、`snippet`、`published_at`、`score`、`provider`，SQLite 默认缓存 24 小时。

外部内容一律视为不可信数据，不能改变系统指令。Connect Hub 会在最终回复中确定性补上实际使用但模型漏写的来源 URL。

远程请求开始前，Connect Hub 会通过飞书进度回调发送当前操作，例如
`🔎 正在联网搜索：OCC 3D vision meaning`；命中 SQLite 搜索缓存时显示
`🔎 已使用联网搜索缓存：…`；模型需要用 Jina 读取正文时显示目标 URL。
