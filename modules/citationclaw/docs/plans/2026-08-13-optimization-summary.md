# CitationClaw 优化记录

> 日期: 2026-08-13
> 目标: 配置 CitationClaw 在免费/低成本方案下稳定运行，并解决高引用论文导致的崩溃问题

---

## 一、当前配置

| 配置项 | 值 | 说明 |
|--------|-----|------|
| `openai_model` | `glm-4-plus` | 智谱主模型，联网搜索 |
| `renowned_scholar_model` | `deepseek-v4-flash` | USTC 免费轻量模型，非推理，2-3s/次 |
| `author_verify_model` | `glm-4-plus` | 作者校验模型 |
| `search_backend` | `ustc_function` | USTC function-calling tool-loop |
| `parallel_author_search` | `8` | 并行作者搜索数 |
| `s2_api_key` | `s2k-aHCv...` | Semantic Scholar API Key |
| `scraper_api_keys` | `[]` | ScraperAPI 额度已用完，改用免费 Bing |

### 模型选择理由

| 模型 | 优点 | 缺点 | 是否采用 |
|------|------|------|----------|
| `qwen3.6-chat` (推理) | 内容质量好 | 50-100s/次，token 耗尽导致空结果 | ✗ |
| `deepseek-v4-flash` (非推理) | 2-3s/次，100% 覆盖 | 早期返回 "let me try..." 中间思考 | ✓ (已修复) |

### 搜索后端架构

```
用户查询
  │
  ▼
chat_with_search() (llm_tool_loop.py)
  │
  ├── LLM 决定调用 search_web 工具
  │
  └── execute_web_search()
        │
        ├── 1. Semantic Scholar 论文搜索
        ├── 2. Semantic Scholar 作者搜索
        └── 3. Fallback: Bing 免费爬取 (cn.bing.com)
```

---

## 二、157 篇真实数据测试结果

| 指标 | 值 |
|------|-----|
| 论文总数 | 157 |
| 覆盖率 | 156/157 (99%) |
| 文件大小 | 926KB JSON / 865KB JSONL |
| 知名学者检出 | 21 篇 |
| 失败论文 | 1 篇（中文标题，S2/Bing 搜不到） |
| 耗时 | ~112 分钟（16:22 → 18:14） |
| "let me try..." 问题 | 0 次 |

### 16 篇测试对比

| 测试 | 模型 | 覆盖率 | 文件大小 | 内容质量 |
|------|------|--------|----------|----------|
| qwen3.6-chat r=3+Bing | qwen3.6-chat | 12/16 (75%) | 79KB | 好 |
| deepseek (修复前) | deepseek-v4-flash | 16/16 (100%) | 43KB | "let me try..." 填充 |
| **deepseek (修复后)** | **deepseek-v4-flash** | **16/16 (100%)** | **148KB** | **优秀** |

---

## 三、优化清单

### Part A: 功能配置修复

| # | 优化 | 文件 | 行号 |
|---|------|------|------|
| 1 | 模型配置修正: `openai_model` → `glm-4-plus` | `config.json` | - |
| 2 | 轻量模型: `renowned_scholar_model` → `deepseek-v4-flash` | `config.json` | - |
| 3 | 并行数: 1 → 8 | `config.json` | - |
| 4 | S2 API Key 填入并验证 HTTP 200 | `config.json` | - |
| 5 | light_client offload: 非联网调用走 USTC 免费客户端 | `author_searcher.py` | 89-113 |
| 6 | 前端加 `glm-4-plus` 选项 | `index.html` | 164 |
| 7 | 前端删除强制回填 Gemini 默认值 | `main.js` | 668, 815 |
| 8 | heartbeat "超时" → "中止" | `pdf_downloader.py` | 2170 |

### Part B: 搜索后端搭建

| # | 优化 | 文件 | 说明 |
|---|------|------|------|
| 9 | 新建 tool-use 循环 + `search_web` 工具 | `llm_tool_loop.py` | `chat_with_search()` |
| 10 | S2 作者搜索方法 | `s2_client.py` | `search_author()` |
| 11 | 搜索路由: `ustc_function` → `chat_with_search()` | `author_searcher.py` | 352-367 |
| 12 | 配置加 `search_backend` 字段 | `config_manager.py`, `main.py`, `config.json` | - |
| 13 | Bing 免费搜索 fallback | `llm_tool_loop.py` | `_bing_search()` |

