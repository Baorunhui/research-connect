#!/usr/bin/env python
# 图表解读：纯文本方案（不读图）。
#
# 设计要点：
# - 与读图解耦：模型看不到图片，改用「图注 caption + 正文中引用该图号的段落 + 标题/摘要」
#   作为上下文，由文本模型生成比图注本身更详细的中文总结。
# - category 用启发式判断（无 VLM）：Table 图注前缀 → table；图注/引用关键词 → result/method；
#   Figure 1 默认 method；其余 other。
# - 批量调用：一篇论文的全部图/表按批（默认每批 8 个，DPR_FIGURE_BATCH_SIZE）分块，
#   每批一次文本 LLM 调用，成本 ≈ 每篇 1 次调用，替代旧的「VLM 逐图 classify + VLM 逐图深读」。
# - 复用主文本模型（resolve_llm_api_key / resolve_llm_base_url / resolve_llm_model），
#   可直接复用 Step 6 传入的每篇独立 client（避免多线程 kwargs 竞争）。
# - 全程 best-effort：任何图/请求失败只跳过该图，不抛错中断主流程。

import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(__file__)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from llm import OpenAIClient, resolve_llm_api_key, resolve_llm_base_url, resolve_llm_model  # noqa: E402


def figures_enabled() -> bool:
    """总开关：DPR_INTERPRET_FIGURES 缺省为开启，置 0/off/false/no 关闭。"""
    v = str(os.getenv("DPR_INTERPRET_FIGURES") or "").strip().lower()
    if not v:
        return True
    return v not in {"0", "off", "false", "no"}


def max_deep_figures() -> int:
    """兼容保留：select_key_figures 的深度解读上限（VLM 链路使用）。"""
    try:
        return max(0, int(os.getenv("DPR_VISION_MAX_FIGURES") or "4"))
    except Exception:
        return 4


def batch_size() -> int:
    """文本解读每批图/表数量（每批一次 LLM 调用）。"""
    try:
        return max(1, int(os.getenv("DPR_FIGURE_BATCH_SIZE") or "8"))
    except Exception:
        return 8


# 单图正文引用文本总字符上限
REF_MAX_CHARS = 1200
# 引用点前后各取的字符半径
REF_RADIUS = 400
# 单条 interpretation 长度上限
INTERPRET_MAX_CHARS = 1200
# 单批调用输出预算
BATCH_MAX_TOKENS = 4096


def _log(message: str) -> None:
    print(f"[FIGURE] {message}", flush=True)


def _strip_code_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        # 去掉可能的 ```json 围栏
        first_newline = t.find("\n")
        if first_newline != -1:
            t = t[first_newline + 1 :]
        if t.endswith("```"):
            t = t[:-3].strip()
    return t.strip()


def _parse_json_payload(text: str):
    """尝试把文本解析为 JSON；失败返回 None。"""
    t = _strip_code_fence(text)
    try:
        return json.loads(t)
    except Exception:
        pass
    # 兼容“外层多包了一层 ```json```”
    try:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start : end + 1])
    except Exception:
        pass
    try:
        start, end = t.find("["), t.rfind("]")
        if start != -1 and end > start:
            return json.loads(t[start : end + 1])
    except Exception:
        pass
    return None


def _clean_caption(text: str, max_chars: int = 900) -> str:
    """
    清洗模型产出的图注：
    - 去掉 Markdown 加粗 / 编号 / 列表 / 小标题等“分析脚手架”。
    - 去掉开头可能残留的“用户希望我解读……”之类复述性前缀。
    - 若超长则在句号/句号边界截断，避免一句被拦腰切断。
    """
    t = _strip_code_fence(text or "")
    lines = []
    for ln in t.splitlines():
        s = ln.strip().lstrip("0123456789.*+-#> ").strip()
        s = s.rstrip("0123456789.*+-#>").strip()
        if s:
            lines.append(s)
    t = " ".join(lines)
    # 去掉任意残留的 Markdown 标记（加粗/斜体/代码/引用/标题）
    t = re.sub(r"[*#`>~]+", "", t).replace("**", "").strip()
    if not t:
        return t
    # 去掉“用户希望我解读…/请(你帮我)解读…/下面…/如图所示”等复述前缀及其后的句读
    # （复述性前缀通常以句号或冒号收尾，一并去掉直到第一个句读）
    m = re.match(
        r"^(用户希望|请(?:你)?(?:帮我)?|下面|如图所示|该图/表)(?:[\s:：,，]*(?:解读|分析|描述)?[\s:：,，]*"
        r"(?:一(?:张|副|个)?|这张|该)?(?:图|表|图表)?)?[\s:：,，。]*(?:。|：|:)?",
        t, flags=re.I,
    )
    if m and len(m.group(0)) <= 30:
        t = t[m.end():].strip()
    if len(t) <= max_chars:
        return t
    # 在 max_chars 前的最后一个句末标点处截断（中文/英文句号）
    head = t[:max_chars]
    cut = max(head.rfind("。"), head.rfind("."), head.rfind("！"), head.rfind("？"))
    if cut > max_chars // 3:
        return head[: cut + 1].strip()
    return head.strip()


