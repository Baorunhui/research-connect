import json

from connect_hub.provider_config import ModuleCommandRelay, citation_configuration, daily_environment


def test_daily_environment_projects_enabled_external_providers():
    config = {"providers": {
        "embedding.paper": {"enabled": True, "base_url": "https://embed", "api_key": "ek"},
        "rerank.paper": {"enabled": True, "base_url": "https://rerank", "api_key": "rk", "model": "rm"},
        "supabase.arxiv": {
            "enabled": True,
            "base_url": "https://papers.supabase.co",
            "anon_key": "anon",
            "papers_table": "arxiv_papers",
            "bm25_rpc": "match_bm25",
            "vector_rpc": "match_exact",
        },
        "academic.deepxiv": {"enabled": True, "base_url": "https://deepxiv", "api_key": "dk"},
        "citation.semantic_scholar": {"enabled": True, "api_key": "s2k"},
    }, "paper_sources": {"deepxiv": {"enabled": True}, "kaggle": {"enabled": False}}}
    env = daily_environment(config)
    assert env["DPR_EMBED_API_KEY"] == "ek"
    assert env["RERANK_API_KEY"] == "rk"
    assert env["SUPABASE_URL"] == "https://papers.supabase.co"
    assert env["SUPABASE_ANON_KEY"] == "anon"
    assert env["SUPABASE_PAPERS_TABLE"] == "arxiv_papers"
    assert env["SUPABASE_BM25_RPC"] == "match_bm25"
    assert env["SUPABASE_VECTOR_RPC_EXACT"] == "match_exact"
    assert env["DEEPXIV_TOKEN"] == "dk"
    assert env["DEEPXIV_API_BASE_URL"] == "https://deepxiv"
    assert env["SEMANTIC_SCHOLAR_API_KEY"] == "s2k"
    assert env["DPR_DEFAULT_USE_DEEPXIV"] == "1"
    assert env["DPR_DEFAULT_USE_KAGGLE"] == "0"


def test_disabled_providers_do_not_reach_module_runtime():
    providers = {
        "llm.primary": {"enabled": False, "base_url": "https://llm", "model": "m", "api_key": "lk"},
        "embedding.paper": {"enabled": False, "base_url": "https://embed", "api_key": "ek"},
        "rerank.paper": {"enabled": False, "base_url": "https://rerank", "model": "rm", "api_key": "rk"},
        "supabase.arxiv": {"enabled": False, "base_url": "https://papers", "anon_key": "anon"},
        "academic.deepxiv": {"enabled": False, "base_url": "https://deepxiv", "api_key": "dk"},
        "citation.search_llm": {"enabled": False, "base_url": "https://search", "model": "sm", "api_key": "sk"},
        "citation.scraperapi": {"enabled": False, "api_key": "scraper"},
        "citation.semantic_scholar": {"enabled": False, "api_key": "s2"},
        "citation.openalex": {"enabled": False, "email": "a@example.com"},
        "citation.wos": {"enabled": False, "api_key": "wos"},
        "document.mineru": {"enabled": False, "api_key": "mineru"},
    }
    env = daily_environment({"providers": providers})
    assert all(value in {"", "0"} for value in env.values())
    citation = citation_configuration({"providers": providers})
    for key in (
        "openai_api_key", "openai_base_url", "openai_model", "light_api_key",
        "light_base_url", "dashboard_model", "s2_api_key", "openalex_email",
        "wos_api_key", "mineru_api_token",
    ):
        assert citation[key] == ""
    assert citation["scraper_api_keys"] == []


def test_public_workflow_dispatch_schedules_site_publish(monkeypatch, tmp_path):
    scheduled = []

    class Client:
        pass

    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    relay = ModuleCommandRelay(
        Client(), "daily-site", "http://127.0.0.1:8567",
        auto_publish_dir=tmp_path,
    )
    monkeypatch.setattr(relay, "_wait_and_publish", lambda run_id: scheduled.append(run_id))
    monkeypatch.setattr("connect_hub.provider_config.threading.Thread", ImmediateThread)
    relay._watch_dispatched_run(
        {"path": "/api/local/workflows/dispatch"},
        200,
        json.dumps({"ok": True, "run": {"id": "run-123"}}).encode(),
    )
    assert scheduled == ["run-123"]


def test_completed_local_run_is_published(monkeypatch, tmp_path):
    published = []

    class Client:
        def upload_site(self, site_id, project_dir, *, site_kind):
            published.append((site_id, project_dir, site_kind))
            return "https://reports.test/s/site/"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({
                "ok": True,
                "run": {"status": "completed", "conclusion": "success"},
            }).encode()

    monkeypatch.setattr("connect_hub.provider_config.urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    relay = ModuleCommandRelay(
        Client(), "daily-site", "http://127.0.0.1:8567",
        auto_publish_dir=tmp_path,
    )
    relay._publish_runs.add("run-123")
    relay._wait_and_publish("run-123")
    assert published == [("daily-site", tmp_path.resolve(), "daily-paper")]
    assert "run-123" not in relay._publish_runs