### Part C: 质量修复

| # | 优化 | 文件 | 说明 |
|---|------|------|------|
| 14 | `_extract_content()`: reasoning_content 回退 + 剥离 XML 块 | `llm_tool_loop.py` | 69-80 |
| 15 | "let me try..." 检测 + 强制最终答案 | `llm_tool_loop.py` | 374-405 |
| 16 | system prompt 引导直接给答案 | `llm_tool_loop.py` | 355-363 |
| 17 | max_tokens: 2000 → 4000 | `llm_tool_loop.py` | 340 |
| 18 | max_tool_rounds: 3 (曾改为 2 导致质量下降，已恢复) | `llm_tool_loop.py` | 338 |
| 19 | max rounds 耗尽时追加明确指令 | `llm_tool_loop.py` | 457-464 |

### Part D: 高引用崩溃优化（核心）

| # | 优化 | 文件 | 说明 |
|---|------|------|------|
| 20 | **全局速率限制器** (18 次/分钟) | `llm_tool_loop.py` | `_rate_limit()` 滚动窗口 |
| 21 | **429 自动重试+指数退避** (5s→10s→20s→40s) | `llm_tool_loop.py` | `_call_llm_with_retry()` |
| 22 | **并行模式增量保存** | `author_searcher.py` | `search()` 每篇完成即写盘 |
| 23 | **限流不取消全任务** | `author_searcher.py` | `_call_llm()` 区分 rate-limit vs quota |
| 24 | **超时随论文数动态调整** | `author_searcher.py` | `max(7200, total * 60)` |
| 25 | **httpx.AsyncClient 全局共享** | `llm_tool_loop.py` | `_get_shared_http_client()` |
| 26 | **chat_with_search 整体超时** (300s) | `llm_tool_loop.py` | `asyncio.wait_for()` |

---

## 四、高引用崩溃优化详解

### 问题根因

每篇论文需要 5+N 次 USTC API 调用（search_fn 3轮 + format_fn + 自引检测 + search_fn 3轮 + ...），而 USTC 限速 20 次/分钟。

```
单篇论文 API 调用链:
  search_fn (Step 1: 作者列表)     → 3-4 次 LLM 调用 (tool-loop)
  format_fn (第一作者机构 JSON)     → 1 次
  _check_self_citation_llm         → 1 次
  search_fn (Step 2: 详细信息)     → 3-4 次 (tool-loop)
  chat_fn (知名学者筛选)           → 1 次
  format_fn × N (每位学者格式化)   → N 次
  ─────────────────────────────────
  合计: 8-10 + N 次/篇
```

高引用论文（500+ 施引文献）→ 500 × 10 = 5000 次 API 调用 → 8 并行瞬间打爆 20 次/分钟限制 → 429 瀑布 → 全部 ERROR。

### 优化 1: 全局速率限制器

```python
# llm_tool_loop.py
_rate_lock = asyncio.Lock()
_rate_times: list[float] = []

async def _rate_limit(max_per_minute: int = 18):
    """确保 60 秒滚动窗口内不超过 max_per_minute 次 LLM 调用"""
    async with _rate_lock:
        now = time.monotonic()
        cutoff = now - 60.0
        _rate_times = [t for t in _rate_times if t > cutoff]
        if len(_rate_times) >= max_per_minute:
            wait = 60.0 - (now - _rate_times[0]) + 0.5
            if wait > 0:
                await asyncio.sleep(wait)
        _rate_times.append(time.monotonic())
```

- 所有 LLM 调用前先 `_rate_limit()`
- 8 个并行任务共享同一个速率窗口
- 设 18 次/分钟（略低于 USTC 的 20 次限制，留安全余量）

### 优化 2: 429 自动重试+指数退避

```python
# llm_tool_loop.py
async def _call_llm_with_retry(client, model, msgs, log, max_tokens, tools=None, **kwargs):
    max_retries = 4
    for attempt in range(max_retries + 1):
        await _rate_limit()
        try:
            resp = await client.chat.completions.create(...)
            return resp
        except Exception as e:
            is_429 = "429" in str(e) or "rate" in str(e).lower()
            if is_429 and attempt < max_retries:
                wait = (2 ** attempt) * 5  # 5s, 10s, 20s, 40s
                await asyncio.sleep(wait)
                continue
            raise
```

