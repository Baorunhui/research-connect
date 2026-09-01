from __future__ import annotations

import asyncio
import base64
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .archive import InvalidReportArchive, install_report_zip
from .config import Settings
from .provider_config import (
    canonical_site_id,
    catalog_payload,
    citation_public_config,
    daily_public_config,
    merge_citation_update,
    merge_daily_update,
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
    command_policy: list[dict[str, str]] = Field(default_factory=list, max_length=128)


class SiteRunUpdate(BaseModel):
    run: dict[str, Any]
    log: str = Field(default="", max_length=100_000)


class SiteConfigUpdate(BaseModel):
    config: dict[str, Any]


class SiteCommandComplete(BaseModel):
    status_code: int = Field(ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body_b64: str = ""
    error_message: str = ""


@dataclass(frozen=True)
class AgentIdentity:
    install_id: str | None
    is_admin: bool = False


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

    def require_agent(authorization: str | None = Header(default=None)) -> AgentIdentity:
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise HTTPException(status_code=401, detail="invalid agent token")
        token = authorization[len(prefix):].strip()
        if secrets.compare_digest(token, settings.agent_token):
            return AgentIdentity(install_id=None, is_admin=True)
        installation = storage.authenticate_installation(token)
        if not installation:
            raise HTTPException(status_code=401, detail="invalid agent token")
        return AgentIdentity(install_id=str(installation["install_id"]))

    def assert_owner(record: dict[str, Any], identity: AgentIdentity) -> None:
        if identity.is_admin:
            return
        if not identity.install_id or record.get("owner_install_id") != identity.install_id:
            raise HTTPException(status_code=403, detail="resource belongs to another installation")

    def resolve_job(job_id: str, identity: AgentIdentity) -> dict[str, Any]:
        job = storage.get_job(job_id=job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        assert_owner(job, identity)
        return job

    def resolve_site(site_id: str, identity: AgentIdentity) -> dict[str, Any]:
        site = storage.get_site(site_id=site_id)
        if not site:
            raise HTTPException(status_code=404, detail="site not found")
        assert_owner(site, identity)
        return site

    def canonical_record(site: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
        canonical_id = canonical_site_id(str(site["site_id"]))
        canonical = storage.get_site_config(canonical_id)
        if canonical is not None:
            config = dict(canonical.get("config") or {})
            if str(site["site_id"]) == canonical_id:
                return canonical_id, config, True
            legacy = storage.get_site_config(str(site["site_id"]))
            migrations = config.get("module_config_imports") if isinstance(config.get("module_config_imports"), dict) else {}
            if legacy is not None and not migrations.get(str(site["site_id"])):
                legacy_config = dict(legacy.get("config") or {})
                if site["module_name"] == "daily-paper":
                    config = merge_daily_update(config, legacy_config)
                elif site["module_name"] == "citationclaw":
                    config = merge_citation_update(config, legacy_config)
                config = merge_public_update(config, {"module_config_imports": {str(site["site_id"]): True}})
                storage.save_site_config(canonical_id, config)
            return canonical_id, config, True
        # Upgrade path: an older deployment may only have per-module config.
        legacy = storage.get_site_config(str(site["site_id"]))
        return canonical_id, dict((legacy or {}).get("config") or {}), False

    def save_canonical(site: dict[str, Any], config: dict[str, Any]) -> None:
        canonical_id = canonical_site_id(str(site["site_id"]))
        if storage.get_site(site_id=canonical_id) is None:
            # Normally Connect Hub creates this site first. Keeping creation here
            # makes an upgraded standalone Report Hub self-healing.
            storage.create_site(
                site_id=canonical_id,
                public_token=secrets.token_urlsafe(24),
                module_name="other",
                title="Research Connect 配置中心",
                owner_install_id=site.get("owner_install_id"),
            )
        storage.save_site_config(canonical_id, config)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "report-hub", "version": "0.1.0"}

    @app.get("/", response_class=HTMLResponse)
    def root() -> str:
        return "<h1>Research Connect Report Hub</h1><p>Service is running.</p>"

    @app.post("/api/v1/jobs", status_code=201)
    def create_job(body: JobCreate, identity: AgentIdentity = Depends(require_agent)) -> dict[str, Any]:
        job_id = body.job_id or f"job-{uuid.uuid4().hex[:16]}"
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise HTTPException(status_code=422, detail="invalid job_id")
        existing = storage.get_job(job_id=job_id)
        if existing:
            assert_owner(existing, identity)
            return _job_response(existing, settings)
        token = secrets.token_urlsafe(24)
        job = storage.create_job(
            job_id=job_id, public_token=token, module_name=body.module_name, title=body.title,
            owner_install_id=identity.install_id,
        )
        return _job_response(job, settings)

    @app.post("/api/v1/sites", status_code=201)
    def create_site(body: SiteCreate, identity: AgentIdentity = Depends(require_agent)) -> dict[str, Any]:
        if not JOB_ID_PATTERN.fullmatch(body.site_id):
            raise HTTPException(status_code=422, detail="invalid site_id")
        existing = storage.get_site(site_id=body.site_id)
        if existing and existing.get("owner_install_id") is None and identity.install_id:
            storage.claim_unowned_site(body.site_id, identity.install_id)
            existing = storage.get_site(site_id=body.site_id)
        if existing:
            assert_owner(existing, identity)
            storage.update_site_command_policy(
                body.site_id, _validate_command_policy(body.command_policy)
            )
            existing = storage.get_site(site_id=body.site_id)
        site = existing or storage.create_site(
            site_id=body.site_id,
            public_token=secrets.token_urlsafe(24),
            module_name=body.module_name,
            title=body.title,
            owner_install_id=identity.install_id,
            command_policy=_validate_command_policy(body.command_policy),
        )
        return _site_response(site, settings)

    @app.put("/api/v1/sites/{site_id}/report")
    async def upload_site(site_id: str, request: Request, identity: AgentIdentity = Depends(require_agent)) -> dict[str, Any]:
        site = resolve_site(site_id, identity)
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
    )
    async def upload_site_part(
        site_id: str,
        upload_id: str,
        part_number: int,
        total_parts: int,
        request: Request,
        x_chunk_sha256: str = Header(default=""),
        identity: AgentIdentity = Depends(require_agent),
    ) -> dict[str, Any]:
        site = resolve_site(site_id, identity)
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
        "/api/v1/sites/{site_id}/runs/{run_id}"
    )
    def update_site_run(site_id: str, run_id: str, body: SiteRunUpdate, identity: AgentIdentity = Depends(require_agent)) -> dict[str, Any]:
        resolve_site(site_id, identity)
        if not JOB_ID_PATTERN.fullmatch(run_id):
            raise HTTPException(status_code=422, detail="invalid run_id")
        actual_id = str(body.run.get("id") or "")
        if actual_id and actual_id != run_id:
            raise HTTPException(status_code=422, detail="run id mismatch")
        storage.upsert_site_run(
            site_id=site_id, run_id=run_id, run=body.run, log_text=body.log
        )
        return {"accepted": True, "run_id": run_id}

    @app.get("/api/v1/sites/{site_id}/config")
    def agent_site_config(site_id: str, identity: AgentIdentity = Depends(require_agent)) -> dict[str, Any]:
        site = resolve_site(site_id, identity)
        canonical_id, config, configured = canonical_record(site)
        if site["module_name"] == "daily-paper":
            config = daily_public_config(config)
        elif site["module_name"] == "citationclaw":
            config = citation_public_config(config)
        record = storage.get_site_config(canonical_id)
        return {
            "configured": configured,
            "config": config,
            "updated_at": (record or {}).get("updated_at"),
        }

    @app.put("/api/v1/sites/{site_id}/config")
    def save_agent_site_config(site_id: str, body: SiteConfigUpdate, identity: AgentIdentity = Depends(require_agent)) -> dict[str, Any]:
        site = resolve_site(site_id, identity)
        _canonical_id, current, _configured = canonical_record(site)
        if site["module_name"] == "daily-paper":
            merged = merge_daily_update(current, body.config)
        elif site["module_name"] == "citationclaw":
            merged = merge_citation_update(current, body.config)
        else:
            merged = merge_public_update(current, body.config)
        save_canonical(site, merged)
        return {"accepted": True, "configured": True}

    @app.get("/api/v1/sites/{site_id}/commands/next")
    def next_site_command(site_id: str, identity: AgentIdentity = Depends(require_agent)) -> dict[str, Any]:
        resolve_site(site_id, identity)
        item = storage.claim_site_command(site_id)
        if not item:
            return {"command": None}
        return {"command": {
            "command_id": item["command_id"], "method": item["method"], "path": item["path"],
            "headers": json.loads(item["request_headers_json"] or "{}"),
            "body_b64": item["request_body_b64"],
        }}

    @app.post(
        "/api/v1/sites/{site_id}/commands/{command_id}/complete",
    )
    def finish_site_command(site_id: str, command_id: str, body: SiteCommandComplete, identity: AgentIdentity = Depends(require_agent)) -> dict[str, Any]:
        resolve_site(site_id, identity)
        item = storage.get_site_command(command_id)
        if not item or item["site_id"] != site_id:
            raise HTTPException(status_code=404, detail="command not found")
        storage.complete_site_command(
            command_id, status_code=body.status_code, headers=body.headers,
            body_b64=body.body_b64, error_message=body.error_message,
        )
        return {"accepted": True}

    @app.post("/api/v1/jobs/{job_id}/events")
    async def append_event(job_id: str, body: EventCreate, identity: AgentIdentity = Depends(require_agent)) -> dict[str, Any]:
        job = resolve_job(job_id, identity)
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

    @app.put("/api/v1/jobs/{job_id}/report")
    async def upload_report(job_id: str, request: Request, identity: AgentIdentity = Depends(require_agent)) -> dict[str, Any]:
        job = resolve_job(job_id, identity)
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
        return canonical_record(site)[1]

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
        _canonical_id, config, configured = canonical_record(site)
        projected = daily_public_config(config)
        local = dict(projected.get("local") or {})
        for key in ("subscriptions", "recommend_setting", "source_backends", "supabase"):
            if key in projected:
                local[key] = projected[key]
        chat = local.get("chat") if isinstance(local.get("chat"), dict) else {}
        chat["api_key"] = ""
        local["chat"] = chat
        source_backends = local.get("source_backends") if isinstance(local.get("source_backends"), dict) else {}
        arxiv = source_backends.get("arxiv") if isinstance(source_backends.get("arxiv"), dict) else None
        if isinstance(arxiv, dict):
            arxiv["anon_key_configured"] = bool(
                arxiv.get("anon_key_configured") or str(arxiv.get("anon_key") or "").strip()
            )
            arxiv["anon_key"] = ""
        return {
            "ok": True,
            "configured": configured,
            "local": local,
        }

    @app.post("/s/{public_token}/api/local/config/partial")
    def save_public_daily_config(public_token: str, body: dict[str, Any]) -> dict[str, Any]:
        site = public_site(public_token)
        current = public_config_payload(site)
        merged = merge_daily_update(current, body)
        save_canonical(site, merged)
        return {"ok": True, "configured": True}

    @app.post("/s/{public_token}/api/local/chat/models")
    def public_daily_chat_models(
        public_token: str, body: dict[str, Any]
    ) -> Any:
        site = public_site(public_token)
        try:
            base_url, api_key, _model = _public_chat_credentials(
                daily_public_config(public_config_payload(site)), body
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
                daily_public_config(public_config_payload(site)), body
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
        projected = citation_public_config(public_config_payload(site))
        configured = dict(projected.get("_configured_secrets") or {})
        redacted = _redact(projected)
        redacted["_configured_secrets"] = configured
        return redacted

    @app.post("/s/{public_token}/api/config")
    def save_public_citation_config(
        public_token: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        site = public_site(public_token)
        current = public_config_payload(site)
        save_canonical(site, merge_citation_update(current, body))
        return {"status": "success", "message": "配置已保存", "configured": True}

    @app.api_route(
        "/s/{public_token}/api/{api_path:path}", methods=["GET", "POST", "DELETE"]
    )
    async def public_module_command(public_token: str, api_path: str, request: Request) -> Response:
        site = public_site(public_token)
        if not _site_command_allowed(site, request.method, api_path):
            raise HTTPException(status_code=403, detail="module command is not declared")
        raw = await request.body()
        # A Daily Paper PDF is base64-encoded inside JSON (50 MiB source limit),
        # while all other module commands should remain small.
        request_limit = (
            72 * 1024 * 1024
            if str(site.get("module_name") or "") == "daily-paper"
            and api_path == "paper/summarize"
            else 4 * 1024 * 1024
        )
        if len(raw) > request_limit:
            raise HTTPException(status_code=413, detail="request is too large")
        query = ("?" + request.url.query) if request.url.query else ""
        if api_path in {"run", "run/from-cache"} and raw:
            try:
                runtime = json.loads(raw.decode("utf-8"))
                if isinstance(runtime, dict):
                    current = public_config_payload(site)
                    save_canonical(site, merge_public_update(current, {
                        "runtime_defaults": {"citationclaw": runtime}
                    }))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        command_id = uuid.uuid4().hex
        storage.enqueue_site_command(
            command_id=command_id, site_id=site["site_id"], method=request.method,
            path=f"/api/{api_path}{query}",
            headers={"content-type": request.headers.get("content-type", "application/json")},
            body_b64=base64.b64encode(raw).decode("ascii"),
        )
        # Report chat may perform a search-model call followed by a summarizer
        # call. Keep the relay request bounded, but allow that documented chain
        # to finish instead of cutting it off at the former 140-second limit.
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            item = storage.get_site_command(command_id)
            if item and item["status"] in {"completed", "failed"}:
                if item["status"] == "failed":
                    storage.delete_site_command(command_id)
                    return JSONResponse(
                        {"status": "error", "message": item["error_message"] or "本机服务请求失败"},
                        status_code=item["response_status"] or 502,
                    )
                response_headers = json.loads(item["response_headers_json"] or "{}")
                content_type = response_headers.get("content-type", "application/json")
                response_body = base64.b64decode(item["response_body_b64"] or "")
                response_status = item["response_status"] or 200
                storage.delete_site_command(command_id)
                return Response(
                    content=response_body,
                    status_code=response_status,
                    media_type=content_type.split(";", 1)[0],
                )
            await asyncio.sleep(0.25)
        storage.delete_queued_site_command(command_id)
        return JSONResponse(
            {"status": "error", "message": "本机服务暂未响应，请确认 Connect Hub 正在运行"},
            status_code=504,
        )

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


_COMMAND_METHODS = {"GET", "POST", "DELETE"}
_COMMAND_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~!$&'()+,;=:@%/-]{0,255}(?:/\*)?$")


def _validate_command_policy(
    policy: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Normalize a site-owned module command policy.

    Paths are relative to ``/api/`` and support only exact matches or a trailing
    ``/*`` prefix match.  Keeping the grammar deliberately small prevents a
    module from registering ambiguous regexes or path traversal patterns.
    """

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in policy:
        method = str(raw.get("method") or "").strip().upper()
        path = str(raw.get("path") or "").strip().strip("/")
        if method not in _COMMAND_METHODS:
            raise HTTPException(status_code=422, detail=f"invalid command method: {method}")
        if (
            not path
            or ".." in path.split("/")
            or "\\" in path
            or not _COMMAND_PATH_RE.fullmatch(path)
        ):
            raise HTTPException(status_code=422, detail=f"invalid command path: {path}")
        key = (method, path)
        if key not in seen:
            seen.add(key)
            normalized.append({"method": method, "path": path})
    return normalized


def _site_command_allowed(site: dict[str, Any], method: str, path: str) -> bool:
    try:
        policy = json.loads(str(site.get("command_policy_json") or "[]"))
    except json.JSONDecodeError:
        return False
    verb = str(method or "").upper()
    target = str(path or "").strip("/")
    for rule in policy if isinstance(policy, list) else []:
        if not isinstance(rule, dict) or str(rule.get("method") or "").upper() != verb:
            continue
        declared = str(rule.get("path") or "").strip("/")
        if declared.endswith("/*"):
            prefix = declared[:-1]
            if target.startswith(prefix):
                return True
        elif target == declared:
            return True
    return False
