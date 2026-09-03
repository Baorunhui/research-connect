from __future__ import annotations

import io
import hashlib
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
import time
import base64
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from report_hub.app import _site_command_allowed, create_app
from report_hub.config import Settings
from report_hub.storage import Storage


TOKEN = "test-token-that-is-longer-than-thirty-two-characters"


def test_existing_sites_table_gains_command_policy_column(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "report-hub.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE sites ("
            "site_id TEXT PRIMARY KEY, public_token TEXT NOT NULL UNIQUE, "
            "module_name TEXT NOT NULL, title TEXT NOT NULL, "
            "report_ready INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, owner_install_id TEXT)"
        )
        db.execute(
            "INSERT INTO sites(site_id, public_token, module_name, title, created_at, updated_at) "
            "VALUES ('old-site', 'old-token', 'citationclaw', 'old', 'now', 'now')"
        )

    storage = Storage(tmp_path)
    storage.initialize()
    site = storage.get_site(site_id="old-site")

    assert site is not None
    assert site["command_policy_json"] == "[]"


def client(tmp_path) -> TestClient:
    settings = Settings(
        public_base_url="https://reports.example.test",
        data_dir=tmp_path,
    )
    api = TestClient(create_app(settings))
    _installation, token = api.app.state.storage.issue_installation("test client")
    api.headers.update({"Authorization": f"Bearer {token}"})
    return api


def auth() -> dict[str, str]:
    return {}


def install_auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def report_zip(filename: str = "index.html", content: bytes = b"<h1>done</h1>") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(filename, content)
    return output.getvalue()


def test_agent_api_requires_token(tmp_path):
    api = client(tmp_path)
    response = api.post(
        "/api/v1/sites",
        headers={"Authorization": ""},
        json={"site_id": "unauthorized", "module_name": "other", "title": "unauthorized"},
    )
    assert response.status_code == 401


