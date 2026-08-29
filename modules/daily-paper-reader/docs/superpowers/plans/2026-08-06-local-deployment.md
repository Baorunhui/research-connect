# Daily Paper Reader 本地化部署实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `daily-paper-reader` 从「GitHub Actions + GitHub Pages + 浏览器密钥」迁成单机本地应用：本地后端常驻（托管前端 / 定时跑流水线 / AI 问答代理），删除所有浏览器密钥、GitHub 集成与 CDN 资源加载。

**Architecture:** 升级现有 `src/local_debug_server.py` → `src/local_server.py`，一个绑定 `127.0.0.1` 的常驻进程承担四件事。前端删除开屏解锁弹窗、`secret.private`、反馈/Gist/订阅管理，AI 问答改为 `fetch('/api/chat')` 走后端代理。

**Tech Stack:** Python `http.server`（后端，无新依赖）、原生 JS（前端）、OpenAI 兼容 `/chat/completions`。

## Global Constraints

- 仅绑定 `127.0.0.1`，无游客模式/密码 — 参照 spec「三」。
- 定时跑**不做 git 操作**，纯本地读写 — 参照 spec「六」。
- 先固定简单档位（`daily-now` 默认参数）跑通，再谈完整参数 — 参照 spec「六」。
- AI 问答 baseUrl/key 放本地 `config.yaml`/`.env`，前端**不得**出现 API Key — 参照 spec「三/四」。
- 现有 `pytest` 全保持通过 — 参照 spec「七」。
- 所有 commit 末追加 `Co-Authored-By: xixi <3495302215@qq.com>`。
- commit 身份用仓库既有 `ziwen <244824379@qq.com>`（本地已配置）。

---

### Task 1: config.yaml 增加 `local` 配置段

**Files:**
- Modify: `config.yaml`（追加 `local:` 段）
- Modify: `.env.example`（追加 `local` 注释说明）

**Interfaces:**
- Consumes: 无（独立）。
- Produces: `local.port`（int）、`local.schedule.enabled`（bool）、`local.schedule.time`（`"HH:MM"`）、`local.schedule.fetch_days`（str）、`local.chat.model/base_url/api_key`。后续 Task 2/3 从 config 读取这些键。

- [ ] **Step 1: 在 config.yaml 末尾追加 local 段**

在 `config.yaml` 末尾（`source_backends.arxiv` 之后）追加：

```yaml
local:
  port: 8567
  schedule:            # 定时自动跑流水线（每天一次）
    enabled: true
    time: "18:30"      # 后台 UTC 18:30（北京时间 02:30），同现状
    fetch_days: ""     # 留空则用 arxiv_paper_setting.days_window
  chat:                # AI 问答模型；仅存在本机，前端不读
    model: deepseek-v4-flash
    base_url: https://api.deepseek.com   # 任意 OpenAI 兼容 URL
    api_key: ""        # 留空则从 .env 读 DEEPSEEK_API_KEY
```

- [ ] **Step 2: 在 .env.example 追加说明注释**

在 `.env.example` 末尾追加，提醒 Key 归口：

```
# ===== 本地服务（local_server.py）=====
# local.chat.api_key 留空时，从这里读取 DEEPSEEK_API_KEY 作为 AI 问答 Key；
# 也可直接在 config.yaml 的 local.chat.api_key 填写（仅本机，不提交）。
```

- [ ] **Step 3: 验证 YAML 可解析**

Run: `python -c "import yaml,sys; d=yaml.safe_load(open('config.yaml',encoding='utf-8')); assert 'local' in d and 'chat' in d['local'] and 'schedule' in d['local']; print('OK', d['local']['port'])"`
Expected: `OK 8567`

- [ ] **Step 4: Commit**

```bash
git add config.yaml .env.example
git commit -m "config: 增加本地服务 local 配置段

Co-Authored-By: xixi <3495302215@qq.com>"
```

---

### Task 2: local_server.py — 定时调度器（新增模块）

