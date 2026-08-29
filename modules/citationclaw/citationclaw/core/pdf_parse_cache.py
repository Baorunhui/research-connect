"""Cache index for PDF parse results.

Maps paper_key -> {pdf_path, parsed_at, has_content_list, has_authors}
Persisted as data/cache/pdf_parsed/index.json
"""
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone
from citationclaw.app.config_manager import DATA_DIR
from citationclaw.core.cache_store import IndexedJsonMap


class PDFParseCache:
    def __init__(self, base_dir: Path = DATA_DIR / "cache" / "pdf_parsed"):
        self._base = base_dir
        self._base.mkdir(parents=True, exist_ok=True)
        self._store = IndexedJsonMap("pdf-parse-index", self._base / "index.json")
        self._index = self._load_index()

    def _load_index(self) -> dict:
        return self._store.load_all()

    def _save_index(self):
        self._store.save_all(self._index)

    def has(self, paper_key: str) -> bool:
        return paper_key in self._index

    def get_meta(self, paper_key: str) -> Optional[dict]:
        return self._index.get(paper_key)

    def store(self, paper_key: str, meta: dict):
        """Store metadata about a parsed paper."""
        meta["stored_at"] = datetime.now(timezone.utc).isoformat()
        self._index[paper_key] = meta
        self._save_index()

    def get_parsed_dir(self, paper_key: str) -> Path:
        return self._base / paper_key

    def store_authors(self, paper_key: str, authors: list):
        """Store LLM-extracted authors for a paper."""
        out = self._base / paper_key
        out.mkdir(parents=True, exist_ok=True)
        self._store.index.put_json("pdf-parse-authors", paper_key, authors)
        if paper_key in self._index:
            self._index[paper_key]["has_authors"] = True
            self._save_index()

    def get_authors(self, paper_key: str) -> Optional[list]:
        return self._store.index.get_json("pdf-parse-authors", paper_key)

    def stats(self) -> dict:
        return {
            "total": len(self._index),
            "with_authors": sum(1 for v in self._index.values() if v.get("has_authors")),
        }
