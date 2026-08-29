# Research Connect

Research Connect 是一套面向个人用户的本地优先研究工具框架。每位用户在自己的 Windows 电脑、Linux 工作站或服务器上安装一份，通过飞书机器人调用论文日报、查引用和小红书内容生成等模块。

项目不是中心化计算 SaaS：论文处理、缓存、配置和本地任务数据默认留在用户自己的机器上。飞书承担跨设备入口和通知；轻量 Report Hub 只保存用户选择发布的任务进度与最终网页，让手机或其他电脑长期打开同一链接。

> 当前仓库已完成源码归档和架构收口，尚未完成一键安装及新版论文日报/CitationClaw 的正式接入，不能视为最终发行版。

## 仓库结构

```text
apps/connect-hub/                 飞书、LLM 网关、任务和统一事件中心
apps/report-hub/                  公网任务进度与静态报告托管
modules/daily-paper-reader/      论文日报源码快照
modules/citationclaw/            查引用源码快照
modules/xhs-agent/               小红书内容生成源码快照
docs/                            总体架构、路线和模块版本记录
```

仓库采用单体源码仓库而不是 Git submodule。最终用户只需要一次普通 `git clone`；各模块仍可使用独立 Python 虚拟环境，避免 Docling、Playwright 等依赖污染常驻的轻量 Connect Hub。

## 目标部署方式

- Python：Windows 与 Linux 上提供统一配置向导、环境检查和启动入口。
- Docker：提供预构建镜像和 Compose 配置，默认 CPU、SQLite 和本地卷。
- 飞书：使用出站 WebSocket 长连接，不要求用户开放机器人回调端口。
- 实时网页：Connect Hub 创建任务后立即返回链接，浏览器通过 WebSocket 接收进度。
- 公网报告：用户电脑仅通过出站 HTTPS 向公共或自托管 Report Hub 上报进度、上传静态结果，不需要内网穿透。

具体边界见 [自托管架构](docs/SELF_HOSTING_ARCHITECTURE.md)、[Report Hub 协议](docs/REPORT_HUB_V1.md)、[公网服务器交付说明](docs/PUBLIC_SERVER_HANDOFF.md) 和 [开发路线](docs/ROADMAP.md)。

## 安全基线

仓库不提交真实飞书密钥、LLM Key、Scraper Key、SQLite、PDF、模型或生成结果。请从各模块的 `.env.example` 创建本机配置；不要把本机 `.env` 加入 Git。

## 模块来源

模块上游地址和本次导入版本记录在 [SOURCE_VERSIONS.md](docs/SOURCE_VERSIONS.md)。CitationClaw 使用的是用户提供的 `CitationClaw-20260829.tar.gz`，并非自动替换成公开仓库版本。
