from __future__ import annotations

import io
import zipfile

from connect_hub.contracts import JobEvent
from connect_hub.reporting import (
    ReportHubClient,
    _public_event_type,
    build_daily_paper_site_archive,
    build_report_archive,
)


def test_site_chunk_client_sends_small_numbered_parts(monkeypatch):
    client = ReportHubClient("https://reports.test", "x" * 40)
    calls = []

    def fake_request(method, path, data, *, content_type, extra_headers=None):
        calls.append((method, path, data, extra_headers))
        completed = len(calls) == 3
        return (
            '{"accepted":true,"completed":%s,"public_url":"%s"}'
            % ("true" if completed else "false", "https://reports.test/s/token/" if completed else "")
        ).encode()

    monkeypatch.setattr(client, "_request", fake_request)
    url = client._upload_site_chunks("daily-site", b"x" * (8 * 1024 * 1024 + 1))

    assert url == "https://reports.test/s/token/"
    assert len(calls) == 3
    assert all(len(item[2]) <= 4 * 1024 * 1024 for item in calls)
    assert all(item[3]["X-Chunk-SHA256"] for item in calls)


def test_build_report_archive_from_static_directory(tmp_path):
    report = tmp_path / "report"
    report.mkdir()
    (report / "index.html").write_text("<h1>done</h1>", encoding="utf-8")
    (report / "asset.css").write_text("body{}", encoding="utf-8")

    payload = build_report_archive(report)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["asset.css", "index.html"]
        assert archive.read("index.html") == b"<h1>done</h1>"


def test_build_report_archive_renders_markdown_as_mobile_html(tmp_path):
    report = tmp_path / "daily.md"
    report.write_text("# <日报>", encoding="utf-8")

    with zipfile.ZipFile(io.BytesIO(build_report_archive(report))) as archive:
        html = archive.read("index.html").decode()
    assert "viewport" in html
    assert "&lt;日报&gt;" in html


def test_daily_site_archive_contains_real_site_and_public_config(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "index.html").write_text("<html><head></head></html>", encoding="utf-8")
    (tmp_path / "app" / "main.js").write_text("ok", encoding="utf-8")
    (tmp_path / "docs" / "README.md").write_text("日报", encoding="utf-8")
    (tmp_path / "private.py").write_text("secret", encoding="utf-8")

    with zipfile.ZipFile(io.BytesIO(build_daily_paper_site_archive(tmp_path))) as archive:
        names = archive.namelist()
        html = archive.read("index.html").decode()
    assert names == ["index.html", "app/main.js", "docs/README.md"]
    assert "DPR_LOCAL_API_BASE" in html
    assert "data-research-connect-site-base" in html
    assert "base.href = match[1] + '/'" in html
    assert "DPR_PUBLIC_READ_ONLY" not in html
    assert "private.py" not in names


def test_public_event_mapping_omits_internal_cost_and_artifact_events():
    assert _public_event_type("job.accepted") == "job.started"
    assert _public_event_type("job.interrupted") == "job.failed"
    assert _public_event_type("job.cost") == ""
    assert _public_event_type("job.artifact") == ""


def test_job_error_payload_is_report_hub_compatible():
    event = JobEvent(
        event_id="evt-1",
        job_id="job-1",
        event_type="job.failed",
        message="failed",
        payload={"error_code": "PROVIDER_RATE_LIMITED"},
    )
    assert event.payload["error_code"] == "PROVIDER_RATE_LIMITED"
