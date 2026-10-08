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
    "施引来自 OpenAlex 的论文目录，不是谷歌学术的完整列表。"
    "OpenAlex 没有收录的引用不会出现在这里。"
)


def _target_bibliography(target_papers: List[dict], kaggle) -> List[Optional[dict]]:
    """One batched snapshot lookup for the author's own papers.

    Falls back to single lookups when the snapshot object has no batch method,
    and those calls omit ``brief`` so older stubs keep working.
    """
    blank: List[Optional[dict]] = [None] * len(target_papers)
    if kaggle is None or not target_papers:
        return blank
    many = getattr(kaggle, "lookup_many", None)
    if callable(many):
        got = list(many(target_papers) or [])
        if len(got) < len(target_papers):
            got.extend([None] * (len(target_papers) - len(got)))
        return got[: len(target_papers)]
    return [
        kaggle.lookup(
            title=tp.get("title", ""),
            arxiv_id=tp.get("arxiv_id", ""),
            doi=tp.get("doi", ""),
        )
        for tp in target_papers
    ]


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
    targets_out: List[dict] = []
    citers: Dict[tuple, dict] = {}
    seen_citing: set = set()
    seen_self: set = set()
    match_cache: Dict[tuple, dict] = {}
    n_name_only = n_meta = n_resolved = n_fetched = n_kaggle = 0
    author_names: List[str] = []
    for tp in target_papers:
        entry = citing.get(normalize_title(tp.get("title", ""))) or {}
        for rec in entry.get("records") or []:
            for author in rec.get("authors") or []:
                author_names.append(str(author.get("name", "") or ""))
    prepared = honor.candidates_many(author_names) if hasattr(honor, "candidates_many") else None
    target_meta = _target_bibliography(target_papers, kaggle)
    for tp, meta in zip(target_papers, target_meta):
        title = tp.get("title", "")
        if meta:
            n_meta += 1
        entry = citing.get(normalize_title(title)) or {}
        records = entry.get("records") or []
        if entry.get("openalex_ids"):
            n_resolved += 1
        n_fetched += len(records)
        hits_here = []
        n_self_here = 0
        # Title search sets this list (possibly empty). An empty list means the
        # target authors are unknown, so nothing is dropped as a self-citation.
        # Scholar-profile papers omit the key and keep matching the profile name.
        if "self_cite_names" in tp:
            self_keys = {name_key(n) for n in (tp.get("self_cite_names") or [])}
            self_keys.discard("")
        else:
            profile_key = name_key(scholar_name)
            self_keys = {profile_key} if profile_key else set()
        for rec in records:
            authors = rec.get("authors") or []
            if self_keys and any(name_key(a.get("name", "")) in self_keys for a in authors):
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
                    if prepared is None:
                        res = honor.match(*ck)
                    else:
                        key = name_key(ck[0])
                        res = honor.match(*ck, prepared=prepared.get(key, []) if key else [])
                    match_cache[ck] = res
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
    log(f"查了 {len(target_papers)} 篇，目录里找到 {n_resolved} 篇；"
        f"施引 {n_fetched} 条，去掉重复后 {len(seen_citing)} 篇，跳过本人引用 {len(seen_self)} 篇")
    log(f"对上荣誉名单 {len(honor_citers)} 人；只是同名、单位或邮箱对不上的 {n_name_only} 个没有算进去")
    honor_stats = dict(honor.stats())
    honor_stats.pop("path", None)
    return {
        "mode": REPORT_MODE, "disclaimer": DISCLAIMER, "scholar_name": scholar_name,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "coverage": coverage,
        "honor_list": honor_stats, "kaggle_index": kaggle is not None,
        "honor_citers": honor_citers, "targets": targets_out,
        "data_sources": "OpenAlex cites（施引关系 + 作者单位）· Kaggle arXiv 快照（题录核对）· 本地荣誉名单",
        "match_rule": "姓名键一致，且（邮箱完整地址 / 邮箱域名 / 规范化单位）至少一项一致；仅姓名一致不计入",
    }


