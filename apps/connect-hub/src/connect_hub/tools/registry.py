from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Mapping

from connect_hub.contracts import ConnectJobError, JobErrorCode, ModuleManifest
from connect_hub.jobs import JobCoordinator
from connect_hub.storage import ConversationStore
from connect_hub.tools.base import ToolContext, ToolDefinition, ToolExecution


class ToolRegistry:
    def __init__(
        self,
        audit_store: ConversationStore,
        jobs: JobCoordinator | None = None,
    ) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._lock = threading.RLock()
        self._audit_store = audit_store
        self._jobs = jobs or JobCoordinator(audit_store)

    @property
    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._tools))

    @property
    def jobs(self) -> JobCoordinator:
        return self._jobs

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name or not definition.name.replace("_", "").isalnum():
            raise ValueError(f"invalid tool name: {definition.name!r}")
        with self._lock:
            if definition.name in self._tools:
                raise ValueError(f"tool already registered: {definition.name}")
            ModuleManifest(
                module_name=definition.module_name,
                module_version=definition.module_version,
                supported_job_types=(definition.job_type or definition.name,),
            ).validate()
            self._tools[definition.name] = definition

    def unregister(self, name: str) -> bool:
        """Remove a runtime-configurable tool without disturbing active calls."""
        with self._lock:
            return self._tools.pop(name, None) is not None

    def cancel_latest(self, session_key: str):
        return self._jobs.cancel_latest(session_key)

    def openai_tools(self, names: set[str] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            selected = sorted(self._tools if names is None else set(self._tools) & names)
            return [self._tools[name].as_openai_tool() for name in selected]

    def kind(self, name: str) -> str:
        with self._lock:
            definition = self._tools.get(name)
        return definition.kind if definition is not None else "unknown"

    def progress_message(self, name: str) -> str:
        with self._lock:
            definition = self._tools.get(name)
        return definition.progress_message if definition is not None else ""

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: ToolContext,
        tool_call_id: str,
    ) -> ToolExecution:
        started = time.monotonic()
        with self._lock:
            definition = self._tools.get(name)
        if definition is None:
            return ToolExecution(name, False, error="tool is not registered")

        try:
            _validate(arguments, definition.parameters, path="$arguments")
        except ValueError as exc:
            return ToolExecution(name, False, error=f"invalid arguments: {exc}")

        handle = None
        job_id = ""
        if definition.track_job:
            handle = self._jobs.start(
                session_key=context.session_key,
                job_type=definition.job_type or definition.name,
                module_name=definition.module_name,
                module_version=definition.module_version,
                input_data=dict(arguments),
                notifier=context.progress,
                start_message=definition.progress_message,
                start_url=definition.start_url,
            )
            job_id = handle.job_id
        audit_id = self._audit_store.start_tool_run(
            context.session_key, tool_call_id, name, dict(arguments)
        )
        if handle is None:
            execution_context = context
        else:
            execution_context = ToolContext(
                session_key=context.session_key,
                progress=lambda message: self._jobs.progress(job_id, message),
                event_reporter=lambda message, **kwargs: self._jobs.progress(
                    job_id, message, **kwargs
                ),
                job_id=job_id,
                cancellation=handle.cancellation,
                process_runner=self._jobs.process_runner,
                artifact_recorder=lambda **kwargs: self._jobs.add_artifact(
                    job_id, **kwargs
                ),
                usage_recorder=lambda **kwargs: self._jobs.record_usage(
                    job_id, **kwargs
                ),
                process_started=lambda pid, executable: self._audit_store.set_job_process(
                    job_id, pid, executable
                ),
                process_finished=lambda pid: self._audit_store.clear_job_process(
                    job_id, pid
                ),
                public_url=definition.start_url,
            )
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{name}")
        future = executor.submit(definition.handler, dict(arguments), execution_context)
        try:
            output = future.result(timeout=definition.timeout_seconds)
            if handle is not None:
                handle.cancellation.raise_if_cancelled()
                _record_output_artifacts(output, execution_context)
                self._jobs.complete(job_id, output)
            elapsed = int((time.monotonic() - started) * 1000)
            execution = ToolExecution(
                name, True, output=output, elapsed_ms=elapsed, job_id=job_id
            )
            self._audit_store.finish_tool_run(
                audit_id, status="completed", result=execution.model_payload()
            )
            return execution
        except FutureTimeout:
            if handle is not None:
                handle.cancellation.cancel()
                self._jobs.process_runner.cancel(job_id)
            future.cancel()
            elapsed = int((time.monotonic() - started) * 1000)
            error = f"tool exceeded {definition.timeout_seconds}s timeout"
            if handle is not None:
                self._jobs.fail(
                    job_id,
                    ConnectJobError(
                        JobErrorCode.JOB_TIMEOUT,
                        "任务执行超时，相关受控子进程已请求终止。",
                        retryable=True,
                        technical_message=error,
                    ),
                )
            self._audit_store.finish_tool_run(audit_id, status="timed_out", error=error)
            return ToolExecution(
                name, False, error=error, elapsed_ms=elapsed, job_id=job_id
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            if handle is not None:
                classified = self._jobs.fail(job_id, exc)
                error = classified.technical_message
                audit_status = (
                    "cancelled"
                    if classified.code == JobErrorCode.JOB_CANCELLED.value
                    else "failed"
                )
            else:
                error = f"{type(exc).__name__}: {exc}"
                audit_status = "failed"
            self._audit_store.finish_tool_run(
                audit_id, status=audit_status, error=error
            )
            return ToolExecution(
                name, False, error=error, elapsed_ms=elapsed, job_id=job_id
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


def _record_output_artifacts(output: Any, context: ToolContext) -> None:
    if not isinstance(output, Mapping):
        return
    report_path = str(output.get("report_path") or "").strip()
    if report_path:
        context.record_artifact(kind="file", path=report_path)
    public_url = str(output.get("public_url") or "").strip()
    if public_url:
        context.record_artifact(kind="url", url=public_url, name="public report")
    images = output.get("images")
    if isinstance(images, list):
        for path in images:
            text = str(path or "").strip()
            if text:
                context.record_artifact(kind="image", path=text)


def _validate(value: Any, schema: Mapping[str, Any], *, path: str) -> None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, Mapping):
            raise ValueError(f"{path} must be an object")
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                raise ValueError(f"{path}.{key} is required")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ValueError(f"{path} has unsupported fields: {', '.join(extras)}")
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, Mapping):
                _validate(item, child, path=f"{path}.{key}")
    elif expected == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValueError(f"{path} must contain at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{path} must contain at most {schema['maxItems']} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate(item, item_schema, path=f"{path}[{index}]")
    elif expected == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
        if "minLength" in schema and len(value.strip()) < schema["minLength"]:
            raise ValueError(f"{path} must contain at least {schema['minLength']} character(s)")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{path} must contain at most {schema['maxLength']} character(s)")
    elif expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{path} must be an integer")
    elif expected == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise ValueError(f"{path} must be a number")
    elif expected == "boolean" and not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")

    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    if isinstance(value, (int, float)):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} must be <= {schema['maximum']}")
