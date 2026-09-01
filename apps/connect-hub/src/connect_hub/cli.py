from __future__ import annotations

import argparse
import hashlib
import importlib.util
import logging
import os
import platform
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from connect_hub.config import MONOREPO_ROOT, load_settings
from connect_hub.connectors.feishu import FeishuConnector
from connect_hub.adapters import CitationClawAdapter, DailyPaperAdapter
from connect_hub.llm import LLMGateway
from connect_hub.jobs import JobCoordinator
from connect_hub.service import ChatService
from connect_hub.storage import ConversationStore
from connect_hub.tools import ToolRegistry
from connect_hub.tools.builtin import system_status_tool
from connect_hub.tools.daily_paper import daily_paper_tools
from connect_hub.tools.web import web_tools
from connect_hub.tools.xhs import xhs_generate_tool
from connect_hub.websearch import ExaMCPWebSearchProvider, JinaReaderProvider
from connect_hub.reporting import ReportHubClient, ReportHubError
from connect_hub.provider_config import (
    CredentialStore,
    ModuleCommandRelay,
    RuntimeConfigManager,
    apply_to_settings,
    bootstrap_config,
    citation_configuration,
    daily_configuration,
    daily_environment,
)
from research_connect_core import DataPaths, configure_playwright_browsers


def _build_runtime(
    *, interrupt_stale_jobs: bool = False, env_file: str | Path | None = None
) -> tuple[object, object, object, object]:
    settings = load_settings(env_file)
    store = ConversationStore(settings.db_path)
    report_hub = (
        ReportHubClient(
            settings.report_hub_api_url,
            settings.report_hub_agent_token,
            timeout_seconds=settings.report_hub_timeout_seconds,
        )
        if settings.report_hub_api_url and settings.report_hub_agent_token
        else None
    )
    daily_site_id = _module_site_id(settings, "daily-paper")
    citation_site_id = _module_site_id(settings, "citationclaw")
    config_site_id = _module_site_id(settings, "connect-config")
    credential_store = CredentialStore(settings.db_path.parent / "providers.json")
    unified_config: dict[str, object] = credential_store.load()
    shortcut_urls: dict[str, str] = {}
    if report_hub is not None and report_hub.configured:
        for shortcut, site_id, module_name, title in (
            ("paper_reader", daily_site_id, "daily-paper", "Daily Paper Reader"),
            ("citationclaw", citation_site_id, "citationclaw", "CitationClaw"),
        ):
            try:
                shortcut_urls[shortcut], _ready = report_hub.ensure_site(
                    site_id=site_id, module_name=module_name, title=title
                )
            except ReportHubError as exc:
                logging.getLogger(__name__).warning(
                    "could not resolve %s public site: %s", module_name, exc
                )
        try:
            config_site_url, _ready = report_hub.ensure_site(
                site_id=config_site_id, module_name="other", title="Research Connect 配置中心"
            )
            shortcut_urls["config"] = report_hub.configuration_url(config_site_url)
            remote = report_hub.get_site_config(config_site_id)
            if bool(remote.get("configured")) and isinstance(remote.get("config"), dict):
                unified_config = dict(remote["config"])
            else:
                unified_config = bootstrap_config(settings)
                report_hub.put_site_config(config_site_id, unified_config)
            # Reading both legacy module views once lets an upgraded Report Hub
            # import their former per-site settings into the single installation
            # record. This is a one-time migration, not a precedence rule.
            report_hub.get_site_config(daily_site_id)
            report_hub.get_site_config(citation_site_id)
            migrated = report_hub.get_site_config(config_site_id)
            if isinstance(migrated.get("config"), dict):
                unified_config = dict(migrated["config"])
            credential_store.save(unified_config)
        except ReportHubError as exc:
            logging.getLogger(__name__).warning("could not initialize unified configuration: %s", exc)
    if unified_config:
        settings = apply_to_settings(settings, unified_config)
    gateway = LLMGateway(settings.providers)
    tools = ToolRegistry(
        store,
        jobs=JobCoordinator(
            store,
            interrupt_stale=interrupt_stale_jobs,
            report_hub=report_hub,
        ),
    )
    tools.register(system_status_tool(lambda: tools.names))
    xhs_dir = settings.xhs_agent_dir
    if xhs_dir.exists():
        tools.register(
            xhs_generate_tool(
                agent_dir=xhs_dir,
                provider=lambda: gateway.primary_provider,
                output_dir=DataPaths.for_module("xhs-agent").artifacts,
                timeout_seconds=settings.xhs_agent_timeout_seconds,
                offline=settings.xhs_agent_offline,
            )
        )
    daily_env: dict[str, str] = {
        "DPR_EMBED_API_URL": settings.daily_paper_embed_api_url,
        "DPR_EMBED_API_KEY": settings.daily_paper_embed_api_key,
        "DPR_EMBED_ALLOW_LOCAL_FALLBACK": "0",
        "RERANK_PROFILE": settings.daily_paper_rerank_profile,
        "RERANK_PROVIDER": settings.daily_paper_rerank_provider,
        "RERANK_MODEL": settings.daily_paper_rerank_model,
        "RERANK_API_BASE_URL": settings.daily_paper_rerank_base_url,
        "PUBLIC_RERANK_API_BASE_URL": settings.daily_paper_rerank_base_url,
        "SILICONFLOW_RERANK_URL": settings.daily_paper_rerank_base_url,
        "RERANK_API_KEY": settings.daily_paper_rerank_api_key,
        "PUBLIC_RERANK_API_KEY": settings.daily_paper_rerank_api_key,
        "SILICONFLOW_API_KEY": settings.daily_paper_rerank_api_key,
        "DPR_PUBLIC_SERVICE_API_KEY": settings.daily_paper_rerank_api_key,
    }
    if settings.providers:
        daily_provider = settings.providers[0]
        daily_env.update(
            {
                "SUMMARY_API_KEY": daily_provider.api_key,
                "DEEPSEEK_API_KEY": daily_provider.api_key,
                "SUMMARY_BASE_URL": daily_provider.base_url,
                "DEEPSEEK_BASE_URL": daily_provider.base_url,
                "LLM_PRIMARY_BASE_URL": daily_provider.base_url,
                "SUMMARY_MODEL": daily_provider.model,
                "DEEPSEEK_MODEL": daily_provider.model,
            }
        )
    daily_paper = DailyPaperAdapter(
        transport=settings.daily_paper_transport,
        endpoint=settings.daily_paper_endpoint,
        timeout_seconds=settings.daily_paper_timeout_seconds,
        poll_seconds=settings.daily_paper_poll_seconds,
        extra_env=daily_env,
        skip_llm_refine=settings.daily_paper_skip_llm_refine,
        project_dir=settings.daily_paper_dir,
    )
    # Apply the configuration snapshot loaded during bootstrap immediately.
    # RuntimeConfigManager normally applies remote changes, but its sync is
    # intentionally change-driven.  A freshly started Daily Paper service has
    # no in-memory/local copy yet, even when providers.json already contains a
    # valid snapshot, so relying only on a later "changed" event can create the
    # first workflow with blank provider fields.
    if unified_config and daily_paper.configured:
        try:
            daily_paper.extra_env.update(daily_environment(unified_config))
            daily_paper.apply_configuration(daily_configuration(unified_config))
            daily_paper.apply_runtime_environment(daily_paper.extra_env)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "could not bootstrap Daily Paper configuration: %s", exc
            )
    feishu_inbound_dir = DataPaths.for_module("connect-hub").artifacts / "inbound"
    feishu_inbound_dir.mkdir(parents=True, exist_ok=True)
    if daily_paper.configured:
        for definition in daily_paper_tools(
            daily_paper,
            output_dir=DataPaths.for_module("daily-paper-reader").artifacts,
            public_url=(shortcut_urls.get("paper_reader") or settings.daily_paper_public_url),
            report_hub=report_hub,
            site_id=daily_site_id,
            inbound_dir=feishu_inbound_dir,
        ):
            tools.register(definition)
    citationclaw = CitationClawAdapter(
        settings.citationclaw_endpoint,
        timeout_seconds=settings.citationclaw_timeout_seconds,
        poll_seconds=settings.citationclaw_poll_seconds,
    )
    search_provider = None
    if settings.web_search_provider == "exa_mcp":
        search_provider = ExaMCPWebSearchProvider(
            settings.web_search_endpoint,
            timeout_seconds=settings.web_search_timeout_seconds,
        )
    elif settings.web_search_provider != "disabled":
        raise ValueError(
            f"unsupported WEB_SEARCH_PROVIDER: {settings.web_search_provider}"
        )
    url_provider = None
    if settings.url_fetch_provider == "jina":
        url_provider = JinaReaderProvider(
            settings.jina_reader_endpoint,
            timeout_seconds=settings.web_search_timeout_seconds,
        )
    elif settings.url_fetch_provider != "disabled":
        raise ValueError(
            f"unsupported URL_FETCH_PROVIDER: {settings.url_fetch_provider}"
        )
    def sync_web_tools() -> None:
        tools.unregister("web_search")
        tools.unregister("read_web_page")
        for definition in web_tools(
            store,
            search_provider=search_provider,
            url_provider=url_provider,
            max_results=settings.web_search_max_results,
            cache_hours=settings.web_search_cache_hours,
        ):
            tools.register(definition)

    sync_web_tools()
    config_manager = None
    if report_hub is not None and report_hub.configured and shortcut_urls.get("config"):
        def apply_runtime(config: object) -> None:
            nonlocal search_provider, url_provider
            if not isinstance(config, dict):
                return
            runtime_settings = apply_to_settings(settings, config)
            gateway.update_providers(runtime_settings.providers)
            if runtime_settings.web_search_provider == "exa_mcp":
                if search_provider is None:
                    search_provider = ExaMCPWebSearchProvider(
                        runtime_settings.web_search_endpoint,
                        timeout_seconds=runtime_settings.web_search_timeout_seconds,
                    )
                else:
                    search_provider.endpoint = runtime_settings.web_search_endpoint
            else:
                search_provider = None
            if runtime_settings.url_fetch_provider == "jina":
                if url_provider is None:
                    url_provider = JinaReaderProvider(
                        runtime_settings.jina_reader_endpoint,
                        timeout_seconds=runtime_settings.web_search_timeout_seconds,
                    )
                else:
                    url_provider.endpoint = runtime_settings.jina_reader_endpoint.rstrip("/") + "/"
            else:
                url_provider = None
            sync_web_tools()
            daily_paper.extra_env.update(daily_environment(config))
            if daily_paper.configured:
                try:
                    daily_paper.apply_configuration(daily_configuration(config))
                    daily_paper.apply_runtime_environment(daily_paper.extra_env)
                except Exception as exc:
                    logging.getLogger(__name__).warning("could not apply Daily Paper configuration: %s", exc)
            if citationclaw.configured:
                try:
                    citationclaw.apply_configuration(citation_configuration(config))
                except Exception as exc:
                    logging.getLogger(__name__).warning("could not apply CitationClaw configuration: %s", exc)

        config_manager = RuntimeConfigManager(
            report_hub, config_site_id, credential_store, apply_runtime
        )
        # The remote value has already been applied to Settings. Mark it as the
        # baseline and let the first message push it into running module APIs.
    service = ChatService(
        gateway,
        store,
        history_messages=settings.history_messages,
        history_chars=settings.history_chars,
        tools=tools,
        shortcut_urls=shortcut_urls,
        config_sync=(config_manager.sync if config_manager is not None else None),
    )
    if config_manager is not None:
        try:
            config_manager.sync(force=True)
        except Exception as exc:
            logging.getLogger(__name__).warning("initial configuration sync failed: %s", exc)
        relay = ModuleCommandRelay(
            report_hub, citation_site_id, settings.citationclaw_endpoint,
            config_sync=lambda: config_manager.sync(force=True),
        )
        relay.start()
        # The connector owns the service for its whole lifetime; retain the daemon
        # here so it is not garbage-collected while Feishu is running.
        service.module_command_relay = relay
    return settings, gateway, store, service


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def command_check(env_file: str | Path | None = None) -> int:
    settings = load_settings(env_file)
    _configure_logging(settings.log_level)
    failures: list[str] = []
    warnings: list[str] = []

    def report(label: str, ok: bool, detail: str, *, required: bool = True) -> None:
        print(f"[{'OK' if ok else 'WARN' if not required else 'FAIL'}] {label}: {detail}")
        if not ok:
            (failures if required else warnings).append(label)

    supported_python = (3, 11) <= sys.version_info < (3, 14)
    report("Python", supported_python, f"{platform.python_version()} · {platform.system()} {platform.machine()}")
    report("Feishu", settings.feishu_configured, f"domain={settings.feishu_domain}; credentials {'configured' if settings.feishu_configured else 'missing'}")
    report(
        "LLM",
        settings.llm_configured,
        ", ".join(p.name + ":" + p.model for p in settings.providers) or "missing base_url/model/api_key",
    )
    report(
        "Report Hub",
        bool(settings.report_hub_api_url and settings.report_hub_agent_token),
        settings.report_hub_api_url or "missing REPORT_HUB_API_URL / REPORT_HUB_AGENT_TOKEN",
    )
    required_modules = ("fitz", "PIL", "playwright", "citationclaw", "connect_hub")
    missing_modules = [name for name in required_modules if importlib.util.find_spec(name) is None]
    report("Python dependencies", not missing_modules, "ready" if not missing_modules else "missing " + ", ".join(missing_modules))
    daily_ok = settings.daily_paper_dir.is_dir() and (settings.daily_paper_dir / "src" / "local_server.py").is_file()
    report("Daily Paper source", daily_ok, str(settings.daily_paper_dir))
    xhs_ok = settings.xhs_agent_dir.is_dir() and (settings.xhs_agent_dir / "src" / "xhs_agent").is_dir()
    report("XHS Agent source", xhs_ok, str(settings.xhs_agent_dir))
    try:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        probe = settings.db_path.parent / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        data_ok = True
    except OSError:
        data_ok = False
    report("Data directory", data_ok, str(settings.db_path.parent))

    docling_ok = importlib.util.find_spec("docling") is not None and importlib.util.find_spec("pypdfium2") is not None
    report("Docling", docling_ok, "ready" if docling_ok else "not installed; PDF figures fall back to PyMuPDF", required=False)
    try:
        browser_path = configure_playwright_browsers()
        browser_names = {"chrome", "chrome.exe", "headless_shell", "headless_shell.exe"}
        browser_ok = any(path.is_file() and path.name in browser_names for path in browser_path.rglob("*"))
        browser_detail = str(browser_path)
    except OSError as exc:
        browser_ok = False
        browser_detail = str(exc)[:160]
    report("Playwright Chromium", browser_ok, browser_detail, required=False)

    if settings.report_hub_api_url:
        report("Report Hub health", _healthy(settings.report_hub_api_url.rstrip("/") + "/healthz"), settings.report_hub_api_url, required=False)
    if settings.feishu_allowed_open_ids:
        print(f"[INFO] Feishu allowlist: {len(settings.feishu_allowed_open_ids)} user(s)")
    else:
        print("[INFO] Feishu allowlist: empty; application availability scope applies")
    print(f"Doctor result: {len(failures)} failure(s), {len(warnings)} optional warning(s)")
    return 0 if not failures else 2


