from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Mapping, Protocol


SCHEMA_VERSION = "connect.job.v1"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    CANCEL_FAILED = "cancel_failed"


ACTIVE_JOB_STATUSES = frozenset(
    {JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.CANCELLING.value}
)
TERMINAL_JOB_STATUSES = frozenset(status.value for status in JobStatus) - ACTIVE_JOB_STATUSES


class JobEventType(str, Enum):
    ACCEPTED = "job.accepted"
    STARTED = "job.started"
    PROGRESS = "job.progress"
    ARTIFACT = "job.artifact"
    COST = "job.cost"
    COMPLETED = "job.completed"
    FAILED = "job.failed"
    CANCELLED = "job.cancelled"
    INTERRUPTED = "job.interrupted"


class JobErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    MODULE_INCOMPATIBLE = "MODULE_INCOMPATIBLE"
    MODULE_EXECUTION_FAILED = "MODULE_EXECUTION_FAILED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_UNAUTHORIZED = "PROVIDER_UNAUTHORIZED"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_QUOTA_EXHAUSTED = "PROVIDER_QUOTA_EXHAUSTED"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    JOB_TIMEOUT = "JOB_TIMEOUT"
    JOB_CANCELLED = "JOB_CANCELLED"
    CANCEL_FAILED = "CANCEL_FAILED"
    SERVICE_RESTARTED = "SERVICE_RESTARTED"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


@dataclass(frozen=True)
class ModuleManifest:
    module_name: str
    module_version: str
    supported_job_types: tuple[str, ...]
    schema_versions: tuple[str, ...] = (SCHEMA_VERSION,)
    capabilities: tuple[str, ...] = ("progress", "cancel", "artifacts", "cost")

    def validate(self, required_schema: str = SCHEMA_VERSION) -> None:
        if not self.module_name.strip():
            raise ValueError("module_name is required")
        if not self.module_version.strip():
            raise ValueError("module_version is required")
        if required_schema not in self.schema_versions:
            raise ValueError(
                f"module {self.module_name} does not support {required_schema}"
            )
        if not self.supported_job_types:
            raise ValueError("at least one supported_job_type is required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "module_name": self.module_name,
            "module_version": self.module_version,
            "schema_versions": list(self.schema_versions),
            "supported_job_types": list(self.supported_job_types),
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class JobRequest:
    job_id: str
    job_type: str
    input: Mapping[str, Any]
    options: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if not self.job_id.strip() or not self.job_type.strip():
            raise ValueError("job_id and job_type are required")


@dataclass(frozen=True)
class JobAccepted:
    job_id: str
    status: str = JobStatus.QUEUED.value
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True)
class JobEvent:
    event_id: str
    job_id: str
    event_type: str
    message: str = ""
    stage: str = ""
    current: int | None = None
    total: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "job_id": self.job_id,
            "event_type": self.event_type,
            "message": self.message,
            "stage": self.stage,
            "current": self.current,
            "total": self.total,
            "payload": dict(self.payload),
        }


class EventSink(Protocol):
    def __call__(self, event: JobEvent) -> None: ...


class ModuleAdapter(Protocol):
    manifest: ModuleManifest

    def submit(self, request: JobRequest, events: EventSink) -> JobAccepted: ...
    def cancel(self, job_id: str) -> bool: ...
    def health(self) -> Mapping[str, Any]: ...


class ConnectJobError(RuntimeError):
    def __init__(
        self,
        code: JobErrorCode | str,
        user_message: str,
        *,
        stage: str = "",
        provider: str = "",
        retryable: bool = False,
        technical_message: str = "",
    ) -> None:
        super().__init__(technical_message or user_message)
        self.code = code.value if isinstance(code, JobErrorCode) else str(code)
        self.user_message = user_message
        self.stage = stage
        self.provider = provider
        self.retryable = retryable
        self.technical_message = technical_message or user_message

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "error_code": self.code,
            "stage": self.stage,
            "provider": self.provider,
            "retryable": self.retryable,
            "user_message": self.user_message,
            "technical_message": self.technical_message,
        }


def classify_exception(exc: BaseException) -> ConnectJobError:
    if isinstance(exc, ConnectJobError):
        return exc
    text = _sanitize_diagnostic(str(exc).strip())
    lowered = text.lower()
    if "429" in lowered or "rate limit" in lowered or "频率限制" in text:
        return ConnectJobError(
            JobErrorCode.PROVIDER_RATE_LIMITED,
            "外部服务触发频率限制，任务已停止；请稍后重试。",
            retryable=True,
            technical_message=text,
        )
    if "401" in lowered or "unauthorized" in lowered or "invalid token" in lowered:
        return ConnectJobError(
            JobErrorCode.PROVIDER_UNAUTHORIZED,
            "外部服务凭据无效，请检查对应 API Key。",
            technical_message=text,
        )
    if "unreachable" in lowered or "connection" in lowered or "无法连接" in text:
        return ConnectJobError(
            JobErrorCode.PROVIDER_UNAVAILABLE,
            "外部服务当前无法连接，请检查网络或服务状态。",
            retryable=True,
            technical_message=text,
        )
    if "timeout" in lowered or "timed out" in lowered or "超时" in text:
        return ConnectJobError(
            JobErrorCode.JOB_TIMEOUT,
            "任务执行超时，相关子进程已请求终止。",
            retryable=True,
            technical_message=text,
        )
    return ConnectJobError(
        JobErrorCode.MODULE_EXECUTION_FAILED,
        "模块执行失败，请查看服务端日志中的具体原因。",
        technical_message=f"{type(exc).__name__}: {text}",
    )


def _sanitize_diagnostic(text: str) -> str:
    sanitized = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-[redacted]", text)
    sanitized = re.sub(
        r"(?i)\b(api[_ -]?key|authorization|bearer|token|secret)\b"
        r"\s*[:=]\s*([^\s,;]+)",
        lambda match: f"{match.group(1)}=[redacted]",
        sanitized,
    )
    return sanitized[:4000]