def test_invite_registration_issues_isolated_install_token(tmp_path):
    storage = Storage(tmp_path)
    storage.initialize()
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")
    invite, code = storage.issue_invite("demo", max_uses=2, expires_at=expires)
    api = client(tmp_path)

    first = api.post(
        "/api/v1/installations/register",
        json={
            "invite_code": code,
            "feishu_app_id": "cli_first_app",
            "device_name": "Windows laptop",
        },
    )
    assert first.status_code == 201
    payload = first.json()
    assert payload["agent_token"].startswith(f"rhi_{payload['install_id']}_")
    assert api.post(
        "/api/v1/sites",
        headers=install_auth(payload["agent_token"]),
        json={"site_id": "self-site", "module_name": "other", "title": "self"},
    ).status_code == 201
    assert storage.get_invite(invite["invite_id"])["used_count"] == 1

    duplicate = api.post(
        "/api/v1/installations/register",
        json={
            "invite_code": code,
            "feishu_app_id": "cli_first_app",
            "device_name": "duplicate",
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "feishu_app_already_registered"
    assert storage.get_invite(invite["invite_id"])["used_count"] == 1

    second = api.post(
        "/api/v1/installations/register",
        json={
            "invite_code": code,
            "feishu_app_id": "cli_second_app",
            "device_name": "Linux server",
        },
    )
    assert second.status_code == 201
    exhausted = api.post(
        "/api/v1/installations/register",
        json={
            "invite_code": code,
            "feishu_app_id": "cli_third_app",
            "device_name": "third",
        },
    )
    assert exhausted.status_code == 403
    assert exhausted.json()["detail"] == "invite_exhausted"


def test_revoked_and_expired_invites_are_rejected(tmp_path):
    storage = Storage(tmp_path)
    storage.initialize()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    invite, code = storage.issue_invite("revoked", max_uses=3, expires_at=future)
    assert storage.revoke_invite(invite["invite_id"])
    api = client(tmp_path)
    revoked = api.post(
        "/api/v1/installations/register",
        json={"invite_code": code, "feishu_app_id": "cli_revoked", "device_name": "x"},
    )
    assert revoked.status_code == 403
    assert revoked.json()["detail"] == "invite_revoked"

    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    _expired, expired_code = storage.issue_invite("expired", max_uses=3, expires_at=past)
    expired = api.post(
        "/api/v1/installations/register",
        json={"invite_code": expired_code, "feishu_app_id": "cli_expired", "device_name": "x"},
    )
    assert expired.status_code == 403
    assert expired.json()["detail"] == "invite_expired"


def test_stable_site_accepts_chunked_atomic_upload(tmp_path):
    api = client(tmp_path)
    site = api.post(
        "/api/v1/sites",
        headers=auth(),
        json={"site_id": "chunked-site", "module_name": "daily-paper", "title": "日报"},
    ).json()
    token = site["public_url"].rstrip("/").rsplit("/", 1)[-1]
    payload = report_zip(content=b"<h1>chunked site</h1>")
    split = max(1, len(payload) // 2)
    chunks = [payload[:split], payload[split:]]
    for index, chunk in enumerate(chunks):
        response = api.put(
            f"/api/v1/sites/chunked-site/uploads/upload-1/parts/{index}?total_parts=2",
            headers={**auth(), "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
            content=chunk,
        )
        assert response.status_code == 200
        assert response.json()["completed"] is (index == 1)
    assert api.get(f"/s/{token}/").text == "<h1>chunked site</h1>"
    assert not (tmp_path / "site_uploads" / "chunked-site" / "upload-1").exists()


def test_report_zip_rejects_traversal_and_missing_index(tmp_path):
    api = client(tmp_path)
    api.post(
        "/api/v1/sites",
        headers=auth(),
        json={"site_id": "safe-site", "module_name": "other", "title": "safe"},
    )
    assert api.put(
        "/api/v1/sites/safe-site/report", headers=auth(), content=report_zip("../bad.html")
    ).status_code == 422
    assert api.put(
        "/api/v1/sites/safe-site/report", headers=auth(), content=report_zip("readme.html")
    ).status_code == 422


def test_public_module_command_round_trip(tmp_path):
    api = client(tmp_path)
    site = api.post(
        "/api/v1/sites", headers=auth(),
        json={
            "site_id": "citationclaw-relay",
            "module_name": "citationclaw",
            "title": "citation",
            "command_policy": [{"method": "GET", "path": "task/status"}],
        },
    ).json()
    token = site["public_url"].rstrip("/").rsplit("/", 1)[-1]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: api.get(f"/s/{token}/api/task/status"))
        command = None
        for _ in range(100):
            command = api.get(
                "/api/v1/sites/citationclaw-relay/commands/next", headers=auth()
            ).json()["command"]
            if command:
                break
            time.sleep(0.01)
        assert command and command["path"] == "/api/task/status"
        api.post(
            f"/api/v1/sites/citationclaw-relay/commands/{command['command_id']}/complete",
            headers=auth(), json={"status_code": 200, "headers": {"content-type": "application/json"},
                                 "body_b64": base64.b64encode(b'{"status":"idle"}').decode()},
        )
        response = future.result(timeout=5)
    assert response.json()["status"] == "idle"


def test_public_citation_profile_command_is_relayed(tmp_path):
    api = client(tmp_path)
    site = api.post(
        "/api/v1/sites", headers=auth(),
        json={
            "site_id": "citation-profile-relay",
            "module_name": "citationclaw",
            "title": "citation",
            "command_policy": [{"method": "POST", "path": "profile/run"}],
        },
    ).json()
    token = site["public_url"].rstrip("/").rsplit("/", 1)[-1]
    payload = {
        "profile_url": "https://scholar.google.com/citations?user=abc&name=Wenfei+Yang",
        "top_n": 30,
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            lambda: api.post(f"/s/{token}/api/profile/run", json=payload)
        )
        command = None
        for _ in range(100):
            command = api.get(
                "/api/v1/sites/citation-profile-relay/commands/next", headers=auth()
            ).json()["command"]
            if command:
                break
            time.sleep(0.01)
        assert command and command["path"] == "/api/profile/run"
        api.post(
            f"/api/v1/sites/citation-profile-relay/commands/{command['command_id']}/complete",
            headers=auth(),
            json={
                "status_code": 200,
                "headers": {"content-type": "application/json"},
                "body_b64": base64.b64encode(
                    b'{"status":"success","message":"started"}'
                ).decode(),
            },
        )
        response = future.result(timeout=5)

    assert response.json()["status"] == "success"


def test_public_module_command_requires_declared_method_and_path(tmp_path):
    api = client(tmp_path)
    site = api.post(
        "/api/v1/sites",
        headers=auth(),
        json={
            "site_id": "citation-policy",
            "module_name": "citationclaw",
            "title": "citation",
            "command_policy": [
                {"method": "GET", "path": "results/view/*"},
                {"method": "POST", "path": "profile/run"},
            ],
        },
    ).json()
    token = site["public_url"].rstrip("/").rsplit("/", 1)[-1]

    assert api.get(f"/s/{token}/api/task/status").status_code == 403
    assert api.get(f"/s/{token}/api/profile/run").status_code == 403
    stored = api.app.state.storage.get_site(site_id="citation-policy")
    assert _site_command_allowed(stored, "GET", "results/view/report.html")
    assert _site_command_allowed(stored, "POST", "profile/run")

    # Re-registering the same stable site replaces its policy without changing
    # the public URL. This is how a restarted Connect Hub publishes new routes.
    updated = api.post(
        "/api/v1/sites",
        headers=auth(),
        json={
            "site_id": "citation-policy",
            "module_name": "citationclaw",
            "title": "citation",
            "command_policy": [{"method": "GET", "path": "task/status"}],
        },
    ).json()
    assert updated["public_url"] == site["public_url"]
    assert api.get(f"/s/{token}/api/results/view/report.html").status_code == 403


def test_public_daily_paper_command_round_trip_and_allowlist(tmp_path):
    api = client(tmp_path)
    site = api.post(
        "/api/v1/sites", headers=auth(),
        json={
            "site_id": "daily-paper-relay",
            "module_name": "daily-paper",
            "title": "daily",
            "command_policy": [{"method": "POST", "path": "paper/summarize"}],
        },
    ).json()
    token = site["public_url"].rstrip("/").rsplit("/", 1)[-1]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: api.post(
            f"/s/{token}/api/paper/summarize",
            json={"source": "url", "url": "https://arxiv.org/abs/2601.00001"},
        ))
        command = None
        for _ in range(100):
            command = api.get(
                "/api/v1/sites/daily-paper-relay/commands/next", headers=auth()
            ).json()["command"]
            if command:
                break
            time.sleep(0.01)
        assert command and command["path"] == "/api/paper/summarize"
        api.post(
            f"/api/v1/sites/daily-paper-relay/commands/{command['command_id']}/complete",
            headers=auth(), json={
                "status_code": 200,
                "headers": {"content-type": "application/json"},
                "body_b64": base64.b64encode(
                    b'{"ok":true,"job_id":"sum-test","status":"queued"}'
                ).decode(),
            },
        )
        response = future.result(timeout=5)
    assert response.json()["job_id"] == "sum-test"
    assert api.get(f"/s/{token}/api/local/secret").status_code == 403


def test_install_tokens_are_isolated_and_revocable(tmp_path):
    api = client(tmp_path)
    first, first_token = api.app.state.storage.issue_installation("Alice")
    second, second_token = api.app.state.storage.issue_installation("Bob")
    created = api.post(
        "/api/v1/sites", headers=install_auth(first_token),
        json={"site_id": "daily-paper-alice", "module_name": "daily-paper", "title": "Alice"},
    )
    assert created.status_code == 201
    assert api.get(
        "/api/v1/sites/daily-paper-alice/commands/next", headers=install_auth(first_token)
    ).status_code == 200
    assert api.get(
        "/api/v1/sites/daily-paper-alice/commands/next", headers=install_auth(second_token)
    ).status_code == 403
    assert api.get(
        "/api/v1/sites/daily-paper-alice/commands/next", headers=install_auth(TOKEN)
    ).status_code == 401
    collision = api.post(
        "/api/v1/sites", headers=install_auth(second_token),
        json={"site_id": "daily-paper-alice", "module_name": "daily-paper", "title": "Bob"},
    )
    assert collision.status_code == 403
    assert api.app.state.storage.revoke_installation(first["install_id"]) is True
    assert api.get(
        "/api/v1/sites/daily-paper-alice/commands/next", headers=install_auth(first_token)
    ).status_code == 401
    rotated = api.app.state.storage.rotate_installation(first["install_id"])
    assert rotated and rotated != first_token
    assert api.get(
        "/api/v1/sites/daily-paper-alice/commands/next", headers=install_auth(rotated)
    ).status_code == 200
    assert second["install_id"] != first["install_id"]


def test_unowned_legacy_site_cannot_be_adopted(tmp_path):
    api = client(tmp_path)
    api.app.state.storage.create_site(
        site_id="daily-paper-legacy",
        public_token="legacy-public-token",
        module_name="daily-paper",
        title="legacy",
    )
    _installation, token = api.app.state.storage.issue_installation("legacy owner")
    adopted = api.post(
        "/api/v1/sites", headers=install_auth(token),
        json={"site_id": "daily-paper-legacy", "module_name": "daily-paper", "title": "legacy"},
    )
    assert adopted.status_code == 403
    assert api.app.state.storage.get_site(site_id="daily-paper-legacy")["owner_install_id"] is None


def test_installation_can_list_delete_and_clear_own_storage(tmp_path):
    api = client(tmp_path)
    installation, token = api.app.state.storage.issue_installation("Alice")
    other, other_token = api.app.state.storage.issue_installation("Bob")
    for site_id, auth_token in (("alice-one", token), ("alice-two", token), ("bob-one", other_token)):
        created = api.post(
            "/api/v1/sites",
            headers=install_auth(auth_token),
            json={"site_id": site_id, "module_name": "other", "title": site_id},
        )
        assert created.status_code == 201
        assert api.put(
            f"/api/v1/sites/{site_id}/report",
            headers=install_auth(auth_token),
            content=report_zip(content=site_id.encode()),
        ).status_code == 200

    summary = api.get(
        "/api/v1/installations/current/storage", headers=install_auth(token)
    )
    assert summary.status_code == 200
    assert summary.json()["install_id"] == installation["install_id"]
    assert summary.json()["site_count"] == 2
    assert summary.json()["total_bytes"] > 0

    assert api.delete(
        "/api/v1/installations/current/sites/bob-one", headers=install_auth(token)
    ).status_code == 403
    deleted = api.delete(
        "/api/v1/installations/current/sites/alice-one", headers=install_auth(token)
    )
    assert deleted.status_code == 200
    assert not (tmp_path / "sites" / "alice-one").exists()

    cleared = api.delete(
        "/api/v1/installations/current/data", headers=install_auth(token)
    )
    assert cleared.status_code == 200
    assert cleared.json()["deleted_sites"] == 1
    assert api.app.state.storage.authenticate_installation(token) is not None
    assert api.app.state.storage.get_site(site_id="bob-one")["owner_install_id"] == other["install_id"]


def test_admin_storage_deletion_removes_data_and_token(tmp_path):
    storage = Storage(tmp_path)
    storage.initialize()
    installation, token = storage.issue_installation("delete me")
    storage.create_site(
        site_id="delete-me",
        public_token="delete-public",
        module_name="other",
        title="delete",
        owner_install_id=installation["install_id"],
    )
    site_dir = tmp_path / "sites" / "delete-me"
    site_dir.mkdir(parents=True)
    (site_dir / "index.html").write_text("delete", encoding="utf-8")

    assert storage.delete_installation(installation["install_id"])
    assert storage.authenticate_installation(token) is None
    assert storage.get_installation(installation["install_id"]) is None
    assert storage.get_site(site_id="delete-me") is None
    assert not site_dir.exists()