def command_chat(env_file: str | Path | None = None) -> int:
    settings, _gateway, _store, service = _build_runtime(env_file=env_file)
    _configure_logging(settings.log_level)
    print("Local chat ready. Type /help or Ctrl-D to exit.")
    while True:
        try:
            text = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        reply = service.handle("local:console", text)
        print(reply.text)


def command_feishu(env_file: str | Path | None = None) -> int:
    settings, _gateway, _store, service = _build_runtime(
        interrupt_stale_jobs=True, env_file=env_file
    )
    _configure_logging(settings.log_level)
    connector = FeishuConnector(
        settings,
        service,
        inbound_dir=DataPaths.for_module("connect-hub").artifacts / "inbound",
    )
    connector.start()
    return 0


def command_serve(env_file: str | Path | None = None) -> int:
    """Start lightweight module APIs and the always-on Feishu connector."""
    settings = load_settings(env_file)
    _configure_logging(settings.log_level)
    services: list[_LocalService] = []
    try:
        if settings.daily_paper_transport == "local_http":
            services.append(
                _LocalService.start_if_needed(
                    name="daily-paper",
                    endpoint=settings.daily_paper_endpoint,
                    health_path="/api/local/health",
                    command=(
                        sys.executable,
                        str(settings.daily_paper_dir / "src" / "local_server.py"),
                        "--serve",
                        "--no-schedule",
                    ),
                    cwd=settings.daily_paper_dir,
                )
            )
        citation_url = urlparse(settings.citationclaw_endpoint)
        services.append(
            _LocalService.start_if_needed(
                name="citationclaw",
                endpoint=settings.citationclaw_endpoint,
                health_path="/api/task/status",
                command=(
                    sys.executable,
                    "-m",
                    "citationclaw",
                    "--host",
                    citation_url.hostname or "127.0.0.1",
                    "--port",
                    str(citation_url.port or 8000),
                    "--no-browser",
                ),
                cwd=MONOREPO_ROOT / "modules" / "citationclaw",
            )
        )
        _publish_original_module_sites(settings)
        return command_feishu(env_file)
    finally:
        for service in reversed(services):
            service.stop()


