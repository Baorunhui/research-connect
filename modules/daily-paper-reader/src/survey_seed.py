"""综述种子论文：抓取全文 + LLM 结构化分析 + 参考文献直取。

种子论文用于锚定综述的任务范式（解决「泛主题综述找不到本任务原生文献」问题）：
- fetch_seed_text：arXiv id/链接 → DeepXiv raw 全文 → arXiv Atom 兜底；
  PDF 种子由 local_server 预抽全文后以 {"text","title"} 直传。
- analyze_seed：一次结构化 LLM 调用，产出任务定义/输入边界/范式/英文查询组/
  引文 arXiv id/非 arXiv 延伸文献/数据集名。
- fetch_citation_papers：种子引文逐条经 arXiv Atom 拉元数据（免 token 永远可用），
  DeepXiv 可用时富化被引数。单条失败跳过，不中断综述。
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional

import requests

from deepxiv_client import DeepXivClient, DeepXivError
from llm import DeepSeekClient

ARXIV_ATOM_URL = "https://export.arxiv.org/api/query"
# 直连会话（忽略环境代理）：arXiv API 国内直连可达，本地代理反而易超时
_ATOM_SESSION = requests.Session()
_ATOM_SESSION.trust_env = False
_ATOM_NAMESPACE = "{http://www.w3.org/2005/Atom}"
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
# 引文直取的并行上限（受 arXiv API 礼貌限速约束，顺序请求 1s 间隔）
_CITATION_FETCH_CAP = 60
_SEED_TEXT_CHAR_CAP = 60000

_SEED_ANALYSIS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_definition": {"type": "string"},
        "input_boundary": {"type": "string"},
        "target_paradigm": {"type": "string"},
        "queries": {"type": "array", "items": {"type": "string"}},
        "cited_arxiv_ids": {"type": "array", "items": {"type": "string"}},
        "non_arxiv_refs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "venue": {"type": "string"},
                    # year 不声明类型：LLM 常回传整数年份，严格 string 校验会整份判废
                    "year": {},
                },
                "required": ["title"],
            },
        },
        "dataset_names": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["task_definition", "target_paradigm", "queries"],
}

_SEED_ANALYSIS_SYSTEM = """
你是一位严谨的学术调研规划专家。用户会给你一篇「种子论文」的全文（或标题+摘要），
后续要围绕它所研究的任务生成一篇领域综述。请严格按 JSON schema 输出：

- task_definition（中文）：该论文研究的任务定义——输入是什么（形式/模态）、输出是什么、
  划分为哪些子任务（如文档解析/序列推理/三维生成/评估验证）；这是综述「任务定义」节的骨架。
- input_boundary（中文）：辨析该任务与相近任务的输入边界（例如 CAD 工程图纸 vs
  分步示意图说明书 vs 纯文本装配指令），说明本综述聚焦哪一类输入。
- target_paradigm（英文）：2-4 句任务范式定义（输入-输出形式+要解决的问题类型+典型方法族），
  将作为筛选候选论文的硬性标尺——只有同一/相近任务范式的论文才允许进入综述。
- queries：5-10 条英文检索查询，覆盖该任务的不同叫法与子方向
  （含数据集名、方法族名），每条一个自然检索短语，不要带引号。
- cited_arxiv_ids：种子论文参考文献中出现的 arXiv id 列表（形如 2302.01881，去版本号）。
- non_arxiv_refs：种子参考文献中重要的非 arXiv 文献（会议/期刊经典工作），
  每条给 title/venue/year，最多 15 条；没有则空列表。
- dataset_names：种子论文提到的数据集名称列表。

