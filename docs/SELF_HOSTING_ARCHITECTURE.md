# 本地优先自托管架构

## 1. 产品边界

Research Connect 的默认用户不是运维团队，而是希望在自己的电脑或实验室服务器上运行个人工具的研究者。部署方案必须同时满足：

1. 计算和数据默认留在用户机器；
2. 飞书机器人可以从电脑和手机随时发起任务；
3. 任务链接在任务开始时就可打开，并实时展示进度；
4. 不要求每位用户购买云服务器；
5. Windows 与 Linux 使用同一套任务、事件和配置语义；
6. 常驻部分轻量，Docling、论文日报和其他重任务按需启动。

## 2. 最终运行形态

```text
飞书电脑端 / 手机端
        │
        │ 飞书出站长连接 + HTTPS 报告链接
        ▼
用户自己的 Windows / Linux 机器
┌───────────────────────────────────────────────┐
│ Connect Hub（唯一常驻核心）                  │
│  - 飞书连接与对话理解                        │
│  - SQLite jobs/job_events/artifacts/usage     │
│  - 公共任务页与 WebSocket 事件流              │
│  - PublicAccessProvider                       │
│                                               │
│  仅监听本机的模块服务                         │
│  ├─ Daily Paper（按任务启动）                 │
│  ├─ CitationClaw（按任务启动或轻量服务）      │
│  └─ XHS Agent（按任务启动）                   │
└──────────────────────┬────────────────────────┘
                       │ 出站隧道
                       ▼
              临时或固定公网 HTTPS 地址
```

飞书机器人本身不需要公网回调地址；它使用飞书 WebSocket 长连接。需要公网化的只有 Connect Hub 的只读任务页，模块的配置、密钥、启动和删除接口始终只绑定 `127.0.0.1`。

## 3. 任务链接生命周期

1. Connect Hub 启动时建立公网连接，得到当前 `public_base_url`。
2. 用户发起命令后，Hub 先在 SQLite 创建任务和高熵 `public_token`。
3. Hub 立即向飞书发送 `public_base_url/r/<public_token>`，不等待领域任务启动或完成。
4. 浏览器先读取任务快照，再通过 WebSocket 订阅增量事件；正常路径不轮询。
5. 领域模块在后台运行，事件写入 `job_events` 并广播给浏览器和飞书进度通知。
6. 完成后，同一 URL 原地显示日报或 CitationClaw Dashboard；结果文件登记为 artifact。
7. 公网连接失败时，任务仍继续运行，飞书继续汇报关键进度和错误。

公开任务页默认只读。取消操作从飞书命令进入，避免拿到报告链接的人控制本机任务。

## 4. PublicAccessProvider

公网连接必须是适配层，而不是写死某一家服务：

```python
class PublicAccessProvider(Protocol):
    def start(self, local_url: str) -> PublicEndpoint: ...
    def health(self) -> PublicAccessHealth: ...
    def stop(self) -> None: ...
```

统一输出至少包含：`public_base_url`、`provider`、`ephemeral`、`supports_websocket`、`started_at` 和诊断信息。

首批计划支持：

- `auto`：从已安装/可下载的零配置隧道中选择可用项，适合首次体验；
- `cpolar`：国内网络优先的可选 Provider，需要时由用户配置 token；
- `cloudflare`：Quick Tunnel 或 Named Tunnel，适合开发及国际网络；
- `custom`：用户已经有反向代理、公网 IP 或固定域名；
- `disabled`：不发布网页，只使用飞书进度和最终附件。

### 无法同时保证的约束

一台位于 NAT 后的个人电脑，如果不使用第三方隧道、不注册任何服务、也没有项目方运营的中继，就无法获得“全球可访问、永久固定、重启后不变”的公网 URL。因此产品要明确区分：

- 零配置临时地址：安装最简单，但应用重启后旧链接可能失效；
- 注册型固定隧道：多一步账号/token 配置，链接可以稳定；
- 项目公共中继：用户最省事，但项目方需要承担服务器、带宽和安全责任；
- 飞书附件兜底：最终结果长期留在飞书，实时页面只保证任务运行期间可用。

第一版采用“临时隧道 + 飞书最终附件兜底”，同时保留固定 Provider。是否运营公共中继以后单独决策。

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

- 支持 Python 3.10+；
- 提供 Bash 和 PowerShell 安装/启动脚本；
- Connect Hub 使用轻量核心环境，各领域模块允许独立 venv；
- `setup` 负责生成配置，`doctor` 检查飞书、LLM、隧道和模块依赖；
- 不依赖 Bash 专属语义完成 Windows 安装。

### Docker 发行方式

- 提供 CPU 默认镜像和 Compose；
- Windows 通过 Docker Desktop/WSL2，Linux 使用原生 Docker；
- SQLite、缓存和 artifacts 使用本地持久卷；
- 不引入 PostgreSQL、Redis、Celery 或 Kubernetes；
- 隧道与 Connect Hub 同生命周期，模块仍可按任务启动。