**Files:**
- Create: `src/local_scheduler.py`
- Test: `tests/test_local_scheduler.py`

**Interfaces:**
- Consumes: 无。
- Produces:
  - `class LocalScheduler`: `__init__(self, time_str: str, trigger_fn: Callable[[], None])`
  - `scheduler.next_run_after(now: datetime) -> datetime` — 静态/纯函数计算下一个触发时刻（带「当日已触发」状态传入）。
  - `scheduler.should_fire(now: datetime, already_fired_today: bool|None) -> str|None` — 返回 `"fire"` / `"wait"` / `"skip_today"`。
  - `class SchedulerThread(threading.Thread)`: `__init__(self, time_str, on_fire: Callable[[], None])`，daemon；`run()` 循环；`stop()` 退出。

- [ ] **Step 1: 写失败测试（触发判定纯函数）**

`tests/test_local_scheduler.py`:

```python
from datetime import datetime
from src.local_scheduler import LocalScheduler


def test_next_run_after_same_day_future():
    s = LocalScheduler("18:30", trigger_fn=lambda: None)
    now = datetime(2026, 8, 6, 10, 0)
    nxt = s.next_run_after(now)
    assert nxt == datetime(2026, 8, 6, 18, 30)


def test_next_run_after_past_rolls_to_next_day():
    s = LocalScheduler("18:30", trigger_fn=lambda: None)
    now = datetime(2026, 8, 6, 20, 0)
    nxt = s.next_run_after(now)
    assert nxt == datetime(2026, 8, 7, 18, 30)


def test_should_fire_returns_fire_when_matching_and_not_fired():
    s = LocalScheduler("18:30", trigger_fn=lambda: None)
    now = datetime(2026, 8, 6, 18, 30)
    assert s.should_fire(now, already_fired_today=None) == "fire"


def test_should_fire_skip_when_already_fired_today():
    s = LocalScheduler("18:30", trigger_fn=lambda: None)
    now = datetime(2026, 8, 6, 18, 30)
    assert s.should_fire(now, already_fired_today=True) == "skip_today"
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_local_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.local_scheduler'`

- [ ] **Step 3: 实现最小模块**

`src/local_scheduler.py`:

```python
"""本地定时调度器：在指定本地时刻触发一次回调，当日只触发一次。"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional


class LocalScheduler:
    def __init__(self, time_str: str, trigger_fn: Callable[[], None]) -> None:
        hour, minute = self._parse_time(time_str)
        self._hour = hour
        self._minute = minute
        self._trigger_fn = trigger_fn

    @staticmethod
    def _parse_time(time_str: str) -> tuple[int, int]:
        parts = str(time_str or "").strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"无效调度时间: {time_str!r}")
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"调度时间超出范围: {time_str!r}")
        return hour, minute

    def _target(self) -> timedelta:
        return timedelta(hours=self._hour, minutes=self._minute)

    def next_run_after(self, now: datetime) -> datetime:
        today_target = now.replace(hour=self._hour, minute=self._minute, second=0, microsecond=0)
        if now < today_target:
            return today_target
        return today_target + timedelta(days=1)

    def should_fire(self, now: datetime, already_fired_today: Optional[bool]) -> str:
        """返回 'fire' | 'wait' | 'skip_today'。"""
        if now.hour == self._hour and now.minute == self._minute:
            if already_fired_today:
                return "skip_today"
            return "fire"
        return "wait"

    def trigger(self) -> None:
        self._trigger_fn()


class SchedulerThread(threading.Thread):
    def __init__(self, time_str: str, on_fire: Callable[[], None]) -> None:
        super().__init__(daemon=True)
        self._scheduler = LocalScheduler(time_str, trigger_fn=on_fire)
        self._stop = threading.Event()
        self._fired_today: Optional[datetime] = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            now = datetime.now()
            decision = self._scheduler.should_fire(now, self._fired_today is not None and self._fired_today.date() == now.date())
            if decision == "fire":
                self._fired_today = now
                try:
                    self._scheduler.trigger()
                except Exception:
                    # 本调度器失败不影响服务主循环；由触发方负责记日志
                    pass
            self._stop.wait(20)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_local_scheduler.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add src/local_scheduler.py tests/test_local_scheduler.py
git commit -m "feat: 本地定时调度器

Co-Authored-By: xixi <3495302215@qq.com>"
```

