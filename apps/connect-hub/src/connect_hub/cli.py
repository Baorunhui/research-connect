from __future__ import annotations

import argparse
import logging
import sys

from connect_hub.config import load_settings
from connect_hub.connectors.feishu import FeishuConnector
from connect_hub.adapters import DailyPaperAdapter
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


def _build_runtime(*, interrupt_stale_jobs: bool = False) -> tuple[object, object, object, object]:
    settings = load_settings()
    gateway = LLMGateway(settings.providers)
    store = ConversationStore(settings.db_path)
    tools = ToolRegistry(
        store,
        jobs=JobCoordinator(store, interrupt_stale=interrupt_stale_jobs),
    )
    tools.register(system_status_tool(lambda: tools.names))
    xhs_dir = settings.xhs_agent_dir
    if xhs_dir.exists() and settings.providers:
        tools.register(
            xhs_generate_tool(
                agent_dir=xhs_dir,
                provider=settings.providers[0],
                output_dir=settings.db_path.parent / "xhs_outputs",
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
    )
    if daily_paper.configured:
        for definition in daily_paper_tools(
            daily_paper,
            output_dir=settings.db_path.parent / "daily_paper_reports",
            public_url=settings.daily_paper_public_url,
        ):
            tools.register(definition)
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


def command_check() -> int:
    settings = load_settings()
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


def command_chat() -> int:
    settings, _gateway, _store, service = _build_runtime()
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


def command_feishu() -> int:
    settings, _gateway, _store, service = _build_runtime(interrupt_stale_jobs=True)
    _configure_logging(settings.log_level)
    connector = FeishuConnector(settings, service)
    connector.start()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Feishu connector and LLM gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="validate configuration without revealing secrets")
    subparsers.add_parser("chat", help="test the LLM gateway in a local terminal")
    subparsers.add_parser("feishu", help="start the Feishu WebSocket connector")
    args = parser.parse_args(argv)
    if args.command == "check":
        return command_check()
    if args.command == "chat":
        return command_chat()
    if args.command == "feishu":
        return command_feishu()
    return 2


if __name__ == "__main__":
    sys.exit(main())
