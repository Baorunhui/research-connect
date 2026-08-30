from __future__ import annotations

from connect_hub.adapters.citationclaw import CitationClawAdapter


def test_citationclaw_adapter_propagates_external_job_and_progress(monkeypatch):
    adapter = CitationClawAdapter("http://127.0.0.1:8000", poll_seconds=1)
    requests = []
    statuses = iter(
        [
            {
                "schema_version": "connect.job.v1",
                "external_job_id": "job-abc",
                "status": "running",
                "logs": [{"timestamp": "1", "message": "Phase 1 · 开始检索"}],
                "progress": {"current": 1, "total": 2, "percentage": 50},
            },
            {
                "schema_version": "connect.job.v1",
                "external_job_id": "job-abc",
                "status": "completed",
                "logs": [],
                "progress": {"current": 2, "total": 2, "percentage": 100},
                "result": {"dashboard": "/tmp/report.html"},
            },
        ]
    )

    def fake_request(method, path, payload):
        requests.append((method, path, payload))
        if path == "/api/run":
            return {"schema_version": "connect.job.v1", "status": "success"}
        return next(statuses)

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr("connect_hub.adapters.citationclaw.time.sleep", lambda _seconds: None)
    progress = []
    result = adapter.run(
        external_job_id="job-abc",
        papers=[{"title": "A paper", "aliases": []}],
        output_prefix="abc",
        on_progress=lambda message, **kwargs: progress.append((message, kwargs)),
        is_cancelled=lambda: False,
    )

    assert requests[0][2]["external_job_id"] == "job-abc"
    assert result["dashboard"] == "/tmp/report.html"
    assert any("Phase 1" in message for message, _kwargs in progress)
    assert any("100%" in message for message, _kwargs in progress)
