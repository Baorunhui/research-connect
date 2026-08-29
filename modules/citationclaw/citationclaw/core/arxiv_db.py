"""arXiv title → author local database (SQLite).

The second of the two local databases:

    scholars.db  —  author name → honors/affiliation   (is this person renowned?)
    arxiv.db     —  paper title → arxiv_id + authors   (who wrote this paper?)

Location: ~/.citationclaw/arxiv.db  (or $CITATIONCLAW_DATA_DIR/arxiv.db)

Uses in the scholar-profile fast pipeline:
  1. Step 3+4  Target papers are resolved via their exact arXiv ID
               (S2Client.get_paper_by_arxiv_id) instead of S2 fuzzy title
               search when a local hit exists. Every paper seen in the
               pipeline is opportunistically cached (incremental build).
  2. Step 5    Citing-paper author lists are completed from arXiv — S2
               author lists are often incomplete (missing middle authors),
               which directly reduces renowned-scholar match recall.
               arXiv author lists are complete and authoritative for
               arXiv papers.

CLI:
    python -m citationclaw.core.arxiv_db count
    python -m citationclaw.core.arxiv_db lookup <title>
    python -m citationclaw.core.arxiv_db lookup-id <arxiv_id>
    python -m citationclaw.core.arxiv_db build <id1,id2,...>
    python -m citationclaw.core.arxiv_db build-titles <file.txt>
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.environ.get(
    "CITATIONCLAW_DATA_DIR", str(Path.home() / ".citationclaw"),
)) / "arxiv.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS arxiv_papers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    arxiv_id   TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    authors    TEXT NOT NULL DEFAULT '[]',
    year       INTEGER,
    updated_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_arxiv_title_norm ON arxiv_papers(title_norm);
"""


# ── normalizers ─────────────────────────────────────────────────────────


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. CJK preserved."""
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[^\w\u4e00-\u9fff\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def normalize_arxiv_id(arxiv_id: str) -> str:
    """Strip version suffix: '2401.00001v2' → '2401.00001'."""
    aid = (arxiv_id or "").strip()
    return re.sub(r"v\d+$", "", aid)


def normalize_authors(raw) -> list:
    """Coerce an author payload (list of str / dict / None) to [str, ...]."""
    out = []
    for a in raw or []:
        if a is None:
            continue
        if isinstance(a, dict):
            name = (a.get("name") or "").strip()
        elif isinstance(a, str):
            name = a.strip()
        else:
            continue
        if name:
            out.append(name)
    return out


# ── DB ──────────────────────────────────────────────────────────────────


class ArxivDB:
    """SQLite-backed arXiv title→author lookup."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_CREATE_SQL)
        return self._conn

    # ── public API ─────────────────────────────────────────────────

    def lookup_by_title(self, title: str, fuzzy_cutoff: float = 0.80) -> Optional[dict]:
        """Exact (normalized) match first, then bounded fuzzy match.

        Fuzzy strategy: candidate rows sharing the query's longest significant
        word (indexed LIKE scan, capped at 500 rows) compared by SequenceMatcher.
        """
        norm = normalize_title(title)
        if not norm:
            return None
        row = self.conn.execute(
            "SELECT * FROM arxiv_papers WHERE title_norm = ? LIMIT 1", (norm,)
        ).fetchone()
        if row is not None:
            return self._row_to_dict(row)

        # fuzzy pass
        words = [w for w in norm.split() if len(w) >= 5]
        if not words:
            return None
        best_word = max(words, key=len)
        rows = self.conn.execute(
            "SELECT * FROM arxiv_papers WHERE title_norm LIKE ? LIMIT 500",
            (f"%{best_word}%",),
        ).fetchall()
        best, best_ratio = None, 0.0
        for r in rows:
            ratio = SequenceMatcher(None, norm, r["title_norm"]).ratio()
            if ratio > best_ratio:
                best, best_ratio = r, ratio
        if best is not None and best_ratio >= fuzzy_cutoff:
            d = self._row_to_dict(best)
            d["_fuzzy"] = round(best_ratio, 3)
            return d
        return None

    def lookup_by_id(self, arxiv_id: str) -> Optional[dict]:
        aid = normalize_arxiv_id(arxiv_id)
        if not aid:
            return None
        row = self.conn.execute(
            "SELECT * FROM arxiv_papers WHERE arxiv_id = ? LIMIT 1", (aid,)
        ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def upsert(self, record: dict) -> bool:
        """Insert or update one paper. record: {arxiv_id, title, authors, year}."""
        aid = normalize_arxiv_id(record.get("arxiv_id", ""))
        title = (record.get("title") or "").strip()
        if not aid or not title:
            return False
        authors = normalize_authors(record.get("authors"))
        year = record.get("year")
        try:
            year = int(year) if year else None
        except (ValueError, TypeError):
            year = None
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cur = self.conn.execute(
            "SELECT id FROM arxiv_papers WHERE arxiv_id = ?", (aid,)
        )
        if cur.fetchone() is not None:
            self.conn.execute(
                "UPDATE arxiv_papers SET title=?, title_norm=?, authors=?, "
                "year=?, updated_at=? WHERE arxiv_id=?",
                (title, normalize_title(title), json.dumps(authors, ensure_ascii=False),
                 year, now, aid),
            )
        else:
            self.conn.execute(
                "INSERT INTO arxiv_papers (arxiv_id, title, title_norm, authors, year, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (aid, title, normalize_title(title), json.dumps(authors, ensure_ascii=False),
                 year, now),
            )
        self.conn.commit()
        return True

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM arxiv_papers").fetchone()[0]

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        try:
            authors = json.loads(row["authors"] or "[]")
        except json.JSONDecodeError:
            authors = []
        return {
            "arxiv_id": row["arxiv_id"],
            "title": row["title"],
            "title_norm": row["title_norm"],
            "authors": authors,
            "year": row["year"],
            "updated_at": row["updated_at"],
        }


