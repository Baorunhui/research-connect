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
    """The local authoritative store for unified provider and module settings."""

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
        daily_paper_embed_api_url=_runtime_value(embedding, "base_url", settings.daily_paper_embed_api_url),
        daily_paper_embed_api_key=_runtime_value(embedding, "api_key", settings.daily_paper_embed_api_key),
        daily_paper_rerank_base_url=_runtime_value(rerank, "base_url", settings.daily_paper_rerank_base_url),
        daily_paper_rerank_model=_runtime_value(rerank, "model", settings.daily_paper_rerank_model),
        daily_paper_rerank_api_key=_runtime_value(rerank, "api_key", settings.daily_paper_rerank_api_key),
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
        "base_url": llm.get("base_url", "") if _enabled(llm) else "",
        "api_key": llm.get("api_key", "") if _enabled(llm) else "",
        "model": llm.get("model", "") if _enabled(llm) else "",
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
    paper_sources = config.get("paper_sources") if isinstance(config.get("paper_sources"), Mapping) else {}
    embedding, rerank, supabase, deepxiv, semantic_scholar = (
        _provider(providers, name)
        for name in (
            "embedding.paper", "rerank.paper", "supabase.arxiv", "academic.deepxiv",
            "citation.semantic_scholar",
        )
    )
    return {
        "DPR_EMBED_API_URL": _active_value(embedding, "base_url"),
        "DPR_EMBED_API_KEY": _active_value(embedding, "api_key"),
        "RERANK_API_BASE_URL": _active_value(rerank, "base_url"),
        "PUBLIC_RERANK_API_BASE_URL": _active_value(rerank, "base_url"),
        "RERANK_API_KEY": _active_value(rerank, "api_key"),
        "PUBLIC_RERANK_API_KEY": _active_value(rerank, "api_key"),
        "RERANK_MODEL": _active_value(rerank, "model"),
        # These are a runtime projection of the single unified provider, not a
        # second configuration source. source_config.py applies the allowlisted
        # values to the per-run config snapshot, which protects CLI/bot runs
        # from stale or incomplete native config.yaml copies.
        "SUPABASE_URL": _active_value(supabase, "base_url"),
        "SUPABASE_ANON_KEY": _active_value(supabase, "anon_key"),
        "SUPABASE_PAPERS_TABLE": _active_value(supabase, "papers_table"),
        "SUPABASE_BM25_RPC": _active_value(supabase, "bm25_rpc"),
        "SUPABASE_VECTOR_RPC": _active_value(supabase, "vector_rpc"),
        "SUPABASE_VECTOR_RPC_EXACT": _active_value(supabase, "vector_rpc"),
        "DEEPXIV_API_BASE_URL": _active_value(deepxiv, "base_url"),
        "DEEPXIV_TOKEN": _active_value(deepxiv, "api_key"),
        "SEMANTIC_SCHOLAR_API_KEY": _active_value(semantic_scholar, "api_key"),
        "DPR_DEFAULT_USE_DEEPXIV": (
            "1" if _enabled(deepxiv) and _source_enabled(paper_sources, "deepxiv") else "0"
        ),
        "DPR_DEFAULT_USE_KAGGLE": "1" if _source_enabled(paper_sources, "kaggle") else "0",
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
        "openai_api_key": _active_value(search_llm, "api_key"),
        "openai_base_url": _active_value(search_llm, "base_url"),
        "openai_model": _active_value(search_llm, "model"),
        "light_api_key": _active_value(llm, "api_key"), "light_base_url": _active_value(llm, "base_url"), "dashboard_model": _active_value(llm, "model"),
        "scraper_api_keys": [str(scraper.get("api_key"))] if _enabled(scraper) and scraper.get("api_key") else [],
        "s2_api_key": _active_value(_provider(providers, "citation.semantic_scholar"), "api_key"),
        "openalex_email": _active_value(_provider(providers, "citation.openalex"), "email"),
        "wos_api_key": _active_value(_provider(providers, "citation.wos"), "api_key"),
        "mineru_api_token": _active_value(_provider(providers, "document.mineru"), "api_key"),
    })
    return result


class RuntimeConfigManager:
    def __init__(self, store: CredentialStore, apply: Callable[[Mapping[str, Any]], None]) -> None:
        self.store, self.apply = store, apply
        self._lock, self._last_check, self._serialized = threading.Lock(), 0.0, ""

    def sync(self, *, force: bool = False) -> bool:
        with self._lock:
            if not force and time.monotonic() - self._last_check < 3:
                return False
            self._last_check = time.monotonic()
            config = self.store.load()
            serialized = json.dumps(config, ensure_ascii=False, sort_keys=True)
            if not config or serialized == self._serialized:
                return False
            self.apply(config)
            self._serialized = serialized
            return True