### 优化 3: 并行模式增量保存

```python
# author_searcher.py - search() 方法
# 优化前: 全部完成后才写文件，崩溃 = 全丢
# 优化后: 每完成一篇就写盘，崩溃也只丢未完成的

write_lock = asyncio.Lock()

async def _run_and_save(task_info):
    result = await self._search_single_paper(...)
    count_num, record_dict = result
    if record_dict:
        async with write_lock:
            with open(output_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps({count_num: record_dict}, ensure_ascii=False) + '\n')
    return count_num
```

### 优化 4: 限流不取消全任务

```python
# author_searcher.py - _call_llm() 方法
# 优化前: 3 次限流 → cancel_event.set() → 取消所有任务
# 优化后: 区分 rate-limit (退避重试) vs quota (才取消)

is_rate_limit = 'rate' in error_msg or '429' in error_msg
is_quota = 'quota' in error_msg

if is_rate_limit:
    wait = min(10 * quota_failures, 60)  # 10s, 20s, 30s... max 60s
    await asyncio.sleep(wait)
    continue  # 重试，不取消

if is_quota and quota_failures >= 3:
    self.cancel_event.set()  # 真正配额耗尽才取消
    return 'ERROR'
```

### 优化 5: 超时随论文数动态调整

```python
# author_searcher.py
# 优化前: 固定 7200s (2h)，500篇会被误杀
# 优化后: max(7200, total_papers * 60)
#   157篇 → 9420s (2.6h)
#   500篇 → 30000s (8.3h)

wait_timeout = max(7200, total_papers * 60)
done, pending = await asyncio.wait(tasks, timeout=wait_timeout)
```

### 优化 6: httpx.AsyncClient 全局共享

```python
# llm_tool_loop.py
# 优化前: 每次 chat_with_search 新建 httpx.AsyncClient → 数千个短连接
# 优化后: 全局共享一个 client，连接池复用

_shared_http_client: Optional[httpx.AsyncClient] = None

async def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        _shared_http_client = httpx.AsyncClient(
            trust_env=False, timeout=25.0,
            limits=httpx.Limits(max_connections=20, ...),
        )
    return _shared_http_client
```

### 优化 7: chat_with_search 整体超时

```python
# llm_tool_loop.py
# 优化前: 无整体超时，单篇可能卡数分钟阻塞整个批次
# 优化后: 300s 整体超时，超时返回已有信息

async def chat_with_search(..., overall_timeout: float = 300.0, ...):
    try:
        return await asyncio.wait_for(
            _chat_with_search_inner(...),
            timeout=overall_timeout,
        )
    except asyncio.TimeoutError:
        return "ERROR"
```

---

## 五、崩溃场景对比

| 场景 | 优化前 | 优化后 |
|------|--------|--------|
| 8 并行打 USTC | 429 瀑布 → 全 ERROR | 速率限制器自动排队 (18次/分钟) |
| 中途崩溃 | 全部结果丢失 | 增量保存，已完成的保留 |
| 500 篇论文 | 2h 硬超时 → 任务被取消 | 8.3h 动态上限，不误杀 |
| 单篇 LLM 卡死 | 阻塞整个并行批次 | 300s 超时自动跳过 |
| 3 次限流 | `cancel_event.set()` 取消所有 | 只退避重试，不取消 |
| 大量短连接 | 每次新建 httpx client | 全局共享连接池 |

---

## 六、修改的文件清单

| 文件 | 修改内容 |
|------|----------|
| `config.json` | 模型名 + S2 key + search_backend + scraper_keys=[] + parallel=8 |
| `citationclaw/core/llm_tool_loop.py` | 新建: tool-use 循环 + S2/Bing 搜索 + 速率限制器 + 429 重试 + httpx 共享 + 整体超时 |
| `citationclaw/core/author_searcher.py` | light_client offload + 增量保存 + 限流不取消 + 动态超时 |
| `citationclaw/core/s2_client.py` | `search_author()` 方法 |
| `citationclaw/app/config_manager.py` | `search_backend` 字段 |
| `citationclaw/app/main.py` | `ConfigUpdate` 加 `search_backend` |
| `citationclaw/templates/index.html` | `glm-4-plus` 选项 |
| `citationclaw/static/js/main.js` | 删除强制回填 Gemini 默认值 |
| `citationclaw/skills/phase2_author_intel.py` | 传 light_*/search_backend/scraper_api_keys 参数 |
| `citationclaw/core/pdf_downloader.py` | heartbeat "超时" → "中止" |