# ── fetch + cache (uses ArxivClient) ────────────────────────────────────


async def fetch_and_cache(
    db: ArxivDB,
    client,
    arxiv_id: str = "",
    title: str = "",
    log=None,
) -> Optional[dict]:
    """Fetch one paper from the arXiv API and upsert it into the local DB.

    Prefers exact ID fetch when arxiv_id is given; otherwise title search
    with a match check. Returns the stored record, or None on failure.
    Never raises — failures are logged and swallowed (best-effort cache).
    """
    log = log or (lambda _m: None)
    try:
        entry = None
        if arxiv_id:
            entry = await client.get_paper(arxiv_id)
        elif title:
            entry = await client.search_paper(title)
            if entry:
                from citationclaw.core.arxiv_client import ArxivClient
                if not ArxivClient._titles_match(title, entry.get("title", "")):
                    entry = None  # search returned a different paper
        if entry is None:
            return None
        db.upsert({
            "arxiv_id": entry.get("arxiv_id", ""),
            "title": entry.get("title", ""),
            "authors": entry.get("authors", []),
            "year": entry.get("year"),
        })
        return db.lookup_by_id(entry.get("arxiv_id", ""))
    except Exception as e:
        log(f"  [arXiv库] 缓存失败 ({arxiv_id or title[:40]}): {e}")
        return None


def ensure_cache(
    db: ArxivDB,
    client,
    arxiv_id: str,
    title: str = "",
    log=None,
) -> bool:
    """Best-effort fire-and-forget cache from sync/async contexts.

    Schedules fetch_and_cache on the running loop when available; otherwise
    does nothing. Returns True when a cache task was scheduled.
    """
    if not arxiv_id:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    loop.create_task(fetch_and_cache(db, client, arxiv_id=arxiv_id, title=title, log=log))
    return True


# ── CLI ─────────────────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python -m citationclaw.core.arxiv_db <cmd> [args]")
        print(f"  Commands: count | lookup <title> | lookup-id <arxiv_id> |")
        print(f"            build <id1,id2,...> | build-titles <file.txt>")
        print(f"  DB path: {DB_PATH}")
        return
    cmd = sys.argv[1]
    db = ArxivDB()

    if cmd == "count":
        print(f"arxiv.db 记录数: {db.count()}")
    elif cmd == "lookup" and len(sys.argv) >= 3:
        title = " ".join(sys.argv[2:])
        r = db.lookup_by_title(title)
        print(json.dumps(r, ensure_ascii=False, indent=2) if r else "未找到")
    elif cmd == "lookup-id" and len(sys.argv) >= 3:
        r = db.lookup_by_id(sys.argv[2])
        print(json.dumps(r, ensure_ascii=False, indent=2) if r else "未找到")
    elif cmd == "build" and len(sys.argv) >= 3:
        ids = [normalize_arxiv_id(x) for x in sys.argv[2].split(",") if x.strip()]
        print(f"开始缓存 {len(ids)} 篇 (arXiv API, ~3 req/s)...")

        async def _run():
            from citationclaw.core.arxiv_client import ArxivClient
            c = ArxivClient()
            try:
                ok = 0
                for i, aid in enumerate(ids, 1):
                    if db.lookup_by_id(aid):
                        ok += 1
                        continue
                    r = await fetch_and_cache(db, c, arxiv_id=aid, log=print)
                    ok += 1 if r else 0
                    if i % 10 == 0:
                        print(f"  进度 {i}/{len(ids)}")
                return ok
            finally:
                await c.close()

        ok = asyncio.run(_run())
        print(f"完成: {ok}/{len(ids)} 篇已入库 (总计 {db.count()} 条)")
    elif cmd == "build-titles" and len(sys.argv) >= 3:
        path = Path(sys.argv[2])
        titles = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.startswith("#")]
        print(f"开始按标题缓存 {len(titles)} 篇...")

        async def _run():
            from citationclaw.core.arxiv_client import ArxivClient
            c = ArxivClient()
            try:
                ok = 0
                for i, t in enumerate(titles, 1):
                    if db.lookup_by_title(t):
                        ok += 1
                        continue
                    r = await fetch_and_cache(db, c, title=t, log=print)
                    ok += 1 if r else 0
                    if i % 10 == 0:
                        print(f"  进度 {i}/{len(titles)}")
                return ok
            finally:
                await c.close()

        ok = asyncio.run(_run())
        print(f"完成: {ok}/{len(titles)} 篇已入库 (总计 {db.count()} 条)")
    else:
        print(f"未知命令: {cmd}")
    db.close()


if __name__ == "__main__":
    main()
