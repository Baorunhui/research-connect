# -*- coding: utf-8 -*-
"""本地论文库：SQLite + FTS5，本地召回模式（local recall mode）的存储层。

设计动机：共享 Supabase 时段性超时（57014）会让日报召回卡死。本地模式把
「最近 N 天窗口」的论文元数据由 arXiv API 增量抓取落进本库，召回全程本地完成，
不再依赖任何远端数据库。Kaggle 大索引（archive/kaggle_arxiv）只服务综述与
长窗口，本库只存日报窗口的增量数据（万级以内）。

表结构：
- papers      论文元数据（id 主键，published 上建索引供时间窗过滤）
- papers_fts  FTS5 全文索引（title + abstract，id UNINDEXED 关联 papers）
- meta        建库信息（created_at 等，仿 kaggle_arxiv）
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT_DIR / "data" / "local_recall" / "index.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY,
  source TEXT,
  title TEXT,
  abstract TEXT,
  authors TEXT,
  primary_category TEXT,
  categories TEXT,
  published TEXT,
  updated TEXT,
  link TEXT,
  pdf_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published);
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
  id UNINDEXED,
  title,
  abstract
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

PAPER_COLUMNS = (
  "id", "source", "title", "abstract", "authors", "primary_category",
  "categories", "published", "updated", "link", "pdf_url",
)


def resolve_db_path(db_path: str | os.PathLike | None = None) -> Path:
  if db_path:
    return Path(db_path).expanduser()
  return Path(os.getenv("DPR_LOCAL_RECALL_DB") or DEFAULT_DB_PATH)


class LocalPaperStore:
  """本地论文库句柄。所有写操作 upsert 幂等，可跨天/跨运行重复执行。"""

  def __init__(self, db_path: str | os.PathLike | None = None) -> None:
    self.db_path = resolve_db_path(db_path)
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    self._conn = sqlite3.connect(str(self.db_path))
    self._conn.row_factory = sqlite3.Row
    self._conn.executescript(_SCHEMA)
    self._conn.execute(
      "INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)",
      (datetime.now(timezone.utc).isoformat(),),
    )
    self._conn.commit()

  def close(self) -> None:
    self._conn.close()

  def __enter__(self) -> "LocalPaperStore":
    return self

  def __exit__(self, *exc: Any) -> None:
    self.close()

  # ------------------------------------------------------------------
  # 写入
  # ------------------------------------------------------------------
  def upsert_papers(self, rows: Iterable[Dict[str, Any]]) -> int:
    """幂等 upsert 论文（含 FTS 同步）；返回处理的行数。"""
    count = 0
    for row in rows:
      pid = str(row.get("id") or "").strip()
      if not pid:
        continue
      values = {
        "id": pid,
        "source": str(row.get("source") or "arxiv").strip() or "arxiv",
        "title": str(row.get("title") or "").strip(),
        "abstract": str(row.get("abstract") or "").strip(),
        "authors": json.dumps(row.get("authors") or [], ensure_ascii=False),
        "primary_category": str(row.get("primary_category") or "").strip() or None,
        "categories": json.dumps(row.get("categories") or [], ensure_ascii=False),
        "published": str(row.get("published") or "").strip() or None,
        "updated": str(row.get("updated") or "").strip() or None,
        "link": str(row.get("link") or "").strip() or None,
        "pdf_url": str(row.get("pdf_url") or "").strip() or None,
      }
      self._conn.execute(
        "INSERT OR REPLACE INTO papers({cols}) VALUES ({ph})".format(
          cols=", ".join(PAPER_COLUMNS), ph=", ".join(":" + c for c in PAPER_COLUMNS)
        ),
        values,
      )
      # FTS 外部无 content 同步，直接按 id 删旧插新
      self._conn.execute("DELETE FROM papers_fts WHERE id = ?", (pid,))
      self._conn.execute(
        "INSERT INTO papers_fts(id, title, abstract) VALUES (?, ?, ?)",
        (pid, values["title"], values["abstract"]),
      )
      count += 1
    self._conn.commit()
    return count

  def set_meta(self, key: str, value: str) -> None:
    self._conn.execute(
      "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
    )
    self._conn.commit()

  def get_meta(self, key: str, default: str = "") -> str:
    row = self._conn.execute(
      "SELECT value FROM meta WHERE key = ?", (key,)
    ).fetchone()
    return str(row[0]) if row else default

  # ------------------------------------------------------------------
  # 查询
  # ------------------------------------------------------------------
  def count_window(self, date_start: str, date_end: str) -> int:
    """[date_start, date_end) 左闭右开窗口内的论文数（ISO 日期字符串比较）。"""
    row = self._conn.execute(
      "SELECT COUNT(*) FROM papers WHERE published IS NOT NULL AND published >= ? AND published < ?",
      (date_start, date_end),
    ).fetchone()
    return int(row[0]) if row else 0

  def search_fts(
    self,
    match_expr: str,
    date_start: str,
    date_end: str,
    limit: int = 400,
  ) -> List[Dict[str, Any]]:
    """FTS5 词法检索 + 时间窗过滤，按 bm25 相关度（越小越好）升序返回。"""
    if not str(match_expr or "").strip():
      return []
    sql = """
      SELECT p.*, bm25(papers_fts) AS rank_score
      FROM papers_fts f
      JOIN papers p ON p.id = f.id
      WHERE papers_fts MATCH ?
        AND p.published IS NOT NULL AND p.published >= ? AND p.published < ?
      ORDER BY rank_score ASC
      LIMIT ?
    """
    rows = self._conn.execute(sql, (match_expr, date_start, date_end, max(int(limit), 1))).fetchall()
    return [self._row_to_paper(r) for r in rows]

  def _row_to_paper(self, row: sqlite3.Row) -> Dict[str, Any]:
    def _json_list(text: Any) -> List[str]:
      try:
        data = json.loads(text) if text else []
        return [str(x) for x in data] if isinstance(data, list) else []
      except Exception:
        return []

    return {
      "id": row["id"],
      "source": row["source"] or "arxiv",
      "title": row["title"] or "",
      "abstract": row["abstract"] or "",
      "authors": _json_list(row["authors"]),
      "primary_category": row["primary_category"] or "",
      "categories": _json_list(row["categories"]),
      "published": row["published"] or "",
      "updated": row["updated"] or "",
      "link": row["link"] or "",
      "pdf_url": row["pdf_url"] or "",
    }

  def get_papers(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not ids:
      return {}
    out: Dict[str, Dict[str, Any]] = {}
    placeholders = ",".join("?" for _ in ids)
    rows = self._conn.execute(
      f"SELECT * FROM papers WHERE id IN ({placeholders})", list(ids)
    ).fetchall()
    for row in rows:
      out[row["id"]] = self._row_to_paper(row)
    return out
