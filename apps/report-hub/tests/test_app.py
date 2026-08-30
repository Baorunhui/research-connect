from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient

from report_hub.app import create_app
from report_hub.config import Settings


TOKEN = "test-token-that-is-longer-than-thirty-two-characters"


def client(tmp_path) -> TestClient:
    settings = Settings(
        public_base_url="https://reports.example.test",
        agent_token=TOKEN,
        data_dir=tmp_path,
    )
    return TestClient(create_app(settings))


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def report_zip(filename: str = "index.html", content: bytes = b"<h1>done</h1>") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(filename, content)
    return output.getvalue()


def test_job_lifecycle_and_public_report(tmp_path):
    api = client(tmp_path)
    created = api.post(
        "/api/v1/jobs",
        headers=auth(),
        json={"job_id": "daily-001", "module_name": "daily-paper", "title": "3DVG 日报"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["public_url"].startswith("https://reports.example.test/r/")
    public_token = body["public_url"].rsplit("/", 1)[-1]

    progress = api.post(
        "/api/v1/jobs/daily-001/events",
        headers=auth(),
        json={
            "event_id": "event-1",
            "event_type": "job.progress",
            "stage": "search",
            "message": "正在检索论文",
            "current": 1,
            "total": 4,
        },
    )
    assert progress.status_code == 200
    assert progress.json()["duplicate"] is False
    duplicate = api.post(
        "/api/v1/jobs/daily-001/events",
        headers=auth(),
        json={
            "event_id": "event-1",
            "event_type": "job.progress",
            "message": "不会重复写入",
        },
    )
    assert duplicate.json()["duplicate"] is True

    uploaded = api.put(
        "/api/v1/jobs/daily-001/report",
        headers={**auth(), "Content-Type": "application/zip"},
        content=report_zip(),
    )
    assert uploaded.status_code == 200
    snapshot = api.get(f"/api/v1/public/jobs/{public_token}").json()
    assert snapshot["job"]["status"] == "completed"
    assert snapshot["job"]["report_ready"] == 1
    assert snapshot["events"][0]["message"] == "正在检索论文"
    assert api.get(f"/reports/{public_token}/index.html").text == "<h1>done</h1>"
    assert api.get(f"/r/{public_token}").status_code == 200


def test_agent_api_requires_token(tmp_path):
    api = client(tmp_path)
    response = api.post(
        "/api/v1/jobs", json={"module_name": "other", "title": "unauthorized"}
    )
    assert response.status_code == 401


def test_stable_site_keeps_url_and_exposes_runs_and_public_config(tmp_path):
    api = client(tmp_path)
    created = api.post(
        "/api/v1/sites",
        headers=auth(),
        json={"site_id": "daily-install-1", "module_name": "daily-paper", "title": "日报"},
    )
    assert created.status_code == 201
    site = created.json()
    assert site["public_url"].startswith("https://reports.example.test/s/")
    assert api.post(
        "/api/v1/sites",
        headers=auth(),
        json={"site_id": "daily-install-1", "module_name": "daily-paper", "title": "日报"},
    ).json()["public_url"] == site["public_url"]
    token = site["public_url"].rstrip("/").rsplit("/", 1)[-1]

    assert api.put(
        "/api/v1/sites/daily-install-1/report",
        headers=auth(),
        content=report_zip(content=b"<h1>Daily Paper Reader</h1>"),
    ).status_code == 200
    run = {
        "id": "run-001",
        "run_number": 1,
        "status": "in_progress",
        "conclusion": None,
        "created_at": "2026-08-30T00:00:00+00:00",
        "events": [],
    }
    assert api.put(
        "/api/v1/sites/daily-install-1/runs/run-001",
        headers=auth(),
        json={"run": run, "log": "Step 1"},
    ).status_code == 200
    assert api.get(f"/s/{token}/").text == "<h1>Daily Paper Reader</h1>"
    assert api.get(f"/s/{token}/api/local/runs").json()["runs"][0]["id"] == "run-001"
    assert api.get(f"/s/{token}/api/local/runs/run-001/log").json()["log"] == "Step 1"
    assert api.get(
        "/api/v1/sites/daily-install-1/config", headers=auth()
    ).json()["configured"] is False
    saved = api.post(
        f"/s/{token}/api/local/config/partial",
        json={
            "local": {
                "chat": {
                    "base_url": "https://llm.example/v1",
                    "model": "example-model",
                    "api_key": "secret-value",
                }
            }
        },
    )
    assert saved.status_code == 200
    public_config = api.get(f"/s/{token}/api/local/config/structured").json()
    assert public_config["configured"] is True
    assert public_config["local"]["chat"]["api_key"] == ""
    agent_config = api.get(
        "/api/v1/sites/daily-install-1/config", headers=auth()
    ).json()
    assert agent_config["configured"] is True
    assert agent_config["config"]["local"]["chat"]["api_key"] == "secret-value"
    api.post(
        f"/s/{token}/api/local/config/partial",
        json={"local": {"chat": {"api_key": "", "model": "new-model"}}},
    )
    preserved = api.get(
        "/api/v1/sites/daily-install-1/config", headers=auth()
    ).json()["config"]
    assert preserved["local"]["chat"]["api_key"] == "secret-value"
    assert preserved["local"]["chat"]["model"] == "new-model"


def test_report_zip_rejects_traversal_and_missing_index(tmp_path):
    api = client(tmp_path)
    api.post(
        "/api/v1/jobs",
        headers=auth(),
        json={"job_id": "safe-job", "module_name": "other", "title": "safe"},
    )
    assert api.put(
        "/api/v1/jobs/safe-job/report", headers=auth(), content=report_zip("../bad.html")
    ).status_code == 422
    assert api.put(
        "/api/v1/jobs/safe-job/report", headers=auth(), content=report_zip("readme.html")
    ).status_code == 422


def test_websocket_receives_snapshot_and_event(tmp_path):
    api = client(tmp_path)
    created = api.post(
        "/api/v1/jobs",
        headers=auth(),
        json={"job_id": "ws-job", "module_name": "citationclaw", "title": "查引用"},
    ).json()
    token = created["public_url"].rsplit("/", 1)[-1]
    with api.websocket_connect(f"/ws/public/jobs/{token}") as websocket:
        assert websocket.receive_json()["type"] == "snapshot"
        api.post(
            "/api/v1/jobs/ws-job/events",
            headers=auth(),
            json={"event_type": "job.message", "message": "已找到 10 条引用"},
        )
        assert websocket.receive_json()["event"]["message"] == "已找到 10 条引用"