只输出 JSON object，禁止编造：全文里找不到的字段给空值。
"""


def extract_arxiv_id(value: str) -> Optional[str]:
    """从 URL / arXiv: 前缀 / 裸 id 中提取归一化（去版本号）arXiv id。"""
    text = str(value or "").strip()
    if not text:
        return None
    match = _ARXIV_ID_RE.search(text)
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# 种子全文抓取
# --------------------------------------------------------------------------- #


def fetch_seed_text(
    seed: Dict[str, Any],
    *,
    deepxiv: Optional[DeepXivClient] = None,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """按 seed dict 抓全文。返回 {text, title, arxiv_id}。

    seed 形态：
      {"arxiv_id": "..."} 或 {"url": "..."} → DeepXiv raw 优先，Atom 兜底
      {"text": "...", "title": "..."}       → PDF 预抽文本直传
    """
    text = str(seed.get("text") or "").strip()
    title = str(seed.get("title") or "").strip()
    if text:
        return {"text": text[:_SEED_TEXT_CHAR_CAP], "title": title or "（PDF 种子）", "arxiv_id": ""}

    arxiv_id = str(seed.get("arxiv_id") or "").strip() or extract_arxiv_id(str(seed.get("url") or ""))
    if not arxiv_id:
        raise ValueError("种子论文需要提供 arXiv 链接/id，或已抽取全文的 PDF（text+title）")

    # ① DeepXiv raw 全文（一次调用，稳定且免 PDF 解析）
    if deepxiv is not None:
        try:
            raw = deepxiv.get_paper_markdown(arxiv_id)
            if raw:
                meta = None
                try:
                    meta = deepxiv.get_paper_meta(arxiv_id)
                except DeepXivError:
                    meta = None
                return {
                    "text": raw[:_SEED_TEXT_CHAR_CAP],
                    "title": (meta or {}).get("title") or "",
                    "arxiv_id": arxiv_id,
                }
        except DeepXivError as exc:
            log(f"[survey] 种子 DeepXiv 全文获取失败，回退 arXiv Atom：{exc}")

    # ② arXiv Atom 兜底（元数据级：标题+摘要，免 token）
    entry = _fetch_arxiv_atom_entry(arxiv_id, log=log)
    if entry is None:
        raise RuntimeError(f"种子论文 {arxiv_id} 抓取失败（DeepXiv 与 arXiv Atom 均未命中）")
    return {
        "text": f"Title: {entry['title']}\n\nAbstract: {entry['abstract']}",
        "title": entry["title"],
        "arxiv_id": arxiv_id,
    }


# --------------------------------------------------------------------------- #
# arXiv Atom 薄实现（元数据，免 token）
# --------------------------------------------------------------------------- #


def _parse_atom_entries(xml_text: str) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return entries
    for entry in root.findall(f"{_ATOM_NAMESPACE}entry"):
        arxiv_id = ""
        id_text = entry.findtext(f"{_ATOM_NAMESPACE}id") or ""
        match = _ARXIV_ID_RE.search(id_text)
        if match:
            arxiv_id = match.group(1)
        title = (entry.findtext(f"{_ATOM_NAMESPACE}title") or "").strip()
        abstract = re.sub(r"\s+", " ", (entry.findtext(f"{_ATOM_NAMESPACE}summary") or "")).strip()
        published = (entry.findtext(f"{_ATOM_NAMESPACE}published") or "")[:10]
        authors = [a.findtext(f"{_ATOM_NAMESPACE}name") or "" for a in entry.findall(f"{_ATOM_NAMESPACE}author")]
        if arxiv_id and title:
            entries.append(
                {
                    "arxiv_id": arxiv_id,
                    "title": title,
                    "abstract": abstract,
                    "published": published,
                    "authors": "; ".join(a for a in authors if a),
                }
            )
    return entries


def _fetch_arxiv_atom_entries(arxiv_ids: List[str], *, log: Callable[[str], None] = print) -> List[Dict[str, str]]:
    """按 id 列表查 arXiv Atom API（每批最多 20 个 id，批间 1s 礼貌间隔）。"""
    collected: List[Dict[str, str]] = []
    ids = [str(i).strip() for i in arxiv_ids if str(i).strip()][:_CITATION_FETCH_CAP]
    for start in range(0, len(ids), 20):
        batch = ids[start : start + 20]
        try:
            resp = _ATOM_SESSION.get(
                ARXIV_ATOM_URL,
                params={"id_list": ",".join(batch), "max_results": len(batch)},
                timeout=30,
            )
            if resp.status_code == 200:
                collected.extend(_parse_atom_entries(resp.text))
            else:
                log(f"[survey] arXiv Atom 批次失败 HTTP {resp.status_code}：{batch[:3]}…")
        except requests.RequestException as exc:
            log(f"[survey] arXiv Atom 请求失败：{exc}")
        if start + 20 < len(ids):
            time.sleep(1.0)
    return collected


def _fetch_arxiv_atom_entry(arxiv_id: str, *, log: Callable[[str], None] = print) -> Optional[Dict[str, str]]:
    entries = _fetch_arxiv_atom_entries([arxiv_id], log=log)
    return entries[0] if entries else None


# --------------------------------------------------------------------------- #
# LLM 种子分析
# --------------------------------------------------------------------------- #


def _extract_cited_ids_from_text(text: str) -> List[str]:
    """全文正则兜底：收集所有形如 \\d{4}.\\d{4,5} 的 arXiv id（已去重、按出现序）。"""
    seen: Dict[str, None] = {}
    for match in _ARXIV_ID_RE.finditer(text or ""):
        seen.setdefault(match.group(1), None)
    return list(seen.keys())


def analyze_seed(
    seed_text: Dict[str, Any],
    client_factory: Callable[[], DeepSeekClient],
) -> Optional[Dict[str, Any]]:
    """一次结构化调用产出种子分析结果；解析失败返回 None（调用方降级为无种子模式）。"""
    from survey_pipeline import _chat_structured  # noqa: PLC0415 —— 延迟导入避免循环依赖

    user = (
        f"种子论文 arXiv id: {seed_text.get('arxiv_id') or '（PDF 上传）'}\n\n"
        f"种子论文全文（可能截断）：\n{seed_text.get('text') or ''}"
    )
    parsed = _chat_structured(
        client_factory(),
        _SEED_ANALYSIS_SYSTEM,
        user,
        "survey_seed_analysis",
        _SEED_ANALYSIS_SCHEMA,
    )
    if not parsed:
        return None
    queries = [str(q).strip() for q in (parsed.get("queries") or []) if str(q).strip()]
    parsed["queries"] = queries[:10]
    # 年份/会议字段统一转字符串（LLM 可能回传整数）
    parsed["non_arxiv_refs"] = [
        {
            "title": str(ref.get("title") or "").strip(),
            "venue": str(ref.get("venue") or "").strip(),
            "year": str(ref.get("year") or "").strip(),
        }
        for ref in (parsed.get("non_arxiv_refs") or [])
        if isinstance(ref, dict) and str(ref.get("title") or "").strip()
    ][:15]
    # 引文 id：LLM 列举 ∪ 全文正则，均需匹配 arXiv id 形态（不匹配的丢弃），去版本号去重
    llm_ids = [extract_arxiv_id(i) for i in (parsed.get("cited_arxiv_ids") or [])]
    llm_ids = [i for i in llm_ids if i][:_CITATION_FETCH_CAP]
    regex_ids = _extract_cited_ids_from_text(seed_text.get("text") or "")
    merged: Dict[str, None] = {}
    for pid in llm_ids + regex_ids:
        merged.setdefault(pid, None)
    parsed["cited_arxiv_ids"] = list(merged.keys())[:_CITATION_FETCH_CAP]
    parsed.setdefault("task_definition", "")
    parsed.setdefault("input_boundary", "")
    parsed.setdefault("target_paradigm", "")
    parsed.setdefault("non_arxiv_refs", [])
    parsed.setdefault("dataset_names", [])
    return parsed


# --------------------------------------------------------------------------- #
# 引文直取
# --------------------------------------------------------------------------- #


def fetch_citation_papers(
    cited_ids: List[str],
    *,
    deepxiv: Optional[DeepXivClient] = None,
    log: Callable[[str], None] = print,
) -> List[Dict[str, Any]]:
    """种子引文逐条拉元数据 → survey_pipeline 论文 dict 契约（source='seed_citation'）。

    有 DeepXiv 客户端时以 get_paper_meta 为主路（快且自带被引数）；
    无 token 时走 arXiv Atom 批量（免 token，注意礼貌限速）。
    """
    ids = [str(i).strip() for i in cited_ids if str(i).strip()][:_CITATION_FETCH_CAP]
    entries: List[Dict[str, Any]] = []
    if deepxiv is not None:
        for pid in ids:
            try:
                meta = deepxiv.get_paper_meta(pid)
            except DeepXivError:
                meta = None
            if meta and meta.get("title"):
                entries.append(
                    {
                        "arxiv_id": meta["arxiv_id"],
                        "title": meta["title"],
                        "abstract": meta.get("abstract") or meta.get("tldr") or "",
                        "published": meta.get("published") or "",
                        "authors": "",
                        "citation_count": meta.get("citation_count") or 0,
                        "_citation_count": meta.get("citation_count") or 0,
                    }
                )
            time.sleep(0.15)
        if entries:
            return _entries_to_papers(entries, enrich_citations=False)
        log("[survey] DeepXiv 引文元数据未命中，回退 arXiv Atom")
    entries = _fetch_arxiv_atom_entries(ids, log=log)
    return _entries_to_papers(entries, enrich_citations=False)


def _entries_to_papers(entries: List[Dict[str, Any]], *, enrich_citations: bool) -> List[Dict[str, Any]]:
    papers: List[Dict[str, Any]] = []
    for entry in entries:
        pid = entry["arxiv_id"]
        papers.append(
            {
                "paper_id": pid,
                "title": entry["title"],
                "abstract": entry["abstract"],
                "authors": [a for a in entry.get("authors", "").split("; ") if a],
                "published": entry["published"],
                "link": f"https://arxiv.org/abs/{pid}",
                "pdf_url": f"https://arxiv.org/pdf/{pid}",
                "source": "seed_citation",
                "citation_count": int(entry.get("_citation_count") or 0),
            }
        )
    return papers
