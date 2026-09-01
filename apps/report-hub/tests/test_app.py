from __future__ import annotations

import io
import hashlib
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
import time
import base64

from fastapi.testclient import TestClient

from report_hub.app import _site_command_allowed, create_app
from report_hub.config import Settings
from report_hub.provider_config import merged_defaults
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
        agent_token=TOKEN,
        data_dir=tmp_path,
    )
    return TestClient(create_app(settings))


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def install_auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


def test_unified_configuration_page_redacts_and_preserves_secrets(tmp_path):
    api = client(tmp_path)
    site = api.post(
        "/api/v1/sites",
        headers=auth(),
        json={"site_id": "connect-config-1", "module_name": "other", "title": "配置"},
    ).json()
    token = site["public_url"].rstrip("/").rsplit("/", 1)[-1]
    assert api.get(f"/configure/{token}/").status_code == 200
    assert api.get(f"/configure/{token}/api/catalog").json()["pipelines"]
    defaults = api.get(f"/configure/{token}/api/config").json()["providers"]
    assert defaults["supabase.arxiv"]["base_url"] == "https://lyucdwgefyfbmaiopjbk.supabase.co"
    assert defaults["supabase.arxiv"]["anon_key"] == ""
    assert defaults["supabase.arxiv"]["configured"] is True
    assert defaults["embedding.paper"]["configured"] is True
    assert defaults["rerank.paper"]["configured"] is True
    saved = api.post(
        f"/configure/{token}/api/config",
        json={"providers": {"llm.primary": {"base_url": "https://llm.example/v1", "model": "m", "api_key": "secret"}}},
    )
    assert saved.status_code == 200
    public = api.get(f"/configure/{token}/api/config").json()
    assert public["providers"]["llm.primary"]["api_key"] == ""
    assert public["providers"]["llm.primary"]["configured"] is True
    api.post(
        f"/configure/{token}/api/config",
        json={"providers": {"llm.primary": {"api_key": "", "model": "m2"}}},
    )
    private = api.get("/api/v1/sites/connect-config-1/config", headers=auth()).json()["config"]
    assert private["providers"]["llm.primary"]["api_key"] == "secret"
    assert private["providers"]["llm.primary"]["model"] == "m2"


def test_old_blank_public_provider_config_is_upgraded_without_overwriting_custom_service():
    private = merged_defaults({
        "providers": {
            "supabase.arxiv": {"base_url": "", "anon_key": ""},
            "embedding.paper": {"base_url": "https://zwwen.online/embed", "api_key": ""},
            "rerank.paper": {"base_url": "https://private.example/rerank", "api_key": ""},
        }
    })["providers"]
    assert private["supabase.arxiv"]["anon_key"].startswith("sb_publishable_")
    assert private["embedding.paper"]["api_key"]
    assert private["rerank.paper"]["base_url"] == "https://private.example/rerank"
    assert private["rerank.paper"]["api_key"] == ""


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


def test_public_daily_model_tools_use_saved_config(tmp_path, monkeypatch):
    api = client(tmp_path)
    site = api.post(
        "/api/v1/sites",
        headers=auth(),
        json={"site_id": "daily-models", "module_name": "daily-paper", "title": "日报"},
    ).json()
    token = site["public_url"].rstrip("/").rsplit("/", 1)[-1]
    api.post(
        f"/s/{token}/api/local/config/partial",
        json={
            "local": {
                "chat": {
                    "base_url": "https://llm.example/v1",
                    "api_key": "saved-key",
                    "model": "model-a",
                }
            }
        },
    )
    requests = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        if request.full_url.endswith("/models"):
            return FakeResponse({"data": [{"id": "model-b"}, {"id": "model-a"}]})
        return FakeResponse({"choices": [{"message": {"content": "pong"}}]})

    monkeypatch.setattr("report_hub.app.urllib.request.urlopen", fake_urlopen)
    models = api.post(
        f"/s/{token}/api/local/chat/models", json={"base_url": "", "api_key": ""}
    )
    assert models.json()["models"] == ["model-a", "model-b"]
    probe = api.post(
        f"/s/{token}/api/local/chat/test",
        json={"base_url": "", "api_key": "", "model": "model-a"},
    )
    assert probe.json()["reply"] == "pong"
    assert requests[0][0].headers["Authorization"] == "Bearer saved-key"