def _module_site_id(settings: object, module_name: str) -> str:
    identity = str(getattr(settings, "feishu_app_id", "") or "")
    if not identity:
        identity = (
            str(getattr(settings, "daily_paper_dir", ""))
            if module_name == "daily-paper"
            else str(getattr(settings, "citationclaw_endpoint", ""))
        )
    return module_name + "-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _publish_original_module_sites(settings: object) -> None:
    """Best-effort startup sync of the two original module web frontends."""
    api_url = str(getattr(settings, "report_hub_api_url", "") or "")
    agent_token = str(getattr(settings, "report_hub_agent_token", "") or "")
    if not api_url or not agent_token:
        return
    report_hub = ReportHubClient(
        api_url,
        agent_token,
        timeout_seconds=int(getattr(settings, "report_hub_timeout_seconds", 10)),
    )
    specs = (
        (
            "daily-paper",
            "Daily Paper Reader",
            Path(getattr(settings, "daily_paper_dir")),
            "daily-paper",
        ),
        (
            "citationclaw",
            "CitationClaw",
            MONOREPO_ROOT / "modules" / "citationclaw",
            "citationclaw",
        ),
    )
    for module_name, title, project_dir, site_kind in specs:
        site_id = _module_site_id(settings, module_name)
        try:
            report_hub.ensure_site(
                site_id=site_id, module_name=module_name, title=title
            )
            public_url = report_hub.upload_site(
                site_id, project_dir, site_kind=site_kind
            )
            logging.getLogger(__name__).info(
                "published %s original site at %s", module_name, public_url
            )
        except ReportHubError as exc:
            # The bot remains useful when the public host is temporarily down.
            logging.getLogger(__name__).warning(
                "could not publish %s original site: %s", module_name, exc
            )