---

### Task 3: local_server.py — 从 local_debug_server.py 升级

**Files:**
- Create: `src/local_server.py`（基于 `src/local_debug_server.py` 拷贝扩展）
- Test: `tests/test_local_server.py`

**Interfaces:**
- Consumes: `src/local_scheduler.py` 的 `SchedulerThread` / `LocalScheduler`；`config.yaml` 的 `local.*`。
- Produces:
  - `main()`（`--serve` 默认）：启动 `ThreadingHTTPServer((127.0.0.1, port), Handler)` + 启动 `SchedulerThread` + 打印地址。
  - `Handler` 新增 `do_POST /api/chat`（SSE 流式代理）与 `do_GET /api/chat/config`（返回模型名供前端下拉）。
  - `build_chat_command(...)`（复用 `local_debug_server.build_command` 的 daily-now 分支）。

- [ ] **Step 1: 写失败测试（服务初始化 + chat 请求构造 + 定时触发接线）**

`tests/test_local_server.py`:

```python
import json
from src.local_scheduler import LocalScheduler
from src.local_server import build_chat_request_payload


def test_build_chat_request_payload_shape():
    payload = build_chat_request_payload(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["stream"] is True


def test_scheduler_ties_to_port_config():
    # 通过解析 local.port 确认服务配置存在（防止 config 段缺失）
    import yaml
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    assert "local" in cfg
    assert cfg["local"]["port"] == 8567
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_local_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.local_server'`

- [ ] **Step 3: 创建 local_server.py（拷贝 + 扩展 local_debug_server.py）**

将 `src/local_debug_server.py` 全文复制为 `src/local_server.py`，然后做以下扩展：

**3a. 追加 chat 请求构造纯函数**（文件内顶层）：

```python
def build_chat_request_payload(model: str, messages: list[dict], *, max_tokens: int | None = None) -> dict:
    payload: dict = {"model": model, "messages": messages, "stream": True}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    return payload
```

**3b. Handler 增加 `/api/chat/config`（GET）与 `/api/chat`（POST 流式代理）**：

```python
def _chat_config(self):
    cfg = _load_local_chat_config()
    return self._json({"ok": True, "model": cfg["model"], "base_url": cfg["base_url"]})

def do_POST(self):
    parsed = urlparse(self.path)
    if parsed.path == "/api/chat":
        return self._proxy_chat()
    if parsed.path == "/api/local/config":
        return self._save_local_config()
    # ... 保留原有其它分支，仅在此列表追加 /api/chat
```

`_proxy_chat()` 实现要点（写完整代码）：

```python
def _proxy_chat(self):
    import json, urllib.request
    length = int(self.headers.get("Content-Length") or "0")
    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
    cfg = _load_local_chat_config()
    api_key = _resolve_chat_api_key(cfg)
    body = build_chat_request_payload(cfg["model"], payload.get("messages") or [], max_tokens=None)
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    upstream = urllib.request.urlopen(req, timeout=120)
    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream")
    self.send_header("Cache-Control", "no-cache")
    self.send_header("Access-Control-Allow-Origin", "*")
    self.end_headers()
    try:
        while True:
            chunk = upstream.read(4096)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()
    finally:
        upstream.close()
```

**3c. 配置读取辅助函数**：

```python
def _load_local_chat_config() -> dict:
    import yaml as _yaml
    cfg = _yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    chat = (cfg.get("local") or {}).get("chat") or {}
    return {
        "model": str(chat.get("model") or "").strip(),
        "base_url": str(chat.get("base_url") or "").strip().rstrip("/"),
        "api_key": str(chat.get("api_key") or "").strip(),
    }

def _resolve_chat_api_key(cfg: dict) -> str:
    key = cfg.get("api_key") or ""
    if not key:
        key = os.environ.get("DEEPSEEK_API_KEY") or ""
    return key
```

