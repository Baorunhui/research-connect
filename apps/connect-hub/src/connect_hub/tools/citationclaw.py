from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Mapping

from connect_hub.adapters.citationclaw import CitationClawAdapter
from connect_hub.contracts import ConnectJobError, JobErrorCode
from connect_hub.reporting import ReportHubClient, ReportHubError
from connect_hub.tools.base import ToolContext, ToolDefinition


def citationclaw_tool(
    adapter: CitationClawAdapter,
    *,
    report_hub: ReportHubClient | None = None,
    site_id: str = "",
    project_dir: Path | None = None,
    public_url: str = "",
) -> ToolDefinition:
    def lookup(arguments: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
        papers = [dict(item) for item in arguments.get("papers", []) if isinstance(item, Mapping)]
        prefix = re.sub(r"[^A-Za-z0-9_.-]+", "-", context.job_id[-12:]) or "citation"
        stable_public_url = public_url
        if report_hub is not None and report_hub.configured and site_id and project_dir:
            try:
                stable_public_url, ready = report_hub.ensure_site(
                    site_id=site_id,
                    module_name="citationclaw",
                    title="CitationClaw",
                )
                if not ready:
                    stable_public_url = report_hub.upload_site(
                        site_id, project_dir, site_kind="citationclaw"
                    )
                remote_config = report_hub.get_site_config(site_id)
            except ReportHubError as exc:
                raise ConnectJobError(
                    JobErrorCode.PROVIDER_UNAVAILABLE,
                    "暂时无法读取查引用配置，请稍后重试。",
                    stage="configuration",
                    retryable=True,
                    technical_message=str(exc),
                ) from exc
            if not bool(remote_config.get("configured")):
                raise ConnectJobError(
                    JobErrorCode.CONFIG_REQUIRED,
                    "查引用尚未配置。请先打开原版网页的设置页完成配置，再重新发起任务：\n"
                    + stable_public_url
                    + "?panel=config",
                    stage="configuration",
                )
            config_payload = remote_config.get("config")
            if isinstance(config_payload, Mapping):
                adapter.apply_configuration(config_payload)
            context.report_progress(
                f"CitationClaw 原版网页已就绪：\n{stable_public_url}", stage="publish"
            )
        started = time.monotonic()
        try:
            result = adapter.run(
                external_job_id=context.job_id,
                papers=papers,
                output_prefix=prefix,
                on_progress=context.report_progress,
                is_cancelled=lambda: context.cancelled,
            )
        except Exception:
            context.record_usage(
                provider="citationclaw",
                operation="citation_lookup",
                duration_ms=int((time.monotonic() - started) * 1000),
                status_code=500,
            )
            raise
        context.record_usage(
            provider="citationclaw",
            operation="citation_lookup",
            duration_ms=int((time.monotonic() - started) * 1000),
            status_code=200,
            metadata=(result.get("cost_summary") if isinstance(result.get("cost_summary"), Mapping) else None),
        )
        dashboard = str(result.get("dashboard") or "").strip()
        return {
            "status": "completed",
            "papers": papers,
            "dashboard": dashboard,
            "report_path": dashboard,
            "public_url": stable_public_url or context.public_url,
            "artifacts": {
                key: str(result.get(key) or "")
                for key in ("dashboard", "excel", "json")
                if str(result.get(key) or "").strip()
            },
            "cost_summary": result.get("cost_summary") or {},
        }

    return ToolDefinition(
        name="lookup_citations",
        description=(
            "查询一篇或多篇论文的引用者、知名学者引用和引用语境，并生成 CitationClaw 网页报告。"
            "仅在用户明确要求查引用、引用画像或 citation analysis 时调用。默认创建公网任务页。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "papers": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "minLength": 1},
                            "aliases": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["title"],
                        "additionalProperties": False,
                    },
                },
                "publish_web": {"type": "boolean"},
            },
            "required": ["papers"],
            "additionalProperties": False,
        },
        handler=lookup,
        timeout_seconds=adapter.timeout_seconds + 30,
        progress_message="查引用任务已启动，正在检索引用论文并生成引用画像。",
        start_url=public_url,
        module_name="citationclaw",
        module_version=adapter.manifest.module_version,
        job_type="citation_lookup",
        kind="business",
    )
