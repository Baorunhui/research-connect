from __future__ import annotations

import html
import base64
import hashlib
import io
import json
import mimetypes
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from connect_hub.contracts import JobEvent


class ReportHubError(RuntimeError):
    """A public Report Hub request failed."""


SITE_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024


class ReportHubClient:
    """Small dependency-free client for the public Report Hub v1 API."""

    def __init__(self, api_url: str, agent_token: str, *, timeout_seconds: int = 10) -> None:
        self.api_url = api_url.strip().rstrip("/")
        self.agent_token = agent_token.strip()
        self.timeout_seconds = max(2, int(timeout_seconds))

    @property
    def configured(self) -> bool:
        return bool(self.api_url and self.agent_token)

    def create_job(self, *, job_id: str, module_name: str, title: str) -> str:
        normalized_module = module_name if module_name in {
            "daily-paper", "citationclaw", "xhs-agent", "other"
        } else "other"
        response = self._json_request(
            "POST",
            "/api/v1/jobs",
            {
                "job_id": job_id,
                "module_name": normalized_module,
                "title": title[:300] or "Research Connect 任务",
            },
        )
        public_url = str(response.get("public_url") or "").strip()
        if not public_url:
            raise ReportHubError("REPORT_PUBLISH_FAILED: create response has no public_url")
        return public_url

    def send_event(self, event: JobEvent) -> None:
        event_type = _public_event_type(event.event_type)
        if not event_type:
            return
        message = event.message.strip() or event_type
        self._json_request(
            "POST",
            f"/api/v1/jobs/{event.job_id}/events",
            {
                "event_id": event.event_id,
                "event_type": event_type,
                "stage": event.stage or None,
                "message": message[:2000],
                "current": event.current,
                "total": event.total,
                "payload": dict(event.payload),
            },
        )

    def upload_report(self, job_id: str, source: str | Path) -> str:
        archive = build_report_archive(Path(source))
        response = self._request(
            "PUT",
            f"/api/v1/jobs/{job_id}/report",
            archive,
            content_type="application/zip",
        )
        try:
            payload = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReportHubError("REPORT_PUBLISH_FAILED: invalid upload response") from exc
        return str(payload.get("public_url") or "").strip()

    def ensure_site(self, *, site_id: str, module_name: str, title: str) -> tuple[str, bool]:
        normalized_module = module_name if module_name in {
            "daily-paper", "citationclaw", "xhs-agent", "other"
        } else "other"
        response = self._json_request(
            "POST",
            "/api/v1/sites",
            {
                "site_id": site_id,
                "module_name": normalized_module,
                "title": title[:300] or "Research Connect 站点",
            },
        )
        public_url = str(response.get("public_url") or "").strip()
        if not public_url:
            raise ReportHubError("REPORT_PUBLISH_FAILED: site response has no public_url")
        return public_url, bool(response.get("report_ready"))

    def upload_site(
        self, site_id: str, project_dir: str | Path, *, site_kind: str = "daily-paper"
    ) -> str:
        archive = (
            build_citationclaw_site_archive(Path(project_dir))
            if site_kind == "citationclaw"
            else build_daily_paper_site_archive(Path(project_dir))
        )
        if len(archive) > SITE_UPLOAD_CHUNK_BYTES:
            try:
                return self._upload_site_chunks(site_id, archive)
            except ReportHubError as exc:
                # One-release compatibility with an older public server. Once the
                # server update is deployed, large sites never use this path.
                if "status=404" not in str(exc):
                    raise
        response = self._request(
            "PUT", f"/api/v1/sites/{site_id}/report", archive,
            content_type="application/zip",
        )
        try:
            payload = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReportHubError("REPORT_PUBLISH_FAILED: invalid site upload response") from exc
        return str(payload.get("public_url") or "").strip()

    def _upload_site_chunks(self, site_id: str, archive: bytes) -> str:
        upload_id = uuid.uuid4().hex
        total = (len(archive) + SITE_UPLOAD_CHUNK_BYTES - 1) // SITE_UPLOAD_CHUNK_BYTES
        public_url = ""
        for index in range(total):
            chunk = archive[
                index * SITE_UPLOAD_CHUNK_BYTES:(index + 1) * SITE_UPLOAD_CHUNK_BYTES
            ]
            path = (
                f"/api/v1/sites/{site_id}/uploads/{upload_id}/parts/{index}"
                f"?total_parts={total}"
            )
            response = b""
            for attempt in range(3):
                try:
                    response = self._request(
                        "PUT", path, chunk,
                        content_type="application/octet-stream",
                        extra_headers={"X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
                    )
                    break
                except ReportHubError:
                    if attempt == 2:
                        raise
            try:
                payload = json.loads(response.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReportHubError(
                    "REPORT_PUBLISH_FAILED: invalid chunk upload response"
                ) from exc
            public_url = str(payload.get("public_url") or public_url).strip()
        if not public_url:
            raise ReportHubError("REPORT_PUBLISH_FAILED: chunk upload did not complete")
        return public_url

    def update_site_run(
        self, site_id: str, run_id: str, run: Mapping[str, Any], log_text: str
    ) -> None:
        self._json_request(
            "PUT",
            f"/api/v1/sites/{site_id}/runs/{run_id}",
            {"run": dict(run), "log": log_text[-100_000:]},
        )

    def get_site_config(self, site_id: str) -> Mapping[str, Any]:
        return self._json_request("GET", f"/api/v1/sites/{site_id}/config", None)

    def put_site_config(self, site_id: str, config: Mapping[str, Any]) -> None:
        self._json_request("PUT", f"/api/v1/sites/{site_id}/config", {"config": dict(config)})

    def next_site_command(self, site_id: str) -> Mapping[str, Any] | None:
        response = self._json_request("GET", f"/api/v1/sites/{site_id}/commands/next", None)
        command = response.get("command")
        return command if isinstance(command, Mapping) else None

    def complete_site_command(
        self, site_id: str, command_id: str, *, status_code: int,
        headers: Mapping[str, str], body: bytes, error_message: str = "",
    ) -> None:
        self._json_request(
            "POST", f"/api/v1/sites/{site_id}/commands/{command_id}/complete",
            {"status_code": status_code, "headers": dict(headers),
             "body_b64": base64.b64encode(body).decode("ascii"),
             "error_message": error_message},
        )

    def configuration_url(self, site_public_url: str) -> str:
        """Turn a stable-site bearer URL into its unified configuration URL."""

        parsed = urlparse(site_public_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[-2] != "s":
            raise ReportHubError("REPORT_PUBLISH_FAILED: invalid stable site URL")
        return f"{parsed.scheme}://{parsed.netloc}/configure/{parts[-1]}/"

    def _json_request(
        self, method: str, path: str, payload: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        raw = self._request(
            method,
            path,
            None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReportHubError("REPORT_HUB_UNAVAILABLE: invalid JSON response") from exc
        if not isinstance(decoded, Mapping):
            raise ReportHubError("REPORT_HUB_UNAVAILABLE: unexpected response")
        return decoded

    def _request(
        self, method: str, path: str, data: bytes | None, *, content_type: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> bytes:
        if not self.configured:
            raise ReportHubError("REPORT_HUB_NOT_CONFIGURED")
        headers = {
            "Authorization": f"Bearer {self.agent_token}",
            "Content-Type": content_type,
            "User-Agent": "research-connect/0.2",
        }
        headers.update(dict(extra_headers or {}))
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace")
            raise ReportHubError(
                f"REPORT_HUB_HTTP_ERROR: status={exc.code} detail={detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReportHubError(f"REPORT_HUB_UNAVAILABLE: {exc}") from exc


def build_report_archive(source: Path) -> bytes:
    """Build a Report Hub archive with an index.html at its root."""
    source = source.resolve()
    if not source.exists():
        raise ReportHubError(f"REPORT_ARTIFACT_MISSING: {source}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if source.is_dir():
            index = source / "index.html"
            if not index.is_file():
                raise ReportHubError(f"REPORT_ARTIFACT_INVALID: {source} has no index.html")
            for path in sorted(source.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    archive.write(path, path.relative_to(source).as_posix())
        elif source.suffix.lower() in {".html", ".htm"}:
            archive.write(source, "index.html")
            _add_sibling_assets(archive, source)
        elif source.suffix.lower() in {".md", ".txt"}:
            body = html.escape(source.read_text(encoding="utf-8", errors="replace"))
            document = (
                "<!doctype html><meta charset='utf-8'><meta name='viewport' "
                "content='width=device-width,initial-scale=1'><title>Research Connect</title>"
                "<style>body{max-width:920px;margin:auto;padding:20px;font:16px/1.65 system-ui;}"
                "pre{white-space:pre-wrap;word-break:break-word}</style><pre>"
                + body
                + "</pre>"
            )
            archive.writestr("index.html", document.encode("utf-8"))
        else:
            raise ReportHubError(f"REPORT_ARTIFACT_INVALID: unsupported report {source.name}")
    return buffer.getvalue()


def build_daily_paper_site_archive(project_dir: Path) -> bytes:
    """Package Daily Paper's real Docsify site, excluding Python/source/cache files."""
    project_dir = project_dir.resolve()
    index = project_dir / "index.html"
    if not index.is_file() or not (project_dir / "app").is_dir() or not (project_dir / "docs").is_dir():
        raise ReportHubError(
            f"REPORT_ARTIFACT_INVALID: {project_dir} is not a Daily Paper site"
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        index_text = index.read_text(encoding="utf-8", errors="replace")
        marker = """<script>
    (function () {
      var match = String(window.location.pathname || '').match(/^(\/s\/[^/]+)/);
      if (match) window.DPR_LOCAL_API_BASE = match[1];
    })();
  </script>
"""
        index_text = index_text.replace("</head>", marker + "</head>", 1)
        archive.writestr("index.html", index_text.encode("utf-8"))
        for directory_name in ("app", "docs"):
            directory = project_dir / directory_name
            for path in sorted(directory.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    archive.write(path, path.relative_to(project_dir).as_posix())
        nojekyll = project_dir / ".nojekyll"
        if nojekyll.is_file():
            archive.write(nojekyll, ".nojekyll")
    return buffer.getvalue()


def build_citationclaw_site_archive(project_dir: Path) -> bytes:
    """Package CitationClaw's original web UI for its stable public site."""
    project_dir = project_dir.resolve()
    package_dir = project_dir / "citationclaw"
    templates_dir = package_dir / "templates"
    static_dir = package_dir / "static"
    if not (templates_dir / "index.html").is_file() or not static_dir.is_dir():
        raise ReportHubError(
            f"REPORT_ARTIFACT_INVALID: {project_dir} is not a CitationClaw site"
        )
    try:
        from jinja2 import Environment, FileSystemLoader

        rendered = Environment(loader=FileSystemLoader(str(templates_dir))).get_template(
            "index.html"
        ).render(now=date.today().isoformat())
    except Exception as exc:
        raise ReportHubError(f"REPORT_ARTIFACT_INVALID: cannot render CitationClaw UI: {exc}") from exc
    marker = """<script>
    (function () {
      var match = String(window.location.pathname || '').match(/^(\/s\/[^/]+)/);
      if (match) window.CCR_PUBLIC_API_BASE = match[1];
    })();
  </script>
"""
    rendered = rendered.replace("</head>", marker + "</head>", 1)
    rendered = rendered.replace('href="/static/', 'href="static/')
    rendered = rendered.replace('src="/static/', 'src="static/')
    rendered = rendered.replace('href="/docs-assets/', 'href="docs-assets/')
    rendered = rendered.replace('src="/docs-assets/', 'src="docs-assets/')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", rendered.encode("utf-8"))
        for path in sorted(static_dir.rglob("*")):
            if path.is_file() and not path.is_symlink():
                archive.write(path, f"static/{path.relative_to(static_dir).as_posix()}")
        docs_assets = project_dir / "docs" / "assets"
        if docs_assets.is_dir():
            for path in sorted(docs_assets.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    archive.write(path, f"docs-assets/{path.relative_to(docs_assets).as_posix()}")
    return buffer.getvalue()


def _add_sibling_assets(archive: zipfile.ZipFile, source: Path) -> None:
    """Include small relative assets next to a generated standalone HTML report."""
    for path in sorted(source.parent.iterdir()):
        if path == source or path.is_symlink() or not path.is_file():
            continue
        media = mimetypes.guess_type(path.name)[0] or ""
        if media.startswith(("image/", "text/")) or path.suffix.lower() in {".css", ".js"}:
            archive.write(path, path.name)


def _public_event_type(event_type: str) -> str:
    return {
        "job.accepted": "job.started",
        "job.started": "job.started",
        "job.progress": "job.progress",
        "job.message": "job.message",
        "job.completed": "job.completed",
        "job.failed": "job.failed",
        "job.cancelled": "job.cancelled",
        "job.interrupted": "job.failed",
    }.get(event_type, "")
