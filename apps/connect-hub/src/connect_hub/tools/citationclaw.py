from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Mapping

from connect_hub.adapters.citationclaw import CitationClawAdapter
from connect_hub.tools.base import ToolContext, ToolDefinition


def citationclaw_tool(adapter: CitationClawAdapter) -> ToolDefinition:
    def lookup(arguments: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
        papers = [dict(item) for item in arguments.get("papers", []) if isinstance(item, Mapping)]
        prefix = re.sub(r"[^A-Za-z0-9_.-]+", "-", context.job_id[-12:]) or "citation"
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
            "public_url": context.public_url,
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
        module_name="citationclaw",
        module_version=adapter.manifest.module_version,
        job_type="citation_lookup",
        kind="business",
    )
