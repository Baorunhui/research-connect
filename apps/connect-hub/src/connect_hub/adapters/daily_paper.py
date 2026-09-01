from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from connect_hub.contracts import (
    ConnectJobError,
    JobErrorCode,
    ModuleManifest,
    SCHEMA_VERSION,
)


class DailyPaperUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class DailyPaperRequest:
    action: str
    arguments: Mapping[str, Any]


class DailyPaperAdapter:
    """Stable boundary for the separately maintained Daily Paper project.

    A later adapter can implement this contract over MCP, HTTP, or a subprocess
    without coupling Feishu or the LLM gateway to Daily Paper internals.
    """

    manifest = ModuleManifest(
        module_name="daily-paper",
        module_version="0.1.0",
        supported_job_types=(
            "daily_report",
            "paper_research",
            "paper_summary",
            "paper_survey",
        ),
    )

    def __init__(
        self,
        transport: str = "disabled",
        endpoint: str = "",
        *,
        timeout_seconds: int = 1800,
        poll_seconds: int = 3,
        extra_env: Mapping[str, str] | None = None,
        skip_llm_refine: bool = False,
        project_dir: str | Path | None = None,
    ) -> None:
        self.transport = transport
        self.endpoint = endpoint
        self.timeout_seconds = max(60, timeout_seconds)
        self.poll_seconds = max(1, poll_seconds)
        self.extra_env = dict(extra_env or {})
        self.skip_llm_refine = skip_llm_refine
        self.project_dir = Path(project_dir).resolve() if project_dir else None

    @property
    def configured(self) -> bool:
        return self.transport != "disabled" and bool(self.endpoint)

    def apply_configuration(self, config: Mapping[str, Any]) -> None:
        """Persist configuration collected by the public original UI locally."""
        self._request("POST", "/api/local/config/partial", config)

    def apply_runtime_environment(self, values: Mapping[str, str]) -> None:
        """Synchronize allowlisted provider values into the local service process."""
        self._request("POST", "/api/local/runtime-env", {"secret": dict(values)})
        local = config.get("local") if isinstance(config.get("local"), Mapping) else {}
        chat = local.get("chat") if isinstance(local.get("chat"), Mapping) else {}
        api_key = str(chat.get("api_key") or "").strip()
        base_url = str(chat.get("base_url") or "").strip()
        model = str(chat.get("model") or "").strip()
        if api_key:
            self.extra_env.update(SUMMARY_API_KEY=api_key, DEEPSEEK_API_KEY=api_key)
        if base_url:
            self.extra_env.update(
                SUMMARY_BASE_URL=base_url,
                DEEPSEEK_BASE_URL=base_url,
                LLM_PRIMARY_BASE_URL=base_url,
            )
        if model:
            self.extra_env.update(SUMMARY_MODEL=model, DEEPSEEK_MODEL=model)
        rerank = local.get("rerank") if isinstance(local.get("rerank"), Mapping) else {}
        profile = str(rerank.get("profile") or "").strip()
        if profile:
            self.extra_env["RERANK_PROFILE"] = profile

    def invoke(
        self,
        request: DailyPaperRequest,
        *,
        on_progress: Callable[[str], None] | None = None,
        on_event: Callable[[Mapping[str, Any]], None] | None = None,
        on_snapshot: Callable[[Mapping[str, Any], str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]:
        if not self.configured:
            raise DailyPaperUnavailable("Daily Paper adapter is disabled")
        if self.transport not in {"http", "local_http"}:
            raise DailyPaperUnavailable(
                f"unsupported Daily Paper transport: {self.transport}"
            )
        if request.action == "recommend":
            return self._request("POST", "/api/recommend", request.arguments)
        if request.action == "status":
            run_id = str(request.arguments.get("run_id") or "").strip()
            if not run_id or not run_id.replace("-", "").isalnum():
                raise ValueError("invalid Daily Paper run_id")
            return self._request("GET", f"/api/recommend/{run_id}", None)
        if request.action == "recommend_wait":
            if self.transport == "local_http":
                return self._local_workflow_and_wait(
                    request.arguments,
                    on_progress=on_progress,
                    on_event=on_event,
                    on_snapshot=on_snapshot,
                    is_cancelled=is_cancelled,
                )
            return self._recommend_and_wait(
                request.arguments,
                on_progress=on_progress,
                on_event=on_event,
                is_cancelled=is_cancelled,
            )
        if request.action == "paper_summarize_wait":
            return self._async_job_and_wait(
                "/api/paper/summarize",
                request.arguments,
                on_event=on_event,
                is_cancelled=is_cancelled,
                timeout_seconds=self.timeout_seconds,
            )
        if request.action == "survey_wait":
            return self._async_job_and_wait(
                "/api/survey",
                request.arguments,
                on_event=on_event,
                is_cancelled=is_cancelled,
                timeout_seconds=max(self.timeout_seconds, 2700),
            )
        raise ValueError(f"unsupported Daily Paper action: {request.action}")

    def _async_job_and_wait(
        self,
        endpoint: str,
        arguments: Mapping[str, Any],
        *,
        on_event: Callable[[Mapping[str, Any]], None] | None,
        is_cancelled: Callable[[], bool] | None,
        timeout_seconds: int,
    ) -> Mapping[str, Any]:
        """Run one of Daily Paper's native connect.job.v1 HTTP jobs."""

        payload = dict(arguments)
        if endpoint == "/api/survey":
            embedding = {
                "endpoint": str(self.extra_env.get("DPR_EMBED_API_URL") or "").strip(),
                "api_key": str(self.extra_env.get("DPR_EMBED_API_KEY") or "").strip(),
            }
            if embedding["endpoint"] or embedding["api_key"]:
                # The Daily Paper service removes this envelope before creating
                # the public job record.  Credentials must never enter job input,
                # events, logs, or artifacts.
                payload["_runtime_credentials"] = {"embedding": embedding}
        started = self._request("POST", endpoint, payload)
        self._validate_schema(started)
        job_id = str(started.get("job_id") or "").strip()
        if not job_id or not job_id.replace("-", "").isalnum():
            raise DailyPaperUnavailable(
                f"Daily Paper {endpoint} response contains no valid job_id"
            )
        deadline = time.monotonic() + timeout_seconds
        seen_events: set[str] = set()
        while True:
            if is_cancelled is not None and is_cancelled():
                acknowledged = self._cancel_async_job(endpoint, job_id)
                if not acknowledged:
                    raise ConnectJobError(
                        JobErrorCode.CANCEL_FAILED,
                        "已停止等待，但论文服务没有确认后台任务已取消。",
                        stage="cancelling",
                        technical_message=f"cancel not acknowledged for {endpoint}/{job_id}",
                    )
                raise ConnectJobError(JobErrorCode.JOB_CANCELLED, "任务已取消。")

            response = self._request("GET", f"{endpoint}/{job_id}", None)
            self._validate_schema(response)
            job = response.get("job") if isinstance(response.get("job"), Mapping) else {}
            self._report_structured_events(job, seen_events, on_event)
            status = str(job.get("status") or "queued").strip().lower()
            if status == "completed":
                result = job.get("result")
                if not isinstance(result, Mapping):
                    raise DailyPaperUnavailable(
                        f"Daily Paper job {job_id} completed without a structured result"
                    )
                return {
                    "schema_version": SCHEMA_VERSION,
                    "id": job_id,
                    "status": status,
                    "result": result,
                    "events": job.get("events") or [],
                }
            if status == "failed":
                raise DailyPaperUnavailable(
                    f"Daily Paper job {job_id} failed: {job.get('error') or 'unknown error'}"
                )
            if status == "cancelled":
                raise ConnectJobError(JobErrorCode.JOB_CANCELLED, "任务已取消。")
            if time.monotonic() >= deadline:
                acknowledged = self._cancel_async_job(endpoint, job_id)
                raise ConnectJobError(
                    JobErrorCode.JOB_TIMEOUT,
                    "任务超过等待时限，已请求取消。",
                    retryable=True,
                    technical_message=(
                        f"Daily Paper job {job_id} timed out; "
                        f"cancel_acknowledged={acknowledged}"
                    ),
                )
            time.sleep(self.poll_seconds)

    def _cancel_async_job(self, endpoint: str, job_id: str) -> bool:
        try:
            result = self._request("POST", f"{endpoint}/{job_id}/cancel", {})
        except DailyPaperUnavailable:
            return False
        return bool(result.get("ok"))

    def _local_workflow_and_wait(
        self,
        arguments: Mapping[str, Any],
        *,
        on_progress: Callable[[str], None] | None,
        on_event: Callable[[Mapping[str, Any]], None] | None,
        on_snapshot: Callable[[Mapping[str, Any], str], None] | None,
        is_cancelled: Callable[[], bool] | None,
    ) -> Mapping[str, Any]:
        config_response = self._request("GET", "/api/local/config", None)
        config_text = str(config_response.get("content") or "")
        config = yaml.safe_load(config_text) if config_text.strip() else {}
        if not isinstance(config, dict):
            config = {}
        config["subscriptions"] = _build_subscriptions(arguments.get("topics"))
        paper_settings = config.setdefault("arxiv_paper_setting", {})
        if isinstance(paper_settings, dict):
            paper_settings["mode"] = str(arguments.get("mode") or "standard")
            paper_settings["days_window"] = int(arguments.get("fetch_days") or 30)
            paper_settings["prefer_supabase_read"] = True
        local = config.setdefault("local", {})
        if isinstance(local, dict):
            schedule = local.setdefault("schedule", {})
            if isinstance(schedule, dict):
                schedule["enabled"] = False
        started = self._request(
            "POST",
            "/api/local/workflows/dispatch",
            {
                "workflowKey": "daily-now",
                "workflowFile": "daily-paper-reader.yml",
                "inputs": {
                    "fetch_days": str(arguments.get("fetch_days") or 30),
                    "fetch_mode": str(arguments.get("mode") or "standard"),
                    "run_enrich": "true",
                },
                "config": config,
                "secret": dict(self.extra_env),
                "externalJobId": str(arguments.get("job_id") or ""),
                "schemaVersion": SCHEMA_VERSION,
            },
        )
        run = started.get("run") if isinstance(started.get("run"), Mapping) else {}
        run_id = str(run.get("id") or "").strip()
        if not run_id:
            raise DailyPaperUnavailable("Daily Paper workflow response contains no run id")
        deadline = time.monotonic() + self.timeout_seconds
        seen: set[str] = set()
        reported_diagnostics: set[str] = set()
        while True:
            if is_cancelled is not None and is_cancelled():
                if not self._cancel_local(run_id):
                    raise ConnectJobError(
                        JobErrorCode.CANCEL_FAILED,
                        "论文日报取消请求未被模块确认，请检查本地日报服务。",
                        stage="cancelling",
                    )
                raise ConnectJobError(JobErrorCode.JOB_CANCELLED, "论文日报任务已取消。")
            record = self._request("GET", f"/api/local/runs/{run_id}/log", None)
            current = record.get("run") if isinstance(record.get("run"), Mapping) else {}
            log_text = str(record.get("log") or "")
            self._report_log_diagnostics(
                log_text, reported_diagnostics, on_progress
            )
            if on_snapshot is not None:
                on_snapshot(current, log_text)
            events = current.get("events") if isinstance(current.get("events"), list) else []
            for event in events:
                if not isinstance(event, Mapping):
                    continue
                event_id = str(event.get("event_id") or "")
                if event_id and event_id not in seen:
                    seen.add(event_id)
                    if on_event is not None:
                        on_event(event)
            if str(current.get("status") or "queued") == "completed":
                if str(current.get("conclusion") or "") != "success":
                    log = str(record.get("log") or "")[-3000:]
                    raise DailyPaperUnavailable(
                        f"Daily Paper workflow {run_id} failed: {log or current.get('error')}"
                    )
                completion_log = log_text
                local_log_path = Path(str(current.get("log_path") or "")).expanduser()
                if local_log_path.is_file():
                    try:
                        completion_log = local_log_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        pass
                result = self._load_local_recommendation(completion_log, arguments)
                return {
                    "schema_version": SCHEMA_VERSION,
                    "id": run_id,
                    "status": "completed",
                    "result": result,
                    "run": current,
                    "log": log_text,
                    "report_bundle_path": str(self.project_dir / "docs") if self.project_dir else "",
                }
            if str(current.get("status") or "") == "cancelled":
                raise ConnectJobError(JobErrorCode.JOB_CANCELLED, "论文日报任务已取消。")
            if time.monotonic() >= deadline:
                self._cancel_local(run_id)
                raise ConnectJobError(
                    JobErrorCode.JOB_TIMEOUT,
                    "论文日报超过等待时限，已请求终止模块任务。",
                    retryable=True,
                    technical_message=f"Daily Paper workflow {run_id} timed out",
                )
            time.sleep(self.poll_seconds)

    def _load_local_recommendation(
        self, log_text: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Read the recommendation JSON produced by this exact local run."""
        matches = re.findall(
            r"\[INFO\] saved: ([^\r\n]*[/\\]recommend[/\\][^\r\n]+\.json)",
            log_text,
        )
        candidate = Path(matches[-1]).expanduser() if matches else None
        if candidate is not None and not candidate.is_absolute() and self.project_dir:
            candidate = self.project_dir / candidate
        if candidate is not None and candidate.is_file():
            try:
                decoded = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, Mapping):
                result = dict(decoded)
                result.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
                result.setdefault("mode", str(arguments.get("mode") or "standard"))
                result.setdefault("deep_dive", [])
                result.setdefault("quick_skim", [])
                return result
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": str(arguments.get("mode") or "standard"),
            "deep_dive": [],
            "quick_skim": [],
            "result_warning": "recommendation JSON was not found in this run log",
        }

    def _cancel_local(self, run_id: str) -> bool:
        try:
            result = self._request("POST", f"/api/local/runs/{run_id}/cancel", {})
            return bool(result.get("ok"))
        except DailyPaperUnavailable:
            return False

    def _recommend_and_wait(
        self,
        arguments: Mapping[str, Any],
        *,
        on_progress: Callable[[str], None] | None = None,
        on_event: Callable[[Mapping[str, Any]], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Mapping[str, Any]:
        payload = dict(arguments)
        payload.setdefault("schema_version", SCHEMA_VERSION)
        payload["skip_llm_refine"] = self.skip_llm_refine
        topics = payload.get("topics")
        if isinstance(topics, list):
            normalized_topics: list[Any] = []
            for topic in topics:
                if not isinstance(topic, Mapping):
                    normalized_topics.append(topic)
                    continue
                normalized = dict(topic)
                # paper_engine's isolated HTTP integration currently exposes
                # only the configured arXiv backend. Conference names belong
                # in keywords/intent queries, not in paper_sources.
                normalized["paper_sources"] = ["arxiv"]
                normalized_topics.append(normalized)
            payload["topics"] = normalized_topics
        raw_date = str(payload.get("date") or "").strip()
        if raw_date:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
                payload["date"] = raw_date.replace("-", "")
            elif re.fullmatch(r"\d{8}", raw_date):
                payload["date"] = raw_date
            else:
                raise ValueError(
                    "Daily Paper date must be YYYY-MM-DD or YYYYMMDD; "
                    "omit it when the user did not specify a historical date"
                )
        if self.extra_env:
            request_secrets = payload.get("secrets")
            secrets = (
                dict(request_secrets) if isinstance(request_secrets, Mapping) else {}
            )
            secrets.update(self.extra_env)
            payload["secrets"] = secrets
        started = self._request("POST", "/api/recommend", payload)
        self._validate_schema(started)
        run = started.get("run") if isinstance(started.get("run"), Mapping) else {}
        run_id = str(run.get("id") or "").strip()
        if not run_id:
            raise DailyPaperUnavailable("Daily Paper start response contains no run id")

        deadline = time.monotonic() + self.timeout_seconds
        reported: set[str] = set()
        reported_event_ids: set[str] = set()
        while True:
            if is_cancelled is not None and is_cancelled():
                if not self.cancel(run_id):
                    raise ConnectJobError(
                        JobErrorCode.CANCEL_FAILED,
                        "已停止等待，但论文日报服务没有确认后台任务已终止；请检查日报服务。",
                        stage="cancelling",
                        technical_message=(
                            f"Daily Paper cancellation endpoint did not confirm "
                            f"process termination for run {run_id}"
                        ),
                    )
                raise ConnectJobError(
                    JobErrorCode.JOB_CANCELLED,
                    "论文日报任务已取消。",
                    technical_message=f"cancelled Daily Paper run {run_id}",
                )
            record = self._request("GET", f"/api/recommend/{run_id}", None)
            self._validate_schema(record)
            self._report_structured_events(
                record,
                reported_event_ids,
                on_event,
            )
            self._report_log_progress(
                str(record.get("log") or ""),
                reported,
                on_progress,
            )
            status = str(record.get("status") or "queued")
            if status == "completed":
                result = record.get("result")
                if not isinstance(result, Mapping):
                    raise DailyPaperUnavailable(
                        f"Daily Paper run {run_id} completed without a result"
                    )
                log = str(record.get("log") or "")
                config_failure_markers = (
                    "[WARN] 读取 config.yaml 失败",
                    "[ERROR] 未能从订阅配置中解析",
                )
                no_papers = not result.get("deep_dive") and not result.get("quick_skim")
                if no_papers and any(marker in log for marker in config_failure_markers):
                    raise DailyPaperUnavailable(
                        f"Daily Paper run {run_id} returned an empty result after "
                        "configuration parsing failed"
                    )
                if on_progress is not None and "completed" not in reported:
                    on_progress("论文日报进度：全部步骤完成，正在整理飞书消息和 Markdown 文件。")
                    reported.add("completed")
                return record
            if status == "failed":
                error = str(record.get("error") or "unknown pipeline error")
                log = str(record.get("log") or "")[-2000:]
                detail = f"{error}\n{log}".strip()
                raise DailyPaperUnavailable(
                    f"Daily Paper run {run_id} failed: {detail}"
                )
            if time.monotonic() >= deadline:
                cancelled = self.cancel(run_id)
                raise DailyPaperUnavailable(
                    f"Daily Paper run {run_id} exceeded {self.timeout_seconds}s client wait timeout; "
                    + (
                        "backend cancellation was acknowledged"
                        if cancelled
                        else "the backend may still be running because cancellation was not acknowledged"
                    )
                )
            time.sleep(self.poll_seconds)

    @staticmethod
    def _validate_schema(record: Mapping[str, Any]) -> None:
        schema = str(record.get("schema_version") or "").strip()
        if schema and schema != SCHEMA_VERSION:
            raise ConnectJobError(
                JobErrorCode.MODULE_INCOMPATIBLE,
                "论文日报接口版本与 Connect Hub 不兼容。",
                technical_message=(
                    f"Daily Paper returned schema_version={schema!r}; "
                    f"expected {SCHEMA_VERSION!r}"
                ),
            )

    @staticmethod
    def _report_structured_events(
        record: Mapping[str, Any],
        reported_event_ids: set[str],
        on_event: Callable[[Mapping[str, Any]], None] | None,
    ) -> None:
        if on_event is None:
            return
        events = record.get("events")
        if not isinstance(events, list):
            return
        for item in events:
            if not isinstance(item, Mapping):
                continue
            event_id = str(item.get("event_id") or "").strip()
            if not event_id or event_id in reported_event_ids:
                continue
            schema = str(item.get("schema_version") or "").strip()
            if schema and schema != SCHEMA_VERSION:
                continue
            reported_event_ids.add(event_id)
            on_event(item)

    def cancel(self, run_id: str) -> bool:
        """Best-effort connect.job.v1 cancellation handshake.

        Daily Paper owns the subprocess and must implement this endpoint to
        guarantee process-tree termination. Older backends return 404; the
        caller still stops waiting and reports that backend adoption is needed.
        """
        try:
            result = self._request("POST", f"/api/recommend/{run_id}/cancel", {})
        except DailyPaperUnavailable:
            return False
        return bool(result.get("ok", True))

    @staticmethod
    def _report_log_progress(
        log: str,
        reported: set[str],
        on_progress: Callable[[str], None] | None,
    ) -> None:
        if on_progress is None or not log:
            return
        markers = (
            (
                "bm25",
                "[START] Step 2.1 - BM25",
                "论文日报进度 1/6：正在进行 BM25 关键词召回。",
            ),
            (
                "embedding",
                "[START] Step 2.2 - Embedding",
                "论文日报进度 2/6：正在进行 Embedding 语义召回。",
            ),
            (
                "rrf",
                "[START] Step 2.3 - RRF",
                "论文日报进度 3/6：正在融合并去重两路召回结果（RRF）。",
            ),
            (
                "rerank",
                "[START] Step 3 - Rerank",
                "论文日报进度 4/6：正在使用专用 reranker 对候选论文重排序。",
            ),
            (
                "llm_refine",
                "[START] Step 4",
                "论文日报进度 5/6：正在进行 LLM 精筛；该步骤耗时最长，并会按批次持续汇报。",
            ),
            (
                "select",
                "[START] Step 5",
                "论文日报进度 6/6：正在汇总 LLM 精筛结果并选择最终论文。",
            ),
        )
        for key, marker, message in markers:
            if key not in reported and marker in log:
                on_progress(message)
                reported.add(key)

    @staticmethod
    def _report_log_diagnostics(
        log: str,
        reported: set[str],
        on_progress: Callable[[str], None] | None,
    ) -> None:
        """Turn real Daily Paper metrics into concise, deduplicated debug messages.

        The separately maintained pipeline already writes the authoritative
        parameters and funnel counts to its run log. Connect Hub only extracts
        known log records; it never estimates or invents missing values.
        """
        if on_progress is None or not log:
            return

        def match(pattern: str) -> re.Match[str] | None:
            return re.search(pattern, log, flags=re.MULTILINE)

        def emit(key: str, message: str) -> None:
            if key not in reported:
                reported.add(key)
                on_progress(message)

        def saved_json(pattern: str) -> Mapping[str, Any] | None:
            paths = re.findall(pattern, log, flags=re.MULTILINE)
            if not paths:
                return None
            path = Path(paths[-1]).expanduser()
            if not path.is_file():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return None
            return value if isinstance(value, Mapping) else None

        def distribution(values: list[float], buckets: list[tuple[str, float, float]]) -> str:
            parts = []
            for label, lower, upper in buckets:
                count = sum(1 for value in values if lower <= value < upper)
                parts.append(f"{label} {count}篇")
            return "，".join(parts)

        run_date = match(r"^\[INFO\] DPR_RUN_DATE=(\S+)")
        run_mode = match(
            r"^\[INFO\] fetch_days=(\d+), run_mode=([^,\s]+), fetch_mode=([^,\s]+)"
        )
        if run_date and run_mode:
            emit(
                "run_parameters",
                "流水线开始执行：日报运行参数："
                f"日期范围 {run_date.group(1)}，回看 {run_mode.group(1)} 天，"
                f"运行模式 {run_mode.group(2)}，抓取模式 {run_mode.group(3)}。",
            )

        enrichment_complete = match(
            r"config\.yaml 所有字段都完整，无需扩充|已更新 config\.yaml 的相关字段"
        )
        if enrichment_complete:
            checks = {
                name: int(value)
                for name, value in re.findall(
                    r"需要扩充 (keywords\.related|keywords\.rewrite|llm_queries\.rewrite): (\d+) 个",
                    log,
                )
            }
            total_enriched = sum(checks.values())
            detail = (
                "本次配置字段完整，未新增关键词。"
                if total_enriched == 0
                else (
                    f"补齐相关词 {checks.get('keywords.related', 0)} 组、"
                    f"关键词改写 {checks.get('keywords.rewrite', 0)} 项、"
                    f"Intent 改写 {checks.get('llm_queries.rewrite', 0)} 项。"
                )
            )
            emit("enrich_complete", f"LLM 扩充检索关键词已完成：{detail}")

        fetch_skipped = match(r"跳过 Step 1（([^）]+)）：([^\r\n]+)")
        if fetch_skipped:
            emit(
                "fetch_skipped",
                f"抓取 arXiv 论文已跳过：理由：{fetch_skipped.group(1)}；{fetch_skipped.group(2)}",
            )

        if "[INFO] Step 2.1 - BM25:" in log:
            emit("bm25_start", "BM25 关键词召回开始：")

        bm25_window = match(r"Supabase BM25 窗口计数.*?：(\d+) 条")
        bm25_top_k = match(r"Supabase BM25 自适应 Top K = (\d+)")
        bm25_hits = match(r"Supabase BM25 命中 (\d+) 条")
        bm25_tagged = match(r"其中带 tag 的论文数：(\d+)")
        if bm25_window and bm25_top_k and bm25_hits and bm25_tagged:
            emit(
                "bm25_funnel",
                "BM25 召回完成："
                f"时间窗内 {bm25_window.group(1)} 篇 → 原始命中 {bm25_hits.group(1)} 条 "
                f"→ 去重且匹配主题 {bm25_tagged.group(1)} 篇；"
                f"策略为 Supabase BM25、自适应 top_k={bm25_top_k.group(1)}。",
            )

        if "[INFO] Step 2.2 - Embedding:" in log:
            emit("embedding_start", "向量语义召回开始：")

        embedding_model = match(r"使用远程 embedding 服务：model=([^\s]+)")
        embedding_queries = match(
            r"远程 embedding：model=.*? total=(\d+) batch=(\d+)"
        )
        embedding_window = match(r"Supabase 向量召回窗口计数.*?：(\d+) 条")
        embedding_top_k = match(r"Supabase 向量召回自适应 Top K = (\d+)")
        embedding_hits = match(r"Supabase 向量召回命中 (\d+) 条")
        tagged_counts = re.findall(r"其中带 tag 的论文数：(\d+)", log)
        if (
            embedding_model
            and embedding_queries
            and embedding_window
            and embedding_top_k
            and embedding_hits
            and len(tagged_counts) >= 2
        ):
            emit(
                "embedding_funnel",
                "向量语义召回完成："
                f"在 {embedding_window.group(1)} 篇中按 {embedding_queries.group(1)} 个查询、"
                f"top_k={embedding_top_k.group(1)} 得到 {embedding_hits.group(1)} 条按查询命中 "
                f"→ 去重且匹配主题 {tagged_counts[1]} 篇；"
                f"使用远程模型 {embedding_model.group(1)}。",
            )

        if "[INFO] Step 2.3 - RRF:" in log:
            emit("rrf_start", "RRF 融合开始：")

        rrf_keys = match(r"RRF keys=(\d+) \| bm25_queries=(\d+) \| emb_queries=(\d+)")
        rrf_merged = match(r"merged papers=(\d+)")
        if rrf_keys and rrf_merged and bm25_tagged and len(tagged_counts) >= 2:
            emit(
                "rrf_funnel",
                "RRF 融合候选池完成："
                f"输入 BM25 {bm25_tagged.group(1)} 篇 + Embedding {tagged_counts[1]} 篇 "
                f"→ 合并去重 {rrf_merged.group(1)} 篇；",
            )

        if "[INFO] Step 3 - Rerank:" in log:
            emit("rerank_start", "Reranker 开始：")

        rerank_provider = match(
            r"reranker 配置：profile=([^，]+)，provider=([^，]+)，model=([^，]+)"
        )
        rerank_start = match(
            r"开始 rerank：queries=(\d+).*?papers=(\d+)，global_pool=(\d+)"
            r"（lane_top_k=(\d+), guaranteed_per_lane=(\d+), global_top=(\d+)），"
            r"batch_size=(\d+)，max_chars=(\d+)"
        )
        if rerank_provider and rerank_start:
            rerank_data = saved_json(
                r"\[INFO\] 已将打分结果写入：([^\r\n]+(?<!\.llm)\.json)"
            )
            refine_threshold = match(r"min_star=(\d+)")
            if rerank_data is not None and refine_threshold:
                best_stars: dict[str, int] = {}
                queries = rerank_data.get("queries")
                if isinstance(queries, list):
                    for query in queries:
                        ranked = query.get("ranked") if isinstance(query, Mapping) else None
                        if not isinstance(ranked, list):
                            continue
                        for item in ranked:
                            if not isinstance(item, Mapping):
                                continue
                            paper_id = str(item.get("paper_id") or "").strip()
                            star = int(item.get("star_rating") or 0)
                            if paper_id:
                                best_stars[paper_id] = max(best_stars.get(paper_id, 0), star)
                threshold = int(refine_threshold.group(1))
                star_values = [float(value) for value in best_stars.values()]
                star_dist = distribution(
                    star_values,
                    [("5★", 5, 6), ("4★", 4, 5), ("3★", 3, 4), ("2★", 2, 3), ("1★", 1, 2)],
                )
                selected = sum(1 for value in star_values if value >= threshold)
                emit(
                    "rerank_complete",
                    f"Reranker 完成：全局候选池 {rerank_start.group(3)} 篇，"
                    f"模型 {rerank_provider.group(3)}，batch={rerank_start.group(7)}；"
                    f"{len(best_stars)} 篇分数分布：{star_dist}；"
                    f"按照 star_rating≥{threshold} 筛选出 {selected} 篇。",
                )

        rerank_skipped = match(r"当前输入中没有可用于 rerank 的意图查询，跳过 rerank")
        if rerank_skipped:
            emit(
                "rerank_complete",
                "Reranker 完成：0 篇获得星级；由于缺少 Intent 查询而跳过，"
                "按照 star_rating≥4 筛选出 0 篇。",
            )

        if "[INFO] Step 4 - LLM refine:" in log:
            emit("llm_refine_start", "LLM 精炼打分 开始：")

        refine_start = match(
            r"start filter: queries=(\d+), papers=(\d+), min_star=(\d+), "
            r"batch_size=(\d+), max_chars=(\d+), concurrency=(\d+)"
        )
        refine_pool = match(r"global candidates=(\d+) batches=(\d+)")
        scored = match(r"scored_papers=(\d+)")
        llm_data = saved_json(r"\[INFO\] saved: ([^\r\n]+\.llm\.json)")
        if refine_start and llm_data is not None:
            ranked = llm_data.get("llm_ranked")
            ranked_items = [item for item in ranked if isinstance(item, Mapping)] if isinstance(ranked, list) else []
            llm_scores = [float(item.get("score") or item.get("llm_score") or 0) for item in ranked_items]
            threshold = 8.0 if run_mode and run_mode.group(2) == "skims" else 6.0
            llm_dist = distribution(
                llm_scores,
                [("9–10分", 9, 10.000001), ("8–<9分", 8, 9), ("6–<8分", 6, 8), ("<6分", -1e9, 6)],
            )
            selected = sum(1 for value in llm_scores if value >= threshold)
            emit(
                "llm_refine_complete",
                f"LLM 精炼打分 完成：输入 {refine_start.group(2)} 篇，"
                f"候选 {len(llm_scores)} 篇，batch={refine_start.group(4)}，"
                f"并发={refine_start.group(6)}；分数分布：{llm_dist}；"
                f"按照 llm_score≥{threshold:g} 筛选出 {selected} 篇。",
            )

        no_llm_candidates = match(r"no candidates found with star_rating >= min_star")
        if no_llm_candidates:
            threshold = refine_start.group(3) if refine_start else "4"
            emit(
                "llm_refine_complete",
                f"LLM 精炼打分 完成：输入 0 篇，分数分布为空；"
                f"Reranker 的 star_rating≥{threshold} 候选为 0 篇。",
            )

        if "[INFO] Step 5 - Select:" in log:
            emit("selection_start", "选择论文（精读/速读） 开始：")

        selection = match(r"\[STATS\] (\{[^\n]+\})")
        if selection:
            try:
                stats = json.loads(selection.group(1))
            except json.JSONDecodeError:
                stats = None
            if isinstance(stats, Mapping):
                emit(
                    "selection_funnel",
                    "选择论文（精读/速读） 完成："
                    f"选定 {stats.get('deep_selected', 0)} 篇待精读、"
                    f"{stats.get('quick_selected', 0)} 篇待速读；"
                    "逐篇阅读与内容生成将在下一步执行。",
                )

        empty_selection = match(r"没有候选论文（新论文=0 且 carryover=0）")
        if empty_selection:
            emit(
                "selection_complete",
                "选择论文（精读/速读） 完成：选定 0 篇待精读、0 篇待速读；"
                "逐篇阅读与内容生成将在下一步执行。",
            )

        docling_failure = match(r"\[WARN\] Docling 提取降级：([^\n]+)")
        if docling_failure:
            detail = docling_failure.group(1)
            reason = (
                "缺少 pypdfium2 依赖"
                if "No module named 'pypdfium2'" in detail
                else detail[:160]
            )
            emit(
                "docling_fallback",
                f"图片提取警告：Docling 路线发生降级（{reason}）；正文生成继续执行。",
            )

        if "[OK] docs updated:" in log:
            emit("docs_complete", "生成日报文档完成")

    def _request(
        self, method: str, path: str, payload: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        url = self.endpoint.rstrip("/") + path
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-2000:]
            raise DailyPaperUnavailable(
                f"Daily Paper HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise DailyPaperUnavailable(f"Daily Paper is unreachable: {exc}") from exc
        if not isinstance(decoded, Mapping):
            raise DailyPaperUnavailable("Daily Paper returned a non-object response")
        return decoded


def _build_subscriptions(raw_topics: Any) -> dict[str, Any]:
    topics = raw_topics if isinstance(raw_topics, list) else []
    profiles: list[dict[str, Any]] = []
    for item in topics:
        if not isinstance(item, Mapping):
            continue
        keywords = [
            {"query": str(value).strip(), "keyword": str(value).strip()}
            for value in (item.get("keywords") or [])
            if str(value).strip()
        ]
        intents = [
            {"query": str(value).strip(), "enabled": True}
            for value in (item.get("intent_queries") or [])
            if str(value).strip()
        ]
        if not intents:
            # Defensive fallback for CLI/direct module callers. Feishu uses a
            # web-grounded confirmation turn, but the stable adapter must never
            # allow keyword-only input to skip reranking and collapse 991→0.
            tag = str(item.get("tag") or "Research").strip() or "Research"
            description = str(item.get("description") or "").strip()
            focus = description or tag
            distinct_keywords: list[str] = []
            seen_keywords = {focus.casefold(), tag.casefold()}
            for value in (item.get("keywords") or []):
                keyword = str(value).strip()
                if not keyword or keyword.casefold() in seen_keywords:
                    continue
                seen_keywords.add(keyword.casefold())
                distinct_keywords.append(keyword)
                if len(distinct_keywords) >= 3:
                    break
            if distinct_keywords:
                focus += ", especially " + ", ".join(distinct_keywords)
            intents = [
                {
                    "query": f"Find recent papers on {focus}.",
                    "enabled": True,
                    "source": "connect-hub-template-fallback",
                }
            ]
        profiles.append(
            {
                "tag": str(item.get("tag") or "Research").strip(),
                "description": str(item.get("description") or "").strip(),
                "enabled": True,
                "keywords": keywords,
                "intent_queries": intents,
                "paper_sources": ["arxiv"],
            }
        )
    return {
        "schema_migration": {"stage": "A", "diff_threshold_pct": 15},
        "keyword_recall_mode": "or",
        "intent_profiles": profiles,
    }
