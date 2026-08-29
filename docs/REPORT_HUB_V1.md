# Report Hub v1 接入协议

## 目标

领域模块在用户本机运行，Report Hub 在公网服务器上保存任务进度和最终静态报告。创建任务时就会返回永久任务链接；飞书应立即发送该链接，不等待任务结束。

客户端只配置两个值：

```env
REPORT_HUB_API_URL=https://reports.example.com
REPORT_HUB_AGENT_TOKEN=<由服务器管理员生成并分发>
```

所有写接口使用 `Authorization: Bearer <token>`。公开任务页只凭不可猜测的 URL token 读取，不提供取消或配置功能。

## 调用顺序

1. `POST /api/v1/jobs` 创建任务，取得 `public_url`；重复提交相同 `job_id` 返回原链接。
2. Connect Hub 立即把 `public_url` 发到飞书。
3. 模块运行时调用 `POST /api/v1/jobs/{job_id}/events`。
4. 报告完成后，将含根目录 `index.html` 的 ZIP 调用 `PUT /api/v1/jobs/{job_id}/report` 上传。
5. 原 `public_url` 自动显示最终报告；用户电脑关机不影响访问。

## 创建任务

```http
POST /api/v1/jobs
Authorization: Bearer ...
Content-Type: application/json

{
  "job_id": "dp-20260829-abcd",
  "module_name": "daily-paper",
  "title": "3DVG 方向论文日报"
}
```

`module_name` 第一版取值：`daily-paper`、`citationclaw`、`xhs-agent`、`other`。

## 上报事件

```json
{
  "event_id": "dp-20260829-abcd-search-1",
  "event_type": "job.progress",
  "stage": "search",
  "message": "正在检索最近 30 天论文",
  "current": 1,
  "total": 6,
  "payload": {}
}
```

事件类型固定为：`job.started`、`job.progress`、`job.message`、`job.completed`、`job.failed`、`job.cancelled`。`event_id` 用于网络重试去重；同一任务重复 ID 只接受一次。

失败事件把统一错误码放入 `payload.error_code`。取消仍由飞书 → 本机 Connect Hub 执行，公网任务页没有远程控制能力。

## 静态报告约束

- ZIP 根目录必须有 `index.html`；HTML、CSS、JS、图片可放子目录并使用相对链接。
- 第一版默认压缩包不超过 50 MB、解压后不超过 250 MB。
- 报告需要适配手机宽度。
- 报告不能依赖用户本机 `127.0.0.1` API；需要长期保留的数据应在任务完成时导出进 ZIP。
- 本机才能提供的动态操作应显示为不可用或回到飞书发命令，不能让页面整体失效。

## WebSocket

浏览器连接 `/ws/public/jobs/{public_token}` 获取快照和增量事件。断线重连后会重新取得数据库快照。首版服务必须单进程运行，因为 WebSocket 广播器在进程内；若未来横向扩容再引入外部广播层。

## v1 稳定边界

`/api/v1` 路径、上述字段和事件类型是 Connect Hub 与两个领域模块的兼容边界。可以增加可选字段，不直接修改或删除现有字段；破坏性升级使用 `/api/v2`。

