from __future__ import annotations

import json
import base64
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from connect_hub.config import ProviderSettings, Settings
from connect_hub.reporting import ReportHubClient


class CredentialStore:
    """Small local cache; Report Hub remains the demo configuration source."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def save(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, self.path)


def bootstrap_config(settings: Settings) -> dict[str, Any]:
    primary = settings.providers[0] if settings.providers else None
    return {
        "schema_version": "connect.providers.v1",
        "providers": {
            "llm.primary": {
                "enabled": True,
                "base_url": primary.base_url if primary else "",
                "model": primary.model if primary else "",
                "api_key": primary.api_key if primary else "",
            },
            "embedding.paper": {
                "enabled": True,
                "base_url": settings.daily_paper_embed_api_url,
                "model": "BAAI/bge-small-en-v1.5",
                "api_key": settings.daily_paper_embed_api_key,
            },
            "rerank.paper": {
                "enabled": True,
                "base_url": settings.daily_paper_rerank_base_url,
                "model": settings.daily_paper_rerank_model,
                "api_key": settings.daily_paper_rerank_api_key,
            },
            "web.exa": {"enabled": settings.web_search_provider != "disabled", "base_url": settings.web_search_endpoint},
            "fetch.jina": {"enabled": settings.url_fetch_provider != "disabled", "base_url": settings.jina_reader_endpoint},
        },
        "paper_sources": {"arxiv": {"enabled": True}, "deepxiv": {"enabled": False}, "kaggle": {"enabled": False}},
    }


def apply_to_settings(settings: Settings, config: Mapping[str, Any]) -> Settings:
    providers = config.get("providers") if isinstance(config.get("providers"), Mapping) else {}
    llm = _provider(providers, "llm.primary")
    configured_llm: tuple[ProviderSettings, ...] = settings.providers
    if _enabled(llm) and all(str(llm.get(key) or "").strip() for key in ("base_url", "model", "api_key")):
        configured_llm = (
            ProviderSettings(
                name="primary",
                base_url=str(llm["base_url"]).strip().rstrip("/"),
                model=str(llm["model"]).strip(),
                api_key=str(llm["api_key"]).strip(),
                timeout_seconds=(settings.providers[0].timeout_seconds if settings.providers else 120),
                max_retries=(settings.providers[0].max_retries if settings.providers else 1),
            ),
        )
    embedding = _provider(providers, "embedding.paper")
    rerank = _provider(providers, "rerank.paper")
    exa = _provider(providers, "web.exa")
    jina = _provider(providers, "fetch.jina")
    return replace(
        settings,
        providers=configured_llm,
        daily_paper_embed_api_url=str(embedding.get("base_url") or settings.daily_paper_embed_api_url).strip(),
        daily_paper_embed_api_key=str(embedding.get("api_key") or settings.daily_paper_embed_api_key).strip(),
        daily_paper_rerank_base_url=str(rerank.get("base_url") or settings.daily_paper_rerank_base_url).strip(),
        daily_paper_rerank_model=str(rerank.get("model") or settings.daily_paper_rerank_model).strip(),
        daily_paper_rerank_api_key=str(rerank.get("api_key") or settings.daily_paper_rerank_api_key).strip(),
        web_search_provider="exa_mcp" if _enabled(exa) else "disabled",
        web_search_endpoint=str(exa.get("base_url") or settings.web_search_endpoint).strip(),
        url_fetch_provider="jina" if _enabled(jina) else "disabled",
        jina_reader_endpoint=str(jina.get("base_url") or settings.jina_reader_endpoint).strip(),
    )


def daily_configuration(config: Mapping[str, Any]) -> dict[str, Any]:
    providers = config.get("providers") if isinstance(config.get("providers"), Mapping) else {}
    llm, rerank, supabase = (_provider(providers, name) for name in ("llm.primary", "rerank.paper", "supabase.arxiv"))
    modules = config.get("modules") if isinstance(config.get("modules"), Mapping) else {}
    saved = modules.get("daily-paper") if isinstance(modules.get("daily-paper"), Mapping) else {}
    result = json.loads(json.dumps(saved, ensure_ascii=False))
    local = result.setdefault("local", {})
    local["chat"] = {
        "base_url": llm.get("base_url", ""), "api_key": llm.get("api_key", ""),
        "model": llm.get("model", ""),
    }
    local.setdefault("rerank", {"profile": "public-zwwen-rerank"})
    result["source_backends"] = {
        **(result.get("source_backends") or {}),
        "arxiv": {
            **((result.get("source_backends") or {}).get("arxiv") or {}),
            "enabled": _enabled(supabase), "url": supabase.get("base_url", ""), "anon_key": supabase.get("anon_key", ""),
            "papers_table": supabase.get("papers_table", "arxiv_papers"), "use_bm25_rpc": True,
            "bm25_rpc": supabase.get("bm25_rpc", "match_arxiv_papers_bm25"), "use_vector_rpc": True,
            "vector_rpc": supabase.get("vector_rpc", "match_arxiv_papers_exact"),
            "vector_rpc_exact": supabase.get("vector_rpc", "match_arxiv_papers_exact"),
        },
    }
    return result


def daily_environment(config: Mapping[str, Any]) -> dict[str, str]:
    providers = config.get("providers") if isinstance(config.get("providers"), Mapping) else {}
    embedding, rerank, deepxiv = (_provider(providers, name) for name in ("embedding.paper", "rerank.paper", "academic.deepxiv"))
    return {
        "DPR_EMBED_API_URL": str(embedding.get("base_url") or ""), "DPR_EMBED_API_KEY": str(embedding.get("api_key") or ""),
        "RERANK_API_BASE_URL": str(rerank.get("base_url") or ""), "PUBLIC_RERANK_API_BASE_URL": str(rerank.get("base_url") or ""),
        "RERANK_API_KEY": str(rerank.get("api_key") or ""), "PUBLIC_RERANK_API_KEY": str(rerank.get("api_key") or ""),
        "RERANK_MODEL": str(rerank.get("model") or ""), "DEEPXIV_BASE_URL": str(deepxiv.get("base_url") or ""),
        "DEEPXIV_TOKEN": str(deepxiv.get("api_key") or ""),
    }


def citation_configuration(config: Mapping[str, Any]) -> dict[str, Any]:
    providers = config.get("providers") if isinstance(config.get("providers"), Mapping) else {}
    llm = _provider(providers, "llm.primary")
    scraper = _provider(providers, "citation.scraperapi")
    search_llm = _provider(providers, "citation.search_llm")
    modules = config.get("modules") if isinstance(config.get("modules"), Mapping) else {}
    saved = modules.get("citationclaw") if isinstance(modules.get("citationclaw"), Mapping) else {}
    result = json.loads(json.dumps(saved, ensure_ascii=False))
    result.update({
        "openai_api_key": str(search_llm.get("api_key") or ""),
        "openai_base_url": str(search_llm.get("base_url") or ""),
        "openai_model": str(search_llm.get("model") or ""),
        "light_api_key": str(llm.get("api_key") or ""), "light_base_url": str(llm.get("base_url") or ""), "dashboard_model": str(llm.get("model") or ""),
        "scraper_api_keys": [str(scraper.get("api_key"))] if _enabled(scraper) and scraper.get("api_key") else [],
        "s2_api_key": str(_provider(providers, "citation.semantic_scholar").get("api_key") or ""),
        "openalex_email": str(_provider(providers, "citation.openalex").get("email") or ""),
        "wos_api_key": str(_provider(providers, "citation.wos").get("api_key") or ""),
        "mineru_api_token": str(_provider(providers, "document.mineru").get("api_key") or ""),
    })
    return result


class RuntimeConfigManager:
    def __init__(self, client: ReportHubClient, site_id: str, store: CredentialStore, apply: Callable[[Mapping[str, Any]], None]) -> None:
        self.client, self.site_id, self.store, self.apply = client, site_id, store, apply
        self._lock, self._last_check, self._serialized = threading.Lock(), 0.0, ""

    def sync(self, *, force: bool = False) -> bool:
        with self._lock:
            if not force and time.monotonic() - self._last_check < 3:
                return False
            self._last_check = time.monotonic()
            response = self.client.get_site_config(self.site_id)
            config = response.get("config") if isinstance(response.get("config"), Mapping) else {}
            serialized = json.dumps(config, ensure_ascii=False, sort_keys=True)
            if not config or serialized == self._serialized:
                return False
            self.store.save(config)
            self.apply(config)
            self._serialized = serialized
            return True


class ModuleCommandRelay:
    """Outbound bridge from Report Hub public pages to a user's local module API."""

    def __init__(
        self, client: ReportHubClient, site_id: str, local_endpoint: str,
        *, config_sync: Callable[[], object] | None = None, poll_seconds: float = 0.75,
    ) -> None:
        self.client = client
        self.site_id = site_id
        self.local_endpoint = local_endpoint.rstrip("/")
        self.config_sync = config_sync
        self.poll_seconds = max(0.25, poll_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name=f"module-relay-{self.site_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        log = logging.getLogger(__name__)
        while not self._stop.is_set():
            try:
                command = self.client.next_site_command(self.site_id)
                if not command:
                    self._stop.wait(self.poll_seconds)
                    continue
                if self.config_sync:
                    self.config_sync()
                self._execute(command)
            except Exception as exc:
                log.warning("module command relay poll failed: %s", exc)
                self._stop.wait(3)

    def _execute(self, command: Mapping[str, Any]) -> None:
        command_id = str(command.get("command_id") or "")
        try:
            body = base64.b64decode(str(command.get("body_b64") or ""))
            headers = {str(k): str(v) for k, v in dict(command.get("headers") or {}).items()}
            request = urllib.request.Request(
                self.local_endpoint + str(command.get("path") or ""),
                data=body if body else None,
                method=str(command.get("method") or "GET"),
                headers=headers,
            )
            try:
                with urllib.request.urlopen(request, timeout=40) as response:
                    response_body = response.read(8 * 1024 * 1024)
                    status = response.status
                    response_headers = {"content-type": response.headers.get("content-type", "application/json")}
            except urllib.error.HTTPError as exc:
                response_body = exc.read(8 * 1024 * 1024)
                status = exc.code
                response_headers = {"content-type": exc.headers.get("content-type", "application/json")}
            self.client.complete_site_command(
                self.site_id, command_id, status_code=status,
                headers=response_headers, body=response_body,
            )
        except Exception as exc:
            self.client.complete_site_command(
                self.site_id, command_id, status_code=502, headers={"content-type": "application/json"},
                body=b"", error_message=str(exc)[:500],
            )


def _provider(providers: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = providers.get(name)
    return value if isinstance(value, Mapping) else {}


def _enabled(provider: Mapping[str, Any]) -> bool:
    return bool(provider) and provider.get("enabled", True) is not False
