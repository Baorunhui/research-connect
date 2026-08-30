from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from connect_hub.contracts import (
    ConnectJobError,
    JobErrorCode,
    JobEvent,
    JobEventType,
    JobStatus,
    SCHEMA_VERSION,
    classify_exception,
)
from connect_hub.processes import CancellationToken, ManagedProcessRunner
from connect_hub.storage import ConversationStore
from connect_hub.reporting import ReportHubClient, ReportHubError

logger = logging.getLogger(__name__)

UserNotifier = Callable[[str], None]


@dataclass(frozen=True)
class JobHandle:
    job_id: str
    session_key: str
    job_type: str
    module_name: str
    cancellation: CancellationToken


@dataclass(frozen=True)
class CancelResult:
    found: bool
    job_id: str = ""
    message: str = ""


@dataclass
class _ActiveJob:
    handle: JobHandle
    notifier: UserNotifier | None


class JobCoordinator:
    def __init__(
        self,
        store: ConversationStore,
        process_runner: ManagedProcessRunner | None = None,
        report_hub: ReportHubClient | None = None,
        *,
        interrupt_stale: bool = False,
    ) -> None:
        self.store = store
        self.process_runner = process_runner or ManagedProcessRunner()
        self.report_hub = report_hub
        self._public_urls: dict[str, str] = {}
        self._active: dict[str, _ActiveJob] = {}
        self._lock = threading.RLock()
        if interrupt_stale:
            self._interrupt_stale_jobs()

    def _interrupt_stale_jobs(self) -> None:
        stale_jobs = self.store.active_jobs()
        for job in stale_jobs:
            job_id = str(job["id"])
            raw_pid = job.get("pid")
            pid = int(raw_pid) if raw_pid is not None else 0
            executable = str(job.get("process_executable") or "")
            cleanup_attempted = pid > 0
            cleanup_succeeded = False
            if cleanup_attempted:
                cleanup_succeeded = self.process_runner.terminate_pid_tree(
                    pid, expected_executable=executable
                )
            message = "Connect Hub restarted; unfinished jobs are not resumed"
            if cleanup_attempted and cleanup_succeeded:
                message += f"; terminated recorded process tree pid={pid}"
            elif cleanup_attempted:
                message += (
                    f"; recorded pid={pid} was absent or failed identity/safety checks"
                )
            else:
                message += "; no active child PID was recorded"
            self.store.update_job(
                job_id,
                status=JobStatus.INTERRUPTED.value,
                error_code=JobErrorCode.SERVICE_RESTARTED.value,
                error_message=message,
            )
            self.emit(
                job_id,
                JobEventType.INTERRUPTED,
                message="服务重启，未完成任务已中断。",
                payload={
                    "pid": pid or None,
                    "cleanup_attempted": cleanup_attempted,
                    "cleanup_succeeded": cleanup_succeeded,
                },
                notify=False,
            )
        if stale_jobs:
            logger.warning(
                "marked %s stale job(s) as interrupted after process cleanup",
                len(stale_jobs),
            )

    def start(
        self,
        *,
        session_key: str,
        job_type: str,
        module_name: str,
        module_version: str,
        input_data: Mapping[str, Any],
        options: Mapping[str, Any] | None = None,
        notifier: UserNotifier | None = None,
        start_message: str = "",
        publish_public: bool = False,
        public_title: str = "",
    ) -> JobHandle:
        job_id = f"job-{uuid.uuid4().hex}"
        cancellation = CancellationToken()
        handle = JobHandle(job_id, session_key, job_type, module_name, cancellation)
        self.store.create_job(
            job_id,
            session_key=session_key,
            job_type=job_type,
            module_name=module_name,
            module_version=module_version,
            input_data=input_data,
            options=options,
        )
        with self._lock:
            self._active[job_id] = _ActiveJob(handle, notifier)
        if publish_public and self.report_hub is not None and self.report_hub.configured:
            try:
                public_url = self.report_hub.create_job(
                    job_id=job_id,
                    module_name=module_name,
                    title=public_title or _job_label(job_type) or "Research Connect 任务",
                )
                self._public_urls[job_id] = public_url
                self.add_artifact(
                    job_id,
                    kind="url",
                    url=public_url,
                    name="public report",
                )
                if notifier is not None:
                    notifier(f"任务网页已创建，可实时查看进度：\n{public_url}")
            except ReportHubError as exc:
                logger.warning("public report creation failed job_id=%s: %s", job_id, exc)
                if notifier is not None:
                    notifier(
                        "公网任务页创建失败（REPORT_HUB_UNAVAILABLE），本地任务仍会继续；"
                        "完成后我会在飞书返回结果。"
                    )
        self.emit(
            job_id,
            JobEventType.ACCEPTED,
            message=f"{_job_label(job_type)}任务已创建。",
        )
        self.store.update_job(job_id, status=JobStatus.RUNNING.value)
        self.emit(
            job_id,
            JobEventType.STARTED,
            message=start_message or f"{_job_label(job_type)}任务已开始执行。",
        )
        return handle

    def emit(
        self,
        job_id: str,
        event_type: JobEventType | str,
        *,
        message: str = "",
        stage: str = "",
        current: int | None = None,
        total: int | None = None,
        payload: Mapping[str, Any] | None = None,
        notify: bool = True,
    ) -> JobEvent:
        kind = event_type.value if isinstance(event_type, JobEventType) else str(event_type)
        event = JobEvent(
            event_id=f"evt-{uuid.uuid4().hex}",
            job_id=job_id,
            event_type=kind,
            message=message.strip(),
            stage=stage.strip(),
            current=current,
            total=total,
            payload=dict(payload or {}),
        )
        inserted = self.store.append_job_event(
            event_id=event.event_id,
            job_id=job_id,
            event_type=event.event_type,
            stage=event.stage,
            message=event.message,
            current=event.current,
            total=event.total,
            payload=event.payload,
        )
        if inserted and notify:
            with self._lock:
                active = self._active.get(job_id)
            if active is not None and active.notifier is not None:
                text = format_job_event(event)
                if text:
                    try:
                        active.notifier(text)
                    except Exception:
                        logger.exception("job notification failed job_id=%s", job_id)
        if inserted and job_id in self._public_urls and self.report_hub is not None:
            try:
                self.report_hub.send_event(event)
            except ReportHubError as exc:
                # Public reporting is deliberately best-effort: a temporary
                # public-server outage must not kill the user's local research.
                logger.warning("public event upload failed job_id=%s: %s", job_id, exc)
        return event

    def public_url(self, job_id: str) -> str:
        return self._public_urls.get(job_id, "")

    def publish_report(self, job_id: str, source: str | Path) -> str:
        if job_id not in self._public_urls or self.report_hub is None:
            return ""
        try:
            url = self.report_hub.upload_report(job_id, source)
            return url or self._public_urls[job_id]
        except ReportHubError as exc:
            logger.warning("public report upload failed job_id=%s: %s", job_id, exc)
            self.progress(
                job_id,
                "最终网页上传失败（REPORT_PUBLISH_FAILED）；任务结果仍保留在本机并会返回飞书。",
                stage="publish",
                payload={"error_code": "REPORT_PUBLISH_FAILED", "detail": str(exc)[:500]},
            )
            return ""

    def progress(
        self,
        job_id: str,
        message: str,
        *,
        stage: str = "",
        current: int | None = None,
        total: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        if not message.strip():
            return
        self.emit(
            job_id,
            JobEventType.PROGRESS,
            message=message,
            stage=stage,
            current=current,
            total=total,
            payload=payload,
        )

    def add_artifact(
        self,
        job_id: str,
        *,
        kind: str,
        path: str = "",
        url: str = "",
        name: str = "",
    ) -> int:
        size_bytes: int | None = None
        digest = ""
        resolved_name = name
        if path:
            file_path = Path(path)
            resolved_name = resolved_name or file_path.name
            if file_path.is_file():
                size_bytes = file_path.stat().st_size
                digest = _sha256(file_path)
        artifact_id = self.store.add_artifact(
            job_id,
            kind=kind,
            path=path,
            url=url,
            name=resolved_name,
            size_bytes=size_bytes,
            sha256=digest,
        )
        self.emit(
            job_id,
            JobEventType.ARTIFACT,
            message=f"已生成产物：{resolved_name or url or kind}",
            payload={"artifact_id": artifact_id, "kind": kind, "path": path, "url": url},
            notify=False,
        )
        return artifact_id

    def record_usage(
        self,
        job_id: str,
        *,
        provider: str,
        operation: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        api_calls: int = 1,
        duration_ms: int = 0,
        status_code: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        usage_id = self.store.record_usage(
            job_id,
            provider=provider,
            operation=operation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            api_calls=api_calls,
            duration_ms=duration_ms,
            status_code=status_code,
            metadata=metadata,
        )
        self.emit(
            job_id,
            JobEventType.COST,
            payload={
                "usage_id": usage_id,
                "provider": provider,
                "operation": operation,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "api_calls": api_calls,
                "duration_ms": duration_ms,
                "status_code": status_code,
            },
            notify=False,
        )
        return usage_id

    def complete(self, job_id: str, result: Any) -> None:
        self.store.update_job(
            job_id,
            status=JobStatus.COMPLETED.value,
            result=result,
            set_result=True,
        )
        self.emit(
            job_id,
            JobEventType.COMPLETED,
            message="任务执行完成，正在整理并发送结果。",
        )
        self._remove_active(job_id)

    def fail(self, job_id: str, exc: BaseException) -> ConnectJobError:
        error = classify_exception(exc)
        if error.code == JobErrorCode.JOB_CANCELLED.value:
            self.cancelled(job_id)
            return error
        status = (
            JobStatus.TIMED_OUT.value
            if error.code == JobErrorCode.JOB_TIMEOUT.value
            else (
                JobStatus.CANCEL_FAILED.value
                if error.code == JobErrorCode.CANCEL_FAILED.value
                else JobStatus.FAILED.value
            )
        )
        self.store.update_job(
            job_id,
            status=status,
            error_code=error.code,
            error_message=error.technical_message,
        )
        self.emit(
            job_id,
            JobEventType.FAILED,
            message=error.user_message,
            stage=error.stage,
            payload=error.payload(),
        )
        self._remove_active(job_id)
        return error

    def cancelled(self, job_id: str) -> None:
        self.store.update_job(
            job_id,
            status=JobStatus.CANCELLED.value,
            error_code=JobErrorCode.JOB_CANCELLED.value,
            error_message="cancelled by user",
        )
        self.emit(
            job_id,
            JobEventType.CANCELLED,
            message="任务已取消，相关受控子进程已停止。",
        )
        self._remove_active(job_id)

    def cancel_latest(self, session_key: str) -> CancelResult:
        latest = self.store.latest_active_job(session_key)
        if latest is None:
            return CancelResult(False, message="当前没有可取消的任务。")
        job_id = str(latest["id"])
        with self._lock:
            active = self._active.get(job_id)
        if active is None:
            self.store.update_job(
                job_id,
                status=JobStatus.CANCEL_FAILED.value,
                error_code=JobErrorCode.CANCEL_FAILED.value,
                error_message="job is not owned by this process",
            )
            return CancelResult(
                False,
                job_id,
                "找到了任务记录，但它不由当前进程管理，无法安全取消。",
            )
        self.store.update_job(job_id, status=JobStatus.CANCELLING.value)
        active.handle.cancellation.cancel()
        self.process_runner.cancel(job_id)
        self.progress(job_id, "已收到取消请求，正在停止任务。", stage="cancelling")
        return CancelResult(True, job_id, f"已请求取消任务 {short_job_id(job_id)}。")

    def cancellation(self, job_id: str) -> CancellationToken | None:
        with self._lock:
            active = self._active.get(job_id)
        return active.handle.cancellation if active is not None else None

    def _remove_active(self, job_id: str) -> None:
        with self._lock:
            self._active.pop(job_id, None)


def short_job_id(job_id: str) -> str:
    return job_id[4:12] if job_id.startswith("job-") else job_id[:8]


def format_job_event(event: JobEvent) -> str:
    short_id = short_job_id(event.job_id)
    if event.event_type == JobEventType.ACCEPTED.value:
        return f"任务 {short_id} 已创建。"
    if event.event_type == JobEventType.STARTED.value:
        return event.message or f"任务 {short_id} 已开始。"
    if event.event_type == JobEventType.PROGRESS.value:
        if event.current is not None and event.total:
            return f"{event.message}（{event.current}/{event.total}）"
        return event.message
    if event.event_type == JobEventType.COMPLETED.value:
        return event.message or f"任务 {short_id} 已完成。"
    if event.event_type == JobEventType.FAILED.value:
        prefix = f"任务 {short_id} 失败"
        if event.stage:
            prefix += f"（{event.stage}）"
        return f"{prefix}：{event.message or '模块执行失败。'}"
    if event.event_type == JobEventType.CANCELLED.value:
        return event.message or f"任务 {short_id} 已取消。"
    if event.event_type == JobEventType.INTERRUPTED.value:
        return event.message or f"任务 {short_id} 因服务重启而中断。"
    return ""


def _job_label(job_type: str) -> str:
    labels = {
        "generate_daily_paper_report": "论文日报",
        "daily_report": "论文日报",
        "paper_research": "论文调研",
        "citation_lookup": "查引用",
        "generate_xhs_package": "小红书内容",
    }
    return labels.get(job_type, "")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
