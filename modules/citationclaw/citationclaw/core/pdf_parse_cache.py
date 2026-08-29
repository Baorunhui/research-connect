"""Cache index for PDF parse results.

Maps paper_key -> {pdf_path, parsed_at, has_content_list, has_authors}
Persisted as data/cache/pdf_parsed/index.json
"""
import json
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone


class PDFParseCache:
    def __init__(self, base_dir: Path = Path("data/cache/pdf_parsed")):
        self._base = base_dir
        self._base.mkdir(parents=True, exist_ok=True)
        self._index_path = self._base / "index.json"
        self._index = self._load_index()

    def _load_index(self) -> dict:
        if self._index_path.exists():
            with open(self._index_path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_index(self):
        with open(self._index_path, "w", encoding="utf-8") as f:
            json.dump(self._index, f, ensure_ascii=False, indent=2)

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
        with open(out / "authors.json", "w", encoding="utf-8") as f:
            json.dump(authors, f, ensure_ascii=False, indent=2)
        if paper_key in self._index:
            self._index[paper_key]["has_authors"] = True
            self._save_index()

    def get_authors(self, paper_key: str) -> Optional[list]:
        path = self._base / paper_key / "authors.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return None

    def stats(self) -> dict:
        return {
            "total": len(self._index),
            "with_authors": sum(1 for v in self._index.values() if v.get("has_authors")),
        }
