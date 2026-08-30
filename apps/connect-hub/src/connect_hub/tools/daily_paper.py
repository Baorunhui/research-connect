from __future__ import annotations

import base64
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
    inbound_dir: Path | None = None,
) -> tuple[ToolDefinition, ...]:
    output_dir = output_dir.resolve()
    safe_inbound_dir = inbound_dir.resolve() if inbound_dir is not None else None

    def prepare_original_site(context: ToolContext) -> str:
        stable_url = public_url
        if report_hub is not None and report_hub.configured and site_id:
            try:
                stable_url, ready = report_hub.ensure_site(
                    site_id=site_id,
                    module_name="daily-paper",
                    title="Daily Paper Reader",
                )
                if not ready:
                    stable_url = report_hub.upload_site(
                        site_id, adapter.project_dir or ""
                    )
                remote_config = report_hub.get_site_config(site_id)
            except ReportHubError as exc:
                raise ConnectJobError(
                    JobErrorCode.PROVIDER_UNAVAILABLE,
                    "暂时无法读取论文服务配置，请稍后重试。",
                    stage="configuration",
                    retryable=True,
                    technical_message=str(exc),
                ) from exc
            if not bool(remote_config.get("configured")):
                raise ConnectJobError(
                    JobErrorCode.CONFIG_REQUIRED,
                    "论文服务尚未配置。请先打开原版网页完成设置，再重新发起任务：\n"
                    + stable_url,
                    stage="configuration",
                )
            config_payload = remote_config.get("config")
            if isinstance(config_payload, Mapping):
                adapter.apply_configuration(config_payload)
        if stable_url:
            context.record_artifact(kind="url", url=stable_url, name="Daily Paper Reader")
        return stable_url

    def refresh_original_site(context: ToolContext, stable_url: str) -> str:
        if report_hub is None or not report_hub.configured or not site_id:
            return stable_url
        try:
            return report_hub.upload_site(site_id, adapter.project_dir or "")
        except ReportHubError as exc:
            context.report_progress(
                "结果已经在本机生成，但公网整站更新失败。",
                stage="publish",
                payload={"error_code": "REPORT_PUBLISH_FAILED", "detail": str(exc)[:500]},
            )
            return stable_url

    def native_event(context: ToolContext, event: Mapping[str, Any]) -> None:
        if str(event.get("event_type") or "") != "job.progress":
            return
        message = _text(event.get("message"))
        if message:
            context.report_progress(
                message,
                stage=_text(event.get("stage")),
                current=(event.get("current") if isinstance(event.get("current"), int) else None),
                total=(event.get("total") if isinstance(event.get("total"), int) else None),
                payload=(event.get("payload") if isinstance(event.get("payload"), Mapping) else None),
            )

    def pdf_payload(path_value: Any) -> tuple[str, str]:
        if safe_inbound_dir is None:
            raise ValueError("PDF upload directory is not configured")
        path = Path(_text(path_value)).expanduser().resolve()
        if not path.is_relative_to(safe_inbound_dir):
            raise ValueError("PDF path is outside the Feishu upload directory")
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise ValueError("uploaded attachment must be a PDF file")
        if path.stat().st_size > 30 * 1024 * 1024:
            raise ValueError("PDF file exceeds the 30 MiB limit")
        return path.name, base64.b64encode(path.read_bytes()).decode("ascii")

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
                if publish_web and not public_url:
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
                # The original module events remain in snapshots for the web UI.
                # Feishu uses the stricter, data-rich log contract emitted by
                # DailyPaperAdapter to avoid duplicate generic start/end lines.
                on_event=None,
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

    daily_tool = ToolDefinition(
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
                                "minItems": 2,
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
                        "required": ["tag", "keywords", "intent_queries"],
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
            "论文日报任务已启动。将执行 BM25召回、语义向量召回、RRF、专用 reranker 和 LLM 精筛；"
            "每进入一个步骤都会在这里汇报。"
        ),
        start_url=public_url,
        module_name="daily-paper",
        module_version=adapter.manifest.module_version,
        job_type="daily_report",
        kind="business",
    )

    def summarize(arguments: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
        publish_web = bool(arguments.get("publish_web", True))
        stable_url = prepare_original_site(context)
        source = _text(arguments.get("source"))
        payload: dict[str, Any] = {"source": source}
        if source == "url":
            url = _text(arguments.get("url"))
            if not url:
                raise ValueError("paper URL is required when source=url")
            payload["url"] = url
        elif source == "pdf":
            filename, encoded = pdf_payload(arguments.get("pdf_path"))
            payload.update(filename=filename, data_b64=encoded)
        else:
            raise ValueError("source must be url or pdf")
        started_at = time.monotonic()
        try:
            record = adapter.invoke(
                DailyPaperRequest("paper_summarize_wait", payload),
                on_event=lambda event: native_event(context, event),
                is_cancelled=lambda: context.cancelled,
            )
        except Exception:
            context.record_usage(
                provider="daily-paper", operation="paper_summary",
                duration_ms=int((time.monotonic() - started_at) * 1000), status_code=500,
            )
            raise
        context.record_usage(
            provider="daily-paper", operation="paper_summary",
            duration_ms=int((time.monotonic() - started_at) * 1000), status_code=200,
        )
        result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
        meta = result.get("meta") if isinstance(result.get("meta"), Mapping) else {}
        md_path = _text(meta.get("md_path"))
        if md_path:
            context.record_artifact(kind="file", path=md_path, name=Path(md_path).name)
        if publish_web:
            stable_url = refresh_original_site(context, stable_url)
        paper_id = _text(meta.get("paper_id"))
        page_url = f"{stable_url}#/{paper_id}" if stable_url and paper_id else stable_url
        return {
            "run_id": _text(record.get("id")),
            "status": "completed",
            "title": _text(meta.get("title")),
            "paper_id": paper_id,
            "cached": bool(meta.get("cached")),
            "md_path": md_path,
            "public_url": (page_url if publish_web else ""),
        }

    summary_tool = ToolDefinition(
        name="summarize_paper",
        description=(
            "使用 Daily Paper 原版论文总结流水线总结一篇论文，并生成与日报论文页一致的网页。"
            "用户明确要求总结论文链接时传 source=url；对话里有飞书上传 PDF 的本地路径时传 "
            "source=pdf 和 pdf_path。不要把普通网页或没有上传过的路径伪装成 PDF。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["url", "pdf"]},
                "url": {"type": "string", "description": "论文网页或 arXiv 链接。"},
                "pdf_path": {"type": "string", "description": "系统消息中给出的飞书 PDF 本地路径。"},
                "publish_web": {"type": "boolean", "description": "默认 true。"},
            },
            "required": ["source"],
            "additionalProperties": False,
        },
        handler=summarize,
        timeout_seconds=adapter.timeout_seconds + 30,
        progress_message="论文总结任务已启动。将按原版流程解析论文、生成速览与精读内容、处理图表并落盘论文页。",
        start_url=public_url,
        module_name="daily-paper",
        module_version=adapter.manifest.module_version,
        job_type="paper_summary",
        kind="business",
    )

    def survey(arguments: Mapping[str, Any], context: ToolContext) -> Mapping[str, Any]:
        publish_web = bool(arguments.get("publish_web", True))
        stable_url = prepare_original_site(context)
        payload = {
            key: value for key, value in arguments.items()
            if key not in {"publish_web", "source_links", "seed"}
        }
        seed = arguments.get("seed")
        if isinstance(seed, Mapping):
            seed_source = _text(seed.get("source"))
            if seed_source == "url":
                payload["seed"] = {"source": "url", "url": _text(seed.get("url"))}
            elif seed_source == "pdf":
                filename, encoded = pdf_payload(seed.get("pdf_path"))
                payload["seed"] = {"source": "pdf", "filename": filename, "data_b64": encoded}
            else:
                raise ValueError("survey seed source must be url or pdf")
        started_at = time.monotonic()
        try:
            record = adapter.invoke(
                DailyPaperRequest("survey_wait", payload),
                on_event=lambda event: native_event(context, event),
                is_cancelled=lambda: context.cancelled,
            )
        except Exception:
            context.record_usage(
                provider="daily-paper", operation="paper_survey",
                duration_ms=int((time.monotonic() - started_at) * 1000), status_code=500,
            )
            raise
        context.record_usage(
            provider="daily-paper", operation="paper_survey",
            duration_ms=int((time.monotonic() - started_at) * 1000), status_code=200,
        )
        result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
        report = result.get("report") if isinstance(result.get("report"), Mapping) else {}
        md_path = _text(report.get("md_path"))
        if md_path:
            context.record_artifact(kind="file", path=md_path, name=Path(md_path).name)
        if publish_web:
            stable_url = refresh_original_site(context, stable_url)
        route = _text(report.get("paper_id") or report.get("route"))
        page_url = f"{stable_url}#/{route}" if stable_url and route else stable_url
        return {
            "run_id": _text(record.get("id")),
            "status": "completed",
            "title": _text(report.get("title")),
            "paper_count": report.get("n_papers"),
            "clusters": report.get("cluster_names") or [],
            "route": route,
            "md_path": md_path,
            "warnings": result.get("warnings") or [],
            "public_url": (page_url if publish_web else ""),
            "enrichment_sources": arguments.get("source_links") or [],
        }

    survey_tool = ToolDefinition(
        name="generate_paper_survey",
        description=(
            "生成一个研究主题的领域综述。若联网可用，先搜索一次以补充准确概念、方法和 benchmark，"
            "再把富化后的主题写入 query 并调用；无需像论文日报 Intent 预检那样等待固定确认。"
            "可选使用 arXiv 链接或飞书上传 PDF 作为种子论文。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 3, "description": "富化后的明确综述主题。"},
                "max_papers": {"type": "integer", "minimum": 5, "maximum": 200, "description": "最终精选候选上限，默认30。"},
                "fetch_days": {"type": "integer", "minimum": 1, "maximum": 1095, "description": "回溯天数，默认365。"},
                "use_rerank": {"type": "boolean", "description": "是否使用 reranker，默认true。"},
                "deep_read": {"type": "boolean", "description": "是否深读核心论文，默认true。"},
                "use_deepxiv": {"type": "boolean", "description": "是否启用 DeepXiv 补充，默认false。"},
                "use_kaggle": {"type": "boolean", "description": "是否使用本地 Kaggle 快照，默认true；未安装时原模块自行降级。"},
                "coarse_top_k": {"type": "integer", "minimum": 500, "maximum": 30000, "description": "粗筛候选量，默认10000。"},
                "seed": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "enum": ["url", "pdf"]},
                        "url": {"type": "string"},
                        "pdf_path": {"type": "string"},
                    },
                    "required": ["source"],
                    "additionalProperties": False,
                },
                "publish_web": {"type": "boolean", "description": "默认true。"},
                "source_links": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=survey,
        timeout_seconds=max(adapter.timeout_seconds, 2700) + 30,
        progress_message="论文综述任务已启动。将执行种子分析、召回、精选、逐篇抽取、聚类、深读、写作与审校。",
        start_url=public_url,
        module_name="daily-paper",
        module_version=adapter.manifest.module_version,
        job_type="paper_survey",
        kind="business",
    )
    return (daily_tool, summary_tool, survey_tool)
