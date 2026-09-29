"""Citing works of a paper list from OpenAlex, metadata only.

For every target paper the matching OpenAlex work(s) are resolved (DOI, then an
exact-title search that also picks up preprint/published duplicates), and all
works citing them are paged with ``filter=cites:W…`` + cursor, 200 per page.
Only ``id,display_name,publication_year,doi,ids,authorships`` are selected:
no locations, PDFs, landing pages, abstracts or full text are ever requested,
and every request is checked to stay on ``api.openalex.org/works``.

Large targets (> ``split_threshold`` citations) are split by publication year
(``group_by=publication_year``) so several cursor chains run in parallel under
one polite-pool rate limit (``mailto``, token bucket, 429/5xx backoff).

Raw citing records are cached per target under ``<cache_dir>/citing`` (gzip
JSON, one file per completed year partition) and never refetched.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx

from citationclaw.core.http_utils import make_async_client
from citationclaw.core.kaggle_arxiv_meta import normalize_arxiv_id, normalize_title
from citationclaw.core.openalex_client import BASE_URL, OpenAlexClient

WORKS_URL = f"{BASE_URL}/works"
CITING_SELECT = "id,display_name,publication_year,doi,ids,authorships"
TARGET_SELECT = "id,display_name,publication_year,doi,ids,cited_by_count"
AUTHOR_WORK_SELECT = "id,display_name,publication_year,doi,ids,cited_by_count,authorships"
PER_PAGE = 200
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_FORBIDDEN = ("pdf", "location", "abstract", "fulltext", "content_url")


def short_id(openalex_id: str) -> str:
    return str(openalex_id or "").rstrip("/").rsplit("/", 1)[-1]


def norm_doi(doi: str) -> str:
    d = str(doi or "").strip().lower()
    return re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/|^doi:", "", d)


def arxiv_id_from_doi(doi: str) -> str:
    d = norm_doi(doi)
    return normalize_arxiv_id(d.split("arxiv.", 1)[1]) if d.startswith("10.48550/arxiv.") else ""


def compact_work(work: dict) -> dict:
    """OpenAlex work → ``{id,title,year,doi,arxiv_id,authors:[{name,affiliation,email}]}``."""
    doi = norm_doi(work.get("doi") or (work.get("ids") or {}).get("doi"))
    authors = []
    for au in work.get("authorships") or []:
        name = (au.get("author") or {}).get("display_name") or au.get("raw_author_name") or ""
        if not name:
            continue
        insts = [i.get("display_name", "") for i in au.get("institutions") or [] if i.get("display_name")]
        raw = [s for s in au.get("raw_affiliation_strings") or [] if s]
        emails = _EMAIL_RE.findall(" ".join(raw))
        affs = list(dict.fromkeys(insts or raw))
        authors.append({"name": name, "affiliation": "; ".join(affs),
                        "email": emails[0].lower() if emails else ""})
    return {"id": short_id(work.get("id")), "title": work.get("display_name") or work.get("title") or "",
            "year": work.get("publication_year"), "doi": doi, "arxiv_id": arxiv_id_from_doi(doi),
            "authors": authors}


class _RateLimiter:
    def __init__(self, rate: float):
        self.interval = 1.0 / max(rate, 0.1)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self.interval
        if at > now:
            await asyncio.sleep(at - now)


class OpenAlexCitingFetcher:
    def __init__(self, cache_dir: Path, email: str = "", api_key: str = "",
                 rate: float = 8.0, concurrency: int = 8, split_threshold: int = 2000,
                 log: Callable[[str], None] = print, client: Optional[httpx.AsyncClient] = None,
                 route: str = "", should_cancel: Callable[[], bool] = lambda: False):
        self.cache_dir = Path(cache_dir)
        self.params = {k: v for k, v in (("mailto", email), ("api_key", api_key)) if v}
        self.limiter = _RateLimiter(rate)
        self.sem = asyncio.Semaphore(concurrency)
        self.split_threshold = split_threshold
        self.log = log
        self.should_cancel = should_cancel
        self._client = client
        self._client_lock = asyncio.Lock()
        self.route = route or os.getenv("CITATIONCLAW_OPENALEX_ROUTE", "auto").strip().lower() or "auto"
        self.requests = 0
        self.retries = 0
        self.requested_urls: List[str] = []
        self.fetched_records = 0
        self._t0 = time.monotonic()

    # ── HTTP ──────────────────────────────────────────────────────────────
    async def _ensure_client(self):
        if self._client is not None:
            return
        async with self._client_lock:
            if self._client is None:
                await self._probe_route()

    async def _probe_route(self):
        order = {"direct": [False], "proxy": [True]}.get(self.route, [False, True])
        for use_proxy in order:
            client = make_async_client(timeout=30.0, use_proxy=use_proxy)
            try:
                r = await client.get(WORKS_URL, params={"per-page": 1, "select": "id", **self.params})
                if r.status_code == 200 or len(order) == 1:
                    self._client = client
                    self.route = "proxy" if use_proxy else "direct"
                    self.log("已连上论文目录。")
                    return
            except httpx.HTTPError as e:
                print(f"[openalex] route {'proxy' if use_proxy else 'direct'} failed: {type(e).__name__}", flush=True)
                if len(order) == 1:
                    self._client = client
                    return
            await client.aclose()
        raise RuntimeError("论文目录暂时连不上，请稍后重试。")

    @staticmethod
    def _check_url(url: str, params: dict):
        if not url.startswith(WORKS_URL):
            print(f"[openalex] refused url: {url}", flush=True)
            raise ValueError("这次请求超出了论文目录的范围，已停止。")
        probe = (url[len(BASE_URL):] + " " + str(params.get("select", ""))).lower()
        if any(f in probe for f in _FORBIDDEN):
            print(f"[openalex] refused full-text request: {url}", flush=True)
            raise ValueError("这次请求超出了论文目录的范围，已停止。")

    async def _get(self, url: str, params: dict, attempts: int = 6) -> Optional[dict]:
        self._check_url(url, params)
        await self._ensure_client()
        full = {**params, **self.params}
        for attempt in range(attempts):
            await self.limiter.wait()
            async with self.sem:
                self.requests += 1
                self.requested_urls.append(url)
                try:
                    r = await self._client.get(url, params=full)
                except httpx.HTTPError as e:
                    status, err, retry_after = 0, type(e).__name__, None
                else:
                    if r.status_code == 200:
                        return r.json()
                    if r.status_code == 404:
                        return None
                    status, err = r.status_code, f"HTTP {r.status_code}"
                    retry_after = r.headers.get("Retry-After")
                    if status not in (429, 500, 502, 503, 504):
                        print(f"[openalex] {err} {url} {r.text[:160]}", flush=True)
                        self.log("论文目录这一页没有返回，先跳过。")
                        return None
            self.retries += 1
            try:
                wait = float(retry_after) if retry_after else min(2 ** attempt, 30)
            except ValueError:
                wait = min(2 ** attempt, 30)
            self.log("论文目录暂时没有响应，稍后会再试。")
            await asyncio.sleep(wait)
        return None

    # ── target resolution ────────────────────────────────────────────────
    def _cache_file(self, kind: str, key: str) -> Path:
        return self.cache_dir / kind / f"{hashlib.sha1(key.encode()).hexdigest()[:20]}.json.gz"

    @staticmethod
    def _read(path: Path) -> Optional[dict]:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write(path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        tmp.replace(path)

    async def resolve(self, title: str, doi: str = "", arxiv_id: str = "", year=None) -> dict:
        """Return ``{works:[{id,title,year,cited_by_count}], cited_by_count, resolved_by}``."""
        tnorm = normalize_title(title)
        cfile = self._cache_file("targets", tnorm or norm_doi(doi) or arxiv_id)
        cached = self._read(cfile)
        if cached is not None:
            return {**cached, "cached": True}
        found: Dict[str, dict] = {}
        by = []
        for d in [norm_doi(doi)] + ([f"10.48550/arxiv.{normalize_arxiv_id(arxiv_id)}"] if arxiv_id else []):
            if not d:
                continue
            w = await self._get(f"{WORKS_URL}/doi:{d}", {"select": TARGET_SELECT})
            if w and w.get("id"):
                found[short_id(w["id"])] = w
                by.append("doi")
        if tnorm:
            clean = " ".join(re.sub(r"[:\-,;'\"()（）\[\]|!?]", " ", title).split())
            data = await self._get(WORKS_URL, {"filter": f"title.search:{clean}",
                                               "select": TARGET_SELECT, "per-page": 10})
            results = (data or {}).get("results") or []
            exact = [w for w in results if normalize_title(w.get("display_name", "")) == tnorm]
            if year:
                exact = [w for w in exact if not w.get("publication_year")
                         or abs(int(w["publication_year"]) - int(year)) <= 2] or exact
            if not exact and not found and results and OpenAlexClient._titles_match(
                    title, results[0].get("display_name", ""), threshold=0.8):
                exact = results[:1]
            for w in exact:
                found.setdefault(short_id(w["id"]), w)
            if exact:
                by.append("title")
        works = [{"id": k, "title": w.get("display_name", ""), "year": w.get("publication_year"),
                  "cited_by_count": int(w.get("cited_by_count") or 0)} for k, w in found.items()]
        works = [w for w in works if w["cited_by_count"] > 0] or works[:1]
        out = {"works": works, "cited_by_count": sum(w["cited_by_count"] for w in works),
               "resolved_by": "+".join(dict.fromkeys(by))}
        if works:
            self._write(cfile, out)
        return {**out, "cached": False}

    async def fetch_author_works(self, author_id: str, top_n: int = 0,
                                 min_citations: int = 0) -> dict:
        """One author's works, most-cited first.

        Returns ``{name, papers:[{title,year,citations,doi,arxiv_id,openalex_id}]}``.
        ``top_n`` / ``min_citations`` stop the cursor early (0 = no limit). Sorted
        results below ``min_citations`` are not requested on later pages.
        """
        aid = short_id(author_id)
        if not re.fullmatch(r"A\d+", aid):
            return {"name": "", "papers": []}
        papers: List[dict] = []
        name = ""
        cursor = "*"
        while cursor and not self.should_cancel():
            data = await self._get(WORKS_URL, {
                "filter": f"authorships.author.id:{aid}",
                "sort": "cited_by_count:desc",
                "select": AUTHOR_WORK_SELECT,
                "per-page": PER_PAGE,
                "cursor": cursor,
            })
            if not data:
                break
            batch = data.get("results") or []
            for work in batch:
                if not name:
                    for au in work.get("authorships") or []:
                        author = au.get("author") or {}
                        if short_id(author.get("id")) == aid:
                            name = author.get("display_name") or ""
                            break
                compact = compact_work(work)
                papers.append({
                    "title": compact["title"],
                    "year": compact["year"],
                    "citations": int(work.get("cited_by_count") or 0),
                    "doi": compact["doi"],
                    "arxiv_id": compact["arxiv_id"],
                    "openalex_id": compact["id"],
                })
            floor = min_citations if min_citations and min_citations > 0 else 0
            enough = top_n and sum(1 for p in papers if p["citations"] >= floor) >= top_n
            below = floor and batch and int(batch[-1].get("cited_by_count") or 0) < floor
            cursor = (data.get("meta") or {}).get("next_cursor") if batch else None
            if enough or below or not batch:
                break
        return {"name": name, "papers": papers}

    # ── citing works ─────────────────────────────────────────────────────
    async def _chain(self, filt: str, label: str, expected: int, progress: dict) -> Optional[List[dict]]:
        records: List[dict] = []
        cursor = "*"
        while cursor and not self.should_cancel():
            data = await self._get(WORKS_URL, {"filter": filt, "select": CITING_SELECT,
                                               "per-page": PER_PAGE, "cursor": cursor})
            if data is None:
                self.log(f"这一部分没有拉完，已拿到 {len(records)} 条。")
                return None
            batch = data.get("results") or []
            records.extend(compact_work(w) for w in batch)
            self.fetched_records += len(batch)
            progress["done"] += len(batch)
            cursor = (data.get("meta") or {}).get("next_cursor") if batch else None
            now = time.monotonic()
            if now - progress["last_log"] >= 5 or not cursor:
                progress["last_log"] = now
                self.log(f"已查到 {progress['done']}/{progress['total']} 条引用")
        return None if self.should_cancel() else records

    async def fetch_citing(self, work_ids: List[str], expected: int = 0, label: str = "") -> dict:
        """All works citing any of ``work_ids`` → ``{records, fetched, expected, complete, cached_parts}``."""
        ids = sorted({short_id(i) for i in work_ids if i})
        if not ids:
            return {"records": [], "fetched": 0, "expected": 0, "complete": True, "cached_parts": 0}
        key = "|".join(ids)
        cites = f"cites:{key}"
        base = self.cache_dir / "citing" / hashlib.sha1(key.encode()).hexdigest()[:20]
        manifest = self._read(base / "manifest.json.gz")
        parts = [("all", cites)]
        if manifest and all((base / f"{n}.json.gz").is_file() for n, _ in manifest["parts"]):
            parts = [tuple(x) for x in manifest["parts"]]
        elif expected > self.split_threshold and not (base / "all.json.gz").is_file():
            groups = await self._get(WORKS_URL, {"filter": cites, "group_by": "publication_year"})
            years = [(g.get("key"), int(g.get("count") or 0)) for g in (groups or {}).get("group_by") or []]
            if years and sum(c for _, c in years) >= expected * 0.98:
                parts = []
                for y, c in sorted(years):
                    if not y:
                        continue
                    if c <= self.split_threshold or (base / f"y{y}.json.gz").is_file():
                        parts.append((f"y{y}", f"{cites},publication_year:{y}"))
                        continue
                    for q, (a, b) in enumerate((("01-01", "03-31"), ("04-01", "06-30"),
                                                ("07-01", "09-30"), ("10-01", "12-31")), 1):
                        parts.append((f"y{y}q{q}", f"{cites},from_publication_date:{y}-{a},"
                                                   f"to_publication_date:{y}-{b}"))
        progress = {"label": label or key[:40], "done": 0, "total": expected, "last_log": 0.0}
        cached_parts = 0
        out: List[dict] = []
        todo = []
        for name, filt in parts:
            hit = self._read(base / f"{name}.json.gz")
            if hit is not None and hit.get("filter") == filt:
                out.extend(hit["records"])
                progress["done"] += len(hit["records"])
                cached_parts += 1
            else:
                todo.append((name, filt))
        if todo:
            self.log(f"预计 {expected} 条引用，开始拉取。")

        async def run(name, filt):
            recs = await self._chain(filt, f"{progress['label']}/{name}", expected, progress)
            if recs is not None:
                self._write(base / f"{name}.json.gz", {"filter": filt, "fetched_at": time.time(),
                                                        "records": recs})
            return recs

        results = await asyncio.gather(*(run(n, f) for n, f in todo))
        complete = all(r is not None for r in results)
        if complete and len(parts) > 1:
            self._write(base / "manifest.json.gz", {"parts": parts})
        for r in results:
            out.extend(r or [])
        seen, uniq = set(), []
        for r in out:
            if r["id"] not in seen:
                seen.add(r["id"])
                uniq.append(r)
        return {"records": uniq, "fetched": len(uniq), "expected": expected,
                "complete": complete, "cached_parts": cached_parts, "partitions": len(parts)}

    async def close(self):
        if self._client is not None:
            await self._client.aclose()


def verify_with_kaggle(records: List[dict], kaggle) -> int:
    """Check/fill citing-record bibliography against the local Kaggle arXiv index.

    Hit → arxiv_id/title/year filled, authors filled only if OpenAlex had none
    (OpenAlex affiliations are kept). Returns the hit count."""
    if kaggle is None:
        return 0
    hits = 0
    for r in records:
        m = kaggle.lookup(title=r.get("title", ""), arxiv_id=r.get("arxiv_id", ""), doi=r.get("doi", ""))
        if not m:
            continue
        hits += 1
        r["verified_by"] = "kaggle_arxiv"
        r["arxiv_id"] = r.get("arxiv_id") or m.get("arxiv_id", "")
        r["title"] = r.get("title") or m.get("title", "")
        r["year"] = r.get("year") or m.get("year")
        if not r.get("authors"):
            r["authors"] = [{"name": n, "affiliation": "", "email": ""} for n in m.get("authors") or []]
    return hits
