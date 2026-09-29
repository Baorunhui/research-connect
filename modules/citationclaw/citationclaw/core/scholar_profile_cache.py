"""Persistent per-scholar cache for the scholar-profile pipeline.

When a scholar already has a finished result folder, the pipeline serves that
result instead of querying Google Scholar / Semantic Scholar / the LLM again.

Cache key: ``gs:<Google Scholar user id>`` (from the profile URL or the saved
profile HTML), ``oa:<OpenAlex author id>`` for an OpenAlex author URL, plus
``name:<lowercased scholar name>`` for uploaded HTML.
Cache value: artifact paths relative to DATA_DIR, e.g.
``{"result_dir": "result-20260601_152415", "excel": ..., "json": ...,
"dashboard": ..., "scholar_name": ..., "profile_url": ..., "updated_at": ...}``.
Stored like the other CitationClaw caches (one JSON file per key under
DATA_DIR/cache, indexed in SQLite). An entry whose files were deleted is a miss.
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from citationclaw.app.config_manager import DATA_DIR
from citationclaw.core.cache_store import IndexedJsonMap

_ARTIFACT_KEYS = ("excel", "json", "dashboard")
_USER_ID_RE = re.compile(r"[?&](?:amp;)?user=([A-Za-z0-9_-]{6,})")
_CANONICAL_RE = re.compile(
    r"<link[^>]+rel=[\"']canonical[\"'][^>]*href=[\"']([^\"']+)[\"']", re.I
)
_OG_URL_RE = re.compile(r"<meta[^>]+property=[\"']og:url[\"'][^>]*content=[\"']([^\"']+)[\"']", re.I)
_NAME_RE = re.compile(r"id=[\"']gsc_prf_in[\"'][^>]*>([^<]+)<", re.I)
_FILENAME_SUFFIX_RE = re.compile(r"\s*-\s*_?(google\s*(学术搜索|scholar)).*$", re.I)


_OA_HOSTS = {"openalex.org", "www.openalex.org", "api.openalex.org"}
_OA_AUTHOR_RE = re.compile(r"^A\d+$")


def scholar_user_id_from_url(url: str) -> str:
    try:
        values = parse_qs(urlparse(str(url or "").strip()).query).get("user") or []
    except ValueError:
        return ""
    return values[0].strip() if values and values[0].strip() else ""


def openalex_author_id_from_url(url: str) -> str:
    """Return the OpenAlex author id (``A`` + digits) from an author profile URL."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if (parsed.hostname or "").lower() not in _OA_HOSTS:
        return ""
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return tail if _OA_AUTHOR_RE.fullmatch(tail) else ""


def is_author_profile_url(url: str) -> bool:
    """Google Scholar profile or OpenAlex author profile."""
    text = str(url or "")
    return "scholar.google" in text or bool(openalex_author_id_from_url(text))


def scholar_identity_from_html(profile_html: str, filename: str = "") -> tuple[str, str]:
    """Return ``(user_id, scholar_name)`` found in a saved Google Scholar profile."""
    text = profile_html or ""
    user_id = ""
    for pattern in (_CANONICAL_RE, _OG_URL_RE):
        match = pattern.search(text)
        if match:
            user_id = scholar_user_id_from_url(_html.unescape(match.group(1)))
            if user_id:
                break
    if not user_id:
        counts: dict[str, int] = {}
        for found in _USER_ID_RE.findall(text):
            counts[found] = counts.get(found, 0) + 1
        if counts:
            user_id = max(counts, key=counts.get)
    name = ""
    match = _NAME_RE.search(text)
    if match:
        name = _html.unescape(match.group(1)).strip()
    if not name and filename:
        stem = Path(filename).stem
        name = _FILENAME_SUFFIX_RE.sub("", stem).strip(" _-")
    return user_id, name


def scholar_cache_keys(
    profile_url: str = "", profile_html: str = "", scholar_name: str = ""
) -> list[str]:
    """All cache keys identifying one scholar, most specific first."""
    user_id = scholar_user_id_from_url(profile_url)
    name = ""
    if profile_html:
        html_user, name = scholar_identity_from_html(profile_html, scholar_name)
        user_id = user_id or html_user
    keys = []
    if user_id:
        keys.append(f"gs:{user_id}")
    oa_id = openalex_author_id_from_url(profile_url)
    if oa_id:
        keys.append(f"oa:{oa_id}")
    if name:
        keys.append("name:" + " ".join(name.lower().split()))
    return keys


class ScholarProfileCache:
    """Maps a scholar to the artifacts of their last completed profile run."""

    def __init__(self, cache_file: Path = DATA_DIR / "cache" / "scholar_profile_cache.json",
                 data_dir: Path = DATA_DIR):
        self.cache_file = cache_file
        self.data_dir = Path(data_dir)
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._store = IndexedJsonMap("scholar-profile", cache_file)

    def lookup(self, keys: list[str]) -> Optional[dict]:
        """Return the cached entry with absolute artifact paths, or None."""
        for key in keys:
            entry = self._store.index.get_json(self._store.namespace, key)
            if not isinstance(entry, dict):
                continue
            resolved = self._resolve(entry)
            if resolved is not None:
                return resolved
        return None

    def store(self, keys: list[str], *, result: dict, scholar_name: str = "",
              profile_url: str = "", params: Optional[dict] = None) -> None:
        if not keys:
            return
        entry: dict = {
            "scholar_name": scholar_name,
            "profile_url": profile_url,
            "params": dict(params or {}),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        result_dir = ""
        for name in _ARTIFACT_KEYS:
            value = str(result.get(name) or "").strip()
            relative = self._relative(value) if value else ""
            entry[name] = relative
            if relative and not result_dir:
                result_dir = Path(relative).parts[0]
        entry["result_dir"] = result_dir
        if not any(entry[name] for name in _ARTIFACT_KEYS):
            return
        for key in keys:
            self._store.index.put_json(self._store.namespace, key, entry)

    def list_entries(self) -> list[dict]:
        entries = []
        for key, entry in self._store.load_all().items():
            if isinstance(entry, dict):
                resolved = self._resolve(entry)
                if resolved is not None:
                    entries.append({"key": key, **resolved})
        return entries

    def _relative(self, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            return path.as_posix()
        try:
            return path.resolve().relative_to(self.data_dir.resolve()).as_posix()
        except ValueError:
            return ""

    def _resolve(self, entry: dict) -> Optional[dict]:
        resolved = dict(entry)
        found = False
        for name in _ARTIFACT_KEYS:
            relative = str(entry.get(name) or "").strip()
            path = (self.data_dir / relative) if relative else None
            if path is not None and path.is_file():
                resolved[name] = str(path)
                found = True
            else:
                resolved[name] = ""
        return resolved if found else None