**3d. main() 启动定时器**（绑定 `127.0.0.1`）：

```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8567)
    parser.add_argument("--no-schedule", action="store_true", help="不启动定时器")
    args = parser.parse_args()
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)

    sched_thread = None
    if not args.no_schedule and _schedule_enabled():
        on_fire = lambda: _dispatch_daily_pipeline()
        sched_thread = SchedulerThread(_schedule_time(), on_fire)
        sched_thread.start()
        print(f"[local-server] 定时器已启动，每天 {_schedule_time()} 自动跑流水线", flush=True)

    print(f"[local-server] serving http://{args.host}:{args.port}", flush=True)
    print("[local-server] AI 问答通过 /api/chat 本地代理", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if sched_thread:
            sched_thread.stop()
```

辅助：`_schedule_enabled()` / `_schedule_time()` / `_dispatch_daily_pipeline()` 从 `config.yaml` 的 `local.schedule` 读取，`_dispatch_daily_pipeline` 复用 `RUN_STORE.create("daily-now", "daily-paper-reader.yml", {}, cmd, config=None, secret=None)`，其中 `cmd = build_command("daily-now", "daily-paper-reader.yml", {"run_enrich": False})`。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_local_server.py -v`
Expected: PASS（2 passed，且 Task 2 的 4 个测试也仍通过）

- [ ] **Step 5: 冒烟验证服务能启动**

Run: `timeout 8 python -c "import time; from src.local_server import main; import threading; t=threading.Thread(target=main,daemon=True); t.start(); time.sleep(3); t.join(timeout=5)"`
Expected: 打印 `[local-server] serving http://127.0.0.1:8567` 与定时器启动行后超时退出（无 traceback）。

- [ ] **Step 6: Commit**

```bash
git add src/local_server.py tests/test_local_server.py
git commit -m "feat: 本地服务后端（托管/定时/AI 问答代理）

Co-Authored-By: xixi <3495302215@qq.com>"
```

---

### Task 4: 前端 — 删除解锁弹窗、secret.session、CDN 资源加载

**Files:**
- Modify: `index.html`（删除 secret-gate overlay、DPRDisableCdn/CDN 加载逻辑、`secret.session.js` 引用、`simple` 模式资源列表精简为本地）
- Delete: `app/secret.session.js`
- Test: `tests/test_index_asset_loader.js`（更新）

**Interfaces:**
- Consumes: 无。
- Produces: `index.html` 只加载本地 `app/*.js`，无 `secret-gate-overlay`、无 CDN；删除 `window.DPR_CDN_ACTIVE`/`DPR_ASSET_BASE` 相关引用（后续 Task 5-7 依赖此 `index.html` 作为新的资源入口）。

- [ ] **Step 1: 更新资产加载测试（预期不再加载 secret.session.js/feedback/gist/subscriptions）**

`tests/test_index_asset_loader.js` 中，把断言改为：加载列表中**不包含** `app/secret.session.js`、`app/feedback.issue.js`、`app/gist-share-utils.js`、`app/subscriptions.*.js`；且 `index.html` 中不再出现 `secret-gate-overlay` 与 `DPR_CDN_ACTIVE`。

- [ ] **Step 2: 运行确认失败**

Run: `node tests/test_index_asset_loader.js`
Expected: FAIL（断言不满足）

- [ ] **Step 3: 精简 index.html 资源加载**

删除：
- `<div id="secret-gate-overlay" ...>` 整块（原 499-527 行）及其 `secret-gate-hidden` 相关 CSS 与 `DPRShowInitialLoadError` 里对 `secretGate` 的处理。
- 第二段 `<script>` 内 CDN 版本化加载逻辑（`DPRLoadAssets` 可保留，但 `candidateBase` 恒为 `''`、`preferCdn=false`；直接删掉 `DEFAULT_CDN_BASE`、`normalizeAssetVersion`、`cdnUrl` 等）；凡本地资源统一 `localUrl`。