def command_module(module_name: str, env_file: str | Path | None = None) -> int:
    """Start one original module UI after the shared public configuration preflight."""
    settings = load_settings(env_file)
    _configure_logging(settings.log_level)
    if not settings.report_hub_api_url or not settings.report_hub_agent_token:
        print("未配置 Report Hub，无法提供公网原版配置页面。")
        return 2
    report_hub = ReportHubClient(
        settings.report_hub_api_url,
        settings.report_hub_agent_token,
        timeout_seconds=settings.report_hub_timeout_seconds,
    )
    site_id = _module_site_id(settings, module_name)
    project_dir = (
        settings.daily_paper_dir
        if module_name == "daily-paper"
        else MONOREPO_ROOT / "modules" / "citationclaw"
    )
    title = "Daily Paper Reader" if module_name == "daily-paper" else "CitationClaw"
    try:
        public_url, _ready = report_hub.ensure_site(
            site_id=site_id, module_name=module_name, title=title
        )
        public_url = report_hub.upload_site(
            site_id,
            project_dir,
            site_kind=("citationclaw" if module_name == "citationclaw" else "daily-paper"),
        )
        remote_config = report_hub.get_site_config(site_id)
    except ReportHubError as exc:
        print(f"公网模块页面不可用：{exc}")
        return 2
    if not bool(remote_config.get("configured")):
        suffix = "?panel=config" if module_name == "citationclaw" else ""
        print(f"{title} 尚未配置，请先打开原版网页完成设置：\n{public_url}{suffix}")
        return 2

    service: _LocalService | None = None
    try:
        if module_name == "daily-paper":
            service = _LocalService.start_if_needed(
                name="daily-paper",
                endpoint=settings.daily_paper_endpoint,
                health_path="/api/local/health",
                command=(
                    sys.executable,
                    str(settings.daily_paper_dir / "src" / "local_server.py"),
                    "--serve",
                    "--no-schedule",
                ),
                cwd=settings.daily_paper_dir,
            )
            adapter = DailyPaperAdapter(
                transport="local_http", endpoint=settings.daily_paper_endpoint
            )
            adapter.apply_configuration(dict(remote_config.get("config") or {}))
            local_url = settings.daily_paper_endpoint
        else:
            citation_url = urlparse(settings.citationclaw_endpoint)
            service = _LocalService.start_if_needed(
                name="citationclaw",
                endpoint=settings.citationclaw_endpoint,
                health_path="/api/task/status",
                command=(
                    sys.executable,
                    "-m",
                    "citationclaw",
                    "--host",
                    citation_url.hostname or "127.0.0.1",
                    "--port",
                    str(citation_url.port or 8000),
                    "--no-browser",
                ),
                cwd=project_dir,
            )
            adapter = CitationClawAdapter(settings.citationclaw_endpoint)
            adapter.apply_configuration(dict(remote_config.get("config") or {}))
            local_url = settings.citationclaw_endpoint
        print(f"{title} 已启动。\n本机：{local_url}\n公网：{public_url}")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        if service is not None:
            service.stop()