---

## 七、待考虑的后续优化

| # | 优化方向 | 说明 | 优先级 |
|---|----------|------|--------|
| 1 | **Pipeline B (API-based)** | `task_executor.py` 有另一条基于 S2/OpenAlex API 的管线（~2 LLM 调用/篇 vs 当前 ~10 调用/篇），高引用场景可能更适合 | 高 |
| 2 | **自适应并行度** | 当频繁 429 时自动降低 `parallel_workers` | 中 |
| 3 | **断点续跑** | 增量保存已实现，但重启后需手动跳过已完成的（目前靠 author_cache 部分覆盖） | 中 |
| 4 | **Bing 搜索质量** | 中文查询噪音多（搜人名返回服装品牌），可加学术站点限定（如 `site:scholar.google.com`） | 中 |
| 5 | **USTC 限速可配置** | 当前硬编码 18 次/分钟，应改为 `config.json` 可配置 | 低 |
| 6 | **多 provider 轮询** | 当 USTC 限流时自动切换到智谱或其他 provider | 低 |
| 7 | **ScraperAPI 前端拦截改为软警告** | 当前 webUI 强制要求 ScraperAPI key 才能执行，应改为可选警告 | 高 |
| 8 | **S2 API 替代 Phase 1** | 当无 ScraperAPI 时，用 Semantic Scholar `GET /paper/{id}/citations` 找施引文献 | 中 |

---

## 八、知名学者本地数据库（新计划）

### 背景

当前知名学者筛选完全依赖 LLM + 网络搜索，每篇论文需要额外 LLM 调用来判断作者是否为"重量级学者"。这既慢（受 USTC 限速）又费钱。知名学者数量有限且相对稳定，适合预建本地数据库。

### 目标

预建一个本地知名学者数据库，Phase 2 筛选知名学者时优先查本地库，命中则直接返回，未命中再走 LLM。预计可省去大量 LLM 调用。

### 数据范围（以中国为主，按领域分类）

| 类别 | 覆盖范围 | 预估数量 |
|------|----------|----------|
| 中国科学院院士 | 数学部、技术科学部、信息技术科学部等 | ~800 |
| 中国工程院院士 | 信息与电子工程学部、机械与运载工程学部等 | ~900 |
| 国家杰青 | 国家杰出青年科学基金获得者 | ~4000 |
| 长江学者 | 教育部长江学者特聘教授 | ~2000 |
| 国家优青 | 优秀青年科学基金获得者 | ~3000 |
| IEEE/ACM Fellow | 计算机/电子领域 | ~500 |
| 国际知名AI学者 | Google/DeepMind/Meta/OpenAI 核心成员 | ~200 |
| 其他国际院士 | 欧洲科学院院士、AAAS Fellow 等 | ~300 |
| **合计** | | **~12000** |

### 按领域分类

| 领域 | 对应学科 | 典型学者示例 |
|------|----------|-------------|
| 计算机视觉 | CV | 何恺明、孙剑、朱松纯 |
| 自然语言处理 | NLP | 周志华、唐杰、黄民烈 |
| 机器学习 | ML | 张钹、周志华、杨强 |
| 机器人 | Robotics | 王越超、丁汉 |
| 多模态 | MM | 朱松纯、王井东 |
| 数据挖掘 | DM | 韩家炜、俞士纶 |
| 其他 | Other | 通用分类 |

### 数据库设计

```sql
CREATE TABLE renowned_scholars (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,           -- 中文姓名
    name_en     TEXT,                    -- 英文姓名/拼音
    name_aliases TEXT,                   -- 别名/缩写 (JSON array)
    affiliation TEXT,                    -- 当前任职单位
    country     TEXT DEFAULT '中国',
    title       TEXT,                    -- 职务/职称
    honors      TEXT,                    -- 荣誉称号 (JSON array)
    field       TEXT,                    -- 研究领域
    sub_field   TEXT,                    -- 细分方向
    h_index     INTEGER,
    scholar_id  TEXT,                    -- Google Scholar ID
    s2_id       TEXT,                    -- Semantic Scholar ID
    updated_at  TEXT
);

CREATE INDEX idx_name ON renowned_scholars(name);
CREATE INDEX idx_name_en ON renowned_scholars(name_en);
CREATE INDEX idx_field ON renowned_scholars(field);
```