def render_html(report: dict) -> str:
    e = lambda v: _html.escape(str(v if v is not None else ""))  # noqa: E731
    cov = report["coverage"]
    stats = report.get("honor_list") or {}

    def _paper_cell(titles: list) -> str:
        shown = [title for title in titles if title][:3]
        extra = len(titles) - len(shown)
        text = "；".join(shown)
        if extra > 0:
            text = f"{text}；还有 {extra} 篇" if text else f"还有 {extra} 篇"
        return e(text)

    rows = "".join(
        f"<tr><td>{e(c['name'])}{(' / ' + e(c['name_zh'])) if c.get('name_zh') else ''}</td>"
        f"<td>{e('、'.join(c['honors']))}</td><td>{e(c['basis'])}：{e(c['matched_value'])}</td>"
        f"<td>{e(c['listed_affiliation'])}</td><td>{len(c['citing_papers'])}</td>"
        f"<td>{_paper_cell(c.get('citing_papers') or [])}</td></tr>"
        for c in report["honor_citers"]
    ) or "<tr><td colspan='6'>这次没有对上荣誉名单里的学者。只是同名、单位或邮箱对不上的不算。</td></tr>"
    trows = "".join(
        f"<tr><td>{e(t['title'])}</td><td>{e(t['year'])}</td><td>{e(t['citations'])}</td>"
        f"<td>{e(t['arxiv_id'] or '—')}</td><td>{e(t['openalex_cited_by'])}</td>"
        f"<td>{e(t['citing_fetched'])}{'' if t['citing_complete'] or not t['openalex_ids'] else '（未取完）'}</td>"
        f"<td>{len(t['honor_hits'])}</td></tr>"
        for t in report["targets"]
    )
    if stats.get("available"):
        honor_note = (
            f"名单里有 {stats.get('total', 0)} 人。姓名要对上，并且单位或邮箱至少有一项也对上。"
            f"只是同名的 {cov['name_only_candidates_not_counted']} 个没有算进去。"
        )
    else:
        honor_note = "这台服务器上还没有荣誉名单，所以这次没有做名单对照。"
    kaggle_note = ""
    if report.get("kaggle_index") is False:
        kaggle_note = "<p>这台服务器上还没有本地论文快照，所以这次没有核对论文编号。</p>"
    stats_line = "".join(
        f"<li><b>{e(value)}</b><span>{e(label)}</span></li>"
        for label, value in (
            ("查过的论文", cov["target_papers"]),
            ("目录里找到", cov["targets_resolved_openalex"]),
            ("目录记载的引用", cov["openalex_cited_by_total"]),
            ("拉到的施引", cov["citing_records_fetched"]),
            ("去掉重复后", cov["unique_citing_works"]),
            ("题录核对上", cov["citing_verified_kaggle"]),
            ("跳过本人引用", cov["self_citations_skipped"]),
            ("主页上的总引用", cov["profile_citations_total"]),
            ("还没查完", cov["targets_incomplete"]),
        )
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(report['scholar_name'])} · 查他引</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;color:#1d2433;background:#f6f7fb}}
main{{max-width:1100px;margin:0 auto;padding:24px 16px 48px}}
h2,h3{{margin:0 0 12px}}
.banner{{background:#fff4e5;border:1px solid #f0b35a;padding:12px 16px;border-radius:8px;line-height:1.5}}
.stats{{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin:16px 0;padding:0;list-style:none}}
.stats li{{background:#fff;border:1px solid #e3e7ef;border-radius:8px;padding:10px 12px}}
.stats b{{display:block;font-size:18px}}
.stats span{{color:#5b6475;font-size:12px}}
.wrap{{overflow-x:auto;background:#fff;border:1px solid #e3e7ef;border-radius:8px}}
table{{border-collapse:collapse;width:100%;min-width:720px}}
td,th{{border-bottom:1px solid #eef0f5;padding:8px 10px;font-size:13px;vertical-align:top;text-align:left}}
tbody tr{{content-visibility:auto;contain-intrinsic-size:auto 52px}}
th{{background:#f8fafc;position:sticky;top:0}}
p{{line-height:1.6}}
.find{{display:block;width:100%;max-width:420px;margin:8px 0 16px;padding:8px 10px;border:1px solid #cfd5e1;border-radius:8px;font:inherit}}
</style></head><body><main>
<h2>{e(report['scholar_name'] or '学者')} · 查他引</h2>
<div class="banner">{e(report['disclaimer'])}</div>
<ul class="stats">{stats_line}</ul>
<p>{e(honor_note)}</p>
{kaggle_note}
<label for="report-find">在这份报告里查找</label>
<input id="report-find" class="find" placeholder="学者、论文或单位">
<h3>对上的学者（{cov['honor_citers']}）</h3>
<div class="wrap"><table><thead><tr><th>学者</th><th>荣誉</th><th>怎么对上的</th><th>单位</th><th>引用了几篇</th><th>引用的论文</th></tr></thead><tbody>{rows}</tbody></table></div>
<h3>查过的论文</h3>
<div class="wrap"><table><thead><tr><th>论文</th><th>年份</th><th>主页上的引用数</th><th>论文编号</th><th>目录里的引用数</th><th>已查到的施引</th><th>对上几位</th></tr></thead><tbody>{trows}</tbody></table></div>
<p style="color:#888">生成于 {e(report['generated_at'])}</p>
<script>
(function () {{
  var box = document.getElementById('report-find');
  if (!box) return;
  var rows = null;
  box.addEventListener('input', function () {{
    if (!rows) {{
      rows = Array.prototype.slice.call(document.querySelectorAll('tbody tr'));
      for (var i = 0; i < rows.length; i++) rows[i]._find = (rows[i].textContent || '').toLowerCase();
    }}
    var q = this.value.trim().toLowerCase();
    for (var j = 0; j < rows.length; j++) rows[j].hidden = !!(q && rows[j]._find.indexOf(q) < 0);
  }});
}})();
</script>
</main></body></html>"""


def write_outputs(report: dict, result_dir: Path, prefix: str) -> dict:
    result_dir.mkdir(parents=True, exist_ok=True)
    json_file = result_dir / f"{prefix}_fast_report.json"
    html_file = result_dir / f"{prefix}_fast_report.html"
    excel_file = result_dir / f"{prefix}_fast_report.xlsx"
    json_file.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
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
