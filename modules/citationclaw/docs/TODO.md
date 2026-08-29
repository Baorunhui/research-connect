# CitationClaw 待办清单

> 最后更新：2026-08-28
> 优先级：P0 = 直接影响万引学者跑通 / P1 = 影响数据质量或体验 / P2 = 记录待执行

---

## P0 — 直接影响万引学者跑通

### 1. 跨目标施引论文去重

**问题**：`task_executor.py:2382-2409` 的 Step 5 循环里，同一篇施引论文如果引用了多篇目标论文，会产生 N 条重复记录。万引学者 top-30 目标 × 3000 引/篇 = 9 万条，去重后可能只有 3-4 万条唯一。

**影响**：
- Excel 膨胀（大量重复行）
- Step 5b LLM 兜底变慢（ScholarSearchCache 按标题去重，但首次调用仍逐个发）
- Dashboard 统计偏差（`unique_papers` 偏高）
- PDF 下载重复（同一篇 PDF 被下多次）

**方案**：在 Step 5 循环前用 S2 paperId（或规范化标题）做全局去重，同一施引论文只建一条记录，但在记录里标注它引用了哪些目标论文（`Citing_Paper` 字段改为列表或分号分隔）。

**改动范围**：`task_executor.py` Step 5 循环 + `scholar_profile_pipeline.py` `build_citing_record`

---

### 2. LLM 超时频繁时自动换 API key

**问题**：`scholar_search_agent.py:170-172` 超时（`asyncio.TimeoutError`）时直接返回 `[]`，不切换 key。当前 key 池只对 429/401/403 切换（line 174）。万引学者 Step 5b 可能上千次 LLM 调用，某根 key 的端点变慢时整批兜底全部超时跳过。

**方案**：
- 加连续超时计数器 `_timeout_count[key_idx]`
- 同一 key 连续超时 ≥2 次 → 标记该 key 为"慢"，切换下一个 key 重试（不标记永久耗尽，下次轮到时仍可尝试）
- 所有 key 都连续超时 → 降级返回 `[]`
- 打日志：`⚠ key #N 连续超时 2 次，切换 key #M`

**改动范围**：`scholar_search_agent.py` `search_paper_authors` while 循环

---

### 3. 中文名学者 Step 1 S2 兜底失败

**问题**：ScraperAPI 已过期（401）→ 自动走 S2 兜底。S2 兜底靠 URL 里的 `&name=作者名` 做 `author/search`（`scholar_profile_scraper.py:190`），但 S2 的 author search 是英文名索引，中文名"张天柱"大概率搜不到 → Step 1 拿到 0 篇论文 → 任务结束。

**方案**（三选一）：
- **A**：UI 提示用户在 URL 里加英文名 `&name=Tianzhu+Zhang`（最小改动，靠用户）
- **B**：S2 兜底搜不到时，追加一次 S2 `paper/search` 按论文标题反查作者（需要至少一个已知论文标题）
- **C**：ScraperAPI 续费（根治，但需要花钱）

**当前临时方案**：A（已在 `pipeline-and-api-keys.md` Step 1 行说明）

---

## P1 — 影响数据质量或体验

### 4. ScraperAPI 过期（401）

**问题**：`scraper_api_keys[0]` 返回 401，快速流水线 Step 1 和经典流水线 Phase 1 均受影响。

**现状**：快速流水线已自动落 S2 兜底（不致命），但 S2 的论文数/引用数与 GS 有偏差（S2 `citationCount` 远低于 GS 被引数），`profile_min_citations` 门槛别设太高。

**方案**：续费或换 key，更新 `config.json` `scraper_api_keys`。

---

### 5. ScholarDB 英文名缺失

**问题**：5395 条知名学者中仅 617 条有 `name_en`，4778 条仅中文名。arXiv/S2 的施引论文作者名是英文，与中文�名匹配不上 → 召回率偏低。

**现状**：Step 4b arXiv 作者补全部分缓解（arXiv 作者名也是英文，问题相同）。`scholar_db.lookup()` 依次查 `name`/`name_en`/`name_aliases`。

**方案**：
- 批量补 `name_en`：对仅中文名的记录用 LLM 或人工补英文名
- 或在 `lookup()` 里加中英名转换层（拼音匹配 / 维基百科消歧）

---

### 6. Step 5b LLM 兜底调用量控制

**问题**：万引学者施引论文中被引 ≥50 的可能上千篇，10 并发 × 3-5s/次 = 5-17 分钟纯 LLM 调用。

**方案**：
- 调高 `profile_llm_fallback_min_citations`（当前 50 → 200+），代价是漏掉中等被引的学者
- 或加 LLM 兜底总预算上限（如最多调 200 次），超出跳过
- 或对 LLM 兜底也加 wall-clock 超时（如 10 min）

---

### 7. arXiv 库首跑为空

**问题**：`~/.citationclaw/arxiv.db` 增量缓存，首次跑全 miss → Step 4b 慢（受 50 次 API + 5 min 超时限制）。

**方案**：
- 对常见高被引论文（如 Transformers, ResNet, BERT 等）预建一批种入库
- 或用 `arxiv_db build-titles <titles.txt>` 从历史结果文件批量导入

---

## P2 — 记录待执行

### 8. 机械盘迁移（附录 D 已记录）

**内容**：两个 SQLite（`scholars.db` + `arxiv.db`）从 `~/.citationclaw/` 迁到项目内 `data/db/`，输出目录一并搬机械盘。方案 Option B 统一，两个 DB 都 gitignore。

**详见**：`docs/pipeline-and-api-keys.md` 附录 D

---

## 已完成（备查）

| 日期 | 内容 |
|---|---|
| 2026-08-28 | Step 4b 加启动/进度日志 + 5 min 超时 + `max_fetch` 300→50 |
| 2026-08-28 | WebSocket 线程广播修复（`run_coroutine_threadsafe`） |
| 2026-08-28 | Phase 5 `gen.generate()` 改 `asyncio.to_thread`（不阻塞 event loop） |
| 2026-08-27 | arXiv 标题→作者本地库接入（Step 3+4 精确解析 + Step 4b 作者补全） |
| 2026-08-27 | Step 5b key 池（429/401/403 自动切换，USTC key 替代 GLM） |
| 2026-08-27 | `save_config` 合并保存修复（不再重置未提交的 `profile_*` 字段） |
| 2026-08-27 | `scholar_search_agent.py` asyncio NameError 修复 |
| 2026-08-27 | 前端 CDN 全部本地化（零外网依赖） |
| 2026-08-27 | 附录 D 外部路径依赖清单与迁移规划 |
