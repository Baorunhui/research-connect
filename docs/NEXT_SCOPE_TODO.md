# 下一阶段待办：尚不符合既定使用原则的部分

本文件只记录下一轮范围。本次统一环境、公共 LLM 运行时、数据根目录和缓存索引工作不顺带修改下列业务链路，避免在模块负责人代码尚未确认时扩大改动。

## P0：模块接入与任务边界

- [ ] Daily Paper 适配最新版真实 workflow，而不是旧 `/api/recommend` 入口；补齐标准 submit、进度事件、artifact、错误回调和进程级取消。
- [ ] CitationClaw 增加 Connect Hub adapter；外部 `job_id` 必须贯穿网页、WebSocket、报告和产物。
- [ ] Connect Hub 成为唯一任务生命周期所有者；CitationClaw 和 Daily Paper 内部 task 状态只作为模块执行状态，不再各自形成第二套任务真相源。
- [ ] 子进程取消后验证整个进程树退出，防止 PDF、浏览器或 LLM 批处理成为孤儿进程。
- [ ] 修复 Connect Hub 现有模块服务只终止父进程、未持续检查取消标志的问题。

## P0：符合轻量部署原则

- [ ] Daily Paper 默认关闭 Kaggle 全量索引、本地 embedding 和自动启用的 scheduler；embedding 固定走远程 Provider。
- [ ] 用 Docling 替换遗留 PaperCropper、DocLayout-YOLO、MinerU 本地模型路径；确认 `figure_pipeline/extract_paper_figures.py` 缺失入口并收口为一个 PDF 解析实现。
- [ ] 检查并移除仓库或默认配置中的硬编码 embedding/API key。
- [ ] 定时日报仅按用户配置时间触发，不实现空闲检测、常驻 worker 或模型保温。

## P0：LLM 精筛与外部 API

- [ ] 在真实学校 API 上压测 LLM 精筛：4 并发、429 `Retry-After`、指数退避、批次拆分和失败规则降级；记录吞吐、token 与总耗时。
- [ ] 将 shared LLM usage callback 接入 Connect Hub `usage_records`，避免各模块各记一份费用。
- [ ] Daily Paper 浏览器端仍有直接 Chat Completions 调用；改由本地服务转发到 shared LLM，避免前端与 Python 各维护一套重试和配置。
- [ ] 核对 Jina、Exa MCP、Supabase 和远程 embedding 的申请步骤、免费额度、日常日报调用量和超额提示。
- [ ] 外部 API 故障统一映射为模块错误码，并通过飞书回调明确提示，不静默切换出错误结果。

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