def _field_from_text(text: str, key: str) -> str:
    """宽容地从文本里提取 key 对应的字符串值（支持 JSON 值、引号内值）。"""
    m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        return m.group(1).strip()
    m2 = re.search(rf"{key}[\s:：]+?([^\s,，。；;]+)", text, re.I)
    return m2.group(1).strip() if m2 else ""


# 类型偏好顺序：方法/架构图 > 主结果图 > 结果表格 > 其它/附录
_CATEGORY_RANK = {
    "method": 0,
    "architecture": 0,
    "result": 1,
    "table": 2,
    "other": 3,
}
_CATEGORIES = ("method", "architecture", "result", "table", "other")

_TABLE_CAPTION_RE = re.compile(r"^\s*(?:Table|Tbl\.?)\s*\d+", re.IGNORECASE)
# 结果类关键词比方法类更具体（method/design 几乎出现在每张图的图注里），先判 result
_RESULT_KEYWORDS = (
    "result",
    "comparison",
    "compare",
    "performance",
    "ablation",
    "accuracy",
    "benchmark",
    "evaluation",
    "latency",
    "throughput",
    "metric",
    "score",
    "gain",
    "improv",
    "state-of-the-art",
    "sota",
)
_METHOD_KEYWORDS = (
    "architecture",
    "framework",
    "overview",
    "pipeline",
    "method",
    "design",
    "system",
    "approach",
    "illustrat",
    "schematic",
    "workflow",
    "module",
    "component",
    "structure",
    "diagram",
)


def heuristic_category(item: dict, refs_text: str = "") -> str:
    """
    启发式分类（无模型）：
    - label 为 Table 或图注以 Table N 开头 → table；
    - 图注/引用段命中结果类关键词 → result；命中方法类关键词 → method；
    - Figure 1 通常是 overview/方法图，默认 method；其余 other。
    """
    label = str(item.get("label") or "").strip().lower()
    caption = str(item.get("caption") or "").strip()
    if label.startswith("tab") or _TABLE_CAPTION_RE.match(caption):
        return "table"
    text = (caption + " " + str(refs_text or ""))[:800].lower()
    if any(k in text for k in _RESULT_KEYWORDS):
        return "result"
    if any(k in text for k in _METHOD_KEYWORDS):
        return "method"
    try:
        if int(item.get("index") or 0) <= 1:
            return "method"
    except Exception:
        pass
    return "other"


def select_key_figures(classified: list[dict], max_n: int | None = None, always_keep: int = 2) -> list[int]:
    """
    纯函数：选出要深度解读的图的 index 列表。策略“图号优先、类别为辅”。
    - 阶段一：始终纳入自然顺序最靠前的 always_keep 张图。论文里 Figure 1/2 通常是
      overview / benchmark / 主结果，最关键；即便被模型误判为 other 或多子图也不可丢。
    - 阶段二：再用类别偏好（method/architecture > result > table > other）按
      (类别, 图号) 补足，直到上限 max_n（默认 DPR_VISION_MAX_FIGURES）。
    - 返回按自然图号升序排列。
    """
    limit = max_deep_figures() if max_n is None else max(0, int(max_n or 0))
    n = len(classified)
    if not n:
        return []
    keep = max(0, int(always_keep or 0))
    preferred: list[int] = []
    # 阶段一：自然序最前的 keep 张强制纳入（可能含其余类型，甚至误判的 other）
    for i in range(min(keep, n)):
        if i not in preferred:
            preferred.append(i)
    # 阶段二：按 (类别rank, 图号) 补足非 other 的图
    order = sorted(
        range(n),
        key=lambda i: (_CATEGORY_RANK.get(classified[i].get("category"), 3), i),
    )
    for i in order:
        if limit and len(preferred) >= limit:
            break
        if i in preferred:
            continue
        if _CATEGORY_RANK.get(classified[i].get("category"), 3) >= 3:
            continue  # other/附录不补
        preferred.append(i)
    if limit:
        preferred = preferred[:limit]
    return sorted(preferred)


def _importance_sort_key(category: str) -> tuple:
    """重排键：方法/架构图优先于结果图、表格、其它；同 rank 稳定保序（保持原图号顺序）。"""
    return (_CATEGORY_RANK.get(str(category or "").strip(), 3),)