最终 `DPRLoadAssets` 列表改为（全本地，无 CDN）：

```js
window.DPRLoadAssets([
  { type: 'style', path: 'app/vendor/docsify/4/lib/themes/vue.css' },
  { type: 'style', path: 'app/app.css' },
  { type: 'script', path: 'app/vendor/js-yaml/4.1.0/dist/js-yaml.min.js' },
  { type: 'script', path: 'app/llm-config-utils.js' },
  { type: 'script', path: 'app/read-state-sync.js' },
  { type: 'script', path: 'app/dpr-sidebar.js' },
  { type: 'script', path: 'app/docsify-plugin.js' },
  { type: 'script', path: 'app/vendor/docsify/4/lib/docsify.min.js' },
]).then(function () {
  document.addEventListener('dpr-docsify-ready', function loadDeferredAssetsOnce() {
    document.removeEventListener('dpr-docsify-ready', loadDeferredAssetsOnce);
    window.DPRLoadAssets([
      { type: 'style', path: 'app/vendor/katex/0.16.9/dist/katex.min.css' },
      { type: 'script', path: 'app/vendor/katex/0.16.9/dist/katex.min.js' },
      { type: 'script', path: 'app/vendor/katex/0.16.9/dist/contrib/auto-render.min.js' },
      { type: 'script', path: 'app/chat.discussion.js' },
      { type: 'script', path: 'app/workflows.runner.js' },
    ]);
  });
});
```

（`site-stats.js`、`conference-stats.json`、`ui.layout-and-subscriptions-entry.js` 按其各自归属：site-stats 保留为本地加载；订阅/反馈/gist 相关 script 移除。）

- [ ] **Step 4: 删除 secret.session.js 并清理引用**

```bash
git rm app/secret.session.js
```
并在 `app/docsify-plugin.js`、`app/chat.discussion.js`、`app/dpr-sidebar.js`、`app/site-stats.js`、`app/workflows.runner.js` 中删除/替换所有 `window.decoded_secret_private` 读取（Task 5-7 逐个处理，本 Task 至少确保不再 `require`/加载它）。

- [ ] **Step 5: 运行确认通过**

Run: `node tests/test_index_asset_loader.js`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add index.html app/secret.session.js tests/test_index_asset_loader.js
git commit -m "feat: 前端移除解锁弹窗/密钥存储/CDN 资源加载

