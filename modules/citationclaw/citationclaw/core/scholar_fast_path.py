"""Scholar-profile quick report: OpenAlex citing works + Kaggle bibliography + honor list.

  * target papers   profile paper list (uploaded HTML, or the existing list)
  * citing papers   OpenAlex ``cites:`` over the matching work(s), cursor-paged to
                    completion, metadata only (``openalex_citing``)
  * bibliography    target and citing records checked/filled from the local
                    Kaggle arXiv index (no PDF / landing page / full text)
  * honors          ``honor_list.HonorList`` (name + affiliation/email)

Nothing here calls Google Scholar, Semantic Scholar or ScraperAPI.
"""
from __future__ import annotations

import html as _html
import json
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from citationclaw.core.honor_list import HonorList, name_key
from citationclaw.core.kaggle_arxiv_meta import KaggleArxivMeta, normalize_title

REPORT_MODE = "openalex_citing_honor_match"
DISCLAIMER = (
    "施引数据来自 OpenAlex（按论文分页拉取全部施引 works，仅题录与作者单位），"
    "题录经本地 Kaggle arXiv 快照核对/补全；不是谷歌学术全量爬取，"
    "OpenAlex 未收录的施引不会被统计。"
)


def build_fast_report(
    target_papers: List[dict],
    scholar_name: str,
    honor: HonorList,
    citing: Dict[str, dict],
    kaggle: Optional[KaggleArxivMeta] = None,
    log: Callable[[str], None] = print,
) -> dict:
    """Assemble the report from pre-fetched citing records.

    ``citing`` maps ``normalize_title(target title)`` to
    ``{records, openalex_ids, openalex_cited_by, complete, resolved_by}``.
    """
    scholar_key = name_key(scholar_name)
    targets_out: List[dict] = []
    citers: Dict[tuple, dict] = {}
    seen_citing: set = set()
    seen_self: set = set()
    match_cache: Dict[tuple, dict] = {}
    n_name_only = n_meta = n_resolved = n_fetched = n_kaggle = 0
    for tp in target_papers:
        title = tp.get("title", "")
        meta = kaggle.lookup(title=title, arxiv_id=tp.get("arxiv_id", ""), doi=tp.get("doi", "")) \
            if kaggle is not None else None
        if meta:
            n_meta += 1
        entry = citing.get(normalize_title(title)) or {}
        records = entry.get("records") or []
        if entry.get("openalex_ids"):
            n_resolved += 1
        n_fetched += len(records)
        hits_here = []
        n_self_here = 0
        for rec in records:
            authors = rec.get("authors") or []
            if scholar_key and any(name_key(a.get("name", "")) == scholar_key for a in authors):
                seen_self.add(rec["id"])
                n_self_here += 1
                continue
            if rec["id"] not in seen_citing:
                seen_citing.add(rec["id"])
                n_kaggle += rec.get("verified_by") == "kaggle_arxiv"
            for a in authors:
                ck = (a.get("name", ""), a.get("affiliation", ""), a.get("email", ""))
                res = match_cache.get(ck)
                if res is None:
                    res = match_cache[ck] = honor.match(*ck)
                    n_name_only += res["name_only"]
                for h in res["hits"]:
                    hits_here.append({
                        **h, "author": a.get("name", ""), "author_affiliation": a.get("affiliation", ""),
                        "author_email": a.get("email", ""), "citing_title": rec.get("title", ""),
                        "citing_year": rec.get("year"), "citing_doi": rec.get("doi", ""),
                        "citing_arxiv_id": rec.get("arxiv_id", ""), "citing_openalex_id": rec["id"],
                        "citing_source": "openalex" + ("+kaggle_arxiv" if rec.get("verified_by") else ""),
                    })
                    agg = citers.setdefault((h["name"].lower(), h["listed_affiliation"].lower()), {
                        "name": h["name"], "name_zh": h.get("name_zh", ""), "honors": [],
                        "basis": h["basis"], "matched_value": h["matched_value"],
                        "listed_affiliation": h["listed_affiliation"], "citing_papers": [],
                        "cited_targets": []})
                    if h["honor"] not in agg["honors"]:
                        agg["honors"].append(h["honor"])
                    if rec.get("title") and rec["title"] not in agg["citing_papers"]:
                        agg["citing_papers"].append(rec["title"])
                    if title not in agg["cited_targets"]:
                        agg["cited_targets"].append(title)
        targets_out.append({
            "title": title, "year": tp.get("year"), "citations": tp.get("citations", 0),
            "arxiv_id": (meta or {}).get("arxiv_id", "") or tp.get("arxiv_id", ""),
            "venue": (meta or {}).get("venue", ""),
            "metadata_source": "kaggle_arxiv" if meta else "profile",
            "openalex_ids": entry.get("openalex_ids") or [],
            "openalex_cited_by": entry.get("openalex_cited_by", 0),
            "resolved_by": entry.get("resolved_by", ""),
            "citing_fetched": len(records), "citing_complete": bool(entry.get("complete")),
            "self_citations": n_self_here, "honor_hits": hits_here,
        })
    honor_citers = sorted(citers.values(), key=lambda c: (-len(c["citing_papers"]), c["name"]))
    coverage = {
        "target_papers": len(target_papers),
        "targets_with_local_metadata": n_meta,
        "targets_resolved_openalex": n_resolved,
        "openalex_cited_by_total": sum(t["openalex_cited_by"] for t in targets_out),
        "citing_records_fetched": n_fetched,
        "unique_citing_works": len(seen_citing),
        "citing_verified_kaggle": n_kaggle,
        "self_citations_skipped": len(seen_self),
        "targets_incomplete": sum(1 for t in targets_out if t["openalex_ids"] and not t["citing_complete"]),
        "honor_citers": len(honor_citers),
        "name_only_candidates_not_counted": n_name_only,
        "profile_citations_total": sum(int(t.get("citations") or 0) for t in target_papers),
    }
    log(f"[快查] 目标 {len(target_papers)} 篇，OpenAlex 找到 {n_resolved} 篇；施引记录 {n_fetched} 条，"
        f"去重后他引 {len(seen_citing)} 篇（Kaggle 题录核对 {n_kaggle} 篇），跳过自引 {len(seen_self)} 篇")
    log(f"[快查] 荣誉名单命中 {len(honor_citers)} 人；仅姓名相同、单位/邮箱不符的候选 {n_name_only} 个未计入")
    return {
        "mode": REPORT_MODE, "disclaimer": DISCLAIMER, "scholar_name": scholar_name,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "coverage": coverage,
        "honor_list": honor.stats(), "honor_citers": honor_citers, "targets": targets_out,
        "data_sources": "OpenAlex cites（施引关系 + 作者单位）· Kaggle arXiv 快照（题录核对）· 本地荣誉名单",
        "match_rule": "姓名键一致，且（邮箱完整地址 / 邮箱域名 / 规范化单位）至少一项一致；仅姓名一致不计入",
    }


