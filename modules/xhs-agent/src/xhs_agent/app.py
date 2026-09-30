from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from .package import write_package
from .pipeline import DEFAULT_MODELS, JobStopped, PipelineConfig, XHSPipeline
from .schemas import SocialContentRequest, SocialContentResponse
from .webui import INDEX_HTML


app = FastAPI(title="xhs_agent", version="0.1.0")

_STAGE_TEXT = {
    "brief": "正在整理论文要点",
    "writer": "正在写正文和标题",
    "card": "正在排卡片",
    "qa": "正在核对事实",
    "render": "正在导出卡片",
}
_jobs: OrderedDict[str, dict] = OrderedDict()
_jobs_lock = threading.Lock()
_MAX_JOBS = 30
_JOB_LIMIT_S = 20 * 60


class _JobProgress:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id

    def progress(self, message: str, *, stage: str, **kwargs) -> None:
        del kwargs
        text = _STAGE_TEXT.get(stage) or message
        with _jobs_lock:
            job = _jobs.get(self.job_id)
            if job and job["status"] == "running":
                job["stage"] = stage
                job["message"] = text

    def stopped(self) -> bool:
        with _jobs_lock:
            job = _jobs.get(self.job_id)
            return job is None or job.get("status") != "running"


def _expire_locked(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for job_id, job in _jobs.items():
        if job.get("status") != "running":
            continue
        started = job.get("started_at")
        if started is None:
            job["started_at"] = now
            continue
        if now - float(started) < _JOB_LIMIT_S:
            continue
        print(f"[xhs] job {job_id} still running after {_JOB_LIMIT_S}s", flush=True)
        message = "这次等太久了，请稍后重试。"
        job["status"] = "failed"
        job["message"] = message
        job["response"] = SocialContentResponse(
            request_id=job_id,
            status="failed",
            error=message,
        ).model_dump()


def _remember_job(job_id: str, payload: dict) -> None:
    _jobs[job_id] = payload
    _jobs.move_to_end(job_id)
    while len(_jobs) > _MAX_JOBS:
        oldest = next((key for key, job in _jobs.items() if job["status"] != "running"), None)
        if oldest is None:
            break
        _jobs.pop(oldest, None)


def _output_root() -> Path:
    return Path(os.getenv("XHS_AGENT_OUTPUT_DIR", "outputs"))


def _safe_package_dir(package_id: str) -> Path:
    root = _output_root().resolve()
    name = str(package_id or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=404, detail="package not found")
    package_dir = (root / name).resolve()
    if package_dir.parent != root:
        raise HTTPException(status_code=404, detail="package not found")
    return package_dir


def _pipeline_config() -> PipelineConfig:
    model = os.getenv("XHS_AGENT_MODEL", "").strip()
    return PipelineConfig(models={step: model for step in DEFAULT_MODELS} if model else None)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _execute(request: SocialContentRequest, runtime: _JobProgress | None = None) -> SocialContentResponse:
    output_root = _output_root()
    offline = os.getenv("XHS_AGENT_OFFLINE", "false").lower() in {"1", "true", "yes"}
    pipeline = XHSPipeline.offline() if offline else XHSPipeline()
    pipeline.config = _pipeline_config()
    if runtime is not None:
        pipeline.runtime = runtime
    result = pipeline.run(request)
    if runtime is not None:
        runtime.progress("正在导出卡片", stage="render")
    return write_package(result, output_root)


def _public_job_error(exc: Exception) -> str:
    text = str(exc)
    if "API_KEY" in text or "api_key" in text or "API key" in text:
        return "服务器还没有可用的模型密钥。"
    if "empty content" in text or "JSON" in text or "json" in text:
        return "模型这次没有写出可用的正文，请再试一次。"
    if "template" in text:
        return "卡片没有排出来，请再试一次。"
    return "生成没有完成，请稍后重试。"


def _finish_job(job_id: str, response: SocialContentResponse) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None or job.get("status") != "running":
            return
        job["response"] = response.model_dump()
        job["status"] = "failed" if response.status == "failed" else "completed"
        job["message"] = response.error or "已完成"


def _run_job(job_id: str, request: SocialContentRequest) -> None:
    try:
        try:
            response = _execute(request, runtime=_JobProgress(job_id))
        except JobStopped:
            print(f"[xhs] job {job_id} stopped after the time limit", flush=True)
            return
        except Exception as exc:
            print(f"[xhs] job {job_id} failed: {exc}", flush=True)
            response = SocialContentResponse(
                request_id=job_id,
                status="failed",
                error=_public_job_error(exc),
            )
        _finish_job(job_id, response)
    except Exception as exc:
        print(f"[xhs] job {job_id} failed while saving: {exc}", flush=True)
        _finish_job(
            job_id,
            SocialContentResponse(
                request_id=job_id,
                status="failed",
                error="生成没有完成，请稍后重试。",
            ),
        )


def _prepare_request(request: SocialContentRequest) -> SocialContentRequest:
    """Refuse a blank or oversized brief before any model call."""
    title = request.source.title.strip()
    summary = request.source.summary.strip()
    if not title or not summary:
        raise HTTPException(status_code=400, detail="请先填写标题和一句话摘要。")
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="标题太长了，请缩短到 200 字以内。")
    if len(summary) > 4000:
        raise HTTPException(status_code=400, detail="摘要太长了，请缩短到 4000 字以内。")
    materials = [item for item in request.source.materials if item.text.strip()]
    if len(materials) > 30:
        raise HTTPException(status_code=400, detail="要点太多了，请留在 30 条以内。")
    if sum(len(item.text) for item in materials) > 12000:
        raise HTTPException(status_code=400, detail="要点太长了，请缩短后再生成。")
    links = []
    for link in request.source.links:
        url = str(link.url or "").strip()
        if not url:
            continue
        label = str(link.label or "").strip() or None
        if label and len(label) > 200:
            label = label[:200]
        links.append(link.model_copy(update={"url": url, "label": label}))
    if len(links) > 20:
        raise HTTPException(status_code=400, detail="链接太多了，请留在 20 条以内。")
    if any(len(link.url) > 2000 for link in links) or sum(len(link.url) for link in links) > 8000:
        raise HTTPException(status_code=400, detail="链接太长了，请缩短后再生成。")
    audience = request.audience
    if audience is not None and len(str(audience.who or "").strip()) > 400:
        raise HTTPException(status_code=400, detail="读者说明太长了，请缩短后再生成。")
    source = request.source.model_copy(update={
        "title": title,
        "summary": summary,
        "materials": materials,
        "links": links,
    })
    updates = {"source": source}
    if audience is not None:
        updates["audience"] = audience.model_copy(update={"who": str(audience.who or "").strip()})
    return request.model_copy(update=updates)


