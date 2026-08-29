# 开发路线

## P0：建立可分发的自托管主链

- [x] 建立单仓库并导入 Connect Hub、Daily Paper、CitationClaw 和 XHS Agent 源码快照。
- [x] 排除虚拟环境、运行数据、缓存、生成结果和真实密钥。
- [ ] 把 Connect Hub 长任务调用改成真正的异步 submit，禁止飞书工作线程等待任务结束。
- [ ] 实现只读公共任务页：创建任务即得到 URL，支持移动端。
- [ ] 实现 Hub WebSocket：快照恢复、增量事件、心跳和重连。
- [ ] 定义并实现 `PublicAccessProvider`，先完成 `custom` 与一个零配置临时 Provider。
- [ ] 公网失败时继续通过飞书汇报，并在完成后发送可长期保存的附件。

## P0：模块接入

- [ ] Daily Paper：对接新版 workflow run，增加 callback、artifact 和真实取消。
- [ ] Daily Paper：移除前端 5 秒轮询，改接 Hub WebSocket。
- [ ] CitationClaw：`/api/run` 接受外部 job ID，事件和产物携带 job ID。
- [ ] CitationClaw：第一版实施全局单任务锁和标准 `MODULE_BUSY` 错误。
- [ ] CitationClaw：复用现有 WebSocket 和取消接口，不公开管理 API。
- [ ] XHS Agent：迁移现有 CLI 适配路径并完成回归测试。

## P1：Python 安装

- [ ] 统一 `setup`、`doctor`、`serve` 命令。
- [ ] Linux Bash 安装/启动脚本。
- [ ] Windows PowerShell 安装/启动脚本。
- [ ] 自动创建核心 venv 和按需模块 venv。
- [ ] 检查 Python、Playwright、Docling、字体、端口和隧道二进制。
- [ ] CI 覆盖 `ubuntu-latest` 与 `windows-latest`。

## P1：Docker 发行

- [ ] 多阶段 CPU Dockerfile，避免把开发缓存放入镜像。
- [ ] Compose 提供核心服务、持久卷和模块 profile。
- [ ] 验证 Linux Docker 与 Windows Docker Desktop/WSL2。
- [ ] 镜像启动时运行配置检查，不把密钥烘焙到镜像。

## P1：公网 Provider 实测

- [ ] 在中国大陆网络实测临时 Provider 的启动成功率、WebSocket、手机访问和重连。
- [ ] 实测 cpolar 固定/临时地址及其账号配置成本。
- [ ] 实测 Cloudflare Quick/Named Tunnel，并标注大陆网络风险。
- [ ] 记录临时 URL 重启失效行为，完成飞书附件兜底。
- [ ] 决定是否需要项目方运营轻量公共中继。

## P2：体验与维护

- [ ] CitationClaw Dashboard 手机布局优化。
- [ ] 一页式配置向导和敏感项检查。
- [ ] 模块升级脚本及上游版本差异检查。
- [ ] 发布前许可证和第三方资源清单。
- [ ] 可选的稳定域名/固定隧道配置指南。