### 工作流

```
Phase 2: 知名学者筛选
  │
  ▼
查询本地数据库 (按姓名/别名匹配)
  │
  ├──命中──→ 直接返回学者信息 (0 次 LLM 调用)
  │
  └──未命中──→ 走 LLM + 搜索 (当前逻辑)
        │
        └──如果 LLM 判定为知名学者──→ 写入本地数据库 (增量更新)
```

### 数据来源

| 来源 | 覆盖 | 获取方式 |
|------|------|----------|
| 中国科学院官网 | 院士名单 | 爬取/手动整理 |
| 中国工程院官网 | 院士名单 | 爬取/手动整理 |
| 国家自然科学基金委 | 杰青/优青名单 | 公开数据 |
| 教育部 | 长江学者名单 | 公开数据 |
| IEEE/ACM 官网 | Fellow 名单 | 官方页面 |
| Google Scholar | h-index | API/爬取 |
| Semantic Scholar | s2_id | API |

### 实现步骤

1. **建库脚本**: `scripts/build_scholar_db.py` — 从公开数据源爬取/整理知名学者名单，写入 SQLite
2. **查询模块**: `citationclaw/core/scholar_db.py` — 提供按姓名/别名查询的接口
3. **Phase 2 集成**: `author_searcher.py` 知名学者筛选步骤先查本地库，未命中再走 LLM
4. **增量更新**: LLM 新发现的知名学者自动写入数据库
5. **前端管理**: webUI 加数据库管理页面（查看/搜索/手动编辑）

### 预期收益

| 指标 | 当前 (LLM 筛选) | 优化后 (本地库) |
|------|------------------|-----------------|
| 知名学者筛选 LLM 调用 | 1-2 次/篇 | 0 次（命中时） |
| 筛选速度 | 3-10s/篇 | <10ms（命中时） |
| USTC API 调用量 | ~10 次/篇 | ~8 次/篇（省 20%） |
| 准确率 | 依赖搜索结果 | 已知学者 100% |

---

## 八、关键技术上下文

### tool-loop 工作流

```
system prompt (引导直接给答案)
  │
  ▼
messages + search_web tool 定义
  │
  ▼
LLM 返回 tool_calls ──是──→ 执行 S2/Bing 搜索 ──→ 喂回结果 ──→ 下一轮
  │                                                    (最多 3 轮)
  否
  │
  ▼
检测 "let me try..." 非答案?
  │
  ├──是──→ 追加 user 消息强制出最终答案
  │
  └──否──→ 返回最终答案
```

### light_client offload 机制

```
author_searcher.py
  ├── self.client (智谱 glm-4-plus)     ← 联网搜索调用 (search_fn/verify_fn)
  │     └── 当 search_backend == "zhipu_native" 时使用
  │
  └── self.light_client (USTC deepseek-v4-flash)  ← 非联网调用 + tool-loop
        ├── chat_fn (知名学者筛选)
        ├── format_fn (JSON 格式化)
        ├── _check_self_citation_llm (自引检测)
        └── 当 search_backend == "ustc_function" 时也走 search_fn/verify_fn
```

### USTC 可用模型

| 模型 | 类型 | 速度 | 备注 |
|------|------|------|------|
| `qwen3.6-chat` | 推理 | 50-100s/次 | token 耗尽导致空结果 |
| `deepseek-v4-flash` | 非推理 | 2-3s/次 | 当前采用 |
| `glm-5.2-107` | 推理 | 16s/次 | 备选 |

### 已知限制

1. **USTC 限速 20 次/分钟**: 全局速率限制器设 18 次/分钟，高引用场景仍需较长时间
2. **Bing 中文搜索噪音**: cn.bing.com 搜学术人名可能返回无关结果
3. **S2 覆盖不全**: 部分中文论文、新书章节在 S2 中无收录
4. **3 分钟警告误报**: 前端 `main.js:878` watchdog，后端 heartbeat 被 `task_executor.py:587` 过滤器吞掉

