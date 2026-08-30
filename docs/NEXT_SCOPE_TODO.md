# 下一阶段待办：尚不符合既定使用原则的部分

本文件只记录下一轮范围。本次统一环境、公共 LLM 运行时、数据根目录和缓存索引工作不顺带修改下列业务链路，避免在模块负责人代码尚未确认时扩大改动。

## P0：模块接入与任务边界

- [x] Daily Paper 适配最新版 `/api/local/workflows/dispatch`，补齐 submit、事件、artifact、错误回调和进程树取消。
- [x] CitationClaw 增加 Connect Hub adapter，外部 `job_id` 贯穿提交、状态、取消与最终报告。
- [x] Connect Hub 成为唯一对外任务生命周期所有者；模块状态仅作为内部执行句柄。
- [x] Linux/Windows 子进程树取消已实现，模块轮询持续检查 Hub 取消标志。
- [ ] 把 Daily Paper 已有的“论文总结”异步接口（`/api/paper/summarize`）注册为飞书固定工具，接入统一任务、进度、取消、产物与固定日报站点刷新。
- [ ] 把 Daily Paper 已有的“论文综述”异步接口（`/api/survey`）注册为飞书固定工具，定义主题、候选数、精选数等稳定参数，并接入统一任务生命周期。
- [x] 增加统一配置预检：飞书与 Python CLI 只检查公网配置记录是否存在；不存在时返回模块原版公网网页。配置保存后由本机 Connect Hub 在任务启动前同步，不探测 LLM/API 连通性。

## P0：符合轻量部署原则

- [x] Daily Paper 默认使用 Supabase/远程 embedding，关闭本地 embedding 自动降级与模块自带 scheduler。
- [x] 增加一次性 Docling 入口；PaperCropper/DocLayout-YOLO 不再进入运行路径，CitationClaw 本地 MinerU 默认关闭。
- [x] 移除硬编码 embedding token。
- [x] 统一 `serve` 只常驻轻量 HTTP 壳和飞书连接；模型/流水线按任务启动并退出。

## P0：LLM 精筛与外部 API

- [x] 按本轮要求不做学校 API 压测和 Provider 额度调研；保留共享运行时已有的 4 并发与 429/`Retry-After` 重试能力。
- [x] 模块调用次数、耗时、状态码及 CitationClaw 费用摘要写入 Connect Hub `usage_records`。
- [x] Daily Paper 本地聊天代理复用 shared LLM transport；CitationClaw 由 Connect Hub 环境覆盖统一 LLM 配置。
- [x] 外部 API 故障映射为统一错误码，写入 SQLite、Report Hub 事件并通过飞书提示。

## P1：网页与跨设备体验

- [ ] Daily Paper 和 CitationClaw 前端移除固定轮询，统一消费 Connect/Report Hub WebSocket；创建任务时立即返回手机可打开的固定 URL。
- [ ] 排查 Daily Paper 静态页面依赖本机 API/CDN 的部分，发布报告必须在任务结束后仍可独立查看。
- [ ] 优化 CitationClaw Dashboard 手机布局。
- [ ] Report Hub 不可达时继续飞书进度，完成后以附件或本地链接降级。

## P1：小红书质量链

- [ ] 把 Pipeline 当前输出解析重试与 shared LLM 的传输重试明确分层，并把降级原因写入统一事件/usage。
- [ ] 增加可选视觉审查；不开启时不引入视觉 API 或本地视觉模型。
- [ ] 补齐每页疏密和布局自适应回归样例。

## P1：发行与清理

- [ ] 为 Windows/Linux 增加 `doctor` 与统一 `serve`，检查 Python、字体、浏览器、端口和外部 Provider。
- [ ] 增加 Windows 与 Linux CI；之后再制作 Docker CPU 镜像。
- [ ] 清理三个上游模块中不再使用的实验脚本、旧缓存实现和冗余测试夹具；删除前逐项核对模块负责人仍在使用的入口。

### 已知的 Daily Paper 上游测试债务

统一环境验证时，Daily Paper 核心测试通过 540 项；完整上游测试另有 8 个失败、6 个初始化错误，暂不在本轮修改：

- 6 个会议数据初始化测试在模块导入阶段强制 `import torch`，而默认轻量环境有意不安装 torch；应改成按实际执行路径延迟导入或归入可选维护测试。
- 3 个 workflow/UI 合约仍断言旧 reranker 和 OpenCV/PaperCropper 依赖，与当前上游 workflow 及“远程 embedding + Docling”路线不一致。
- 3 个测试依赖 Daily Paper 目录作为当前工作目录，应改成相对测试文件定位 fixture/config/workflow。
- 1 个同步 embedding 测试假设 torch 永远存在，应在可选依赖缺失时跳过。
- 1 个生成文档测试依赖仓库中不存在的历史报告 fixture。
