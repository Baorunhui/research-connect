from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

from connect_hub.contracts import ConnectJobError, JobErrorCode


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    published_at: str | None = None
    score: float | None = None
    provider: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "published_at": self.published_at,
            "score": self.score,
            "provider": self.provider,
        }


@dataclass(frozen=True)
class SearchResponse:
    query: str
    results: tuple[SearchResult, ...]
    provider: str
    duration_ms: int = 0
    cached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "results": [item.as_dict() for item in self.results],
            "provider": self.provider,
            "duration_ms": self.duration_ms,
            "cached": self.cached,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SearchResponse":
        results: list[SearchResult] = []
        raw_results = value.get("results")
        if isinstance(raw_results, list):
            for item in raw_results:
                if not isinstance(item, Mapping):
                    continue
                url = _safe_public_url(str(item.get("url") or ""))
                if not url:
                    continue
                raw_score = item.get("score")
                score = float(raw_score) if isinstance(raw_score, (int, float)) else None
                results.append(
                    SearchResult(
                        title=str(item.get("title") or "").strip(),
                        url=url,
                        snippet=str(item.get("snippet") or "").strip(),
                        published_at=(
                            str(item.get("published_at"))
                            if item.get("published_at")
                            else None
                        ),
                        score=score,
                        provider=str(item.get("provider") or value.get("provider") or ""),
                    )
                )
        return cls(
            query=str(value.get("query") or ""),
            results=tuple(results),
            provider=str(value.get("provider") or ""),
            duration_ms=int(value.get("duration_ms") or 0),
            cached=bool(value.get("cached")),
        )


class WebSearchProvider(Protocol):
    def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        freshness_days: int | None = None,
    ) -> SearchResponse: ...


class URLContentProvider(Protocol):
    def fetch(self, url: str, *, max_chars: int = 12000) -> str: ...


