"""Read-only paper metadata lookup against the local Kaggle arXiv snapshot index.

The index is the SQLite file built by Daily Paper's
``scripts/build_kaggle_arxiv_index.py`` from Cornell-University/arxiv
(``arxiv-metadata-oai-snapshot.json``, ~3M papers). Lookups are exact:

    arxiv_id   primary key (version suffix stripped)
    doi        lower-cased, indexed
    title_norm same normalization as ``arxiv_db.normalize_title``, indexed

Location: ``$KAGGLE_ARXIV_INDEX`` (Docker: ``/kaggle-arxiv/index.sqlite3``).
When the file is missing every lookup returns None and callers fall back to
their external APIs.

CLI:
    python -m citationclaw.core.kaggle_arxiv_meta id <arxiv_id>
    python -m citationclaw.core.kaggle_arxiv_meta title <title>
    python -m citationclaw.core.kaggle_arxiv_meta doi <doi>
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

ENV_INDEX = "KAGGLE_ARXIV_INDEX"

_NEW_ID_RE = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?![\d])")
_OLD_ID_RE = re.compile(r"([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.IGNORECASE)
_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s?#]+)", re.IGNORECASE)


def normalize_title(title: str) -> str:
    t = str(title or "").lower()
    t = re.sub(r"[^\w\u4e00-\u9fff\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_arxiv_id(arxiv_id: str) -> str:
    aid = str(arxiv_id or "").strip()
    aid = re.sub(r"^(?:https?://)?(?:www\.)?(?:export\.)?arxiv\.org/(?:abs|pdf)/", "", aid, flags=re.IGNORECASE)
    aid = re.sub(r"^arxiv:", "", aid, flags=re.IGNORECASE)
    aid = re.sub(r"\.pdf$", "", aid, flags=re.IGNORECASE)
    return re.sub(r"v\d+$", "", aid).strip()


def arxiv_id_from_url(url: str) -> str:
    """Extract an arXiv id from an arxiv.org / DOI 10.48550 URL, else ''."""
    u = unquote(str(url or ""))
    low = u.lower()
    if "arxiv.org/" in low:
        tail = re.split(r"arxiv\.org/(?:abs|pdf|html)/", u, flags=re.IGNORECASE)[-1]
        m = _NEW_ID_RE.search(tail) or _OLD_ID_RE.search(tail)
        return normalize_arxiv_id(m.group(1)) if m else ""
    m = re.search(r"10\.48550/arxiv\.(\S+)", u, flags=re.IGNORECASE)
    if m:
        return normalize_arxiv_id(m.group(1))
    return ""


def doi_from_url(url: str) -> str:
    u = unquote(str(url or ""))
    if "doi.org/" not in u.lower():
        return ""
    m = _DOI_RE.search(u)
    return m.group(1).rstrip(".").lower() if m else ""


def _year(published: str, update_date: str) -> Optional[int]:
    for raw in (published, update_date):
        m = re.match(r"(\d{4})", str(raw or ""))
        if m:
            return int(m.group(1))
    return None


class KaggleArxivMeta:
    """Thread-safe read-only connection to the Kaggle arXiv index."""

    def __init__(self, db_path: Optional[Path] = None):
        raw = str(db_path or os.getenv(ENV_INDEX) or "").strip()
        self.db_path = Path(raw) if raw else None
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._ok: Optional[bool] = None

    @property
    def available(self) -> bool:
        if self._ok is None:
            self._ok = self._open()
        return bool(self._ok)

    def _open(self) -> bool:
        if not self.db_path or not self.db_path.is_file():
            return False
        try:
            uri = f"file:{quote(self.db_path.as_posix())}?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
            if not {"arxiv_id", "title", "title_norm", "doi"} <= cols:
                conn.close()
                print(f"[Kaggle] 索引缺少 title_norm/doi 列，需用新版脚本重建: {self.db_path}", flush=True)
                return False
            self._conn = conn
            return True
        except sqlite3.Error as e:
            print(f"[Kaggle] 打开索引失败 {self.db_path}: {e}", flush=True)
            return False

    def _one(self, sql: str, params: tuple) -> Optional[dict]:
        if not self.available:
            return None
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return self._row_to_dict(row) if row else None

    _COLS = ("arxiv_id, title, abstract, authors, categories, published, "
             "update_date, doi, journal_ref, authors_json")
    # 核对施引时用不到摘要。摘要经常在溢出页里，不读它，大批量核对会快一截。
    _BRIEF_COLS = ("arxiv_id, title, '' AS abstract, authors, categories, published, "
                   "update_date, doi, journal_ref, authors_json")

    def _cols(self, brief: bool) -> str:
        return self._BRIEF_COLS if brief else self._COLS

    def lookup_by_id(self, arxiv_id: str, *, brief: bool = False) -> Optional[dict]:
        aid = normalize_arxiv_id(arxiv_id)
        if not aid:
            return None
        return self._one(f"SELECT {self._cols(brief)} FROM papers WHERE arxiv_id = ?", (aid,))

    def lookup_by_doi(self, doi: str, *, brief: bool = False) -> Optional[dict]:
        d = str(doi or "").strip().lower()
        d = re.sub(r"^(?:https?://)?(?:dx\.)?doi\.org/", "", d)
        if not d:
            return None
        if d.startswith("10.48550/arxiv."):
            return self.lookup_by_id(d.split("arxiv.", 1)[1], brief=brief)
        return self._one(
            f"SELECT {self._cols(brief)} FROM papers WHERE doi = ? AND doi <> '' LIMIT 1", (d,)
        )

    def lookup_by_title(self, title: str, *, brief: bool = False) -> Optional[dict]:
        norm = normalize_title(title)
        if len(norm) < 8:
            return None
        return self._one(
            f"SELECT {self._cols(brief)} FROM papers WHERE title_norm = ? "
            "ORDER BY published DESC LIMIT 1",
            (norm,),
        )

    def lookup(self, title: str = "", arxiv_id: str = "", doi: str = "",
               url: str = "", *, brief: bool = False) -> Optional[dict]:
        """Try arxiv_id → DOI → URL-derived ids → exact title. First hit wins."""
        if not self.available:
            return None
        rec = None
        if arxiv_id:
            rec = self.lookup_by_id(arxiv_id, brief=brief)
        if rec is None and doi:
            rec = self.lookup_by_doi(doi, brief=brief)
        if rec is None and url:
            aid = arxiv_id_from_url(url)
            if aid:
                rec = self.lookup_by_id(aid, brief=brief)
            if rec is None:
                d = doi_from_url(url)
                if d:
                    rec = self.lookup_by_doi(d, brief=brief)
        if rec is None and title:
            rec = self.lookup_by_title(title, brief=brief)
        return rec

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._ok = None

    @staticmethod
    def _row_to_dict(row) -> dict:
        (aid, title, abstract, authors_raw, categories, published,
         update_date, doi, journal_ref, authors_json) = row
        try:
            authors = json.loads(authors_json or "[]")
        except json.JSONDecodeError:
            authors = []
        if not authors and authors_raw:
            authors = [a.strip() for a in re.split(r",\s*|\s+and\s+", authors_raw) if a.strip()]
        return {
            "arxiv_id": aid,
            "title": re.sub(r"\s+", " ", title or "").strip(),
            "abstract": abstract or "",
            "authors": authors,
            "categories": categories or "",
            "published": published or "",
            "year": _year(published, update_date),
            "doi": doi or "",
            "venue": journal_ref or "",
            "pdf_url": f"https://arxiv.org/pdf/{aid}",
            "url": f"https://arxiv.org/abs/{aid}",
            "source": "kaggle_arxiv",
        }


_shared: Optional[KaggleArxivMeta] = None
_shared_lock = threading.Lock()


def get_kaggle_meta() -> KaggleArxivMeta:
    """Process-wide shared instance (opened lazily on first lookup)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = KaggleArxivMeta()
        return _shared