def test_module_pages_share_one_installation_config(tmp_path):
    api = client(tmp_path)
    sites = {}
    for site_id, module in (("connect-config-abc", "other"), ("daily-paper-abc", "daily-paper"), ("citationclaw-abc", "citationclaw")):
        sites[module] = api.post(
            "/api/v1/sites", headers=auth(),
            json={"site_id": site_id, "module_name": module, "title": module},
        ).json()["public_url"].rstrip("/").rsplit("/", 1)[-1]
    api.put(
        "/api/v1/sites/connect-config-abc/config", headers=auth(),
        json={"config": {"providers": {
            "llm.primary": {
                "enabled": True, "base_url": "https://llm.example/v1", "model": "shared", "api_key": "shared-secret"
            },
            "supabase.arxiv": {
                "enabled": True,
                "base_url": "https://papers.supabase.co",
                "anon_key": "supabase-secret",
                "papers_table": "arxiv_papers",
                "bm25_rpc": "match_bm25",
                "vector_rpc": "match_vector",
            },
        }}},
    )
    daily = api.get(f"/s/{sites['daily-paper']}/api/local/config/structured").json()
    assert daily["local"]["chat"]["model"] == "shared"
    assert daily["local"]["chat"]["api_key_configured"] is True
    arxiv = daily["local"]["source_backends"]["arxiv"]
    assert arxiv["url"] == "https://papers.supabase.co"
    assert arxiv["anon_key"] == ""
    assert arxiv["anon_key_configured"] is True
    assert arxiv["bm25_rpc"] == "match_bm25"
    assert arxiv["vector_rpc"] == "match_vector"
    citation = api.get(f"/s/{sites['citationclaw']}/api/config").json()
    assert citation["dashboard_model"] == "shared"
    assert citation["_configured_secrets"]["light_api_key"] is True

    api.post(f"/s/{sites['citationclaw']}/api/config", json={
        "openai_base_url": "https://search.example/v1", "openai_model": "search-model",
        "openai_api_key": "search-secret", "light_base_url": "https://new.example/v1",
        "dashboard_model": "shared-2", "light_api_key": "new-shared-secret",
    })
    private = api.get("/api/v1/sites/connect-config-abc/config", headers=auth()).json()["config"]
    assert private["providers"]["llm.primary"]["model"] == "shared-2"
    assert private["providers"]["citation.search_llm"]["model"] == "search-model"
    daily2 = api.get(f"/s/{sites['daily-paper']}/api/local/config/structured").json()
    assert daily2["local"]["chat"]["base_url"] == "https://new.example/v1"

    api.post(f"/s/{sites['daily-paper']}/api/local/config/partial", json={
        "source_backends": {"arxiv": {
            "enabled": True,
            "url": "https://new-papers.supabase.co",
            "anon_key": "",
            "papers_table": "papers_v2",
            "bm25_rpc": "bm25_v2",
            "vector_rpc": "vector_v2",
        }}
    })
    private2 = api.get("/api/v1/sites/connect-config-abc/config", headers=auth()).json()["config"]
    provider = private2["providers"]["supabase.arxiv"]
    assert provider["base_url"] == "https://new-papers.supabase.co"
    assert provider["anon_key"] == "supabase-secret"
    assert provider["papers_table"] == "papers_v2"


def test_disabled_citation_providers_are_not_projected_to_original_ui(tmp_path):
    api = client(tmp_path)
    sites = {}
    for site_id, module in (("connect-config-off", "other"), ("citationclaw-off", "citationclaw")):
        sites[module] = api.post(
            "/api/v1/sites", headers=auth(),
            json={"site_id": site_id, "module_name": module, "title": module},
        ).json()["public_url"].rstrip("/").rsplit("/", 1)[-1]
    api.put("/api/v1/sites/connect-config-off/config", headers=auth(), json={"config": {"providers": {
        "llm.primary": {"enabled": False, "base_url": "https://llm", "model": "m", "api_key": "llm-key"},
        "citation.search_llm": {"enabled": False, "base_url": "https://search", "model": "s", "api_key": "search-key"},
        "citation.scraperapi": {"enabled": False, "api_key": "scraper-key"},
        "citation.semantic_scholar": {"enabled": False, "api_key": "s2-key"},
        "citation.wos": {"enabled": False, "api_key": "wos-key"},
        "document.mineru": {"enabled": False, "api_key": "mineru-key"},
    }}})
    projected = api.get(f"/s/{sites['citationclaw']}/api/config").json()
    assert projected["light_base_url"] == ""
    assert projected["openai_base_url"] == ""
    assert projected["scraper_api_keys"] == []
    assert projected["_configured_secrets"] == {
        "light_api_key": False,
        "openai_api_key": False,
        "scraper_api_keys": False,
        "s2_api_key": False,
        "wos_api_key": False,
        "mineru_api_token": False,
    }