def render_html(report: dict) -> str:
    e = lambda v: _html.escape(str(v if v is not None else ""))  # noqa: E731
    cov = report["coverage"]
    stats = report.get("honor_list") or {}
    rows = "".join(
        f"<tr><td>{e(c['name'])}{(' / ' + e(c['name_zh'])) if c.get('name_zh') else ''}</td>"
        f"<td>{e('、'.join(c['honors']))}</td><td>{e(c['basis'])}: {e(c['matched_value'])}</td>"
        f"<td>{e(c['listed_affiliation'])}</td><td>{len(c['citing_papers'])}</td>"
        f"<td>{e('; '.join(c['citing_papers'][:3]))}</td></tr>"
        for c in report["honor_citers"]
    ) or "<tr><td colspan='6'>OpenAlex 施引作者中未匹配到荣誉名单学者（名单外或单位/邮箱不符的不计入）</td></tr>"
    trows = "".join(
        f"<tr><td>{e(t['title'])}</td><td>{e(t['year'])}</td><td>{e(t['citations'])}</td>"
        f"<td>{e(t['arxiv_id'] or '-')}</td><td>{e(t['openalex_cited_by'])}</td>"
        f"<td>{e(t['citing_fetched'])}{'' if t['citing_complete'] or not t['openalex_ids'] else '（未取完）'}</td>"
        f"<td>{len(t['honor_hits'])}</td></tr>"
        for t in report["targets"]
    )
    sources = "、".join(f"{k} {v}" for k, v in (stats.get("sources") or {}).items()) or "未加载"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{e(report['scholar_name'])} · 他引快查</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#222}}