def to_collector_metadata(rec: dict) -> dict:
    """Kaggle record → MetadataCollector result shape."""
    return {
        "title": rec.get("title", ""),
        "year": rec.get("year"),
        "doi": rec.get("doi", ""),
        "cited_by_count": 0,
        "influential_citation_count": 0,
        "s2_id": "",
        "arxiv_id": rec.get("arxiv_id", ""),
        "venue": rec.get("venue", ""),
        "pdf_url": rec.get("pdf_url", ""),
        "oa_pdf_url": "",
        "abstract": rec.get("abstract", ""),
        "authors": [
            {"name": n, "s2_id": "", "affiliation": ""} for n in rec.get("authors", [])
        ],
        "sources": ["kaggle_arxiv"],
    }


def enrich_papers_local(papers: list) -> int:
    """Fill arxiv_id / authors / venue / abstract of a paper list from the local
    index (by exact title). Existing fields are kept. Returns the hit count."""
    meta = get_kaggle_meta()
    if not meta.available:
        return 0
    hits = 0
    for p in papers or []:
        if not isinstance(p, dict):
            continue
        rec = meta.lookup(title=p.get("title", ""), arxiv_id=p.get("arxiv_id", ""),
                          doi=p.get("doi", ""))
        if not rec:
            continue
        hits += 1
        if not p.get("arxiv_id"):
            p["arxiv_id"] = rec["arxiv_id"]
        for key in ("authors", "venue", "abstract", "doi"):
            if not p.get(key) and rec.get(key):
                p[key] = rec[key]
        if not p.get("year") and rec.get("year"):
            p["year"] = rec["year"]
        p["metadata_source"] = "kaggle_arxiv"
    return hits


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in {"id", "title", "doi"}:
        print("Usage: python -m citationclaw.core.kaggle_arxiv_meta id|title|doi <value>")
        return
    meta = get_kaggle_meta()
    if not meta.available:
        print(f"Kaggle 索引不可用: {meta.db_path}")
        return
    value = " ".join(sys.argv[2:])
    fn = {"id": meta.lookup_by_id, "title": meta.lookup_by_title, "doi": meta.lookup_by_doi}[sys.argv[1]]
    rec = fn(value)
    print(json.dumps(rec, ensure_ascii=False, indent=2) if rec else "未找到")


if __name__ == "__main__":
    main()