class _LocalService:
    def __init__(self, name: str, process: subprocess.Popen[str] | None = None) -> None:
        self.name = name
        self.process = process

    @classmethod
    def start_if_needed(
        cls,
        *,
        name: str,
        endpoint: str,
        health_path: str,
        command: tuple[str, ...],
        cwd: Path,
    ) -> "_LocalService":
        parsed = urlparse(endpoint)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            logging.getLogger(__name__).info("using external %s service at %s", name, endpoint)
            return cls(name)
        if _healthy(endpoint + health_path):
            logging.getLogger(__name__).info("reusing %s service at %s", name, endpoint)
            return cls(name)
        child_env = dict(os.environ)
        # Windows commonly defaults redirected child stdout to a legacy code
        # page (for example GBK/cp936). CitationClaw's startup output contains
        # Unicode symbols, so force every managed Python service to UTF-8.
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        options: dict[str, object] = {
            "cwd": str(cwd),
            "env": child_env,
            "text": True,
        }
        log_dir = DataPaths.for_module("connect-hub").state / "service-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_handle = (log_dir / f"{name}.log").open("a", encoding="utf-8")
        options.update(stdout=log_handle, stderr=subprocess.STDOUT)
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(command, **options)  # type: ignore[arg-type]
        service = cls(name, process)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{name} local service exited with {process.returncode}")
            if _healthy(endpoint + health_path):
                logging.getLogger(__name__).info("started %s service at %s", name, endpoint)
                return service
            time.sleep(0.3)
        service.stop()
        raise RuntimeError(f"{name} local service did not become healthy at {endpoint}")

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Feishu connector and LLM gateway")
    parser.add_argument("--env-file", help="load configuration from this .env file")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="validate configuration without revealing secrets")
    subparsers.add_parser("doctor", help="check configuration, dependencies and platform readiness")
    subparsers.add_parser("chat", help="test the LLM gateway in a local terminal")
    subparsers.add_parser("feishu", help="start the Feishu WebSocket connector")
    subparsers.add_parser("serve", help="start local module APIs and the Feishu connector")
    subparsers.add_parser("daily-paper", help="start Daily Paper with public config preflight")
    subparsers.add_parser("citationclaw", help="start CitationClaw with public config preflight")
    args = parser.parse_args(argv)
    if args.command in {"check", "doctor"}:
        return command_check(args.env_file)
    if args.command == "chat":
        return command_chat(args.env_file)
    if args.command == "feishu":
        return command_feishu(args.env_file)
    if args.command == "serve":
        return command_serve(args.env_file)
    if args.command in {"daily-paper", "citationclaw"}:
        return command_module(args.command, args.env_file)
    return 2


if __name__ == "__main__":
    sys.exit(main())
