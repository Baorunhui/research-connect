from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from connect_hub.contracts import ConnectJobError, JobErrorCode, ModuleManifest, SCHEMA_VERSION


class CitationClawAdapter:
    manifest = ModuleManifest(
        module_name="citationclaw",
        module_version="2.0.0",
        supported_job_types=("citation_lookup",),
    )

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: int = 3600,
        poll_seconds: int = 2,
    ) -> None:
        self.endpoint = endpoint.strip().rstrip("/")
        self.timeout_seconds = max(60, int(timeout_seconds))
        self.poll_seconds = max(1, int(poll_seconds))

    @property
    def configured(self) -> bool:
        return bool(self.endpoint)

    def run(
        self,
        *,
        external_job_id: str,
        papers: list[Mapping[str, Any]],
        output_prefix: str,
        on_progress: Callable[..., None],
        is_cancelled: Callable[[], bool],
    ) -> Mapping[str, Any]:
        started = self._request(
            "POST",
            "/api/run",
            {
                "papers": [dict(item) for item in papers],
                "output_prefix": output_prefix,
                "external_job_id": external_job_id,
            },
        )
        schema = str(started.get("schema_version") or "")
        if schema and schema != SCHEMA_VERSION:
            raise ConnectJobError(
                JobErrorCode.MODULE_INCOMPATIBLE,
                "CitationClaw 接口版本与 Connect Hub 不兼容。",
            )
        deadline = time.monotonic() + self.timeout_seconds
        seen_logs: set[tuple[str, str]] = set()
        last_percentage = -1
        while True:
            if is_cancelled():
                cancelled = self._request(
                    "POST", "/api/task/cancel", {"external_job_id": external_job_id}
                )
                if str(cancelled.get("status") or "") != "success":
                    raise ConnectJobError(
                        JobErrorCode.CANCEL_FAILED,
                        "CitationClaw 没有确认取消请求。",
                        stage="cancelling",
                    )
                raise ConnectJobError(JobErrorCode.JOB_CANCELLED, "查引用任务已取消。")
            status = self._request("GET", "/api/task/status", None)
            returned_id = str(status.get("external_job_id") or "")
            if returned_id and returned_id != external_job_id:
                raise ConnectJobError(
                    JobErrorCode.MODULE_INCOMPATIBLE,
                    "CitationClaw 返回了不属于当前任务的状态。",
                    technical_message=f"expected {external_job_id}, got {returned_id}",
                )
            for item in status.get("logs") or []:
                if not isinstance(item, Mapping):
                    continue
                timestamp = str(item.get("timestamp") or "")
                message = str(item.get("message") or "").strip()
                key = (timestamp, message)
                if message and key not in seen_logs:
                    seen_logs.add(key)
                    if _user_visible_log(message):
                        on_progress(message, stage="citationclaw")
            progress = status.get("progress") if isinstance(status.get("progress"), Mapping) else {}
            percentage = int(progress.get("percentage") or 0)
            if percentage != last_percentage and percentage > 0:
                last_percentage = percentage
                on_progress(
                    f"查引用模块进度：{percentage}%",
                    stage="citationclaw",
                    current=int(progress.get("current") or 0),
                    total=int(progress.get("total") or 100),
                )
            state = str(status.get("status") or "")
            if state == "completed":
                result = status.get("result")
                if not isinstance(result, Mapping):
                    raise ConnectJobError(
                        JobErrorCode.OUTPUT_INVALID,
                        "CitationClaw 已结束，但没有返回结构化产物。",
                    )
                return dict(result)
            if state == "failed":
                raise ConnectJobError(
                    JobErrorCode.MODULE_EXECUTION_FAILED,
                    "查引用模块执行失败，请用 /job 查看错误码和模块日志。",
                    technical_message=str(status.get("error") or "CitationClaw failed"),
                )
            if state == "cancelled":
                raise ConnectJobError(JobErrorCode.JOB_CANCELLED, "查引用任务已取消。")
            if time.monotonic() >= deadline:
                try:
                    self._request("POST", "/api/task/cancel", {"external_job_id": external_job_id})
                finally:
                    raise ConnectJobError(
                        JobErrorCode.JOB_TIMEOUT,
                        "查引用任务执行超时，已请求模块取消。",
                        retryable=True,
                    )
            time.sleep(self.poll_seconds)

    def _request(
        self, method: str, path: str, payload: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            code = JobErrorCode.INVALID_REQUEST if exc.code in {400, 409, 422} else JobErrorCode.PROVIDER_UNAVAILABLE
            raise ConnectJobError(
                code,
                "CitationClaw 拒绝了任务请求。" if exc.code < 500 else "CitationClaw 服务异常。",
                retryable=exc.code >= 500,
                technical_message=f"CitationClaw HTTP {exc.code}: {detail}",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectJobError(
                JobErrorCode.PROVIDER_UNAVAILABLE,
                "CitationClaw 本地服务无法连接，请确认统一服务已启动。",
                retryable=True,
                technical_message=f"CitationClaw unreachable: {exc}",
            ) from exc
        if not isinstance(decoded, Mapping):
            raise ConnectJobError(JobErrorCode.OUTPUT_INVALID, "CitationClaw 返回格式无效。")
        return decoded


def _user_visible_log(message: str) -> bool:
    markers = (
        "Phase ", "Step ", "开始", "完成", "抓取", "检索", "下载", "PDF", "报告", "LLM"
    )
    return any(marker in message for marker in markers)
