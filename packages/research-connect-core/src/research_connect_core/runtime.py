from __future__ import annotations

import uuid
import os
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Mapping

from .events import JobEvent, JobEventSink, JsonLineEventSink, NullEventSink


@dataclass
class StandaloneJobRuntime(AbstractContextManager["StandaloneJobRuntime"]):
    """The same event semantics for a direct CLI run and a Connect-owned job.

    Connect Hub can inject its own sink and job id. A user invoking a module
    directly gets a generated id and either JSONL progress or a no-op sink.
    The runtime intentionally does not persist another task database.
    """

    module_name: str
    job_type: str
    sink: JobEventSink = field(default_factory=NullEventSink)
    job_id: str = field(default_factory=lambda: f"job-{uuid.uuid4().hex}")
    _terminal: bool = field(default=False, init=False)

    def __enter__(self) -> "StandaloneJobRuntime":
        self.emit("job.started", f"{self.module_name} started", stage="starting")
        return self

    def emit(
        self,
        event_type: str,
        message: str = "",
        *,
        stage: str = "",
        current: int | float | None = None,
        total: int | float | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> JobEvent:
        event = JobEvent(
            job_id=self.job_id,
            event_type=event_type,
            message=message,
            stage=stage,
            current=current,
            total=total,
            payload=payload or {},
        )
        self.sink.emit(event)
        if event_type in {"job.completed", "job.failed", "job.cancelled"}:
            self._terminal = True
        return event

    def progress(
        self,
        message: str,
        *,
        stage: str,
        current: int | float | None = None,
        total: int | float | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> JobEvent:
        return self.emit(
            "job.progress",
            message,
            stage=stage,
            current=current,
            total=total,
            payload=payload,
        )

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc_type, traceback
        if self._terminal:
            return False
        if exc is None:
            self.emit("job.completed", f"{self.module_name} completed", stage="completed")
        else:
            self.emit(
                "job.failed",
                f"{self.module_name} failed",
                stage="failed",
                payload={"error_type": type(exc).__name__, "error": str(exc)[:1000]},
            )
        return False


def runtime_from_env(module_name: str, job_type: str) -> StandaloneJobRuntime:
    """Build the CLI runtime understood by Connect Hub and standalone users.

    CONNECT_JOB_ID lets Connect correlate child events.  Set
    CONNECT_EMIT_EVENTS=1 for JSONL on stderr; normal human CLI output remains
    unchanged when the module is run by itself.
    """

    enabled = str(os.getenv("CONNECT_EMIT_EVENTS") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    sink: JobEventSink = JsonLineEventSink() if enabled else NullEventSink()
    job_id = str(os.getenv("CONNECT_JOB_ID") or "").strip() or f"job-{uuid.uuid4().hex}"
    return StandaloneJobRuntime(
        module_name=module_name,
        job_type=job_type,
        sink=sink,
        job_id=job_id,
    )
