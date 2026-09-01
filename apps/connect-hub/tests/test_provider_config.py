from connect_hub.provider_config import citation_configuration, daily_environment


def test_daily_environment_projects_enabled_external_providers():
    config = {"providers": {
        "embedding.paper": {"enabled": True, "base_url": "https://embed", "api_key": "ek"},
        "rerank.paper": {"enabled": True, "base_url": "https://rerank", "api_key": "rk", "model": "rm"},
        "academic.deepxiv": {"enabled": True, "base_url": "https://deepxiv", "api_key": "dk"},
        "citation.semantic_scholar": {"enabled": True, "api_key": "s2k"},
    }, "paper_sources": {"deepxiv": {"enabled": True}, "kaggle": {"enabled": False}}}
    env = daily_environment(config)
    assert env["DPR_EMBED_API_KEY"] == "ek"
    assert env["RERANK_API_KEY"] == "rk"
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