@app.post("/v1/xhs/jobs")
def start_job(request: SocialContentRequest) -> dict[str, str]:
    request = _prepare_request(request)
    job_id = (request.request_id or "").strip() or f"web-{os.urandom(5).hex()}"
    request = request.model_copy(update={"request_id": job_id})
    with _jobs_lock:
        _expire_locked()
        existing = _jobs.get(job_id)
        if existing and existing["status"] == "running":
            return {
                "job_id": job_id,
                "status": "running",
                "stage": existing.get("stage") or "brief",
                "message": existing.get("message") or _STAGE_TEXT["brief"],
            }
        if any(job.get("status") == "running" for job in _jobs.values()):
            raise HTTPException(status_code=409, detail="已经有一份文案在生成，请等它完成后再试。")
        _remember_job(job_id, {
            "status": "running",
            "stage": "brief",
            "message": _STAGE_TEXT["brief"],
            "response": None,
            "started_at": time.time(),
        })
    threading.Thread(target=_run_job, args=(job_id, request), daemon=True).start()
    return {"job_id": job_id, "status": "running", "stage": "brief", "message": _STAGE_TEXT["brief"]}


@app.get("/v1/xhs/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with _jobs_lock:
        _expire_locked()
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {"job_id": job_id, **job}


@app.post("/v1/xhs/packages", response_model=SocialContentResponse)
def create_package(request: SocialContentRequest) -> SocialContentResponse:
    request = _prepare_request(request)
    try:
        return _execute(request)
    except Exception as exc:
        print(f"[xhs] package failed: {exc}", flush=True)
        return SocialContentResponse(
            request_id=request.request_id,
            status="failed",
            error=_public_job_error(exc),
        )


def _package_summary(response_path: Path) -> dict[str, str] | None:
    try:
        payload = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    package_id = str(data.get("package_id") or "").strip()
    note = data.get("xhs_payload") if isinstance(data.get("xhs_payload"), dict) else {}
    if not package_id:
        return None
    return {"package_id": package_id, "title": str(note.get("title") or "").strip()}


@app.get("/v1/xhs/packages")
def list_packages() -> dict[str, list[dict[str, str]]]:
    output_root = _output_root()
    packages: list[dict[str, str]] = []
    if output_root.is_dir():
        for response_path in sorted(output_root.glob("*/response.json"), reverse=True):
            item = _package_summary(response_path)
            if item is None:
                continue
            packages.append(item)
            if len(packages) >= 20:
                break
    return {"packages": packages}


@app.get("/v1/xhs/packages/{package_id}", response_model=SocialContentResponse)
def get_package(package_id: str) -> SocialContentResponse:
    response_path = _safe_package_dir(package_id) / "response.json"
    if not response_path.is_file():
        raise HTTPException(status_code=404, detail="package not found")
    return SocialContentResponse.model_validate_json(response_path.read_text(encoding="utf-8"))


@app.get("/v1/xhs/packages/{package_id}/files/{file_path:path}")
def get_package_file(package_id: str, file_path: str) -> FileResponse:
    package_dir = _safe_package_dir(package_id)
    target = (package_dir / file_path).resolve()
    if package_dir not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target)