Co-Authored-By: xixi <3495302215@qq.com>"
```

---

### Task 5: 前端 — 删除反馈、Gist、订阅管理

**Files:**
- Delete: `app/feedback.issue.js`、`app/gist-share-utils.js`、`app/subscriptions.manager.js`、`app/subscriptions.smart-query.js`、`app/subscriptions.github-token.js`、`app/subscriptions.keywords.js`、`app/subscriptions.zotero.js`、`app/subscriptions.tracked-papers.js`、`app/ui.layout-and-subscriptions-entry.js`
- Modify: `app/dpr-sidebar.js`（移除 feedback 按钮 HTML 与 `dpr-open-feedback` dispatch）、`app/docsify-plugin.js`（移除这些模块调用）
- Test: `tests/test_feedback_issue.js`（删除）、`tests/test_gist_share_markdown.js`（删除）、`tests/test_subscriptions_manager.js`（删除）、`tests/test_subscriptions_smart_query.js`（删除）；新增 `tests/test_index_asset_loader.js` 断言不含这些 script。

**Interfaces:**
- Consumes: Task 4 的 `index.html`（已不引用这些文件）。
- Produces: 前端不再存在反馈/Gist/订阅入口与代码路径。

- [ ] **Step 1: 删除 JS 文件与对应测试**

```bash
git rm app/feedback.issue.js app/gist-share-utils.js app/subscriptions.manager.js app/subscriptions.smart-query.js app/subscriptions.github-token.js app/subscriptions.keywords.js app/subscriptions.zotero.js app/subscriptions.tracked-papers.js app/ui.layout-and-subscriptions-entry.js
git rm tests/test_feedback_issue.js tests/test_gist_share_markdown.js tests/test_subscriptions_manager.js tests/test_subscriptions_smart_query.js
```

- [ ] **Step 2: 清理 dpr-sidebar.js 中的 feedback 按钮**

删除 `app/dpr-sidebar.js:1484` 附近的 `dpr-sidebar-feedback-btn` 按钮 HTML 生成，及 `:1547` 的 `dispatchNamedEvent('dpr-open-feedback')`、`:2303` 附近的 `dpr-sidebar-feedback-btn` 点击分支。

- [ ] **Step 3: 清理 docsify-plugin.js / chat 中的订阅/反馈/secret 引用**

逐一删除 `docsify-plugin.js`、`chat.discussion.js`、`site-stats.js`、`workflows.runner.js`、`dpr-sidebar.js` 中对 `decoded_secret_private`、`SubscriptionsManager`、`FeedbackIssue`、`GistShare`、`subscriptions.*` 的调用/读取（改为不再读取密钥）。

- [ ] **Step 4: 验证前端脚本无残留引用**

Run: `grep -rn "decoded_secret_private\|SubscriptionsManager\|dpr-open-feedback\|GistShare\|subscriptions\.smart\|feedback\.issue\|gist-share" app/*.js`
Expected: 无输出（或仅剩与功能无关注释）

- [ ] **Step 5: 运行 Python + JS 测试**

Run: `python -m pytest 2>&1 | tail -5` 与 `node tests/test_index_asset_loader.js`
Expected: pytest 全过；asset loader 断言通过。

- [ ] **Step 6: Commit**

```bash
git add -A app tests
git commit -m "feat: 删除反馈/Gist/订阅管理模块

Co-Authored-By: xixi <3495302215@qq.com>"
```

---

### Task 6: 前端 — AI 问答改走 /api/chat

**Files:**
- Modify: `app/chat.discussion.js`（模型配置来源改为 `/api/chat/config`；发送改为 `fetch('/api/chat')` 流式读取，去掉 API Key 校验）
- Test: `tests/test_llm_config_utils.js`（更新：chat 不再依赖 `decoded_secret_private`）

**Interfaces:**
- Consumes: Task 3 的 `GET /api/chat/config` 与 `POST /api/chat`、Task 4/5 清理后的挂载。
- Produces: `chat.discussion.js` 从后端取模型名、向后端 `POST` SSE 流、无任何 `apiKey` 读取。后续 Task 8 依赖其正常 SSE 渲染。

- [ ] **Step 1: 更新测试（chat 模型下拉来自后端配置而非 secret）**

`tests/test_llm_config_utils.js`：新增断言 `resolveChatModels` 不再被 chat 依赖（或改为断言存在 `fetch('/api/chat/config')` 的常量），并跑通。

- [ ] **Step 2: 替换模型解析**（`chat.discussion.js:49-55` 附近）

把从 `secret.chatLLMs` 解析 `chatModels` 的逻辑，改为启动时 `GET /api/chat/config` 获取单一模型名：

```js
async function resolveChatConfig() {
  try {
    const resp = await fetch('/api/chat/config');
    if (!resp.ok) return null;
    const data = await resp.json();
    return (data && data.model) ? data.model : null;
  } catch { return null; }
}
```

- [ ] **Step 3: 替换发送逻辑**（原 1054-1249 的 apiKey/baseUrl/endpoint 解析）

删除 `apiKey` 判空块（1062-1073），删除 `baseUrl`/`endpoint` 构造（1088-1103），`doChatFetch` 改为：

```js
const doChatFetch = async (payload) => fetch('/api/chat', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  signal: controller.signal,
  body: JSON.stringify(payload),
});
```

`model` 由 `resolveChatConfig()` 返回；`buildStreamingChatPayload(baseUrl, model, messages)` 中 `baseUrl` 传空字符串、`model` 传后端模型名，仅保留 `max_tokens` 逻辑。

- [ ] **Step 4: 运行确认通过**

Run: `node tests/test_llm_config_utils.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/chat.discussion.js tests/test_llm_config_utils.js
git commit -m "feat: AI 问答改走本地后端 /api/chat 代理

Co-Authored-By: xixi <3495302215@qq.com>"
```

---

### Task 7: 前端 — 移除其余 secret 依赖（docsify-plugin / sidebar / workflows / site-stats）

**Files:**
- Modify: `app/docsify-plugin.js`、`app/dpr-sidebar.js`、`app/workflows.runner.js`、`app/site-stats.js`
- Test: `tests/test_dpr_sidebar_v2.js`、`tests/test_site_stats.js`（如有 secret 依赖则更新）

**Interfaces:**
- Consumes: Task 4/5 的清理。
- Produces: 代码库全局不再读取 `decoded_secret_private` 或任何浏览器密钥。

- [ ] **Step 1: 全局 grep 定位残留**

Run: `grep -rn "decoded_secret_private\|secret.private\|DPRSecret\|secret.session\|chatLLMs" app/*.js`
Expected: 列出所有残留位置（应已被 Task 4-6 清空，如有则处理）。

- [ ] **Step 2: 逐处替换**

将 `docsify-plugin.js:1230-1232`（从 secret.github.token 发请求）、`:3258-3259`、`site-stats.js:215`、`workflows.runner.js:99/119/338` 中对 `decoded_secret_private` 的读取移除。若某功能（如 read-state-sync 的 supabase 写入、workflows 的 token）依赖 secret，则改为从后端配置或直接移除该分支。

- [ ] **Step 3: 运行测试**

Run: `node tests/test_dpr_sidebar_v2.js && node tests/test_site_stats.js`
Expected: PASS

- [ ] **Step 4: 全局无残留验证 + pytest**

Run: `grep -rn "decoded_secret_private\|secret.private" app/*.js; echo "---"; python -m pytest 2>&1 | tail -3`
Expected: grep 无输出；pytest 摘要显示全通过（或仅有与本任务无关的既有失败）。

- [ ] **Step 5: Commit**

```bash
git add -A app && git commit -m "feat: 移除前端浏览器密钥依赖

Co-Authored-By: xixi <3495302215@qq.com>"
```

---

### Task 8: 端到端冒烟 + 文档收尾

**Files:**
- Modify: `README.md`、`AGENTS.md`（简述本地化启动方式）、`CLAUDE.md`（更新「十二、前端核心」里关于 secret/subscriptions 的描述）
- Create（可选）: `scripts/run_local.sh`

**Interfaces:**
- Consumes: Task 1-7 全部。
- Produces: 一条命令启动的本地服务 + 更新后的文档。

- [ ] **Step 1: 创建启动脚本**

`scripts/run_local.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec python src/local_server.py --serve
```

- [ ] **Step 2: 启动服务冒烟**

Run: `python src/local_server.py --serve`（后台运行，`curl http://127.0.0.1:8567/api/local/health` 与 `curl http://127.0.0.1:8567/api/chat/config`）
Expected: health 返回 `{"ok":true,...}`；chat/config 返回 `{"ok":true,"model":"deepseek-v4-flash",...}`。

- [ ] **Step 3: 更新文档**

README/AGENTS/CLAUDE.md 追加本地化部署小节：`python src/local_server.py --serve` 启动，浏览器开 `http://127.0.0.1:8567`；说明密钥统一放本机 `config.yaml`/`.env`，AI 问答走本地代理，定时在 `local.schedule` 配置。

- [ ] **Step 4: 全量测试**

Run: `python -m pytest 2>&1 | tail -5` 与 `for f in tests/test_*.js; do node "$f" || exit 1; done`
Expected: 全通过。

- [ ] **Step 5: Commit**

```bash
git add scripts/run_local.sh README.md AGENTS.md CLAUDE.md
git commit -m "docs: 本地化部署启动脚本与文档

Co-Authored-By: xixi <3495302215@qq.com>"
```