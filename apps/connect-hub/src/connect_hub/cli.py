from __future__ import annotations

import argparse
import logging
import os
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
from connect_hub.tools.citationclaw import citationclaw_tool
from connect_hub.tools.web import web_tools
from connect_hub.tools.xhs import xhs_generate_tool
from connect_hub.websearch import ExaMCPWebSearchProvider, JinaReaderProvider
from connect_hub.reporting import ReportHubClient
from research_connect_core import DataPaths


def _build_runtime(
    *, interrupt_stale_jobs: bool = False, env_file: str | Path | None = None
) -> tuple[object, object, object, object]:
    settings = load_settings(env_file)
    gateway = LLMGateway(settings.providers)
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
    if xhs_dir.exists() and settings.providers:
        tools.register(
            xhs_generate_tool(
                agent_dir=xhs_dir,
                provider=settings.providers[0],
                output_dir=DataPaths.for_module("xhs-agent").artifacts,
                timeout_seconds=settings.xhs_agent_timeout_seconds,
                offline=settings.xhs_agent_offline,
            )
        )
    daily_env: dict[str, str] = {
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
    if daily_paper.configured:
        for definition in daily_paper_tools(
            daily_paper,
            output_dir=DataPaths.for_module("daily-paper-reader").artifacts,
            public_url=settings.daily_paper_public_url,
        ):
            tools.register(definition)
    citationclaw = CitationClawAdapter(
        settings.citationclaw_endpoint,
        timeout_seconds=settings.citationclaw_timeout_seconds,
        poll_seconds=settings.citationclaw_poll_seconds,
    )
    if citationclaw.configured:
        tools.register(citationclaw_tool(citationclaw))
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
    for definition in web_tools(
        store,
        search_provider=search_provider,
        url_provider=url_provider,
        max_results=settings.web_search_max_results,
        cache_hours=settings.web_search_cache_hours,
    ):
        tools.register(definition)
    service = ChatService(
        gateway,
        store,
        history_messages=settings.history_messages,
        tools=tools,
    )
    return settings, gateway, store, service


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def command_check(env_file: str | Path | None = None) -> int:
    settings = load_settings(env_file)
    _configure_logging(settings.log_level)
    print(f"Feishu credentials: {'configured' if settings.feishu_configured else 'missing'}")
    print(f"Feishu domain: {settings.feishu_domain}")
    print(f"LLM providers: {', '.join(p.name + ':' + p.model for p in settings.providers) or 'missing'}")
    print(f"SQLite path: {settings.db_path}")
    if settings.feishu_allowed_open_ids:
        print(f"Feishu allowlist: {len(settings.feishu_allowed_open_ids)} user(s)")
    else:
        print("Feishu allowlist: empty (application availability scope applies)")
    return 0 if settings.feishu_configured and settings.llm_configured else 2


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
    connector = FeishuConnector(settings, service)
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
        return command_feishu(env_file)
    finally:
        for service in reversed(services):
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
        options: dict[str, object] = {
            "cwd": str(cwd),
            "env": dict(os.environ),
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
    subparsers.add_parser("chat", help="test the LLM gateway in a local terminal")
    subparsers.add_parser("feishu", help="start the Feishu WebSocket connector")
    subparsers.add_parser("serve", help="start local module APIs and the Feishu connector")
    args = parser.parse_args(argv)
    if args.command == "check":
        return command_check(args.env_file)
    if args.command == "chat":
        return command_chat(args.env_file)
    if args.command == "feishu":
        return command_feishu(args.env_file)
    if args.command == "serve":
        return command_serve(args.env_file)
    return 2


if __name__ == "__main__":
    sys.exit(main())
