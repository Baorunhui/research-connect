"""CitationClaw compatibility layer for the monorepo file cache.

Cache values remain individual, readable JSON files.  SQLite stores only their
keys, paths and metadata.  Existing one-big-JSON cache files are imported once
so users do not lose completed API work.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from research_connect_core import DataPaths, FileCacheIndex

logger = logging.getLogger(__name__)


class IndexedJsonMap:
    def __init__(self, namespace: str, legacy_file: Path | None = None) -> None:
        self.namespace = namespace
        paths = DataPaths.for_module("citationclaw")
        if legacy_file is not None and legacy_file.resolve().parent != paths.cache.resolve():
            # Isolate explicitly supplied test/custom caches while retaining the
            # same storage model.
            root = legacy_file.parent / ".citationclaw-cache"
            self.namespace = f"{namespace}:{legacy_file.name}"
        else:
            root = paths.cache
        self.index = FileCacheIndex(root)
        self.legacy_file = legacy_file
        self._import_legacy_once()

    def _legacy_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        if self.legacy_file is not None:
            candidates.append(self.legacy_file)
            filename = self.legacy_file.name
            candidates.append(Path(__file__).resolve().parents[2] / "data" / "cache" / filename)
            old_root = str(os.getenv("CITATIONCLAW_DATA_DIR") or "").strip()
            if old_root:
                candidates.append(Path(old_root).expanduser() / "cache" / filename)
        unique: list[Path] = []
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in unique:
                unique.append(resolved)
        return unique

    def _import_legacy_once(self) -> None:
        marker = "__legacy_imported__"
        if self.index.get_record(self.namespace, marker, touch=False):
            return
        for candidate in self._legacy_candidates():
            if not candidate.is_file():
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(payload, Mapping):
                    for key, value in payload.items():
                        self.index.put_json(self.namespace, str(key), value)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Unable to import legacy cache %s: %s", candidate, exc)
            break
        self.index.put_json(self.namespace, marker, {"imported": True})

    def load_all(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for record in self.index.list_records(self.namespace):
            key = str(record["cache_key"])
            if key == "__legacy_imported__":
                continue
            value = self.index.get_json(self.namespace, key, touch=False)
            if value is not None:
                values[key] = value
        return values

    def save_all(self, values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            self.index.put_json(self.namespace, str(key), value)
