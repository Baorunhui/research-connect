# 开发路线

## P0：建立可分发的自托管主链

- [x] 建立单仓库并导入 Connect Hub、Daily Paper、CitationClaw 和 XHS Agent 源码快照。
- [x] 排除虚拟环境、运行数据、缓存、生成结果和真实密钥。
- [ ] 把 Connect Hub 长任务调用改成真正的异步 submit，禁止飞书工作线程等待任务结束。
- [x] 早期实现只读公共任务页；发行版已由模块稳定网页和飞书进度播报替代。
- [ ] 实现 Hub WebSocket：快照恢复、增量事件、心跳和重连。
- [x] 实现独立 Report Hub v1：固定链接、SQLite、WebSocket、静态报告上传。
- [ ] 公网失败时继续通过飞书汇报，并在完成后发送可长期保存的附件。

## P0：模块接入

- [ ] Daily Paper：对接新版 workflow run，增加 callback、artifact 和真实取消。
- [ ] Daily Paper：移除前端 5 秒轮询，改接 Hub WebSocket。
- [ ] CitationClaw：`/api/run` 接受外部 job ID，事件和产物携带 job ID。
- [ ] CitationClaw：第一版实施全局单任务锁和标准 `MODULE_BUSY` 错误。
- [ ] CitationClaw：复用现有 WebSocket 和取消接口，不公开管理 API。
- [ ] XHS Agent：迁移现有 CLI 适配路径并完成回归测试。

## P1：Python 安装

- [ ] 统一 `doctor`、`serve` 命令。
- [x] Linux Bash 统一环境安装脚本。
- [x] Windows PowerShell 统一环境安装脚本。
- [x] 自动创建单一根目录 venv，统一 FastAPI/OpenAI/Playwright 等依赖版本。
- [ ] 检查 Python、Playwright、Docling、字体、端口和 Report Hub 连通性。
- [ ] CI 覆盖 `ubuntu-latest` 与 `windows-latest`。

## P1：Docker 发行

- [ ] 多阶段 CPU Dockerfile，避免把开发缓存放入镜像。
- [ ] Compose 提供核心服务、持久卷和模块 profile。
- [ ] 验证 Linux Docker 与 Windows Docker Desktop/WSL2。
- [ ] 镜像启动时运行配置检查，不把密钥烘焙到镜像。

## P1：Report Hub 生产化

- [ ] 实测国内手机网络访问 Report Hub 的速度、WebSocket 稳定性和报告带宽。
- [ ] 配置正式域名、HTTPS、磁盘监控、备份和报告保留周期。
- [ ] Report Hub 不可达或上传失败时，完成飞书报错和最终附件兜底。
- [ ] 评估共享上传 token 的轮换方式；需要多用户隔离时再增加账号层。

## P2：体验与维护

- [ ] CitationClaw Dashboard 手机布局优化。
- [ ] 一页式配置向导和敏感项检查。
- [ ] 模块升级脚本及上游版本差异检查。
- [ ] 发布前许可证和第三方资源清单。
- [x] 公网 Report Hub 的域名、HTTPS、Python/Docker 配置指南。
