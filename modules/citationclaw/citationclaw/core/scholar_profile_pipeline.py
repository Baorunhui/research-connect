"""Scholar-profile fast pipeline.

Flow:
  1. Scholar profile → paper list (live ScraperAPI or uploaded HTML)
  2. Filter to top-N most-cited papers
  3. For each top paper, fetch citing papers via Semantic Scholar
     (titles + authors in one call, no Google Scholar pagination/sleep)
  4. Authors are matched against the local renowned-scholar SQLite DB
  5. (optional) LLM fallback for high-cited papers with no DB hit
  6. PDF download is deferred to the very end and scoped to citing papers
     that contain ≥1 renowned scholar — minimising download count.

The functions here are intentionally pure / dependency-light so they can be
unit-tested without the FastAPI app or the task executor.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

from citationclaw.core.arxiv_db import ArxivDB, ensure_cache
from citationclaw.core.pipeline_adapter import PipelineAdapter
from citationclaw.core.s2_client import S2Client
from citationclaw.core.scholar_db import ScholarDB
from citationclaw.core.self_citation import SelfCitationDetector


def filter_top_papers(
    papers: List[dict],
    top_n: int = 30,
    min_citations: int = 0,
) -> List[dict]:
    """Select the most-cited papers from a scholar profile.

    ``papers`` is expected sorted by citations desc (as returned by
    ScholarProfileScraper / parse_html). Applies a minimum-citation floor
    first, then caps to ``top_n``.
    """
    if min_citations and min_citations > 0:
        filtered = [p for p in papers if p.get("citations", 0) >= min_citations]
    else:
        filtered = list(papers)
    if top_n and top_n > 0:
        filtered = filtered[:top_n]
    return filtered


def db_hit_to_renowned(db_hit: dict, matched_name: str) -> dict:
    """Convert a ScholarDB lookup result into the renowned-scholar record shape
    expected by ``PipelineAdapter.to_legacy_record`` (and the exporter).

    Keys: name, tier, honors(list), affiliation, country, position.
    """
    honors = db_hit.get("honors") or []
    if isinstance(honors, str):
        honors = [h.strip() for h in honors.split(",") if h.strip()]
    title = db_hit.get("title", "") or ""
    return {
        "name": matched_name,
        "tier": title,
        "honors": honors,
        "affiliation": db_hit.get("affiliation", "") or "",
        "country": db_hit.get("country", "") or "",
        "position": title,
    }


def match_authors_with_db(
    authors: List[dict],
    scholar_db: ScholarDB,
) -> List[dict]:
    """Match a citing paper's authors against the local ScholarDB.

    Tries each author's name (English form as returned by S2). ScholarDB
    indexes both Chinese ``name`` and English ``name_en`` plus aliases, so an
    English query can still hit a Chinese-name record. Returns the list of
    renowned-scholar dicts (may be empty). Deduplicated by author name.
    """
    if not authors:
        return []
    hits: List[dict] = []
    seen: set = set()
    for a in authors:
        name = (a.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        db_hit = scholar_db.lookup(name)
        if db_hit:
            hits.append(db_hit_to_renowned(db_hit, name))
    return hits


async def fetch_target_citations(
    s2: S2Client,
    target_papers: List[dict],
    log,
    max_citations_per_paper: int = 5000,
    concurrency: int = 3,
    cancel_check=None,
    arxiv_db: Optional[ArxivDB] = None,
    arxiv_client=None,
) -> List[Tuple[dict, Optional[dict], List[dict]]]:
    """Fetch citing papers (with authors) for every target paper via S2.

    Returns a list of ``(target_paper, target_s2_meta, citing_papers)`` tuples.
    ``target_s2_meta`` is None when S2 could not resolve the title. Each citing
    paper dict is the S2 parsed shape (title, year, authors, cited_by_count,
    doi, arxiv_id, pdf_url, s2_id, venue, ...).

    S2's own internal semaphore bounds raw request concurrency; the outer
    semaphore here bounds how many target papers are processed in parallel
    (each does a paginated citations walk).

    When ``arxiv_db`` is provided, target titles are first looked up in the
    local arXiv title→author DB; a hit is resolved through the exact
    ``/paper/ArXiv:{id}`` endpoint (more reliable than fuzzy title search),
    and papers whose arxiv_id is still unknown are cached into the DB
    best-effort for future runs.
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _fetch(tp: dict) -> Tuple[dict, Optional[dict], List[dict]]:
        if cancel_check and cancel_check():
            return (tp, None, [])
        async with sem:
            title = tp.get("title", "")
            meta = None
            # Papers obtained from S2's author endpoint already carry their
            # canonical paperId.  Reuse it directly: title search is slower,
            # fuzzy, and especially fragile on the shared anonymous quota.
            if tp.get("s2_id"):
                meta = dict(tp)
                meta.setdefault("cited_by_count", tp.get("citations", 0) or 0)
                log(f"  [S2] 直接使用 paperId: {title[:60]}")
            elif arxiv_db is not None:
                db_hit = arxiv_db.lookup_by_title(title)
                if db_hit and db_hit.get("arxiv_id"):
                    meta = await s2.get_paper_by_arxiv_id(db_hit["arxiv_id"])
                    if meta:
                        log(f"  [S2] arXiv库命中 {db_hit['arxiv_id']}: {title[:60]}")
            if meta is None:
                meta = await s2.search_paper(title)
            if not meta or not meta.get("s2_id"):
                log(f"  [S2] 未找到目标论文: {title[:60]}")
                return (tp, None, [])
            # best-effort: seed the arXiv cache with the resolved arxiv_id
            if (
                arxiv_db is not None
                and arxiv_client is not None
                and meta.get("arxiv_id")
                and arxiv_db.lookup_by_id(meta["arxiv_id"]) is None
            ):
                ensure_cache(arxiv_db, arxiv_client, meta["arxiv_id"], title, log)
            if cancel_check and cancel_check():
                return (tp, meta, [])
            citing = await s2.get_paper_citations(
                meta["s2_id"], max_papers=max_citations_per_paper
            )
            log(f"  [S2] {title[:60]}... → {len(citing)} 篇施引文献")
            return (tp, meta, citing)

    return await asyncio.gather(*[_fetch(tp) for tp in target_papers])


