# Daily Paper Reader — 本地化部署设计

> 日期：2026-08-06
> 状态：已确认（用户 OK）
> 目标：把 `daily-paper-reader` 从「GitHub Actions + GitHub Pages + 浏览器密钥」迁成单机本地应用。

## 一、背景与动机

用户个人/二次开发使用，要求：

1. **本地化**：后端本地跑、前端本地跑；只有 LLM 与论文抓取需要联网。
2. **不填密钥**：前端现有的「设置密钥 / URL」体系令用户反感，需删除。
3. **纯本地**：GitHub Pages 托管、CDN 资源、定时 Actions 等云端依赖全部去掉。

当前项目基建：`src/local_debug_server.py` 已能做静态托管 + 把 workflow 映射成本地 Python 子进程。本设计在其上升级。

## 二、总览

**目标形态**：本地后端常驻（托管前端 / 定时跑流水线 / 代理 AI 问答），绑定 `127.0.0.1`（仅本机可访问）。一条命令启动：

```bash
python src/local_server.py --serve
```

浏览器打开 `http://127.0.0.1:8567` 直接使用。

## 三、本地后端（升级 `local_debug_server.py` → `local_server.py`）

常驻进程承担四件事：

| 职责 | 说明 |
|------|------|
| 静态托管前端 | 复用现有逻辑，绑定 `127.0.0.1` |
| 手动跑流水线 | 保留现有 `/api/local/workflows/dispatch` |
| 定时跑流水线 | 启动时读 `config.yaml` 的 `local.schedule`，到点后台自动跑 `main.py` |
| AI 问答代理 | `/api/chat` 接收浏览器消息 → 读本地模型配置 → 转发 OpenAI 兼容端点 → SSE 流式回传 |

### 定时器

后端进程内 daemon 线程循环检查本地时间，到配置时刻触发一次流水线，带「当日只跑一次」状态标记。时间/开关放 `config.yaml`：

```yaml
local:
  port: 8567
  schedule:          # 留空/注释 = 只手动
    enabled: true
    time: "18:30"    # 后台 UTC 18:30（北京时间 02:30），同现状
    fetch_days: ""   # 留空则用 arxiv_paper_setting.days_window
```

### AI 问答配置

放 `config.yaml` 的 `local.chat` 段（与精炼所用户可不同）：

```yaml
local:
  chat:
    model: deepseek-v4-flash
    base_url: https://api.deepseek.com   # 任意 OpenAI 兼容 URL
    api_key: ${DEEPSEEK_API_KEY}         # 或从 .env 读；Key 不进 config 也可
```

前端完全不需要填 URL / Key。

## 四、前端改动（删除）

| 删除 | 文件 | 原因 |
|------|------|------|
| 开屏解锁弹窗 + 游客模式 | `index.html`、`secret.session.js` | 本地私享，无需密码 |
| 加密/解密存储 | `secret.session.js` | 同上 |
| 反馈 | `feedback.issue.js` | B 类，删除 |
| Gist 分享 | `gist-share-utils.js` | B 类，删除 |
| 订阅智能查询/管理面板 | `subscriptions.*.js`、`ui.layout-*.js` 关联 | B 类，删除 |
| CDN 资源加载/回退 | `index.html` | 全本地资源，简化 |
| 浏览器直连问答 baseUrl 填表 | `chat.discussion.js` | 改走 `/api/chat` |

前端问答调用点改为 `fetch('/api/chat', ...)` 流式读取，不再从浏览器读取 API Key。

## 五、后端 LLM 配置与联网约束

- **LLM**：`DEEPSEEK_API_KEY` 等仍从 `.env` 读（后端已自动加载），精炼/定时跑/问答共用。
- **抓论文**：arXiv API、会议抓取走公网——符合「只有 LLM 和抓论文联网」。
- **Supabase**：`config.yaml` 中 `supabase.enabled: false`，保持纯本地抓取，不连外部存储。

## 六、定时策略（已确认的默认项）

- **不做 git 操作**：定时跑完只写本地 `archive/`、`docs/`，不自动 `git commit`，保持仓库干净、可由用户自行提交。
- **先跑通再扩展**：首版以固定简单档位（`daily-now` 默认参数）跑通，后续再考虑完整可配置参数。

## 七、错误处理与测试

- 定时跑失败只记日志到 `.local-runs/<id>/run.log`，不中断服务，页面历史可见；下次照常重试。
- AI 问答代理对超时/限流/5xx 给出友好错误，不泄漏 Key。
- 现有 `pytest` 全保持通过；新增 `local_server.py` 少量单测（定时触发判定、chat 请求构造、手动/定时 dispatch 复用）。