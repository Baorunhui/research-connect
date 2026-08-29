from __future__ import annotations

import asyncio
import mimetypes
import re
import secrets
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .archive import InvalidReportArchive, install_report_zip
from .config import Settings
from .storage import Storage

JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class JobCreate(BaseModel):
    job_id: str | None = None
    module_name: Literal["daily-paper", "citationclaw", "xhs-agent", "other"]
    title: str = Field(min_length=1, max_length=300)


class EventCreate(BaseModel):
    event_id: str | None = None
    event_type: Literal[
        "job.started", "job.progress", "job.message", "job.completed", "job.failed", "job.cancelled"
    ] = "job.progress"
    stage: str | None = Field(default=None, max_length=120)
    message: str = Field(min_length=1, max_length=2000)
    current: float | None = None
    total: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class Broker:
    def __init__(self) -> None:
        self.connections: dict[str, set[WebSocket]] = defaultdict(set)
        self.lock = asyncio.Lock()

    async def connect(self, token: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self.lock:
            self.connections[token].add(websocket)

    async def disconnect(self, token: str, websocket: WebSocket) -> None:
        async with self.lock:
            self.connections[token].discard(websocket)

    async def publish(self, token: str, data: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for websocket in list(self.connections[token]):
            try:
                await websocket.send_json(data)
            except Exception:
                dead.append(websocket)
        if dead:
            async with self.lock:
                for websocket in dead:
                    self.connections[token].discard(websocket)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    storage = Storage(settings.data_dir)
    storage.initialize()
    broker = Broker()
    static_dir = Path(__file__).with_name("static")
    app = FastAPI(title="Research Connect Report Hub", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.storage = storage
    app.state.broker = broker

    def require_agent(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {settings.agent_token}"
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid agent token")

    def resolve_job(job_id: str) -> dict[str, Any]:
        job = storage.get_job(job_id=job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "report-hub", "version": "0.1.0"}

    @app.get("/", response_class=HTMLResponse)
    def root() -> str:
        return "<h1>Research Connect Report Hub</h1><p>Service is running.</p>"

    @app.post("/api/v1/jobs", status_code=201, dependencies=[Depends(require_agent)])
    def create_job(body: JobCreate) -> dict[str, Any]:
        job_id = body.job_id or f"job-{uuid.uuid4().hex[:16]}"
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise HTTPException(status_code=422, detail="invalid job_id")
        existing = storage.get_job(job_id=job_id)
        if existing:
            return _job_response(existing, settings)
        token = secrets.token_urlsafe(24)
        job = storage.create_job(
            job_id=job_id, public_token=token, module_name=body.module_name, title=body.title
        )
        return _job_response(job, settings)

    @app.post("/api/v1/jobs/{job_id}/events", dependencies=[Depends(require_agent)])
    async def append_event(job_id: str, body: EventCreate) -> dict[str, Any]:
        job = resolve_job(job_id)
        item, created = storage.append_event(
            job_id,
            {
                **body.model_dump(),
                "event_id": body.event_id or str(uuid.uuid4()),
            },
        )
        if created:
            await broker.publish(job["public_token"], {"type": "event", "event": item})
        return {"accepted": True, "duplicate": not created, "event": item}

    @app.put("/api/v1/jobs/{job_id}/report", dependencies=[Depends(require_agent)])
    async def upload_report(job_id: str, request: Request) -> dict[str, Any]:
        job = resolve_job(job_id)
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content-length") from exc
        max_upload = settings.max_upload_mb * 1024 * 1024
        if content_length > max_upload:
            raise HTTPException(status_code=413, detail="report upload is too large")
        data = await request.body()
        if len(data) > max_upload:
            raise HTTPException(status_code=413, detail="report upload is too large")
        target = storage.report_dir / job_id
        try:
            size = install_report_zip(data, target, settings.max_expanded_mb * 1024 * 1024)
        except InvalidReportArchive as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        storage.mark_report_ready(job_id, size)
        await broker.publish(job["public_token"], {"type": "report_ready"})
        return {
            "accepted": True,
            "size_bytes": size,
            "public_url": f"{settings.public_base_url}/r/{job['public_token']}",
        }

    @app.get("/api/v1/public/jobs/{public_token}")
    def public_snapshot(public_token: str) -> dict[str, Any]:
        snapshot = storage.snapshot(public_token)
        if not snapshot:
            raise HTTPException(status_code=404, detail="report not found")
        snapshot["report_url"] = (
            f"/reports/{public_token}/index.html" if snapshot["job"]["report_ready"] else None
        )
        snapshot["job"].pop("public_token", None)
        return snapshot

    @app.get("/r/{public_token}", response_class=HTMLResponse)
    def task_page(public_token: str) -> str:
        if not storage.get_job(public_token=public_token):
            raise HTTPException(status_code=404, detail="report not found")
        html = (static_dir / "task.html").read_text(encoding="utf-8")
        return html.replace("__PUBLIC_TOKEN__", public_token)

    @app.get("/assets/{filename}")
    def asset(filename: str) -> FileResponse:
        if filename not in {"task.css", "task.js"}:
            raise HTTPException(status_code=404)
        return FileResponse(static_dir / filename)

    @app.get("/reports/{public_token}/{asset_path:path}")
    def report_asset(public_token: str, asset_path: str) -> Response:
        job = storage.get_job(public_token=public_token)
        if not job or not job["report_ready"]:
            raise HTTPException(status_code=404, detail="report not ready")
        root = (storage.report_dir / job["job_id"]).resolve()
        path = (root / asset_path).resolve()
        if root not in path.parents and path != root:
            raise HTTPException(status_code=404)
        if not path.is_file():
            raise HTTPException(status_code=404)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = FileResponse(path, media_type=media_type)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.websocket("/ws/public/jobs/{public_token}")
    async def websocket_job(websocket: WebSocket, public_token: str) -> None:
        snapshot = storage.snapshot(public_token)
        if not snapshot:
            await websocket.close(code=4404)
            return
        await broker.connect(public_token, websocket)
        try:
            await websocket.send_json({"type": "snapshot", "snapshot": snapshot})
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await broker.disconnect(public_token, websocket)

    @app.exception_handler(404)
    async def not_found(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=404)

    return app


def _job_response(job: dict[str, Any], settings: Settings) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "public_url": f"{settings.public_base_url}/r/{job['public_token']}",
    }