async def enrich_citing_authors(
    citing_list: List[dict],
    arxiv_db: Optional[ArxivDB],
    arxiv_client,
    log,
    max_fetch: int = 50,
    wall_timeout: float = 300.0,
) -> dict:
    """Complete citing-paper author lists using the local arXiv DB.

    S2 author lists are often incomplete (missing middle authors), which
    directly hurts renowned-scholar match recall. arXiv author lists are
    complete and authoritative for arXiv papers. For each citing paper that
    carries an ``arxiv_id``:

      - DB hit  → authors replaced with the full arXiv list (no network);
      - DB miss → fetched from the arXiv API (rate-limited, bounded by
        ``max_fetch``) and cached for future runs.

    Papers without an arxiv_id are left untouched. Mutates ``citing_list``
    in place; returns stats ``{hit, fetched, missed}``. Best-effort: never
    raises.  A wall-clock ``wall_timeout`` (default 5 min) caps the total
    runtime — on timeout, already-completed papers are kept and the rest
    are skipped (arXiv can be very slow from some networks).
    """
    if arxiv_db is None or arxiv_client is None:
        return {"hit": 0, "fetched": 0, "missed": 0}
    from citationclaw.core.arxiv_db import fetch_and_cache

    sem = asyncio.Semaphore(2)  # stay near arXiv's 3 req/s budget
    budget_lock = asyncio.Lock()
    stats = {"hit": 0, "fetched": 0, "missed": 0}
    done_count = 0
    total = len(citing_list)
    fetch_budget = max(0, int(max_fetch))

    def _replace_authors(citing: dict, names: List[str]):
        citing["authors"] = [
            {"name": n, "s2_id": "", "affiliation": ""} for n in names
        ]

    async def _one(citing: dict):
        nonlocal fetch_budget, done_count
        aid = (citing.get("arxiv_id") or "").strip()
        if not aid:
            done_count += 1
            return
        rec = arxiv_db.lookup_by_id(aid)
        if rec and rec.get("authors"):
            _replace_authors(citing, rec["authors"])
            stats["hit"] += 1
            done_count += 1
            return
        async with budget_lock:
            if fetch_budget <= 0:
                stats["missed"] += 1
                done_count += 1
                return
            fetch_budget -= 1
        try:
            async with sem:
                rec = await fetch_and_cache(
                    arxiv_db, arxiv_client, arxiv_id=aid,
                    title=citing.get("title", ""), log=log,
                )
            if rec and rec.get("authors"):
                _replace_authors(citing, rec["authors"])
                stats["fetched"] += 1
            else:
                stats["missed"] += 1
        except Exception as e:
            stats["missed"] += 1
            log(f"  [arXiv库] 补全失败 {aid}: {e}")
        finally:
            done_count += 1
            if done_count % 25 == 0 and done_count < total:
                log(
                    f"  [arXiv库] 进度 {done_count}/{total}: "
                    f"库命中 {stats['hit']}, API补全 {stats['fetched']}, "
                    f"未覆盖 {stats['missed']}"
                )

    try:
        await asyncio.wait_for(
            asyncio.gather(*[_one(c) for c in citing_list]),
            timeout=wall_timeout,
        )
    except asyncio.TimeoutError:
        log(
            f"  [arXiv库] 补全超时（{int(wall_timeout)}s），已完成 "
            f"{done_count}/{total}，跳过剩余"
        )
    if stats["hit"] or stats["fetched"] or stats["missed"]:
        log(
            f"  [arXiv库] 施引作者补全: 库命中 {stats['hit']} 篇, "
            f"API 补全 {stats['fetched']} 篇, 未覆盖 {stats['missed']} 篇"
        )
    return stats


