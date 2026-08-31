# Changelog

## 0.2.0-demo — 2026-08-31

首个供小范围测试的统一 Python 发行版。

### 已包含

- Windows PowerShell 与 Linux Bash 安装、检查、启动入口；
- 单一 Python 3.11～3.13 虚拟环境和统一数据根；
- 飞书 Connect Hub、统一 LLM/API 配置、SQLite 任务与事件管理；
- Daily Paper 的日报、论文总结、领域综述及原版网页；
- CitationClaw 原版网页入口；
- XHS Agent 工具入口；
- 多安装隔离的公网 Report Hub、稳定站点和配置中继；
- 可选 Docling PDF/图表处理，默认远程 Embedding/Reranker 路线。

### 已知限制

- 当前只提供 Python 安装方式，Docker 和安装包尚未发行；
- CitationClaw 已接入网页和统一配置，但仍保留已知业务缺陷，作为 demo 功能提供；
- 每个本机安装按单用户设计，不在 Connect Hub 内实现多租户；
- 公网报告使用随机 bearer 链接，不是公开发布或细粒度账号权限系统；
- 上游 Daily Paper 维护脚本中仍有依赖本地 Torch 的测试债务，不影响默认远程服务运行路径。

升级和破坏性变化将在后续版本继续记录于此文件。