.banner{{background:#fff4e5;border:1px solid #f0b35a;padding:12px 16px;border-radius:8px}}
table{{border-collapse:collapse;width:100%;margin:12px 0}}td,th{{border:1px solid #ddd;padding:6px;font-size:13px;vertical-align:top}}
th{{background:#f5f5f5}}.kv span{{display:inline-block;margin-right:18px}}</style></head><body>
<h2>{e(report['scholar_name'] or '学者')} · 他引快查（OpenAlex 施引 + Kaggle 题录 + 荣誉名单）</h2>
<div class="banner"><b>数据来源：</b>{e(report['disclaimer'])}</div>
<p class="kv"><span>目标论文 {cov['target_papers']}</span><span>OpenAlex 找到 {cov['targets_resolved_openalex']}</span>
<span>OpenAlex 被引合计 {cov['openalex_cited_by_total']}</span><span>拉取施引记录 {cov['citing_records_fetched']}</span>
<span>去重他引论文 {cov['unique_citing_works']}</span><span>Kaggle 题录核对 {cov['citing_verified_kaggle']}</span>
<span>跳过自引 {cov['self_citations_skipped']}</span><span>主页显示总被引 {cov['profile_citations_total']}</span>
<span>未取完的目标 {cov['targets_incomplete']}</span></p>
<p>荣誉名单：{e(stats.get('total', 0))} 条（{e(sources)}）。匹配规则：{e(report['match_rule'])}。
仅姓名相同但单位/邮箱不符的候选 {cov['name_only_candidates_not_counted']} 个，未计入。</p>
<h3>命中的荣誉学者（{cov['honor_citers']}）</h3>
<table><tr><th>学者</th><th>荣誉</th><th>匹配依据</th><th>名单单位</th><th>施引篇数</th><th>施引论文（前 3）</th></tr>{rows}</table>
<h3>目标论文</h3>
<table><tr><th>标题</th><th>年份</th><th>主页被引</th><th>arXiv</th><th>OpenAlex 被引</th><th>已拉取施引</th><th>荣誉命中</th></tr>{trows}</table>
<p style="color:#888">生成于 {e(report['generated_at'])}</p></body></html>"""


def write_outputs(report: dict, result_dir: Path, prefix: str) -> dict:
    result_dir.mkdir(parents=True, exist_ok=True)
    json_file = result_dir / f"{prefix}_fast_report.json"
    html_file = result_dir / f"{prefix}_fast_report.html"
    excel_file = result_dir / f"{prefix}_fast_report.xlsx"
    json_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_file.write_text(render_html(report), encoding="utf-8")
    rows = [{
        "Target_Paper": t["title"], "Citing_Paper": h["citing_title"], "Citing_Year": h["citing_year"],
        "Author": h["author"], "Author_Affiliation": h["author_affiliation"],
        "Author_Email": h["author_email"], "Honor": h["honor"], "Honor_Source": h["source"],
        "Match_Basis": h["basis"], "Matched_Value": h["matched_value"],
        "Listed_Affiliation": h["listed_affiliation"], "Citing_DOI": h["citing_doi"],
        "Citing_arXiv": h["citing_arxiv_id"], "Citing_OpenAlex": h["citing_openalex_id"],
        "Citing_Data_Source": h["citing_source"],
    } for t in report["targets"] for h in t["honor_hits"]]
    try:
        import pandas as pd
        pd.DataFrame(rows or [{"Note": report["disclaimer"]}]).to_excel(excel_file, index=False)
    except Exception:  # noqa: BLE001 - excel is a convenience copy of the JSON
        excel_file = None
    return {"json": json_file, "dashboard": html_file, "excel": excel_file}
