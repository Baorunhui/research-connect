"""Scholar-profile quick report: local metadata + honor list, no citation-graph crawl.

For a highly cited scholar, fetching every citing paper is slow and rarely
complete. The quick report instead answers "which listed academicians / Fellows
cite this scholar" from data already on disk:

  * target papers   profile paper list (uploaded HTML or one profile fetch),
                    enriched from the local Kaggle arXiv index
  * citing papers   earlier CitationClaw runs under the data roots:
                    ``cache/openalex_phase1/*.json`` and ``result-*/*_results.json``
  * affiliations    the citing records themselves, then ``cache/metadata_cache.json``
  * honors          ``honor_list.HonorList`` (name + affiliation/email)

Nothing here calls Semantic Scholar, OpenAlex, Google Scholar or ScraperAPI.
Coverage is reported explicitly so the result is never mistaken for a full
citation analysis.
"""
from __future__ import annotations

import glob
import html as _html
import json
import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from citationclaw.core.honor_list import HonorList, name_key
from citationclaw.core.kaggle_arxiv_meta import KaggleArxivMeta, normalize_title

REPORT_MODE = "fast_metadata_honor_match"
DISCLAIMER = (
    "快查报告：仅基于本地已有 metadata（历史施引记录、Kaggle arXiv 索引）与本地荣誉名单匹配，"
    "不是全量施引分析；未出现在本地数据中的施引不会被统计。"
)


def default_roots(data_dir: Path) -> List[Path]:
    roots = [Path(data_dir)]
    legacy = os.getenv("CITATIONCLAW_DATA_DIR", "").strip()
    if legacy and Path(legacy).is_dir() and Path(legacy).resolve() != Path(data_dir).resolve():
        roots.append(Path(legacy))
    return roots