def _build_paper_and_metadata(citing: dict) -> Tuple[dict, dict]:
    """Build the (paper, metadata) dicts expected by to_legacy_record from one
    S2 citing-paper dict."""
    authors = citing.get("authors", []) or []
    authors_raw = {}
    for i, a in enumerate(authors):
        name = a.get("name", "")
        if name:
            authors_raw[f"author_{i}_{name}"] = a.get("s2_id", "") or ""

    s2_id = citing.get("s2_id", "") or ""
    paper = {
        "paper_title": citing.get("title", "") or "",
        "paper_link": (
            f"https://www.semanticscholar.org/paper/{s2_id}" if s2_id else ""
        ),
        "paper_year": citing.get("year"),
        "citation": str(citing.get("cited_by_count", 0) or 0),
        "authors_raw": authors_raw,
    }
    metadata = {
        "title": citing.get("title", "") or "",
        "year": citing.get("year"),
        "doi": citing.get("doi", "") or "",
        "venue": citing.get("venue", "") or "",
        "pdf_url": citing.get("pdf_url", "") or "",
        "arxiv_id": citing.get("arxiv_id", "") or "",
        "s2_id": s2_id,
        "authors": [
            {
                "name": a.get("name", "") or "",
                "affiliation": a.get("affiliation", "") or "",
                "country": "",
                "s2_id": a.get("s2_id", "") or "",
            }
            for a in authors
        ],
        "sources": ["s2"],
    }
    return paper, metadata


def build_citing_record(
    adapter: PipelineAdapter,
    citing: dict,
    target_title: str,
    self_cite_result: dict,
    renowned: List[dict],
    record_index: int,
) -> dict:
    """Build a single legacy-format record {idx: {...}} for one citing paper.

    PDF fields are left empty here — download is deferred to the end and only
    performed for papers that contain renowned scholars.
    """
    paper, metadata = _build_paper_and_metadata(citing)
    return adapter.to_legacy_record(
        paper=paper,
        metadata=metadata,
        self_citation=self_cite_result,
        renowned_scholars=renowned,
        citing_paper=target_title,
        record_index=record_index,
        api_authors_snapshot=metadata["authors"],
        pdf_authors_snapshot=None,
        pdf_downloaded=False,
        pdf_path="",
    )


def collect_target_authors(target_meta: Optional[dict]) -> List[dict]:
    """Extract the target paper's own authors (for self-citation detection)."""
    if not target_meta:
        return []
    return target_meta.get("authors", []) or []


def is_self_citation(
    detector: SelfCitationDetector,
    target_authors: List[dict],
    citing_authors: List[dict],
) -> dict:
    """Wrapper around SelfCitationDetector.check returning its dict result."""
    return detector.check(target_authors, citing_authors)
