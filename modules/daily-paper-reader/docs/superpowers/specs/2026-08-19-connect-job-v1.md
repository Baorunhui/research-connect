# connect.job.v1 模块接入契约

`connect.job.v1` 是 Connect Hub 与论文日报、小红书及后续模块之间的第一版稳定任务契约。Python 类型定义位于 `connect_hub.contracts`。

## Manifest

每个模块必须声明：

```json
{
  "module_name": "daily-paper",
  "module_version": "0.1.0",
  "schema_versions": ["connect.job.v1"],
  "supported_job_types": ["daily_report", "paper_research"],
  "capabilities": ["progress", "cancel", "artifacts", "cost"]
}
```

Connect Hub 注册工具时执行兼容性校验。不支持 `connect.job.v1` 的模块不能进入工具注册表。

独立 HTTP 模块应提供：

```text
GET /api/manifest
```

响应就是上述 Manifest JSON。Connect Hub 后续启用严格握手后会在启动时检查它；模块负责人不得在同一个 v1 内删除字段或改变字段含义。

## 请求与状态

任务请求的固定字段是 `schema_version`、`job_id`、`job_type`、`input` 和 `options`。状态集合：

```text
queued -> running -> completed
                  -> failed
                  -> timed_out
                  -> cancelling -> cancelled
                                -> cancel_failed
服务重启：queued/running/cancelling -> interrupted
```

第一版不恢复 `interrupted` 任务。

推荐请求 JSON：

```json
{
  "schema_version": "connect.job.v1",
  "job_id": "job-uuid",
  "job_type": "daily_report",
  "input": {},
  "options": {}
}
```

## 事件

模块通过 `EventSink` 发出以下事件：

- `job.accepted`
- `job.started`
- `job.progress`
- `job.artifact`
- `job.cost`
- `job.completed`
- `job.failed`
- `job.cancelled`

事件必须携带唯一 `event_id`，Connect Hub 在 SQLite 中去重。进度事件可以带 `stage/current/total`；不要只写无法判断阶段的自由文本。

事件 JSON：

```json
{
  "schema_version": "connect.job.v1",
  "event_id": "evt-uuid",
  "job_id": "job-uuid",
  "event_type": "job.progress",
  "stage": "llm_refine",
  "message": "正在进行 LLM 精筛",
  "current": 12,
  "total": 30,
  "payload": {"rate_limit_retries": 1}
}
```

Python/子进程 Adapter 调用 `ToolContext.report_progress(message, stage=..., current=..., total=..., payload=...)`。HTTP Adapter 可以在状态响应的 `events` 数组返回尚未消费的事件，或回调 Connect Hub；无论采用哪种传输，事件 JSON 不能改变。

## 错误

模块应抛出 `ConnectJobError`，不要直接生成飞书文案。至少填写 `code` 和 `user_message`；诊断信息放在 `technical_message`，且不得包含 Key。

常用错误码：

- `INVALID_REQUEST`
- `MODULE_INCOMPATIBLE`
- `MODULE_EXECUTION_FAILED`
- `PROVIDER_UNAVAILABLE`
- `PROVIDER_UNAUTHORIZED`
- `PROVIDER_RATE_LIMITED`
- `PROVIDER_QUOTA_EXHAUSTED`
- `OUTPUT_INVALID`
- `JOB_TIMEOUT`
- `JOB_CANCELLED`
- `CANCEL_FAILED`

Connect Hub 使用确定性模板将事件转换成飞书消息，不让 LLM 猜测错误原因。

失败 JSON：

```json
{
  "ok": false,
  "schema_version": "connect.job.v1",
  "error": {
    "code": "PROVIDER_RATE_LIMITED",
    "stage": "llm_refine",
    "provider": "school-llm",
    "retryable": true,
    "user_message": "LLM 服务触发频率限制，请稍后重试。",
    "technical_message": "sanitized diagnostic without key"
  }
}
```

## 进程取消

本地子进程必须通过 `ToolContext.run_process()` 启动。它会创建独立进程组并在取消/超时时终止整个进程树：

- Linux：`SIGTERM`，宽限期后 `SIGKILL`；
- Windows：`taskkill /PID <pid> /T /F`。

模块不得再直接使用不可取消的 `subprocess.run()` 执行长任务。

HTTP 模块仍由模块服务拥有底层进程。它必须实现自己的取消端点，并在收到取消请求后终止进程树；Connect Hub 的 Daily Paper Adapter 约定调用：

```text
POST /api/recommend/<run_id>/cancel
```

旧版 Daily Paper 尚未实现该端点时，Connect Hub 会停止等待并记录取消，但无法承诺远端后端进程已经退出；这是 Daily Paper 模块接入 v1 时必须补齐的能力。

## Daily Paper HTTP v1 交接清单

保留现有推荐接口可以减少改造，但应补齐以下约定：

```text
GET  /api/manifest
POST /api/recommend
GET  /api/recommend/<run_id>
POST /api/recommend/<run_id>/cancel
```

- `POST /api/recommend` 接收 `schema_version` 和 Connect Hub `job_id`，返回内部 `run_id`。
- 状态响应至少包含 `schema_version`、`job_id`、`run_id`、`status`、`events`，完成后包含 `result`。
- `events` 按生成顺序返回，事件具有稳定 `event_id`，重复轮询时允许重复返回，Connect Hub 会去重。
- LLM 精筛必须按批次发出结构化进度，至少提供 `current/total`、429 次数和当前并发。
- 取消端点只有在底层进程树确实退出后才能返回成功；不能只把内存状态改成 cancelled。
- 错误使用本文件的错误 JSON；HTTP 状态码仍应合理使用 400、401、429、500、503。

## 产物与用量

模块通过 `ToolContext.record_artifact()` 登记文件、图片或 URL，通过 `ToolContext.record_usage()` 登记 provider、operation、token、调用次数、耗时和状态码。

SQLite 统一表：

- `jobs`
- `job_events`
- `artifacts`
- `cache_index`
- `usage_records`

模块不得自行再创建同类任务数据库。