def _mention_regex(label: str, num: int) -> re.Pattern:
    """构造“Figure N / Fig. N / Table N / Tbl. N”引用正则；\\b 保证 Figure 1 不匹配 Figure 10。"""
    if str(label or "").strip().lower().startswith("tab"):
        return re.compile(rf"\b(?:Table|Tbl\.?)\s*{num}\b", re.IGNORECASE)
    return re.compile(rf"\b(?:Figure|Fig\.?)\s*{num}\b", re.IGNORECASE)


def collect_figure_references(
    full_text: str,
    label: str,
    num: int,
    caption: str = "",
    max_chars: int = REF_MAX_CHARS,
    radius: int = REF_RADIUS,
) -> str:
    """
    收集正文中引用给定图号的段落：
    - 对每个引用点取前后 radius 字符窗口，裁剪到词边界；
    - 合并重叠窗口、去重，总长截断到 max_chars；
    - 去掉图注本身（全文抽取可能把 caption 内联进正文，避免上下文重复）。
    返回单行化文本；无引用返回空串。
    """
    text = re.sub(r"\s+", " ", str(full_text or "")).strip()
    try:
        num = int(num or 0)
    except Exception:
        num = 0
    if not text or num <= 0:
        return ""
    windows: list[tuple[int, int]] = []
    for m in _mention_regex(label, num).finditer(text):
        s = max(0, m.start() - radius)
        e = min(len(text), m.end() + radius)
        if s > 0:
            b = text.find(" ", s)
            if b != -1 and b < m.start():
                s = b + 1
        if e < len(text):
            b = text.rfind(" ", m.end() + 1, e)
            if b != -1:
                e = b
        windows.append((s, e))
    if not windows:
        return ""
    windows.sort()
    merged: list[list[int]] = []
    for s, e in windows:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    parts: list[str] = []
    total = 0
    for s, e in merged:
        seg = text[s:e].strip()
        if not seg or seg in parts:
            continue
        parts.append(seg)
        total += len(seg) + 1
        if total >= max_chars:
            break
    refs = " ".join(parts)
    if len(refs) > max_chars:
        refs = refs[:max_chars].strip()
    cap = re.sub(r"\s+", " ", str(caption or "")).strip()
    if len(cap) >= 15 and cap in refs:
        refs = re.sub(r"\s+", " ", refs.replace(cap, " ")).strip()
    return refs


def create_text_client() -> OpenAIClient | None:
    """创建文本模型客户端（与主流水线同源）；未配置 API Key 返回 None。"""
    api_key = resolve_llm_api_key()
    if not api_key:
        _log("[WARN] 未配置 LLM API Key，跳过图表解读。")
        return None
    return OpenAIClient(
        api_key=api_key,
        model=resolve_llm_model(),
        base_url=resolve_llm_base_url(),
    )


def _build_batch_prompt(paper_title: str, paper_abstract: str, paper_method: str, entries: list[dict]) -> str:
    lines = [f"论文标题：{paper_title or '（未知）'}"]
    if paper_abstract:
        lines.append(f"摘要：{paper_abstract}")
    if paper_method:
        lines.append(f"论文方法：{paper_method}")
    lines.append("")
    lines.append("下面是这篇论文的图/表，以及正文中引用它们的段落。请为每个图/表写一段中文解读：")
    lines.append("- 3-6 句话，比图注本身更详细；")
    lines.append("- 先说明这张图/表展示了什么，再解读关键组件/流程/数字，最后说明它如何支撑论文的方法或结论；")
    lines.append("- 正文引用为空时，基于图注和摘要合理展开，不要编造具体数字或结论；")
    lines.append("- 解读中不要用编号、加粗、小标题或任何 Markdown 结构，不要复述指令。")
    lines.append("")
    for e in entries:
        lines.append(f"[{e['id']}] {e['label']} {e['num']}")
        lines.append(f"图注：{e['caption'] or '（无）'}")
        lines.append(f"正文引用：{e['refs'] or '（无）'}")
        lines.append("")
    lines.append(
        '请输出 JSON 数组，每个图/表一个对象：[{"id": 1, "interpretation": "中文解读"}]，id 必须与输入编号一致。'
    )
    return "\n".join(lines)


def _parse_interpretation_array(text: str) -> dict:
    """宽容解析模型输出为 {id: interpretation}；解析失败返回空 dict。"""
    parsed = _parse_json_payload(text)
    items = None
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        for key in ("items", "results", "interpretations", "figures"):
            v = parsed.get(key)
            if isinstance(v, list):
                items = v
                break
    out: dict = {}
    if not items:
        return out
    for obj in items:
        if not isinstance(obj, dict):
            continue
        cap = str(obj.get("interpretation") or obj.get("caption") or "").strip()
        if not cap:
            continue
        try:
            out[int(obj.get("id"))] = cap
        except Exception:
            continue
    return out


