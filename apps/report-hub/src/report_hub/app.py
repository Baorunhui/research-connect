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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .archive import InvalidReportArchive, install_report_zip
from .config import Settings
from .storage import Storage

JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SITE_CACHE_CONTROL = "no-cache, must-revalidate, max-age=0"


class SiteCreate(BaseModel):
    site_id: str
    module_name: Literal["daily-paper", "citationclaw", "xhs-agent", "other"]
    title: str = Field(min_length=1, max_length=300)
    command_policy: list[dict[str, str]] = Field(default_factory=list, max_length=128)


class SiteRunUpdate(BaseModel):
    run: dict[str, Any]
    log: str = Field(default="", max_length=100_000)


class SiteCommandComplete(BaseModel):
    status_code: int = Field(ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body_b64: str = ""
    error_message: str = ""


class InstallationRegistration(BaseModel):
    invite_code: str = Field(min_length=16, max_length=300)
    feishu_app_id: str = Field(min_length=6, max_length=120)
    username: str = Field(min_length=1, max_length=120)


@dataclass(frozen=True)
class AgentIdentity:
    install_id: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    storage = Storage(settings.data_dir)
    storage.initialize()
    app = FastAPI(title="Research Connect Report Hub", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.storage = storage

    def require_agent(authorization: str | None = Header(default=None)) -> AgentIdentity:
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise HTTPException(status_code=401, detail="invalid agent token")
        token = authorization[len(prefix):].strip()
        installation = storage.authenticate_installation(token)
        if not installation:
            raise HTTPException(status_code=401, detail="invalid agent token")
        return AgentIdentity(install_id=str(installation["install_id"]))

    def assert_owner(record: dict[str, Any], identity: AgentIdentity) -> None:
        if record.get("owner_install_id") != identity.install_id:
            raise HTTPException(status_code=403, detail="resource belongs to another installation")

    def resolve_site(site_id: str, identity: AgentIdentity) -> dict[str, Any]:
        site = storage.get_site(site_id=site_id)
        if not site:
            raise HTTPException(status_code=404, detail="site not found")
        assert_owner(site, identity)
        return site

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "report-hub", "version": "0.1.0"}

    @app.get("/", response_class=HTMLResponse)
    def root() -> str:
        return "<h1>Research Connect Report Hub</h1><p>Service is running.</p>"

    @app.post("/api/v1/installations/register", status_code=201)
    def register_installation(body: InstallationRegistration) -> dict[str, Any]:
        app_id = body.feishu_app_id.strip()
        if not app_id.startswith("cli_") or not re.fullmatch(r"cli_[A-Za-z0-9_-]+", app_id):
            raise HTTPException(status_code=422, detail="invalid_feishu_app_id")
        username = body.username.strip()
        if not username or any(ord(character) < 32 for character in username):
            raise HTTPException(status_code=422, detail="invalid_username")
        try:
            installation, token = storage.register_installation(
                invite_code=body.invite_code.strip(),
                feishu_app_id=app_id,
                label=username,
            )
        except ValueError as exc:
            code = str(exc)
            status = 409 if code == "feishu_app_already_registered" else 403
            raise HTTPException(status_code=status, detail=code) from exc
        return {
            "install_id": installation["install_id"],
            "agent_token": token,
            "api_url": settings.public_base_url,
        }

    @app.post("/api/v1/sites", status_code=201)
    def create_site(body: SiteCreate, identity: AgentIdentity = Depends(require_agent)) -> dict[str, Any]:
        if not JOB_ID_PATTERN.fullmatch(body.site_id):
            raise HTTPException(status_code=422, detail="invalid site_id")
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

    @app.get("/api/v1/installations/current/storage")
    def current_installation_storage(
        identity: AgentIdentity = Depends(require_agent),
    ) -> dict[str, Any]:
        return storage.installation_storage_summary(identity.install_id)

    @app.delete("/api/v1/installations/current/sites/{site_id}")
    def delete_current_installation_site(
        site_id: str, identity: AgentIdentity = Depends(require_agent),
    ) -> dict[str, Any]:
        site = resolve_site(site_id, identity)
        storage.delete_site(str(site["site_id"]), owner_install_id=identity.install_id)
        return {"deleted": True, "site_id": site_id}

    @app.delete("/api/v1/installations/current/data")
    def clear_current_installation_data(
        identity: AgentIdentity = Depends(require_agent),
    ) -> dict[str, Any]:
        result = storage.clear_installation_data(identity.install_id)
        return {"deleted": True, **result}

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
        response.headers["Cache-Control"] = SITE_CACHE_CONTROL
        return response

    @app.exception_handler(404)
    async def not_found(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=404)

    return app


def _site_response(site: dict[str, Any], settings: Settings) -> dict[str, Any]:
    return {
        "site_id": site["site_id"],
        "report_ready": bool(site["report_ready"]),
        "public_url": f"{settings.public_base_url}/s/{site['public_token']}/",
    }


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