def _norm_doi(doi: str) -> str:
    d = str(doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d


def _authors_from_result_record(rec: dict) -> List[dict]:
    """Parse ``API_Authors`` ("name | affil | country" lines) or ``Authors_Affiliation``."""
    authors = []
    for line in str(rec.get("API_Authors") or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if parts and parts[0]:
            aff = parts[1] if len(parts) > 1 and parts[1] not in ("未知", "未知机构") else ""
            authors.append({"name": parts[0], "affiliation": aff})
    if authors:
        return authors
    lines = [l.strip() for l in str(rec.get("Authors_Affiliation") or "").splitlines()]
    for i in range(0, len(lines) - 1, 2):
        if lines[i]:
            aff = lines[i + 1] if lines[i + 1] not in ("未知", "未知机构") else ""
            authors.append({"name": lines[i], "affiliation": aff})
    return authors


class LocalCitingIndex:
    """Citing papers and author affiliations recorded by earlier runs."""

    def __init__(self, roots: List[Path]):
        self.roots = [Path(r) for r in roots]
        self.by_target: Dict[str, Dict[str, dict]] = {}
        self.meta_by_doi: Dict[str, dict] = {}
        self.meta_by_title: Dict[str, dict] = {}
        self.files_read = 0

    def load(self) -> "LocalCitingIndex":
        for root in self.roots:
            for path in glob.glob(str(root / "cache" / "openalex_phase1" / "*.json")):
                self._load_openalex_phase1(Path(path))
            for path in glob.glob(str(root / "result-*" / "*_results.json")):
                self._load_results_json(Path(path))
            meta = root / "cache" / "metadata_cache.json"
            if meta.is_file():
                self._load_metadata_cache(meta)
        return self

    def _add(self, target_title: str, citing: dict):
        tkey = normalize_title(target_title)
        ckey = normalize_title(citing.get("title", ""))
        if not tkey or not ckey:
            return
        bucket = self.by_target.setdefault(tkey, {})
        prev = bucket.get(ckey)
        if prev is None or sum(1 for a in citing["authors"] if a.get("affiliation")) > \
                sum(1 for a in prev["authors"] if a.get("affiliation")):
            bucket[ckey] = citing

    def _load_openalex_phase1(self, path: Path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.files_read += 1
        for w in data.get("works") or []:
            self._add(data.get("title", ""), {
                "title": w.get("title") or w.get("paper_title") or "",
                "year": w.get("year"),
                "doi": _norm_doi(w.get("doi")),
                "authors": [{"name": a.get("name", ""), "affiliation": a.get("affiliation", "") or "",
                             "email": a.get("email", "") or ""}
                            for a in (w.get("authors") or []) if a.get("name")],
                "source": f"openalex_phase1:{path.parent.parent.parent.name}",
            })

    def _load_results_json(self, path: Path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, list):
            return
        self.files_read += 1
        for rec in data:
            if not isinstance(rec, dict) or not rec.get("Citing_Paper"):
                continue
            self._add(rec["Citing_Paper"], {
                "title": rec.get("Paper_Title", ""),
                "year": rec.get("Paper_Year"),
                "doi": _norm_doi(rec.get("doi")),
                "authors": _authors_from_result_record(rec),
                "source": f"result:{path.parent.name}",
            })

    def _load_metadata_cache(self, path: Path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.files_read += 1
        for key, rec in (data.items() if isinstance(data, dict) else []):
            if not isinstance(rec, dict):
                continue
            doi = _norm_doi(rec.get("doi") or key)
            if doi:
                self.meta_by_doi[doi] = rec
            t = normalize_title(rec.get("title", ""))
            if t:
                self.meta_by_title[t] = rec

    def citing_for(self, target_title: str) -> List[dict]:
        return list(self.by_target.get(normalize_title(target_title), {}).values())

    def fill_affiliations(self, citing: dict) -> dict:
        meta = self.meta_by_doi.get(citing.get("doi", "")) or \
            self.meta_by_title.get(normalize_title(citing.get("title", "")))
        if not meta:
            return citing
        by_key = {name_key(a.get("name", "")): a for a in meta.get("authors") or []}
        if not citing["authors"]:
            citing["authors"] = [{"name": a.get("name", ""), "affiliation": a.get("affiliation", "") or ""}
                                 for a in meta.get("authors") or [] if a.get("name")]
            return citing
        for a in citing["authors"]:
            if not a.get("affiliation"):
                m = by_key.get(name_key(a.get("name", "")))
                if m and m.get("affiliation"):
                    a["affiliation"] = m["affiliation"]
        return citing


_index_cache: dict = {}


def get_local_index(roots: List[Path], max_age: float = 600.0) -> LocalCitingIndex:
    key = tuple(str(r) for r in roots)
    hit = _index_cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    idx = LocalCitingIndex(roots).load()
    _index_cache[key] = (time.time(), idx)
    return idx


def build_fast_report(
    target_papers: List[dict],
    scholar_name: str,
    honor: HonorList,
    index: LocalCitingIndex,
    kaggle: Optional[KaggleArxivMeta] = None,
    log: Callable[[str], None] = print,
) -> dict:
    """Assemble the quick report dict (pure: only local lookups)."""
    scholar_key = name_key(scholar_name)
    targets_out: List[dict] = []
    citers: Dict[tuple, dict] = {}
    n_citing = n_self = n_name_only = n_with_local = n_meta = 0
    for tp in target_papers:
        title = tp.get("title", "")
        meta = kaggle.lookup(title=title) if kaggle is not None else None
        if meta:
            n_meta += 1
        citing_list = index.citing_for(title)
        if citing_list:
            n_with_local += 1
        hits_here = []
        for citing in citing_list:
            citing = index.fill_affiliations({**citing, "authors": [dict(a) for a in citing["authors"]]})
            if not citing["authors"] and kaggle is not None:
                km = kaggle.lookup(title=citing["title"], doi=citing.get("doi", ""))
                if km:
                    citing["authors"] = [{"name": n, "affiliation": ""} for n in km.get("authors") or []]
            if scholar_key and any(name_key(a.get("name", "")) == scholar_key for a in citing["authors"]):
                n_self += 1
                continue
            n_citing += 1
            for a in citing["authors"]:
                res = honor.match(a.get("name", ""), a.get("affiliation", ""), a.get("email", ""))
                n_name_only += res["name_only"]
                for h in res["hits"]:
                    hit = {**h, "author": a.get("name", ""), "author_affiliation": a.get("affiliation", ""),
                           "author_email": a.get("email", ""), "citing_title": citing["title"],
                           "citing_year": citing.get("year"), "citing_source": citing.get("source", "")}
                    hits_here.append(hit)
                    agg = citers.setdefault((h["name"].lower(), h["listed_affiliation"].lower()), {
                        "name": h["name"], "name_zh": h.get("name_zh", ""), "honors": [],
                        "basis": h["basis"], "matched_value": h["matched_value"],
                        "listed_affiliation": h["listed_affiliation"], "citing_papers": [],
                        "cited_targets": []})
                    if h["honor"] not in agg["honors"]:
                        agg["honors"].append(h["honor"])
                    if citing["title"] not in agg["citing_papers"]:
                        agg["citing_papers"].append(citing["title"])
                    if title not in agg["cited_targets"]:
                        agg["cited_targets"].append(title)
        targets_out.append({
            "title": title, "year": tp.get("year"), "citations": tp.get("citations", 0),
            "arxiv_id": (meta or {}).get("arxiv_id", ""), "venue": (meta or {}).get("venue", ""),
            "metadata_source": "kaggle_arxiv" if meta else "profile",
            "local_citing_papers": len(citing_list), "honor_hits": hits_here,
        })
    honor_citers = sorted(citers.values(), key=lambda c: (-len(c["citing_papers"]), c["name"]))
    coverage = {
        "target_papers": len(target_papers),
        "targets_with_local_metadata": n_meta,
        "targets_with_local_citing_data": n_with_local,
        "local_citing_papers": n_citing,
        "self_citations_skipped": n_self,
        "honor_citers": len(honor_citers),
        "name_only_candidates_not_counted": n_name_only,
        "profile_citations_total": sum(int(t.get("citations") or 0) for t in target_papers),
    }
    log(f"[快查] 目标 {len(target_papers)} 篇，本地 metadata 命中 {n_meta} 篇，"
        f"{n_with_local} 篇有本地施引记录（共 {n_citing} 篇施引，跳过自引 {n_self}）")
    log(f"[快查] 荣誉名单命中 {len(honor_citers)} 人；仅姓名相同、单位/邮箱不符的候选 {n_name_only} 个未计入")
    return {
        "mode": REPORT_MODE, "disclaimer": DISCLAIMER, "scholar_name": scholar_name,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "coverage": coverage,
        "honor_list": honor.stats(), "honor_citers": honor_citers, "targets": targets_out,
        "data_roots": [str(r) for r in index.roots],
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
    ) or "<tr><td colspan='6'>本地数据中未发现荣誉名单学者的施引（不代表不存在）</td></tr>"
    trows = "".join(
        f"<tr><td>{e(t['title'])}</td><td>{e(t['year'])}</td><td>{e(t['citations'])}</td>"
        f"<td>{e(t['arxiv_id'] or '-')}</td><td>{e(t['local_citing_papers'])}</td>"
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
<h2>{e(report['scholar_name'] or '学者')} · 他引快查（metadata + 荣誉名单）</h2>
<div class="banner"><b>这不是全量施引。</b>{e(report['disclaimer'])}</div>
<p class="kv"><span>目标论文 {cov['target_papers']}</span><span>本地 metadata 命中 {cov['targets_with_local_metadata']}</span>
<span>有本地施引记录的目标 {cov['targets_with_local_citing_data']}</span><span>本地施引论文 {cov['local_citing_papers']}</span>
<span>主页显示总被引 {cov['profile_citations_total']}</span><span>跳过自引 {cov['self_citations_skipped']}</span></p>
<p>荣誉名单：{e(stats.get('total', 0))} 条（{e(sources)}）。匹配规则：{e(report['match_rule'])}。
仅姓名相同但单位/邮箱不符的候选 {cov['name_only_candidates_not_counted']} 个，未计入。</p>
<h3>命中的荣誉学者（{cov['honor_citers']}）</h3>
<table><tr><th>学者</th><th>荣誉</th><th>匹配依据</th><th>名单单位</th><th>施引篇数</th><th>施引论文（前 3）</th></tr>{rows}</table>
<h3>目标论文</h3>
<table><tr><th>标题</th><th>年份</th><th>主页被引</th><th>arXiv</th><th>本地施引记录</th><th>荣誉命中</th></tr>{trows}</table>
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
        "Listed_Affiliation": h["listed_affiliation"], "Citing_Data_Source": h["citing_source"],
    } for t in report["targets"] for h in t["honor_hits"]]
    try:
        import pandas as pd
        pd.DataFrame(rows or [{"Note": report["disclaimer"]}]).to_excel(excel_file, index=False)
    except Exception:  # noqa: BLE001 - excel is a convenience copy of the JSON
        excel_file = None
    return {"json": json_file, "dashboard": html_file, "excel": excel_file}
