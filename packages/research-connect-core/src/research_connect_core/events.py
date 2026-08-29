from __future__ import annotations

import json
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, TextIO


SCHEMA_VERSION = "connect.job.v1"


@dataclass(frozen=True)
class JobEvent:
    job_id: str
    event_type: str
    message: str = ""
    stage: str = ""
    current: int | float | None = None
    total: int | float | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: f"evt-{uuid.uuid4().hex}")
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    schema_version: str = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "job_id": self.job_id,
            "event_type": self.event_type,
            "stage": self.stage,
            "message": self.message,
            "current": self.current,
            "total": self.total,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }


class JobEventSink(Protocol):
    def emit(self, event: JobEvent) -> None: ...


class NullEventSink:
    def emit(self, event: JobEvent) -> None:
        del event


class JsonLineEventSink:
    """Write structured events without mixing them with a module's result JSON."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stderr
        self._lock = threading.Lock()

    def emit(self, event: JobEvent) -> None:
        line = json.dumps(event.as_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.stream.write(line + "\n")
            self.stream.flush()
