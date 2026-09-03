# 本地优先自托管架构

## 1. 产品边界

Research Connect 的默认用户不是运维团队，而是希望在自己的电脑或实验室服务器上运行个人工具的研究者。部署方案必须同时满足：

1. 计算、密钥和原始材料默认留在用户机器，模块稳定网页的发布副本保存到 Report Hub；
2. 飞书机器人可以从电脑和手机随时发起任务；
3. Daily Paper/CitationClaw 使用各自原版网页展示进度，其他任务由飞书播报；
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
│ - 飞书连接与对话理解                 ├─────►│ 稳定站点与运行快照     │
│ - 本机任务、缓存、用量与取消         │ 上传 │ 通用命令邮箱           │
│ - 按需启动领域模块                   │      └────────────────────────┘
│   ├─ Daily Paper                     │
│   ├─ CitationClaw                    │
│   └─ XHS Agent                       │
└──────────────────────────────────────┘
```

飞书机器人本身不需要公网回调地址；它使用飞书 WebSocket 长连接。需要公网化的只有独立 Report Hub，领域模块的配置、密钥、启动和删除接口始终只绑定 `127.0.0.1`。

## 3. 页面与进度生命周期

1. Connect Hub 启动时注册配置中心、Daily Paper 和 CitationClaw 三个稳定站点。
2. 本机打包并上传原版网页资源，同一 `site_id` 始终复用高熵 `public_url`。
3. Daily Paper 运行快照和日志同步到稳定站点，原版网页显示实时进度与历史结果。
4. CitationClaw 使用原版网页自己的状态接口，经通用命令邮箱访问本机后端。
5. 飞书同时接收本机 Connect Hub 的关键步骤文字播报、完成结果和错误。
6. 小红书不创建公网进度页，图片和内容包直接通过飞书发送。
7. Report Hub 不可达时，本机任务和飞书播报继续运行，仅公网网页同步暂时失败。

## 4. Report Hub 边界

Report Hub 是唯一需要公网域名的组件。用户电脑主动建立出站 HTTPS 连接，因此不要求公网 IP、端口映射、隧道账号或同一局域网。

- 写接口使用服务器分发的 agent token；浏览链接使用高熵公开 token。
- 安装注册、稳定站点、运行快照和静态资源上传使用版本化 `/api/v1` 协议。
- 服务器只保存发布副本，不运行领域模块，不接触飞书 Secret 或 LLM Key。
- 动态操作通过站点声明的受限命令策略转发，不开放任意本机路径。
- Report Hub 可由项目方共享运营，也可让有条件的用户用 Docker/Python 自托管。
- 完全不发布网页时，可关闭 Report Hub，只通过飞书接收进度和附件。

## 5. 移动端要求

Daily Paper、CitationClaw 和配置中心从一开始按手机宽度设计：

- 单列优先，不复制桌面端管理控制台；
- 任务页首屏显示当前阶段、耗时和主要操作；
- 日志默认折叠，图表自适应容器宽度；
- 大表格使用卡片、横向滚动或字段折叠；
- CitationClaw 的最终 Dashboard 需要专门检查移动布局；
- 断线重连时重新读取本机模块状态或已同步的运行快照。

## 6. 模块接入边界

### Daily Paper

- 异步启动并回调本机统一事件；
- 补充真正的进程树取消；
- 在原版网页显示本机运行状态和已同步快照；
- 完成后登记日报入口和相关静态资源；
- 不公开其 `/api/local/config` 和 `/api/local/secret`。

### CitationClaw

- 第一版明确只允许一个活动任务；
- `/api/run` 使用模块内部任务 ID，并由 Connect Hub 映射到本机统一任务；
- 将 Dashboard、Excel 和 JSON 登记为 artifact；
- 复用现有取消能力并映射到 `connect.job.v1`；
- 不把配置、Provider 测试、结果删除等管理接口暴露公网。

### XHS Agent

- 保留轻量 CLI 适配；
- 图片和内容包通过飞书发送，不创建公共任务页；
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
