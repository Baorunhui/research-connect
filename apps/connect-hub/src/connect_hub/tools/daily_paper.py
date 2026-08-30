from __future__ import annotations

import re
import time
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from connect_hub.adapters.daily_paper import DailyPaperAdapter, DailyPaperRequest
from connect_hub.contracts import ConnectJobError, JobErrorCode
from connect_hub.reporting import ReportHubClient, ReportHubError
from connect_hub.tools.base import ToolContext, ToolDefinition


def _text(value: Any) -> str:
    return str(value or "").strip()


def _papers(result: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = result.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _paper_markdown(paper: Mapping[str, Any], index: int) -> list[str]:
    title = _text(paper.get("title")) or _text(paper.get("id")) or "未命名论文"
    link = _text(paper.get("link"))
    score = paper.get("llm_score")
    score_text = f" · 评分 {score}" if score not in (None, "") else ""
    heading = f"{index}. [{title}]({link}){score_text}" if link else f"{index}. {title}{score_text}"
    tldr = _text(paper.get("llm_tldr_cn") or paper.get("llm_tldr_en"))
    abstract = _text(paper.get("abstract"))
    evidence = _text(paper.get("llm_evidence_cn") or paper.get("llm_evidence_en"))
    lines = [heading]
    if tldr:
        lines.append(f"   - TLDR：{tldr}")
    elif abstract:
        preview = abstract if len(abstract) <= 600 else abstract[:600].rstrip() + "…"
        lines.append(f"   - 原文摘要：{preview}")
    if evidence and evidence != tldr:
        lines.append(f"   - 推荐理由：{evidence}")
    return lines


def render_daily_paper_markdown(
    *,
    run_id: str,
    result: Mapping[str, Any],
    topics: Sequence[Mapping[str, Any]],
    public_url: str = "",
) -> str:
    generated_at = _text(result.get("generated_at"))
    mode = _text(result.get("mode")) or "standard"
    topic_names = [
        _text(item.get("tag")) for item in topics if _text(item.get("tag"))
    ]
    deep = _papers(result, "deep_dive")
    quick = _papers(result, "quick_skim")
    lines = [
        "# 论文日报",
        "",
        f"- 任务：`{run_id}`",
        f"- 主题：{', '.join(topic_names) or '未命名主题'}",
        f"- 模式：{mode}",
        f"- 生成时间：{generated_at or '未知'}",
        f"- 结果：精读 {len(deep)} 篇，速读 {len(quick)} 篇",
    ]
    if public_url:
        lines.append(f"- 网页站点：{public_url}")
    lines.extend(["", "## 精读", ""])
    if deep:
        for index, paper in enumerate(deep, 1):
            lines.extend(_paper_markdown(paper, index))
    else:
        lines.append("本次没有精读推荐。")
    lines.extend(["", "## 速读", ""])
    if quick:
        for index, paper in enumerate(quick, 1):
            lines.extend(_paper_markdown(paper, index))
    else:
        lines.append("本次没有速读推荐。")
    lines.extend(
        [
            "",
            "---",
            "",
            "此文件由 connect-hub 根据 Daily Paper 的召回、融合、专用 reranker 与 LLM 精筛结果生成。完整精读网页需由 Daily Paper Step 6 生成并单独托管。",
            "",
        ]
    )
    return "\n".join(lines)


def daily_paper_tools(
    adapter: DailyPaperAdapter,
    *,
    output_dir: Path,
    public_url: str = "",
    report_hub: ReportHubClient | None = None,
    site_id: str = "",
) -> tuple[ToolDefinition, ...]:
    output_dir = output_dir.resolve()

    def generate(arguments: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
        request_arguments = dict(arguments)
        publish_web = bool(request_arguments.pop("publish_web", True))
        source_links = [
            _text(item)
            for item in (request_arguments.pop("source_links", []) or [])
            if _text(item)
        ]
        request_arguments["schema_version"] = "connect.job.v1"
        request_arguments["job_id"] = context.job_id
        stable_public_url = public_url
        publish_error_reported = False
        last_snapshot = ""
        if report_hub is not None and report_hub.configured and site_id:
            try:
                stable_public_url, ready = report_hub.ensure_site(
                    site_id=site_id,
                    module_name="daily-paper",
                    title="Daily Paper Reader",
                )
                if not ready:
                    stable_public_url = report_hub.upload_site(site_id, adapter.project_dir or "")
                if publish_web:
                    context.report_progress(
                        f"论文日报站点已就绪，可实时查看进度和历史结果：\n{stable_public_url}",
                        stage="publish",
                    )
            except ReportHubError as exc:
                publish_error_reported = True
                context.report_progress(
                    "日报公网整站发布失败（REPORT_HUB_UNAVAILABLE），本地任务仍会继续。",
                    stage="publish",
                    payload={"error_code": "REPORT_HUB_UNAVAILABLE", "detail": str(exc)[:500]},
                )

        # A saved record is the only preflight condition. Do not probe any LLM,
        # embedding or rerank provider here.
        if report_hub is not None and report_hub.configured and site_id:
            try:
                remote_config = report_hub.get_site_config(site_id)
            except ReportHubError as exc:
                raise ConnectJobError(
                    JobErrorCode.PROVIDER_UNAVAILABLE,
                    "暂时无法读取论文日报配置，请稍后重试。",
                    stage="configuration",
                    retryable=True,
                    technical_message=str(exc),
                ) from exc
            if not bool(remote_config.get("configured")):
                raise ConnectJobError(
                    JobErrorCode.CONFIG_REQUIRED,
                    "论文日报尚未配置。请先打开原版网页完成设置，再重新发起任务：\n"
                    + stable_public_url,
                    stage="configuration",
                )
            config_payload = remote_config.get("config")
            if isinstance(config_payload, Mapping):
                adapter.apply_configuration(config_payload)

        def mirror_snapshot(run: Mapping[str, Any], log_text: str) -> None:
            nonlocal last_snapshot, publish_error_reported
            if not (publish_web and report_hub is not None and stable_public_url and site_id):
                return
            signature = json.dumps(
                {
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "updated_at": run.get("updated_at"),
                    "events": run.get("events"),
                    "log_tail": log_text[-1000:],
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if signature == last_snapshot:
                return
            last_snapshot = signature
            run_id = _text(run.get("id"))
            if not run_id:
                return
            try:
                report_hub.update_site_run(site_id, run_id, run, log_text)
            except ReportHubError as exc:
                if not publish_error_reported:
                    publish_error_reported = True
                    context.report_progress(
                        "公网日报的实时进度同步暂时失败，本地任务不受影响。",
                        stage="publish",
                        payload={"error_code": "REPORT_HUB_UNAVAILABLE", "detail": str(exc)[:500]},
                    )
        started_at = time.monotonic()
        try:
            record = adapter.invoke(
                DailyPaperRequest("recommend_wait", request_arguments),
                on_progress=context.report_progress,
                on_event=lambda event: context.report_progress(
                    _text(event.get("message")) or "论文日报任务有新进度。",
                    stage=_text(event.get("stage")),
                    current=(event.get("current") if isinstance(event.get("current"), int) else None),
                    total=(event.get("total") if isinstance(event.get("total"), int) else None),
                    payload=(event.get("payload") if isinstance(event.get("payload"), Mapping) else None),
                ),
                on_snapshot=mirror_snapshot,
                is_cancelled=lambda: context.cancelled,
            )
        except Exception:
            context.record_usage(
                provider="daily-paper",
                operation="workflow",
                duration_ms=int((time.monotonic() - started_at) * 1000),
                status_code=500,
            )
            raise
        context.record_usage(
            provider="daily-paper",
            operation="workflow",
            duration_ms=int((time.monotonic() - started_at) * 1000),
            status_code=200,
        )
        result = record.get("result")
        if not isinstance(result, Mapping):
            raise RuntimeError("Daily Paper completed without structured result")
        run_id = _text(record.get("id")) or "daily-paper"
        if publish_web and report_hub is not None and stable_public_url and site_id:
            try:
                stable_public_url = report_hub.upload_site(site_id, adapter.project_dir or "")
            except ReportHubError as exc:
                context.report_progress(
                    "日报已生成，但公网整站更新失败；本机结果已保留。",
                    stage="publish",
                    payload={"error_code": "REPORT_PUBLISH_FAILED", "detail": str(exc)[:500]},
                )
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", run_id).strip(".-")
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / f"{safe_id or 'daily-paper'}.md"
        topics = arguments.get("topics")
        safe_topics = (
            [item for item in topics if isinstance(item, Mapping)]
            if isinstance(topics, list)
            else []
        )
        report_path.write_text(
            render_daily_paper_markdown(
                run_id=run_id,
                result=result,
                topics=safe_topics,
                public_url=(stable_public_url if publish_web else ""),
            ),
            encoding="utf-8",
        )
        return {
            "run_id": run_id,
            "status": "completed",
            "result": result,
            "report_path": str(report_path),
            "public_url": (stable_public_url if publish_web else ""),
            "report_bundle_path": _text(record.get("report_bundle_path")),
            "enrichment_sources": source_links,
        }

    tool = ToolDefinition(
        name="generate_daily_paper_report",
        description=(
            "按用户指定的研究主题运行论文推荐链：BM25、Embedding、RRF、专用 reranker、"
            "LLM 精筛和最终选择，完成后返回排序结果和 Markdown 日报。这是耗时业务工具，"
            "仅在用户明确要求生成论文日报或调研近期论文、且研究主题已能形成有效检索词时调用；"
            "技术缩写含义不清时先搜索或自然询问。用户未指定时使用最近30天、standard、"
            "发布固定的 Daily Paper 公网站点；不要为了补齐非关键偏好而强制确认。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "topics": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "tag": {"type": "string", "minLength": 1},
                            "description": {"type": "string"},
                            "keywords": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "minLength": 1},
                            },
                            "intent_queries": {
                                "type": "array",
                                "maxItems": 4,
                                "description": (
                                    "用户确认或修改后的联网 Intent 候选。必须是完整英文语义查询，"
                                    "覆盖不同任务、方法或 benchmark 角度，不能只是关键词拼接。"
                                ),
                                "items": {"type": "string", "minLength": 12},
                            },
                            "paper_sources": {
                                "type": "array",
                                "description": (
                                    "当前 HTTP 论文池只支持 arxiv。CVPR、ICCV、ECCV 等会议名"
                                    "应写入 keywords 或 intent_queries，不要作为数据源。"
                                ),
                                "items": {"type": "string", "enum": ["arxiv"]},
                            },
                        },
                        "required": ["tag", "keywords"],
                        "additionalProperties": False,
                    },
                },
                "date": {
                    "type": "string",
                    "description": (
                        "可选的目标日期，只能使用 YYYY-MM-DD 或 YYYYMMDD。"
                        "用户只说最近N天时不要填写，由服务使用当前日期。"
                    ),
                },
                "mode": {"type": "string", "enum": ["standard", "skims"]},
                "fetch_days": {"type": "integer", "minimum": 1, "maximum": 30},
                "publish_web": {
                    "type": "boolean",
                    "description": "是否在结果中附带已配置的公网日报地址。",
                },
                "source_links": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Connect Hub 联网富化时保留的来源 URL。",
                },
            },
            "required": ["topics"],
            "additionalProperties": False,
        },
        handler=generate,
        timeout_seconds=adapter.timeout_seconds + 30,
        progress_message=(
            "论文日报任务已启动。将执行 BM25、Embedding、RRF、专用 reranker 和 LLM 精筛；"
            "每进入一个步骤都会在这里汇报。"
        ),
        module_name="daily-paper",
        module_version=adapter.manifest.module_version,
        job_type="daily_report",
        kind="business",
    )
    return (tool,)
