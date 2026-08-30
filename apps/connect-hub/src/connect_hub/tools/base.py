from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from connect_hub.processes import CancellationToken, ManagedProcessRunner, ProcessResult


@dataclass(frozen=True)
class ToolContext:
    session_key: str
    progress: Callable[[str], None] | None = None
    event_reporter: Callable[..., None] | None = None
    job_id: str = ""
    cancellation: CancellationToken | None = None
    process_runner: ManagedProcessRunner | None = None
    artifact_recorder: Callable[..., int] | None = None
    usage_recorder: Callable[..., int] | None = None
    process_started: Callable[[int, str], None] | None = None
    process_finished: Callable[[int], None] | None = None
    public_url: str = ""

    def report_progress(
        self,
        message: str,
        *,
        stage: str = "",
        current: int | None = None,
        total: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        text = message.strip()
        if not text:
            return
        if self.event_reporter is not None:
            self.event_reporter(
                text,
                stage=stage,
                current=current,
                total=total,
                payload=payload,
            )
        elif self.progress is not None:
            self.progress(text)

    @property
    def cancelled(self) -> bool:
        return bool(self.cancellation and self.cancellation.cancelled)

    def raise_if_cancelled(self) -> None:
        if self.cancellation is not None:
            self.cancellation.raise_if_cancelled()

    def run_process(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        capture_output: bool = True,
    ) -> ProcessResult:
        if not self.job_id or self.cancellation is None or self.process_runner is None:
            raise RuntimeError("managed process execution requires an active connect.job.v1 job")
        return self.process_runner.run(
            command,
            job_id=self.job_id,
            cancellation=self.cancellation,
            cwd=cwd,
            env=env,
            timeout=timeout,
            capture_output=capture_output,
            on_start=self.process_started,
            on_finish=self.process_finished,
        )

    def record_artifact(
        self,
        *,
        kind: str,
        path: str = "",
        url: str = "",
        name: str = "",
    ) -> int | None:
        if self.artifact_recorder is None:
            return None
        return self.artifact_recorder(kind=kind, path=path, url=url, name=name)

    def record_usage(
        self,
        *,
        provider: str,
        operation: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        api_calls: int = 1,
        duration_ms: int = 0,
        status_code: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> int | None:
        if self.usage_recorder is None:
            return None
        return self.usage_recorder(
            provider=provider,
            operation=operation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            api_calls=api_calls,
            duration_ms=duration_ms,
            status_code=status_code,
            metadata=metadata,
        )


ToolHandler = Callable[[Mapping[str, Any], ToolContext], Any]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: ToolHandler
    timeout_seconds: int = 30
    progress_message: str = ""
    module_name: str = "connect-hub"
    module_version: str = "0.1.0"
    job_type: str = ""
    track_job: bool = True
    kind: str = "utility"

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True)
class ToolExecution:
    tool_name: str
    success: bool
    output: Any = None
    error: str = ""
    elapsed_ms: int = 0
    job_id: str = ""

    def model_payload(self) -> dict[str, Any]:
        if self.success:
            return {
                "ok": True,
                "tool": self.tool_name,
                "output": self.output,
                "elapsed_ms": self.elapsed_ms,
                "job_id": self.job_id,
            }
        return {
            "ok": False,
            "tool": self.tool_name,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "job_id": self.job_id,
        }
