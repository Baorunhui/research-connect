from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import secrets
import shutil
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .archive import InvalidReportArchive, install_report_zip
from .config import Settings
from .provider_config import (
    catalog_payload,
    merge_public_update,
    probe_provider,
    public_config,
)
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


class SiteCreate(BaseModel):
    site_id: str
    module_name: Literal["daily-paper", "citationclaw", "xhs-agent", "other"]
    title: str = Field(min_length=1, max_length=300)


class SiteRunUpdate(BaseModel):
    run: dict[str, Any]
    log: str = Field(default="", max_length=100_000)


class SiteConfigUpdate(BaseModel):
    config: dict[str, Any]


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

    def resolve_site(site_id: str) -> dict[str, Any]:
        site = storage.get_site(site_id=site_id)
        if not site:
            raise HTTPException(status_code=404, detail="site not found")
        return site

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

    @app.post("/api/v1/sites", status_code=201, dependencies=[Depends(require_agent)])
    def create_site(body: SiteCreate) -> dict[str, Any]:
        if not JOB_ID_PATTERN.fullmatch(body.site_id):
            raise HTTPException(status_code=422, detail="invalid site_id")
        existing = storage.get_site(site_id=body.site_id)
        site = existing or storage.create_site(
            site_id=body.site_id,
            public_token=secrets.token_urlsafe(24),
            module_name=body.module_name,
            title=body.title,
        )
        return _site_response(site, settings)

    @app.put("/api/v1/sites/{site_id}/report", dependencies=[Depends(require_agent)])
    async def upload_site(site_id: str, request: Request) -> dict[str, Any]:
        site = resolve_site(site_id)
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content-length") from exc
        max_upload = settings.max_upload_mb * 1024 * 1024
        if content_length > max_upload:
            raise HTTPException(status_code=413, detail="site upload is too large")
        data = await request.body()
        if len(data) > max_upload:
            raise HTTPException(status_code=413, detail="site upload is too large")
        try:
            size = install_report_zip(
                data,
                storage.site_dir / site_id,
                settings.max_expanded_mb * 1024 * 1024,
            )
        except InvalidReportArchive as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        storage.mark_site_ready(site_id)
        return {
            "accepted": True,
            "size_bytes": size,
            "public_url": f"{settings.public_base_url}/s/{site['public_token']}/",
        }

    @app.put(
        "/api/v1/sites/{site_id}/uploads/{upload_id}/parts/{part_number}",
        dependencies=[Depends(require_agent)],
    )
    async def upload_site_part(
        site_id: str,
        upload_id: str,
        part_number: int,
        total_parts: int,
        request: Request,
        x_chunk_sha256: str = Header(default=""),
    ) -> dict[str, Any]:
        site = resolve_site(site_id)
        if not JOB_ID_PATTERN.fullmatch(upload_id):
            raise HTTPException(status_code=422, detail="invalid upload_id")
        if total_parts < 1 or total_parts > 4096 or part_number < 0 or part_number >= total_parts:
            raise HTTPException(status_code=422, detail="invalid chunk coordinates")
        max_chunk = min(settings.max_upload_mb * 1024 * 1024, 8 * 1024 * 1024)
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content-length") from exc
        if content_length > max_chunk:
            raise HTTPException(status_code=413, detail="site chunk is too large")
        data = await request.body()
        if len(data) > max_chunk:
            raise HTTPException(status_code=413, detail="site chunk is too large")
        digest = hashlib.sha256(data).hexdigest()
        if x_chunk_sha256 and not secrets.compare_digest(x_chunk_sha256.lower(), digest):
            raise HTTPException(status_code=422, detail="chunk checksum mismatch")

        upload_dir = settings.data_dir / "site_uploads" / site_id / upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        destination = upload_dir / f"{part_number:08d}.part"
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(destination)
        received = sum(1 for path in upload_dir.glob("*.part") if path.is_file())
        if received < total_parts:
            return {
                "accepted": True,
                "completed": False,
                "received_parts": received,
                "total_parts": total_parts,
            }

        expected = [upload_dir / f"{index:08d}.part" for index in range(total_parts)]
        if not all(path.is_file() for path in expected):
            raise HTTPException(status_code=409, detail="site upload has missing chunks")
        total_size = sum(path.stat().st_size for path in expected)
        if total_size > settings.max_upload_mb * 1024 * 1024:
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise HTTPException(status_code=413, detail="site upload is too large")
        archive_data = b"".join(path.read_bytes() for path in expected)
        try:
            size = install_report_zip(
                archive_data,
                storage.site_dir / site_id,
                settings.max_expanded_mb * 1024 * 1024,
            )
        except InvalidReportArchive as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            shutil.rmtree(upload_dir, ignore_errors=True)
        storage.mark_site_ready(site_id)
        return {
            "accepted": True,
            "completed": True,
            "received_parts": total_parts,
            "total_parts": total_parts,
            "size_bytes": size,
            "public_url": f"{settings.public_base_url}/s/{site['public_token']}/",
        }

    @app.put(
        "/api/v1/sites/{site_id}/runs/{run_id}", dependencies=[Depends(require_agent)]
    )
    def update_site_run(site_id: str, run_id: str, body: SiteRunUpdate) -> dict[str, Any]:
        resolve_site(site_id)
        if not JOB_ID_PATTERN.fullmatch(run_id):
            raise HTTPException(status_code=422, detail="invalid run_id")
        actual_id = str(body.run.get("id") or "")
        if actual_id and actual_id != run_id:
            raise HTTPException(status_code=422, detail="run id mismatch")
        storage.upsert_site_run(
            site_id=site_id, run_id=run_id, run=body.run, log_text=body.log
        )
        return {"accepted": True, "run_id": run_id}

    @app.get("/api/v1/sites/{site_id}/config", dependencies=[Depends(require_agent)])
    def agent_site_config(site_id: str) -> dict[str, Any]:
        resolve_site(site_id)
        record = storage.get_site_config(site_id)
        return {
            "configured": record is not None,
            "config": (record or {}).get("config") or {},
            "updated_at": (record or {}).get("updated_at"),
        }

    @app.put("/api/v1/sites/{site_id}/config", dependencies=[Depends(require_agent)])
    def save_agent_site_config(site_id: str, body: SiteConfigUpdate) -> dict[str, Any]:
        resolve_site(site_id)
        storage.save_site_config(site_id, body.config)
        return {"accepted": True, "configured": True}

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

    @app.get("/s/{public_token}/api/local/runs")
    def public_site_runs(public_token: str) -> dict[str, Any]:
        site = storage.get_site(public_token=public_token)
        if not site:
            raise HTTPException(status_code=404, detail="site not found")
        return {"ok": True, "runs": storage.list_site_runs(site["site_id"])}

    @app.get("/s/{public_token}/api/local/runs/{run_id}/log")
    def public_site_run_log(public_token: str, run_id: str) -> dict[str, Any]:
        site = storage.get_site(public_token=public_token)
        if not site:
            raise HTTPException(status_code=404, detail="site not found")
        record = storage.get_site_run(site["site_id"], run_id)
        if not record:
            raise HTTPException(status_code=404, detail="run not found")
        return {"ok": True, **record}

    def public_site(public_token: str) -> dict[str, Any]:
        site = storage.get_site(public_token=public_token)
        if not site:
            raise HTTPException(status_code=404, detail="site not found")
        return site

    def public_config_payload(site: dict[str, Any]) -> dict[str, Any]:
        record = storage.get_site_config(site["site_id"])
        return dict((record or {}).get("config") or {})

    def configuration_site(public_token: str) -> dict[str, Any]:
        site = public_site(public_token)
        if site["module_name"] != "other":
            raise HTTPException(status_code=404, detail="configuration site not found")
        return site

    @app.get("/configure/{public_token}")
    def configuration_without_slash(public_token: str) -> Response:
        configuration_site(public_token)
        return Response(status_code=307, headers={"Location": f"/configure/{public_token}/"})

    @app.get("/configure/{public_token}/", response_class=HTMLResponse)
    def configuration_page(public_token: str) -> str:
        configuration_site(public_token)
        return (static_dir / "config.html").read_text(encoding="utf-8")

    @app.get("/configure/{public_token}/api/catalog")
    def configuration_catalog(public_token: str) -> dict[str, Any]:
        configuration_site(public_token)
        return catalog_payload()

    @app.get("/configure/{public_token}/api/config")
    def configuration_snapshot(public_token: str) -> dict[str, Any]:
        site = configuration_site(public_token)
        return public_config(public_config_payload(site))

    @app.post("/configure/{public_token}/api/config")
    def save_configuration(public_token: str, body: dict[str, Any]) -> dict[str, Any]:
        site = configuration_site(public_token)
        merged = merge_public_update(public_config_payload(site), body)
        storage.save_site_config(site["site_id"], merged)
        return {"ok": True, "configured": True, "config": public_config(merged)}

    @app.post("/configure/{public_token}/api/probe/{provider_id:path}")
    def probe_configuration_provider(
        public_token: str, provider_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        site = configuration_site(public_token)
        update = {"providers": {provider_id: dict(body.get("provider") or {})}}
        effective = merge_public_update(public_config_payload(site), update)
        return probe_provider(provider_id, effective)

    @app.get("/s/{public_token}/api/local/config/structured")
    def public_daily_config(public_token: str) -> dict[str, Any]:
        site = public_site(public_token)
        config = public_config_payload(site)
        local = dict(config.get("local") or {}) if isinstance(config.get("local"), dict) else {}
        for key in ("subscriptions", "recommend_setting"):
            if key in config:
                local[key] = config[key]
        return {
            "ok": True,
            "configured": storage.get_site_config(site["site_id"]) is not None,
            "local": _redact(local),
        }

    @app.post("/s/{public_token}/api/local/config/partial")
    def save_public_daily_config(public_token: str, body: dict[str, Any]) -> dict[str, Any]:
        site = public_site(public_token)
        current = public_config_payload(site)
        merged = _merge_config(current, body)
        storage.save_site_config(site["site_id"], merged)
        return {"ok": True, "configured": True}

    @app.post("/s/{public_token}/api/local/chat/models")
    def public_daily_chat_models(
        public_token: str, body: dict[str, Any]
    ) -> Any:
        site = public_site(public_token)
        try:
            base_url, api_key, _model = _public_chat_credentials(
                public_config_payload(site), body
            )
            request = urllib.request.Request(
                _openai_endpoint(base_url, "models"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "research-connect-report-hub/0.1",
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            items = payload.get("data") if isinstance(payload, dict) else []
            models = sorted(
                {
                    str(item.get("id") or "").strip()
                    for item in (items or [])
                    if isinstance(item, dict) and str(item.get("id") or "").strip()
                }
            )
            if not models:
                raise ValueError("端点返回了空模型列表")
            return {"ok": True, "models": models, "count": len(models)}
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            return JSONResponse(
                {"ok": False, "error": f"拉取模型列表失败：{_upstream_error(exc)}"},
                status_code=502,
            )

    @app.post("/s/{public_token}/api/local/chat/test")
    def public_daily_chat_test(
        public_token: str, body: dict[str, Any]
    ) -> Any:
        site = public_site(public_token)
        try:
            base_url, api_key, model = _public_chat_credentials(
                public_config_payload(site), body
            )
            if not model:
                raise ValueError("请先填写模型名称")
            payload = json.dumps(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 8,
                    "temperature": 0,
                    "stream": False,
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                _openai_endpoint(base_url, "chat/completions"),
                data=payload,
                method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "research-connect-report-hub/0.1",
                },
            )
            started = time.monotonic()
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8", errors="replace"))
            latency_ms = int((time.monotonic() - started) * 1000)
            choices = result.get("choices") if isinstance(result, dict) else []
            content = ""
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    content = str(message.get("content") or "").strip()[:40]
            return {
                "ok": True,
                "model": model,
                "latency_ms": latency_ms,
                "reply": content,
            }
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            return JSONResponse(
                {"ok": False, "error": f"连接失败：{_upstream_error(exc)}"},
                status_code=502,
            )

    @app.get("/s/{public_token}/api/config")
    def public_citation_config(public_token: str) -> dict[str, Any]:
        site = public_site(public_token)
        return _redact(public_config_payload(site))

    @app.post("/s/{public_token}/api/config")
    def save_public_citation_config(
        public_token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        site = public_site(public_token)
        current = public_config_payload(site)
        storage.save_site_config(site["site_id"], _merge_config(current, body))
        return {"status": "success", "message": "配置已保存", "configured": True}

    @app.get("/s/{public_token}")
    def site_without_slash(public_token: str) -> Response:
        site = storage.get_site(public_token=public_token)
        if not site or not site["report_ready"]:
            raise HTTPException(status_code=404, detail="site not ready")
        return Response(status_code=307, headers={"Location": f"/s/{public_token}/"})

    @app.get("/s/{public_token}/{asset_path:path}")
    def site_asset(public_token: str, asset_path: str) -> Response:
        site = storage.get_site(public_token=public_token)
        if not site or not site["report_ready"]:
            raise HTTPException(status_code=404, detail="site not ready")
        root = (storage.site_dir / site["site_id"]).resolve()
        relative = asset_path or "index.html"
        path = (root / relative).resolve()
        if root not in path.parents and path != root:
            raise HTTPException(status_code=404)
        if not path.is_file():
            raise HTTPException(status_code=404)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = FileResponse(path, media_type=media_type)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

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


def _site_response(site: dict[str, Any], settings: Settings) -> dict[str, Any]:
    return {
        "site_id": site["site_id"],
        "report_ready": bool(site["report_ready"]),
        "public_url": f"{settings.public_base_url}/s/{site['public_token']}/",
    }


def _is_secret_field(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return any(part in normalized for part in ("api_key", "token", "secret", "password"))


def _redact(value: Any, key: str = "") -> Any:
    if key and _is_secret_field(key):
        if isinstance(value, list):
            return []
        return ""
    if isinstance(value, dict):
        return {name: _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _merge_config(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_config(merged[key], value)
        elif _is_secret_field(str(key)) and (value == "" or value == []):
            # Public forms deliberately do not echo secrets. An empty field means
            # "keep the existing value", not "erase it".
            if key not in merged:
                merged[key] = value
        else:
            merged[key] = value
    return merged


def _public_chat_credentials(
    config: dict[str, Any], request: dict[str, Any]
) -> tuple[str, str, str]:
    local = config.get("local") if isinstance(config.get("local"), dict) else {}
    saved = local.get("chat") if isinstance(local.get("chat"), dict) else {}
    base_url = str(request.get("base_url") or saved.get("base_url") or "").strip()
    api_key = str(request.get("api_key") or saved.get("api_key") or "").strip()
    model = str(request.get("model") or saved.get("model") or "").strip()
    if not base_url:
        raise ValueError("缺少 API 端点，请先填写 OpenAI 兼容端点")
    if not re.match(r"^https?://", base_url, re.IGNORECASE):
        raise ValueError("API 端点必须以 http:// 或 https:// 开头")
    if not api_key:
        raise ValueError("未配置 API Key")
    return base_url, api_key, model


def _openai_endpoint(base_url: str, suffix: str) -> str:
    base = str(base_url).strip().rstrip("/")
    if base.lower().endswith("/chat/completions"):
        base = base[: -len("/chat/completions")].rstrip("/")
    if not re.search(r"/v\d+$", base, re.IGNORECASE):
        base += "/v1"
    return f"{base}/{suffix.lstrip('/')}"


def _upstream_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"上游返回 HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)[:200]
    return str(exc)[:200]
