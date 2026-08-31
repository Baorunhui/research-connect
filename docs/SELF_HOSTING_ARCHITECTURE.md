# 本地优先自托管架构

## 1. 产品边界

Research Connect 的默认用户不是运维团队，而是希望在自己的电脑或实验室服务器上运行个人工具的研究者。部署方案必须同时满足：

1. 计算、密钥和原始材料默认留在用户机器，发布的报告副本保存到 Report Hub；
2. 飞书机器人可以从电脑和手机随时发起任务；
3. 任务链接在任务开始时就可打开，并实时展示进度；
4. 不要求每位用户购买云服务器；
5. Windows 与 Linux 使用同一套任务、事件和配置语义；
6. 常驻部分轻量，Docling、论文日报和其他重任务按需启动。

## 2. 最终运行形态

```text
飞书电脑端 / 手机端
        │ 飞书长连接                            │ 固定 HTTPS 报告链接
        ▼                                       ▼
用户自己的 Windows / Linux 机器             公共 Report Hub
┌──────────────────────────────────────┐      ┌────────────────────────┐
│ Connect Hub（唯一常驻本机核心）      │ HTTPS│ SQLite 任务/事件       │
│ - 飞书连接与对话理解                 ├─────►│ WebSocket 实时任务页   │
│ - 本机任务、缓存、用量与取消         │ 上传 │ 静态报告和图片         │
│ - 按需启动领域模块                   │      └────────────────────────┘
│   ├─ Daily Paper                     │
│   ├─ CitationClaw                    │
│   └─ XHS Agent                       │
└──────────────────────────────────────┘
```

飞书机器人本身不需要公网回调地址；它使用飞书 WebSocket 长连接。需要公网化的只有独立 Report Hub，领域模块的配置、密钥、启动和删除接口始终只绑定 `127.0.0.1`。

## 3. 任务链接生命周期

1. Connect Hub 用服务端分配的上传 token 向 Report Hub 创建任务。
2. Report Hub 在 SQLite 创建任务和高熵 `public_token`，立即返回固定 `public_url`。
3. Connect Hub 立即向飞书发送该链接，不等待领域任务启动或完成。
4. 浏览器先读取任务快照，再通过 WebSocket 订阅增量事件；正常路径不轮询。
5. 领域模块在后台运行，事件写入 `job_events` 并广播给浏览器和飞书进度通知。
6. 完成后，本机把静态报告和资源打包上传；同一 URL 原地显示日报或 CitationClaw Dashboard。
7. 本机任务结束或关机后，公网服务器继续保存并提供旧报告。
8. Report Hub 不可达时，任务仍继续运行，飞书继续汇报关键进度和错误，并提示报告上传失败。

公开任务页默认只读。取消操作从飞书命令进入，避免拿到报告链接的人控制本机任务。

## 4. Report Hub 边界

Report Hub 是唯一需要公网域名的组件。用户电脑主动建立出站 HTTPS/WebSocket 连接，因此不要求公网 IP、端口映射、隧道账号或同一局域网。

- 写接口使用服务器分发的 agent token；浏览链接使用高熵公开 token。
- 创建任务、事件上报和静态报告上传使用版本化 `/api/v1` 协议。
- 服务器只保存发布副本，不运行领域模块，不接触飞书 Secret 或 LLM Key。
- 动态本机功能不能作为最终报告的硬依赖；任务完成时导出可独立浏览的静态包。
- Report Hub 可由项目方共享运营，也可让有条件的用户用 Docker/Python 自托管。
- 完全不发布网页时，可关闭 Report Hub，只通过飞书接收进度和附件。

## 5. 移动端要求

公共任务页从一开始按手机宽度设计：

- 单列优先，不复制桌面端管理控制台；
- 首屏只显示任务名、当前阶段、耗时和取消提示；
- 日志默认折叠，图表自适应容器宽度；
- 大表格使用卡片、横向滚动或字段折叠；
- CitationClaw 的最终 Dashboard 需要专门检查移动布局；
- 断线重连时先拉取 SQLite 快照，再继续接收 WebSocket 事件，不能丢进度。

## 6. 模块接入边界

### Daily Paper

- 接受 Hub `job_id`，异步启动并回调统一事件；
- 补充真正的进程树取消；
- 将当前 5 秒轮询改成 Hub WebSocket；
- 完成后登记日报入口和相关静态资源；
- 不公开其 `/api/local/config` 和 `/api/local/secret`。

### CitationClaw

- 第一版明确只允许一个活动任务；
- `/api/run` 接受 Hub `job_id`，WebSocket 和 `all_done` 携带任务 ID；
- 将 Dashboard、Excel 和 JSON 登记为 artifact；
- 复用现有取消能力并映射到 `connect.job.v1`；
- 不把配置、Provider 测试、结果删除等管理接口暴露公网。

### XHS Agent

- 保留轻量 CLI 适配；
- 图片和内容包通过飞书发送，公共任务页只作为可选预览；
- 后续视觉审查仍为用户可选项。

## 7. 安装形态约束

### Python 发行方式

- 支持 Python 3.11～3.13，推荐 Python 3.11；
- 提供 Bash 和 PowerShell 安装/启动脚本；
- Python 版默认使用仓库根目录的统一 `.venv`，避免每个模块各维护一套重复环境；Docling 作为同一环境中的可选扩展安装；
- `setup.sh/setup.ps1` 负责创建环境与配置模板，`doctor` 检查飞书、LLM、Report Hub、模块依赖和平台能力；
- 不依赖 Bash 专属语义完成 Windows 安装。

### Docker 发行方式

- 提供 CPU 默认镜像和 Compose；
- Windows 通过 Docker Desktop/WSL2，Linux 使用原生 Docker；
- SQLite、缓存和 artifacts 使用本地持久卷；
- 不引入 PostgreSQL、Redis、Celery 或 Kubernetes；
- Connect Hub 常驻，模块仍可按任务启动；Report Hub 作为独立公网服务部署。