class ModuleCommandRelay:
    """Outbound bridge from Report Hub public pages to a user's local module API."""

    def __init__(
        self, client: ReportHubClient, site_id: str, local_endpoint: str,
        *, config_sync: Callable[[], object] | None = None, poll_seconds: float = 0.75,
        request_timeout_seconds: int = 130, max_response_bytes: int = 8 * 1024 * 1024,
        auto_publish_dir: str | Path | None = None,
        auto_publish_kind: str = "daily-paper",
        publish_poll_seconds: float = 5.0,
        on_success: Callable[[Mapping[str, Any], bytes], None] | None = None,
    ) -> None:
        self.client = client
        self.site_id = site_id
        self.local_endpoint = local_endpoint.rstrip("/")
        self.config_sync = config_sync
        self.poll_seconds = max(0.25, poll_seconds)
        self.request_timeout_seconds = max(10, int(request_timeout_seconds))
        self.max_response_bytes = max(1024 * 1024, int(max_response_bytes))
        self.auto_publish_dir = Path(auto_publish_dir).resolve() if auto_publish_dir else None
        self.auto_publish_kind = auto_publish_kind
        self.publish_poll_seconds = max(1.0, float(publish_poll_seconds))
        self.on_success = on_success
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._publish_lock = threading.Lock()
        self._publish_runs: set[str] = set()

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
                with urllib.request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                    response_body = response.read(self.max_response_bytes)
                    status = response.status
                    response_headers = {"content-type": response.headers.get("content-type", "application/json")}
            except urllib.error.HTTPError as exc:
                response_body = exc.read(self.max_response_bytes)
                status = exc.code
                response_headers = {"content-type": exc.headers.get("content-type", "application/json")}
            self.client.complete_site_command(
                self.site_id, command_id, status_code=status,
                headers=response_headers, body=response_body,
            )
            if 200 <= status < 300 and self.on_success is not None:
                self.on_success(command, body)
            self._watch_dispatched_run(command, status, response_body)
        except Exception as exc:
            self.client.complete_site_command(
                self.site_id, command_id, status_code=502, headers={"content-type": "application/json"},
                body=b"", error_message=str(exc)[:500],
            )

    def _watch_dispatched_run(
        self, command: Mapping[str, Any], status: int, response_body: bytes
    ) -> None:
        """Publish a fresh site snapshot after a public-page workflow succeeds.

        The watcher starts only for a successful workflow dispatch, so no
        permanent run polling is introduced and it keeps working after the
        browser tab is closed.
        """
        if self.auto_publish_dir is None or status < 200 or status >= 300:
            return
        if str(command.get("path") or "").split("?", 1)[0] != "/api/local/workflows/dispatch":
            return
        try:
            payload = json.loads(response_body.decode("utf-8"))
            run = payload.get("run") if isinstance(payload, Mapping) else None
            run_id = str(run.get("id") or "") if isinstance(run, Mapping) else ""
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not run_id:
            return
        with self._publish_lock:
            if run_id in self._publish_runs:
                return
            self._publish_runs.add(run_id)
        threading.Thread(
            target=self._wait_and_publish,
            args=(run_id,),
            name=f"site-publish-{run_id}",
            daemon=True,
        ).start()

    def _wait_and_publish(self, run_id: str) -> None:
        log = logging.getLogger(__name__)
        try:
            while not self._stop.is_set():
                request = urllib.request.Request(
                    f"{self.local_endpoint}/api/local/runtime/runs/{run_id}",
                    method="GET",
                )
                try:
                    with urllib.request.urlopen(request, timeout=15) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    log.debug("waiting for local run %s before publishing: %s", run_id, exc)
                    self._stop.wait(self.publish_poll_seconds)
                    continue
                run = payload.get("run") if isinstance(payload, Mapping) else None
                status = str(run.get("status") or "").lower() if isinstance(run, Mapping) else ""
                conclusion = str(run.get("conclusion") or "").lower() if isinstance(run, Mapping) else ""
                if status not in {"completed", "cancelled", "interrupted", "failed"}:
                    self._stop.wait(self.publish_poll_seconds)
                    continue
                if status == "completed" and conclusion == "success":
                    public_url = self.client.upload_site(
                        self.site_id,
                        self.auto_publish_dir,
                        site_kind=self.auto_publish_kind,
                    )
                    log.info("published completed local run %s at %s", run_id, public_url)
                else:
                    log.info(
                        "skipped site publish for local run %s (%s/%s)",
                        run_id, status, conclusion,
                    )
                return
        except Exception as exc:
            log.warning("could not publish completed local run %s: %s", run_id, exc)
        finally:
            with self._publish_lock:
                self._publish_runs.discard(run_id)


def _provider(providers: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = providers.get(name)
    return value if isinstance(value, Mapping) else {}


def _enabled(provider: Mapping[str, Any]) -> bool:
    return bool(provider) and provider.get("enabled", True) is not False


def _active_value(provider: Mapping[str, Any], key: str) -> str:
    return str(provider.get(key) or "").strip() if _enabled(provider) else ""


def _runtime_value(provider: Mapping[str, Any], key: str, fallback: str) -> str:
    if not provider:
        return str(fallback or "").strip()
    return _active_value(provider, key)


def _source_enabled(sources: Mapping[str, Any], name: str) -> bool:
    value = sources.get(name)
    return isinstance(value, Mapping) and value.get("enabled", False) is True
