from __future__ import annotations

import io
import zipfile

from connect_hub.contracts import JobEvent
from connect_hub.reporting import build_report_archive, _public_event_type


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