class ExaMCPWebSearchProvider:
    """Anonymous hosted Exa MCP client using Streamable HTTP directly.

    This deliberately avoids the Node-based mcp-remote bridge and the Python
    MCP SDK. The server is initialized for one bounded search and the only
    enabled tool is the read-only ``web_search_exa`` tool.
    """

    name = "exa-mcp"

    def __init__(
        self,
        endpoint: str = "https://mcp.exa.ai/mcp?tools=web_search_exa",
        *,
        timeout_seconds: int = 30,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        self.endpoint = endpoint.strip()
        self.timeout_seconds = max(5, timeout_seconds)
        self.max_response_bytes = max(100_000, max_response_bytes)

    def search(
        self,
        query: str,
        *,
        max_results: int,
        include_domains: list[str] | None = None,
        freshness_days: int | None = None,
    ) -> SearchResponse:
        normalized_query = " ".join(query.split()).strip()
        if not normalized_query:
            raise ValueError("search query cannot be empty")
        limit = min(10, max(1, int(max_results)))
        domains = [item.strip().lower() for item in (include_domains or []) if item.strip()]
        requested = min(10, limit * 2) if domains or freshness_days else limit
        query_for_provider = normalized_query
        if domains:
            query_for_provider += " Sources should come from: " + ", ".join(domains) + "."
        if freshness_days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, freshness_days))).date()
            query_for_provider += f" Prefer pages published on or after {cutoff.isoformat()}."

        started = time.monotonic()
        initialized, session_id = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "research-connect-hub", "version": "0.1.0"},
                },
            }
        )
        if not session_id or "result" not in initialized:
            raise _provider_error("Exa MCP initialization returned no session")
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
            expect_response=False,
        )
        called, _ = self._post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "web_search_exa",
                    "arguments": {"query": query_for_provider, "numResults": requested},
                },
            },
            session_id=session_id,
        )
        if isinstance(called.get("error"), Mapping):
            raise _provider_error(str(called["error"].get("message") or "Exa MCP tool error"))
        raw_result = called.get("result")
        if not isinstance(raw_result, Mapping):
            raise _provider_error("Exa MCP returned no tool result")
        raw_content = raw_result.get("content")
        texts = [
            str(item.get("text") or "")
            for item in raw_content
            if isinstance(item, Mapping) and item.get("type") == "text"
        ] if isinstance(raw_content, list) else []
        parsed = _parse_exa_text("\n\n".join(texts), self.name)
        filtered = _filter_results(
            parsed,
            include_domains=domains,
            freshness_days=freshness_days,
        )
        return SearchResponse(
            query=normalized_query,
            results=tuple(filtered[:limit]),
            provider=self.name,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _post(
        self,
        payload: Mapping[str, Any],
        *,
        session_id: str = "",
        expect_response: bool = True,
    ) -> tuple[dict[str, Any], str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
            "User-Agent": "research-connect-hub/0.1.0",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(self.max_response_bytes + 1)
                returned_session = str(response.headers.get("Mcp-Session-Id") or session_id)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", errors="replace")
            if exc.code == 429:
                raise ConnectJobError(
                    JobErrorCode.PROVIDER_RATE_LIMITED,
                    "Exa 匿名搜索触发频率限制，请稍后重试或暂时关闭联网。",
                    provider=self.name,
                    retryable=True,
                    technical_message=f"Exa MCP HTTP 429: {detail}",
                ) from exc
            raise _provider_error(f"Exa MCP HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise _provider_error(f"Exa MCP connection failed: {exc}") from exc
        if len(body) > self.max_response_bytes:
            raise _provider_error("Exa MCP response exceeded the safety size limit")
        if not expect_response or not body.strip():
            return {}, returned_session
        return _decode_mcp_body(body), returned_session


class JinaReaderProvider:
    name = "jina-reader"

    def __init__(
        self,
        endpoint: str = "https://r.jina.ai/",
        *,
        timeout_seconds: int = 30,
    ) -> None:
        self.endpoint = endpoint.rstrip("/") + "/"
        self.timeout_seconds = max(5, timeout_seconds)

    def fetch(self, url: str, *, max_chars: int = 12000) -> str:
        safe_url = _safe_public_url(url)
        if not safe_url:
            raise ValueError("Jina Reader only accepts public HTTP(S) URLs")
        request = urllib.request.Request(
            self.endpoint + safe_url,
            headers={
                "Accept": "text/plain",
                "User-Agent": "research-connect-hub/0.1.0",
            },
        )
        limit = max(1000, min(int(max_chars), 50000))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(limit * 4 + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise ConnectJobError(
                    JobErrorCode.PROVIDER_RATE_LIMITED,
                    "Jina Reader 触发匿名频率限制，请稍后重试。",
                    provider=self.name,
                    retryable=True,
                    technical_message="Jina Reader HTTP 429",
                ) from exc
            raise _provider_error(f"Jina Reader HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise _provider_error(f"Jina Reader connection failed: {exc}") from exc
        text = body.decode("utf-8", errors="replace").strip()
        return text[:limit]


def _decode_mcp_body(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    if text.startswith("{"):
        decoded = json.loads(text)
        return decoded if isinstance(decoded, dict) else {}
    messages: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            decoded = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            messages.append(decoded)
    if not messages:
        raise _provider_error("Exa MCP returned malformed SSE")
    return messages[-1]


def _parse_exa_text(text: str, provider: str) -> list[SearchResult]:
    blocks = re.split(r"(?m)(?=^Title:\s*)", text)
    results: list[SearchResult] = []
    seen: set[str] = set()
    for block in blocks:
        if not block.startswith("Title:"):
            continue
        title_match = re.search(r"(?m)^Title:\s*(.+)$", block)
        url_match = re.search(r"(?m)^URL:\s*(\S+)$", block)
        if title_match is None or url_match is None:
            continue
        url = _safe_public_url(url_match.group(1).strip())
        if not url or url in seen:
            continue
        published_match = re.search(r"(?m)^Published:\s*(.+)$", block)
        highlight_match = re.search(r"(?ms)^Highlights:\s*\n?(.*)$", block)
        snippet = (highlight_match.group(1) if highlight_match else "").strip()
        snippet = re.sub(r"\n\.{3}\n", "\n", snippet)
        snippet = re.sub(r"\n{3,}", "\n\n", snippet)[:1600]
        results.append(
            SearchResult(
                title=title_match.group(1).strip()[:500],
                url=url,
                snippet=snippet,
                published_at=(published_match.group(1).strip() if published_match else None),
                provider=provider,
            )
        )
        seen.add(url)
    return results


def _filter_results(
    results: list[SearchResult],
    *,
    include_domains: list[str],
    freshness_days: int | None,
) -> list[SearchResult]:
    filtered = results
    if include_domains:
        filtered = [
            item
            for item in filtered
            if any(
                (urlparse(item.url).hostname or "").lower() == domain
                or (urlparse(item.url).hostname or "").lower().endswith("." + domain)
                for domain in include_domains
            )
        ]
    if freshness_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, freshness_days))
        dated: list[SearchResult] = []
        for item in filtered:
            parsed = _parse_datetime(item.published_at)
            if parsed is not None and parsed >= cutoff:
                dated.append(item)
        filtered = dated
    return filtered


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_public_url(value: str) -> str:
    url = value.strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return ""
    if re.fullmatch(r"127(?:\.\d{1,3}){3}", host) or host == "::1":
        return ""
    return url


def _provider_error(technical_message: str) -> ConnectJobError:
    return ConnectJobError(
        JobErrorCode.PROVIDER_UNAVAILABLE,
        "联网服务暂时不可用，请稍后重试或关闭联网继续。",
        provider="web-search",
        retryable=True,
        technical_message=technical_message,
    )