def test_disabled_shared_llm_is_not_reenabled_by_blank_module_forms(tmp_path):
    api = client(tmp_path)
    sites = {}
    for site_id, module in (
        ("connect-config-disabled", "other"),
        ("daily-paper-disabled", "daily-paper"),
        ("citationclaw-disabled", "citationclaw"),
    ):
        sites[module] = api.post(
            "/api/v1/sites", headers=auth(),
            json={"site_id": site_id, "module_name": module, "title": module},
        ).json()["public_url"].rstrip("/").rsplit("/", 1)[-1]
    api.put("/api/v1/sites/connect-config-disabled/config", headers=auth(), json={"config": {"providers": {
        "llm.primary": {"enabled": False, "base_url": "https://old", "model": "old", "api_key": "old-key"},
    }}})
    daily = api.get(f"/s/{sites['daily-paper']}/api/local/config/structured").json()
    assert daily["local"]["chat"]["base_url"] == ""
    assert daily["local"]["chat"]["api_key_configured"] is False
    api.post(f"/s/{sites['daily-paper']}/api/local/config/partial", json={
        "local": {"chat": {"base_url": "", "model": "", "api_key": ""}},
        "local_only_setting": True,
    })
    api.post(f"/s/{sites['citationclaw']}/api/config", json={
        "light_base_url": "", "dashboard_model": "", "light_api_key": "",
        "openai_base_url": "", "openai_model": "", "openai_api_key": "",
        "scraper_api_keys": [],
    })
    private = api.get("/api/v1/sites/connect-config-disabled/config", headers=auth()).json()["config"]
    assert private["providers"]["llm.primary"]["enabled"] is False
    assert private["providers"]["llm.primary"]["api_key"] == "old-key"


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
        "/api/v1/sites/daily-paper-alice/config", headers=install_auth(first_token)
    ).status_code == 200
    assert api.get(
        "/api/v1/sites/daily-paper-alice/config", headers=install_auth(second_token)
    ).status_code == 403
    assert api.get(
        "/api/v1/sites/daily-paper-alice/config", headers=auth()
    ).status_code == 200
    collision = api.post(
        "/api/v1/sites", headers=install_auth(second_token),
        json={"site_id": "daily-paper-alice", "module_name": "daily-paper", "title": "Bob"},
    )
    assert collision.status_code == 403
    assert api.app.state.storage.revoke_installation(first["install_id"]) is True
    assert api.get(
        "/api/v1/sites/daily-paper-alice/config", headers=install_auth(first_token)
    ).status_code == 401
    rotated = api.app.state.storage.rotate_installation(first["install_id"])
    assert rotated and rotated != first_token
    assert api.get(
        "/api/v1/sites/daily-paper-alice/config", headers=install_auth(rotated)
    ).status_code == 200
    assert second["install_id"] != first["install_id"]


def test_first_install_token_adopts_legacy_unowned_site(tmp_path):
    api = client(tmp_path)
    api.post(
        "/api/v1/sites", headers=auth(),
        json={"site_id": "daily-paper-legacy", "module_name": "daily-paper", "title": "legacy"},
    )
    installation, token = api.app.state.storage.issue_installation("legacy owner")
    adopted = api.post(
        "/api/v1/sites", headers=install_auth(token),
        json={"site_id": "daily-paper-legacy", "module_name": "daily-paper", "title": "legacy"},
    )
    assert adopted.status_code == 201
    assert api.app.state.storage.get_site(site_id="daily-paper-legacy")["owner_install_id"] == installation["install_id"]


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
