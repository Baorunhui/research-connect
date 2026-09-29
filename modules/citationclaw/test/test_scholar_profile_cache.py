"""Unit tests for the per-scholar profile result cache."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio

import pytest

import citationclaw.app.task_executor as te
from citationclaw.app.config_manager import AppConfig
from citationclaw.app.log_manager import LogManager
from citationclaw.core.scholar_profile_cache import (
    ScholarProfileCache,
    scholar_cache_keys,
    scholar_identity_from_html,
)

PROFILE_HTML = (
    '<html><head><link rel="canonical" '
    'href="https://scholar.google.com/citations?user=9sCGe-gAAAAJ&amp;hl=en"></head>'
    '<body><div id="gsc_prf_in">Tianzhu Zhang</div>'
    '<a href="/citations?user=XmxB9fIAAAAJ">co-author</a></body></html>'
)


def run(coro):
    # A private loop keeps the default loop intact for other test modules.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _write_result(data_dir, folder="result-20260601_152415"):
    result_dir = data_dir / folder
    result_dir.mkdir(parents=True)
    paths = {}
    for name, filename in (("excel", "p_results.xlsx"), ("json", "p_results.json"),
                           ("dashboard", "p_dashboard.html")):
        path = result_dir / filename
        path.write_text("x", encoding="utf-8")
        paths[name] = str(path)
    return paths


def test_keys_from_url_and_html():
    assert scholar_cache_keys(
        profile_url="https://scholar.google.com/citations?user=9sCGe-gAAAAJ&hl=en"
    ) == ["gs:9sCGe-gAAAAJ"]
    assert scholar_identity_from_html(PROFILE_HTML) == ("9sCGe-gAAAAJ", "Tianzhu Zhang")
    assert scholar_cache_keys(profile_html=PROFILE_HTML) == [
        "gs:9sCGe-gAAAAJ", "name:tianzhu zhang",
    ]


def test_name_falls_back_to_saved_filename():
    user_id, name = scholar_identity_from_html(
        "<html></html>", "_Wenfei Yang_ - _Google 学术搜索_.html"
    )
    assert user_id == ""
    assert name == "Wenfei Yang"


def test_store_then_lookup_and_miss_after_delete(tmp_path):
    cache = ScholarProfileCache(cache_file=tmp_path / "cache" / "sp.json", data_dir=tmp_path)
    paths = _write_result(tmp_path)
    keys = scholar_cache_keys(profile_html=PROFILE_HTML)
    cache.store(keys, result=paths, scholar_name="Tianzhu Zhang")

    by_url = cache.lookup(scholar_cache_keys(
        profile_url="https://scholar.google.com/citations?user=9sCGe-gAAAAJ"
    ))
    assert by_url is not None
    assert by_url["dashboard"] == paths["dashboard"]
    assert by_url["result_dir"] == "result-20260601_152415"

    for path in paths.values():
        os.remove(path)
    assert cache.lookup(keys) is None


def test_cached_scholar_skips_external_lookup(tmp_path, monkeypatch):
    paths = _write_result(tmp_path)
    cache = ScholarProfileCache(cache_file=tmp_path / "cache" / "sp.json", data_dir=tmp_path)
    url = "https://scholar.google.com/citations?user=9sCGe-gAAAAJ"
    cache.store(scholar_cache_keys(profile_url=url), result=paths)

    class _Forbidden:
        def __init__(self, *args, **kwargs):
            raise AssertionError("external lookup must not run on a cache hit")

    monkeypatch.setattr(te, "DATA_DIR", tmp_path)
    monkeypatch.setattr(te, "ScholarProfileCache", lambda: cache)
    monkeypatch.setattr(te, "ScholarProfileScraper", _Forbidden)
    executor = te.TaskExecutor(LogManager(), config_manager=None)

    result = run(executor.execute_scholar_profile(
        config=AppConfig(), output_prefix="scholar_profile", profile_url=url,
    ))
    assert result["cached"] is True
    assert result["dashboard"] == paths["dashboard"]

    with pytest.raises(AssertionError, match="external lookup"):
        run(executor.execute_scholar_profile(
            config=AppConfig(), output_prefix="scholar_profile", profile_url=url,
            force_refresh=True,
        ))
