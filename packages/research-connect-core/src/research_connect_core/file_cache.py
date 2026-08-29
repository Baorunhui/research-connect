from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FileCacheIndex:
    """SQLite metadata index with payloads stored as ordinary files.

    SQLite contains only keys, paths, hashes and small metadata. JSON payloads,
    PDFs and parsed document trees remain inspectable files and can be backed up
    independently. Writes are atomic and paths never depend on the current cwd.
    """

    def __init__(self, root: str | Path, *, db_name: str = "cache-index.sqlite3") -> None:
        self.root = Path(root).expanduser().resolve()
        self.files_dir = self.root / "files"
        self.db_path = self.root / db_name
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    namespace TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    accessed_at TEXT NOT NULL,
                    PRIMARY KEY(namespace, cache_key),
                    UNIQUE(relative_path)
                );
                CREATE INDEX IF NOT EXISTS idx_cache_entries_accessed
                    ON cache_entries(namespace, accessed_at);
                """
            )

    @staticmethod
    def _validate_part(value: str, label: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{label} is required")
        if len(text) > 1000:
            raise ValueError(f"{label} is too long")
        return text

    def _relative_path(self, namespace: str, cache_key: str, suffix: str) -> Path:
        namespace_hash = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:16]
        key_hash = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
        clean_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        if any(ch not in ".abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in clean_suffix):
            raise ValueError("invalid cache file suffix")
        return Path(namespace_hash) / key_hash[:2] / f"{key_hash}{clean_suffix}"

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def put_bytes(
        self,
        namespace: str,
        cache_key: str,
        data: bytes,
        *,
        suffix: str = ".bin",
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Path:
        namespace = self._validate_part(namespace, "namespace")
        cache_key = self._validate_part(cache_key, "cache_key")
        relative = self._relative_path(namespace, cache_key, suffix)
        path = self.files_dir / relative
        digest = hashlib.sha256(data).hexdigest()
        now = _utc_now()
        mime = media_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        meta_text = json.dumps(dict(metadata or {}), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._atomic_write(path, data)
            with self._connect() as db:
                old = db.execute(
                    "SELECT relative_path, created_at FROM cache_entries WHERE namespace=? AND cache_key=?",
                    (namespace, cache_key),
                ).fetchone()
                created_at = str(old["created_at"]) if old else now
                db.execute(
                    """
                    INSERT INTO cache_entries(
                        namespace, cache_key, relative_path, media_type, size_bytes,
                        sha256, metadata_json, created_at, updated_at, accessed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(namespace, cache_key) DO UPDATE SET
                        relative_path=excluded.relative_path,
                        media_type=excluded.media_type,
                        size_bytes=excluded.size_bytes,
                        sha256=excluded.sha256,
                        metadata_json=excluded.metadata_json,
                        updated_at=excluded.updated_at,
                        accessed_at=excluded.accessed_at
                    """,
                    (
                        namespace,
                        cache_key,
                        relative.as_posix(),
                        mime,
                        len(data),
                        digest,
                        meta_text,
                        created_at,
                        now,
                        now,
                    ),
                )
                if old and str(old["relative_path"]) != relative.as_posix():
                    stale = self.files_dir / str(old["relative_path"])
                    stale.unlink(missing_ok=True)
        return path

    def put_json(
        self,
        namespace: str,
        cache_key: str,
        value: Any,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> Path:
        data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        return self.put_bytes(
            namespace,
            cache_key,
            data,
            suffix=".json",
            media_type="application/json",
            metadata=metadata,
        )

    def get_record(self, namespace: str, cache_key: str, *, touch: bool = True) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM cache_entries WHERE namespace=? AND cache_key=?",
                (namespace, cache_key),
            ).fetchone()
            if row is None:
                return None
            path = self.files_dir / str(row["relative_path"])
            if not path.is_file():
                db.execute(
                    "DELETE FROM cache_entries WHERE namespace=? AND cache_key=?",
                    (namespace, cache_key),
                )
                return None
            if touch:
                db.execute(
                    "UPDATE cache_entries SET accessed_at=? WHERE namespace=? AND cache_key=?",
                    (_utc_now(), namespace, cache_key),
                )
            value = dict(row)
            value["path"] = path
            try:
                value["metadata"] = json.loads(str(row["metadata_json"]) or "{}")
            except json.JSONDecodeError:
                value["metadata"] = {}
            return value

    def get_path(self, namespace: str, cache_key: str, *, touch: bool = True) -> Path | None:
        record = self.get_record(namespace, cache_key, touch=touch)
        return Path(record["path"]) if record else None

    def get_json(self, namespace: str, cache_key: str, *, touch: bool = True) -> Any | None:
        path = self.get_path(namespace, cache_key, touch=touch)
        if path is None:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def delete(self, namespace: str, cache_key: str) -> bool:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT relative_path FROM cache_entries WHERE namespace=? AND cache_key=?",
                (namespace, cache_key),
            ).fetchone()
            if row is None:
                return False
            db.execute(
                "DELETE FROM cache_entries WHERE namespace=? AND cache_key=?",
                (namespace, cache_key),
            )
            (self.files_dir / str(row["relative_path"])).unlink(missing_ok=True)
            return True

    def count(self, namespace: str | None = None) -> int:
        with self._connect() as db:
            if namespace is None:
                row = db.execute("SELECT COUNT(*) AS n FROM cache_entries").fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) AS n FROM cache_entries WHERE namespace=?", (namespace,)
                ).fetchone()
            return int(row["n"] if row else 0)

    def list_records(self, namespace: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM cache_entries WHERE namespace=? ORDER BY updated_at DESC",
                (namespace,),
            ).fetchall()
        return [dict(row) for row in rows]