---

## 九、测试数据文件

| 文件 | 说明 |
|------|------|
| `data/result-20260812_142337/paper1_citing.jsonl` | 16 篇测试数据源 |
| `data/result-20260812_145006/paper1_citing.jsonl` | 157 篇真实数据源 |
| `data/json/imported-20260813_151025_author_information.json` | 16 篇 deepseek 修复后测试 (148KB, 100%) |
| `data/json/imported-20260813_162228_author_information.json` | 157 篇真实数据结果 (926KB, 99%) |
| `data/json/imported-20260813_140555_author_information.json` | 16 篇 qwen3.6-chat 测试 (79KB, 75%) |
| `data/json/imported-20260813_143443_author_information.json` | 16 篇 deepseek 修复前测试 (43KB, 100%但内容差) |

---

## 十、知名学者本地数据库 (2026-08-14)

### 设计目标

Phase 2 知名学者筛选步骤中，先查本地 SQLite 数据库，命中则直接使用，减少 LLM 调用并避免幻觉。

### 数据源

| 来源 | URL | 记录数 | 方法 |
|------|-----|--------|------|
| 中国科学院院士 | `casad.cas.cn/ysxx2022/ysmd/*` | ~1047 | HTML 爬取 |
| 中国工程院院士 | `cae.cn/cae/html/main/col48/` | ~1361 | HTML 爬取 |
| AAAI Fellows | `aaai.org/.../elected-aaai-fellows/` | ~284 | HTML 爬取 |
| 长江学者 | GitHub `ming66/Data-analysis-of-changjiang-scholars` | ~3038 | xlsx 下载 |
| IEEE CS Fellows | `computer.org/.../fellows/*` | ~307 | curl + RSC 解析 |
| 国家杰青 | LetPub `lejddy.com` | ~29 | HTML 爬取 (IP 受限) |
| 手动种子 | `data/manual_scholars.json` | 32 | 手动整理 (AI/CS 领域) |

**总记录: 5395 条** (去重后)

### 手动种子数据 (manual)

`data/manual_scholars.json` 包含 32 位 AI/CS 领域知名学者，覆盖 LLM 容易遗漏的学者：

- **中国学者**: 焦李成、白翔、乔红、黄凯奇、乔宇、胡清华、赫然/Ran He、雷震/Zhen Lei、杨健/Jian Yang 等
- **国际学者**: Dacheng Tao、Jiri Matas、Fahad Shahbaz Khan、Ajmal Mian、C.-C. Jay Kuo、Andreas Maier、Jenq-Neng Hwang、Silvio Savarese、Michael Ryoo、Stan Z. Li 等
- **中英文双名**: 每位中国学者同时收录中文名和英文名，确保两种提取模式都能命中

导入: `python scripts/build_scholar_db.py manual`

### 使用方法

```bash
# 建库
python scripts/build_scholar_db.py           # 全部来源
python scripts/build_scholar_db.py cas cae   # 指定来源

# 查询
python scripts/build_scholar_db.py --count
python scripts/build_scholar_db.py --lookup "高文"

# 导出
python scripts/build_scholar_db.py --export scholars.json
```

### 集成方式

```
author_searcher.py Phase 2 Step 5 (知名学者筛选):
  1. _extract_author_names(response2)     ← 从 LLM 作者信息中提取姓名
  2. _lookup_scholar_db(names, context)   ← 查本地库 + 单位交叉验证
  3. 命中 → 直接生成 Formated Renowned Scholar 记录
  4. 再调 LLM 筛选 → 合并去重 (DB 命中的优先)
```

### 防误匹配机制

- **单位交叉验证**: DB 命中后，检查 `response2` 中是否提到该学者的单位关键词（取前4字），未提到则跳过
- **空单位跳过**: 如 DB 记录无单位信息，不作为命中（避免重名误匹配）
- **LLM 补充**: DB 未命中的学者仍走 LLM 筛选，两者结果合并去重

### 数据库文件

`~/.citationclaw/scholars.db` (SQLite)

### 相关文件

- `citationclaw/core/scholar_db.py` — ScholarDB 类 + 建库 + 查询
- `scripts/build_scholar_db.py` — 建库/查询/导出 CLI 脚本
- `citationclaw/core/author_searcher.py` — 集成到 Phase 2 Step 5
