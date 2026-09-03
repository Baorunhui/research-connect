from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from research_connect_core import DataPaths

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MONOREPO_ROOT = PROJECT_ROOT.parents[1]


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(name: str, default: int, minimum: int = 0) -> int:
    raw = str(os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return max(minimum, value)


@dataclass(frozen=True)
class ProviderSettings:
    name: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: int = 120
    max_retries: int = 1

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


@dataclass(frozen=True)
class Settings:
    feishu_app_id: str
    feishu_app_secret: str
    feishu_domain: str
    feishu_require_mention: bool
    feishu_allowed_open_ids: frozenset[str]
    providers: tuple[ProviderSettings, ...]
    db_path: Path
    history_messages: int
    history_chars: int
    workers: int
    log_level: str
    daily_paper_transport: str
    daily_paper_endpoint: str
    daily_paper_timeout_seconds: int
    daily_paper_poll_seconds: int
    daily_paper_public_url: str
    daily_paper_embed_api_url: str
    daily_paper_embed_api_key: str
    daily_paper_skip_llm_refine: bool
    daily_paper_rerank_profile: str
    daily_paper_rerank_provider: str
    daily_paper_rerank_model: str
    daily_paper_rerank_base_url: str
    daily_paper_rerank_api_key: str
    daily_paper_dir: Path
    citationclaw_endpoint: str
    citationclaw_timeout_seconds: int
    citationclaw_poll_seconds: int
    xhs_agent_dir: Path
    xhs_agent_timeout_seconds: int
    xhs_agent_offline: bool
    web_search_provider: str
    web_search_endpoint: str
    web_search_timeout_seconds: int
    web_search_max_results: int
    web_search_cache_hours: int
    url_fetch_provider: str
    jina_reader_endpoint: str
    report_hub_api_url: str
    report_hub_agent_token: str
    report_hub_timeout_seconds: int
    config_api_endpoint: str

    @property
    def feishu_configured(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret)

    @property
    def llm_configured(self) -> bool:
        return any(provider.configured for provider in self.providers)


def load_settings(env_file: str | Path | None = None) -> Settings:
    if env_file is None:
        env_path = PROJECT_ROOT / ".env"
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        env_path = Path(env_file).resolve()
        load_dotenv(dotenv_path=env_path, override=False)

    timeout = _as_int("LLM_TIMEOUT_SECONDS", 120, minimum=1)
    retries = _as_int("LLM_MAX_RETRIES", 1, minimum=0)
    primary = ProviderSettings(
        name="primary",
        base_url=str(os.getenv("LLM_BASE_URL") or "").strip().rstrip("/"),
        api_key=str(os.getenv("LLM_API_KEY") or "").strip(),
        model=str(os.getenv("LLM_MODEL") or "").strip(),
        timeout_seconds=timeout,
        max_retries=retries,
    )
    fallback = ProviderSettings(
        name="fallback",
        base_url=str(os.getenv("LLM_FALLBACK_BASE_URL") or "").strip().rstrip("/"),
        api_key=str(os.getenv("LLM_FALLBACK_API_KEY") or "").strip(),
        model=str(os.getenv("LLM_FALLBACK_MODEL") or "").strip(),
        timeout_seconds=timeout,
        max_retries=retries,
    )

    allowed = frozenset(
        item.strip()
        for item in str(os.getenv("FEISHU_ALLOWED_OPEN_IDS") or "").split(",")
        if item.strip()
    )
    domain = str(os.getenv("FEISHU_DOMAIN") or "feishu").strip().lower()
    if domain not in {"feishu", "lark"}:
        raise ValueError("FEISHU_DOMAIN must be 'feishu' or 'lark'")

    configured_db = str(os.getenv("CONNECT_HUB_DB_PATH") or "").strip()
    db_path = (
        Path(configured_db)
        if configured_db
        else DataPaths.for_module("connect-hub").state / "connect_hub.sqlite3"
    )
    if configured_db and not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    configured_xhs = str(os.getenv("XHS_AGENT_DIR") or "").strip()
    xhs_agent_dir = Path(configured_xhs) if configured_xhs else MONOREPO_ROOT / "modules" / "xhs-agent"
    if not xhs_agent_dir.is_absolute():
        xhs_agent_dir = PROJECT_ROOT / xhs_agent_dir
    configured_daily = str(os.getenv("DAILY_PAPER_DIR") or "").strip()
    daily_paper_dir = Path(configured_daily) if configured_daily else MONOREPO_ROOT / "modules" / "daily-paper-reader"
    if not daily_paper_dir.is_absolute():
        daily_paper_dir = PROJECT_ROOT / daily_paper_dir

    return Settings(
        feishu_app_id=str(os.getenv("FEISHU_APP_ID") or "").strip(),
        feishu_app_secret=str(os.getenv("FEISHU_APP_SECRET") or "").strip(),
        feishu_domain=domain,
        feishu_require_mention=_as_bool(os.getenv("FEISHU_REQUIRE_MENTION"), default=True),
        feishu_allowed_open_ids=allowed,
        providers=tuple(p for p in (primary, fallback) if p.configured),
        db_path=db_path.resolve(),
        history_messages=_as_int("CONNECT_HUB_HISTORY_MESSAGES", 20, minimum=0),
        history_chars=_as_int("CONNECT_HUB_HISTORY_CHARS", 5500, minimum=1000),
        workers=_as_int("CONNECT_HUB_WORKERS", 4, minimum=1),
        log_level=str(os.getenv("CONNECT_HUB_LOG_LEVEL") or "INFO").strip().upper(),
        daily_paper_transport=str(os.getenv("DAILY_PAPER_TRANSPORT") or "local_http").strip().lower(),
        daily_paper_endpoint=str(os.getenv("DAILY_PAPER_ENDPOINT") or "http://127.0.0.1:8567").strip(),
        daily_paper_timeout_seconds=_as_int(
            "DAILY_PAPER_TIMEOUT_SECONDS", 1800, minimum=60
        ),
        daily_paper_poll_seconds=_as_int("DAILY_PAPER_POLL_SECONDS", 3, minimum=1),
        daily_paper_public_url=str(
            os.getenv("DAILY_PAPER_PUBLIC_URL") or ""
        ).strip(),
        daily_paper_embed_api_url=str(
            os.getenv("DAILY_PAPER_EMBED_API_URL") or "https://zwwen.online/embed"
        ).strip(),
        daily_paper_embed_api_key=str(
            os.getenv("DAILY_PAPER_EMBED_API_KEY") or ""
        ).strip(),
        daily_paper_skip_llm_refine=_as_bool(
            os.getenv("DAILY_PAPER_SKIP_LLM_REFINE"), default=False
        ),
        daily_paper_rerank_profile=str(
            os.getenv("DAILY_PAPER_RERANK_PROFILE") or "public-zwwen-rerank"
        ).strip(),
        daily_paper_rerank_provider=str(
            os.getenv("DAILY_PAPER_RERANK_PROVIDER") or "public_zwwen"
        ).strip(),
        daily_paper_rerank_model=str(
            os.getenv("DAILY_PAPER_RERANK_MODEL")
            or "Qwen/Qwen3-Reranker-0.6B"
        ).strip(),
        daily_paper_rerank_base_url=str(
            os.getenv("DAILY_PAPER_RERANK_BASE_URL")
            or "https://zwwen.online/rerank"
        ).strip(),
        daily_paper_rerank_api_key=str(
            os.getenv("DAILY_PAPER_RERANK_API_KEY") or ""
        ).strip(),
        daily_paper_dir=daily_paper_dir.resolve(),
        citationclaw_endpoint=str(
            os.getenv("CITATIONCLAW_ENDPOINT") or "http://127.0.0.1:8000"
        ).strip().rstrip("/"),
        citationclaw_timeout_seconds=_as_int("CITATIONCLAW_TIMEOUT_SECONDS", 3600, minimum=60),
        citationclaw_poll_seconds=_as_int("CITATIONCLAW_POLL_SECONDS", 2, minimum=1),
        xhs_agent_dir=xhs_agent_dir.resolve(),
        xhs_agent_timeout_seconds=_as_int("XHS_AGENT_TIMEOUT_SECONDS", 900, minimum=30),
        xhs_agent_offline=_as_bool(os.getenv("XHS_AGENT_OFFLINE"), default=False),
        web_search_provider=str(
            os.getenv("WEB_SEARCH_PROVIDER") or "exa_mcp"
        ).strip().lower(),
        web_search_endpoint=str(
            os.getenv("WEB_SEARCH_ENDPOINT")
            or "https://mcp.exa.ai/mcp?tools=web_search_exa"
        ).strip(),
        web_search_timeout_seconds=_as_int(
            "WEB_SEARCH_TIMEOUT_SECONDS", 30, minimum=5
        ),
        web_search_max_results=_as_int("WEB_SEARCH_MAX_RESULTS", 5, minimum=1),
        web_search_cache_hours=_as_int("WEB_SEARCH_CACHE_HOURS", 24, minimum=0),
        url_fetch_provider=str(
            os.getenv("URL_FETCH_PROVIDER") or "jina"
        ).strip().lower(),
        jina_reader_endpoint=str(
            os.getenv("JINA_READER_ENDPOINT") or "https://r.jina.ai/"
        ).strip(),
        report_hub_api_url=str(os.getenv("REPORT_HUB_API_URL") or "").strip().rstrip("/"),
        report_hub_agent_token=str(os.getenv("REPORT_HUB_AGENT_TOKEN") or "").strip(),
        report_hub_timeout_seconds=_as_int("REPORT_HUB_TIMEOUT_SECONDS", 10, minimum=2),
        config_api_endpoint=str(
            os.getenv("CONNECT_CONFIG_ENDPOINT") or "http://127.0.0.1:8791"
        ).strip().rstrip("/"),
    )