def _call_batch(client: OpenAIClient, prompt: str) -> str:
    messages = [
        {
            "role": "system",
            "content": "你是论文图表解读助手。根据图/表图注与引用它的正文段落，为每个图/表写一段中文解读。只输出 JSON 数组，不要输出任何其它文字。",
        },
        {"role": "user", "content": prompt},
    ]
    client.kwargs.update({"temperature": 0.3, "max_tokens": BATCH_MAX_TOKENS})
    try:
        resp = client.chat(messages=messages)
    except Exception as e:
        _log(f"[WARN] 图表解读调用失败: {e}")
        return ""
    return str(resp.get("content") or "").strip()


def _split_and_sort(merged: list[dict]) -> tuple[list[dict], list[dict]]:
    new_figures = [m["item"] for m in merged if m["kind"] == "figure"]
    new_tables = [m["item"] for m in merged if m["kind"] == "table"]
    # 重排顺序、重要靠前：稳定排序保证同 rank（含全部 other）保持原图号顺序
    new_figures.sort(key=lambda it: _importance_sort_key(it.get("category")))
    new_tables.sort(key=lambda it: _importance_sort_key(it.get("category")))
    return new_figures, new_tables


def interpret_paper_figures(
    figures: list[dict],
    tables: list[dict],
    paper: dict,
    docs_dir: str,
    full_text: str = "",
    client: OpenAIClient | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    总入口（纯文本、不读图）：启发式分类 + 正文引用收集 + 批量文本模型解读。
    - 保留原列表所有字段，仅补 category、interpretation；caption（docling 英文图注原文）永不覆盖。
    - 只有“有图注或有正文引用”的图/表才进入 LLM 批量；两者皆无的跳过并标记
      interpretation_skipped（避免无上下文时编造，也避免重复运行时重复计费）。
    - 结果按重要性重排：方法/架构图靠前（前端按数组序号渲染 Figure N），同类保持原图号顺序。
    - 全程 best-effort：任一步失败只影响该图，不抛错。
    """
    if not figures and not tables:
        return figures, tables

    merged = [{"kind": "figure", "item": it} for it in figures] + [{"kind": "table", "item": it} for it in tables]
    full_text = str(full_text or "").strip()

    # 1) 启发式分类 + 正文引用收集（纯本地，无网络）
    entries: list[dict] = []
    for i, m in enumerate(merged):
        item = m["item"]
        default_label = "Table" if m["kind"] == "table" else "Figure"
        label = str(item.get("label") or default_label).strip() or default_label
        try:
            num = int(item.get("index") or 0)
        except Exception:
            num = 0
        caption = str(item.get("caption") or "").strip()
        refs = collect_figure_references(full_text, label, num, caption) if (full_text and num > 0) else ""
        item["category"] = heuristic_category(item, refs)
        if caption or refs:
            entries.append({"id": i + 1, "label": label, "num": num, "caption": caption, "refs": refs})

    # 2) 无 client 且无 key → 原样返回（category 已写回）
    if client is None:
        client = create_text_client()
    if client is None:
        for i, m in enumerate(merged):
            if (i + 1) not in {e["id"] for e in entries}:
                m["item"]["interpretation_skipped"] = True
        return _split_and_sort(merged)

    # 3) 批量调用文本模型
    paper_title = str(paper.get("title") or "").strip()
    paper_abstract = str(paper.get("abstract") or "").strip()[:800]
    paper_method = str(paper.get("method") or "").strip()[:300]
    results: dict = {}
    bs = batch_size()
    for start in range(0, len(entries), bs):
        chunk = entries[start : start + bs]
        prompt = _build_batch_prompt(paper_title, paper_abstract, paper_method, chunk)
        raw = _call_batch(client, prompt)
        if not raw:
            continue
        for eid, cap in _parse_interpretation_array(raw).items():
            if eid not in results:
                results[eid] = _clean_caption(cap, max_chars=INTERPRET_MAX_CHARS)

    # 4) 回写 interpretation（与 caption 并存，不覆盖）
    context_ids = {e["id"] for e in entries}
    for i, m in enumerate(merged):
        item = m["item"]
        cap = results.get(i + 1, "")
        if cap:
            item["interpretation"] = cap
        elif (i + 1) not in context_ids:
            item["interpretation_skipped"] = True

    return _split_and_sort(merged)
