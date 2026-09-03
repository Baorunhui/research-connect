from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from connect_hub.reporting import (
    PUBLIC_MODULE_COMMAND_POLICIES,
    ReportHubClient,
    ReportHubError,
    build_daily_paper_site_archive,
    build_config_site_archive,
)


def test_ensure_site_registers_module_command_policy(monkeypatch):
    client = ReportHubClient("https://reports.test", "x" * 40)
    captured = {}

    def fake_json_request(method, path, payload):
        captured.update(method=method, path=path, payload=payload)
        return {"public_url": "https://reports.test/s/token/", "report_ready": True}

    monkeypatch.setattr(client, "_json_request", fake_json_request)
    client.ensure_site(
        site_id="citation-site", module_name="citationclaw", title="CitationClaw"
    )

    assert captured["payload"]["command_policy"] == list(
        PUBLIC_MODULE_COMMAND_POLICIES["citationclaw"]
    )
    assert {"method": "POST", "path": "profile/run"} in captured["payload"]["command_policy"]
    assert {"method": "POST", "path": "chat/report"} in captured["payload"]["command_policy"]


def test_public_policies_cover_all_original_web_ui_relay_operations():
    daily = {
        (rule["method"], rule["path"])
        for rule in PUBLIC_MODULE_COMMAND_POLICIES["daily-paper"]
    }
    citation = {
        (rule["method"], rule["path"])
        for rule in PUBLIC_MODULE_COMMAND_POLICIES["citationclaw"]
    }

    # All module-specific operations, including config, are forwarded to the
    # user's local services. Report Hub only enforces the declared policy.
    assert {
        ("GET", "local/health"),
        ("GET", "local/config/structured"),
        ("GET", "local/runs"),
        ("GET", "local/runs/*"),
        ("GET", "local/runtime/runs"),
        ("GET", "local/runtime/runs/*"),
        ("GET", "chat/config"),
        ("GET", "paper/summarize"),
        ("GET", "paper/summarize/*"),
        ("GET", "survey"),
        ("GET", "survey/*"),
        ("POST", "chat"),
        ("POST", "local/smart-query"),
        ("POST", "local/config/partial"),
        ("POST", "local/chat/models"),
        ("POST", "local/chat/test"),
        ("POST", "local/workflows/dispatch"),
        ("POST", "local/runtime/runs/*"),
        ("POST", "paper/summarize"),
        ("POST", "survey"),
    }.issubset(daily)
    assert {
        ("GET", "providers"),
        ("GET", "config"),
        ("GET", "presets"),
        ("GET", "quota/check"),
        ("GET", "task/status"),
        ("GET", "results/folders"),
        ("GET", "results/list"),
        ("GET", "results/view/*"),
        ("GET", "results/download/*"),
        ("POST", "run"),
        ("POST", "config"),
        ("POST", "run/from-cache"),
        ("POST", "scholar/papers"),
        ("POST", "profile/run"),
        ("POST", "profile/upload"),
        ("POST", "task/cancel"),
        ("POST", "task/year-traverse-respond"),
        ("POST", "test_openai"),
        ("POST", "pretest/search_llm"),
        ("POST", "pretest/light_model"),
        ("POST", "chat/ui"),
        ("POST", "chat/report"),
        ("DELETE", "results/folder/*"),
    } == citation


def test_configuration_site_is_packaged_by_the_client():
    archive = build_config_site_archive()
    with zipfile.ZipFile(io.BytesIO(archive)) as payload:
        html = payload.read("index.html").decode("utf-8")
    assert "Research Connect 配置中心" in html
    assert "api/config/catalog" in html
    assert "api/config/value" in html


def test_original_module_frontends_do_not_bypass_public_site_prefix():
    root = Path(__file__).resolve().parents[3]
    sources = [
        root / "modules/citationclaw/citationclaw/templates/index.html",
        root / "modules/citationclaw/citationclaw/static/js/main.js",
        root / "modules/daily-paper-reader/app/local-settings.js",
    ]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert "fetch('/api/" not in text, source
        assert 'fetch("/api/' not in text, source

    dashboard = (
        root / "modules/citationclaw/citationclaw/core/dashboard_generator.py"
    ).read_text(encoding="utf-8")
    assert "publicApiBase + '/api/chat/report'" in dashboard


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


def test_daily_site_archive_rejects_missing_declared_asset(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "index.html").write_text(
        "<html><head></head><script>load([{path: 'app/missing.js'}])</script></html>",
        encoding="utf-8",
    )

    with pytest.raises(ReportHubError, match="app/missing.js"):
        build_daily_paper_site_archive(tmp_path)
