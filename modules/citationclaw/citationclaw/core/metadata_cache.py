"""Cache for Phase 2 metadata results.

File: data/cache/metadata_cache.json
Key: DOI or paper_title.lower()
Value: {authors, affiliations, h_index, citations, source, fetched_at}
"""
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from citationclaw.app.config_manager import DATA_DIR
from citationclaw.core.cache_store import IndexedJsonMap

CACHE_FILE = DATA_DIR / "cache" / "metadata_cache.json"
WRITE_EVERY = 10


class MetadataCache:
    def __init__(self, cache_file: Optional[Path] = None):
        self._file = cache_file or CACHE_FILE
        self._data: Dict[str, Any] = {}
        self._pending = 0
        self._lock = None
        self._stats = {"hits": 0, "misses": 0, "updates": 0}
        self._store = IndexedJsonMap("metadata", self._file)
        self._load()

    def _get_lock(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load(self):
        self._data = self._store.load_all()

    def _make_key(self, doi: str, title: str) -> str:
        if doi:
            return doi.lower().strip()
        return title.lower().strip()

    async def get(self, doi: str = "", title: str = "") -> Optional[dict]:
        async with self._get_lock():
            key = self._make_key(doi, title)
            entry = self._data.get(key)
            if entry:
                self._stats["hits"] += 1
                return entry
            self._stats["misses"] += 1
            return None

    async def update(self, doi: str, title: str, metadata: dict):
        async with self._get_lock():
            key = self._make_key(doi, title)
            metadata["fetched_at"] = datetime.now(timezone.utc).isoformat()
            self._data[key] = metadata
            self._stats["updates"] += 1
            self._pending += 1
            if self._pending >= WRITE_EVERY:
                self._write()
                self._pending = 0

    async def flush(self):
        async with self._get_lock():
            if self._pending > 0:
                self._write()
                self._pending = 0

    def _write(self):
        self._store.save_all(self._data)

    def stats(self) -> dict:
        return dict(self._stats)
