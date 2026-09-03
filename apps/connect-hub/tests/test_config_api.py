from fastapi.testclient import TestClient

from connect_hub.config_api import create_config_app
from connect_hub.provider_config import CredentialStore


def test_local_configuration_api_owns_catalog_and_secrets(tmp_path):
    path = tmp_path / "providers.json"
    api = TestClient(create_config_app(path))

    catalog = api.get("/api/config/catalog")
    assert catalog.status_code == 200
    assert catalog.json()["pipelines"]
    saved = api.post(
        "/api/config/value",
        json={
            "providers": {
                "llm.primary": {
                    "enabled": True,
                    "base_url": "https://llm.example/v1",
                    "model": "demo-model",
                    "api_key": "local-secret",
                }
            }
        },
    )
    assert saved.status_code == 200
    public = api.get("/api/config/value")
    assert public.headers["cache-control"].startswith("no-store")
    assert public.json()["providers"]["llm.primary"]["api_key"] == ""
    assert public.json()["providers"]["llm.primary"]["configured"] is True
    assert CredentialStore(path).load()["providers"]["llm.primary"]["api_key"] == "local-secret"
