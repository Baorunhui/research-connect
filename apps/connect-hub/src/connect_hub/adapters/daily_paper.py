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
        supported_job_types=("daily_report", "paper_research"),
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
        raise ValueError(f"unsupported Daily Paper action: {request.action}")

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
            if on_snapshot is not None:
                on_snapshot(current, str(record.get("log") or ""))
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
                return {
                    "schema_version": SCHEMA_VERSION,
                    "id": run_id,
                    "status": "completed",
                    "result": {
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "mode": str(arguments.get("mode") or "standard"),
                        "deep_dive": [],
                        "quick_skim": [],
                    },
                    "run": current,
                    "log": str(record.get("log") or ""),
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
