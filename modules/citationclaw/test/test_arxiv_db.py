import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from citationclaw.core.arxiv_db import (
    ArxivDB, normalize_title, normalize_arxiv_id, normalize_authors,
    fetch_and_cache,
)
from citationclaw.core.scholar_profile_pipeline import enrich_citing_authors


@pytest.fixture
def db(tmp_path):
    d = ArxivDB(db_path=tmp_path / "arxiv_test.db")
    yield d
    d.close()


# ── normalizers ────────────────────────────────────────────────────────

def test_normalize_title_basic():
    assert normalize_title("Deep Learning: A Survey, 2020!") == "deep learning a survey 2020"


def test_normalize_title_whitespace():
    assert normalize_title("  Foo   BAR  ") == "foo bar"


def test_normalize_title_cjk_preserved():
    assert normalize_title("深度学习：综述") == "深度学习 综述"


def test_normalize_title_empty():
    assert normalize_title("") == ""
    assert normalize_title(None) == ""


def test_normalize_arxiv_id_strips_version():
    assert normalize_arxiv_id("2401.00001v2") == "2401.00001"
    assert normalize_arxiv_id("hep-th/9901001v1") == "hep-th/9901001"
    assert normalize_arxiv_id("2401.00001") == "2401.00001"
    assert normalize_arxiv_id("") == ""


def test_normalize_authors_mixed():
    raw = [{"name": "Alice Smith"}, "Bob Jones", {"name": ""}, None, "  "]
    assert normalize_authors(raw) == ["Alice Smith", "Bob Jones"]
    assert normalize_authors(None) == []


# ── ArxivDB core ───────────────────────────────────────────────────────

def test_upsert_and_lookup_by_id(db):
    assert db.upsert({"arxiv_id": "2401.00001", "title": "Paper A",
                      "authors": ["Alice", "Bob"], "year": "2024"}) is True
    rec = db.lookup_by_id("2401.00001v2")  # version suffix stripped
    assert rec is not None
    assert rec["title"] == "Paper A"
    assert rec["authors"] == ["Alice", "Bob"]
    assert rec["year"] == 2024


def test_upsert_update_not_duplicate(db):
    db.upsert({"arxiv_id": "2401.00001", "title": "Old Title", "authors": ["A"]})
    db.upsert({"arxiv_id": "2401.00001", "title": "New Title", "authors": ["B", "C"]})
    assert db.count() == 1
    rec = db.lookup_by_id("2401.00001")
    assert rec["title"] == "New Title"
    assert rec["authors"] == ["B", "C"]


def test_upsert_rejects_invalid(db):
    assert db.upsert({"arxiv_id": "", "title": "X"}) is False
    assert db.upsert({"arxiv_id": "1.0", "title": ""}) is False
    assert db.count() == 0


def test_lookup_by_title_exact(db):
    db.upsert({"arxiv_id": "2401.00001", "title": "Deep Learning: A Survey",
               "authors": ["Alice"]})
    rec = db.lookup_by_title("deep learning a survey")  # normalized match
    assert rec is not None
    assert rec["arxiv_id"] == "2401.00001"
    assert "_fuzzy" not in rec


def test_lookup_by_title_fuzzy(db):
    db.upsert({"arxiv_id": "2401.00001",
               "title": "Attention Is All You Need For Transformers",
               "authors": ["A"]})
    # minor word change → fuzzy hit (shares long word 'transformers')
    rec = db.lookup_by_title("Attention Is All You Need For Transformer")
    assert rec is not None
    assert rec["_fuzzy"] >= 0.80
    assert rec["arxiv_id"] == "2401.00001"


def test_lookup_by_title_no_hit(db):
    db.upsert({"arxiv_id": "2401.00001", "title": "Quantum Entanglement Review",
               "authors": ["A"]})
    assert db.lookup_by_title("completely different topic words here") is None
    assert db.lookup_by_title("") is None


def test_lookup_by_id_miss(db):
    assert db.lookup_by_id("9999.99999") is None
    assert db.lookup_by_id("") is None


def test_count_empty(db):
    assert db.count() == 0


# ── fetch_and_cache ────────────────────────────────────────────────────

def test_fetch_and_cache_by_id(db):
    client = MagicMock()
    client.get_paper = AsyncMock(return_value={
        "arxiv_id": "2401.00001", "title": "Cached Paper",
        "authors": [{"name": "Alice"}, {"name": "Bob"}], "year": 2024,
    })
    rec = asyncio.get_event_loop().run_until_complete(
        fetch_and_cache(db, client, arxiv_id="2401.00001")
    )
    assert rec is not None
    assert db.count() == 1
    assert db.lookup_by_id("2401.00001")["authors"] == ["Alice", "Bob"]
    client.get_paper.assert_awaited_once_with("2401.00001")


def test_fetch_and_cache_title_mismatch_not_cached(db):
    client = MagicMock()
    client.search_paper = AsyncMock(return_value={
        "arxiv_id": "2402.00002", "title": "Totally Different Paper",
        "authors": [{"name": "X"}], "year": 2024,
    })
    rec = asyncio.get_event_loop().run_until_complete(
        fetch_and_cache(db, client, title="Attention Is All You Need")
    )
    assert rec is None
    assert db.count() == 0


def test_fetch_and_cache_failure_swallowed(db):
    client = MagicMock()
    client.get_paper = AsyncMock(side_effect=RuntimeError("boom"))
    rec = asyncio.get_event_loop().run_until_complete(
        fetch_and_cache(db, client, arxiv_id="2401.00001", log=lambda m: None)
    )
    assert rec is None
    assert db.count() == 0


def test_fetch_and_cache_no_id_no_title(db):
    client = MagicMock()
    client.get_paper = AsyncMock()
    client.search_paper = AsyncMock()
    rec = asyncio.get_event_loop().run_until_complete(
        fetch_and_cache(db, client)
    )
    assert rec is None
    client.get_paper.assert_not_awaited()
    client.search_paper.assert_not_awaited()


# ── enrich_citing_authors ──────────────────────────────────────────────

def test_enrich_db_hit_replaces_authors(db):
    db.upsert({"arxiv_id": "2401.00001", "title": "T",
               "authors": ["A1", "A2", "A3"]})
    client = MagicMock()
    client.get_paper = AsyncMock()
    citing = [{"arxiv_id": "2401.00001v2", "authors": [{"name": "A1"}]}]
    stats = asyncio.get_event_loop().run_until_complete(
        enrich_citing_authors(citing, db, client, lambda m: None)
    )
    assert stats["hit"] == 1
    assert stats["fetched"] == 0
    assert [a["name"] for a in citing[0]["authors"]] == ["A1", "A2", "A3"]
    client.get_paper.assert_not_awaited()


def test_enrich_fetches_when_miss(db):
    client = MagicMock()
    client.get_paper = AsyncMock(return_value={
        "arxiv_id": "2401.00001", "title": "T",
        "authors": [{"name": "Full1"}, {"name": "Full2"}], "year": 2024,
    })
    citing = [{"arxiv_id": "2401.00001", "title": "T", "authors": [{"name": "Full1"}]}]
    stats = asyncio.get_event_loop().run_until_complete(
        enrich_citing_authors(citing, db, client, lambda m: None)
    )
    assert stats["fetched"] == 1
    assert [a["name"] for a in citing[0]["authors"]] == ["Full1", "Full2"]
    # now cached for next run
    assert db.lookup_by_id("2401.00001") is not None


def test_enrich_skips_papers_without_arxiv_id(db):
    client = MagicMock()
    client.get_paper = AsyncMock()
    citing = [{"title": "No arXiv", "authors": [{"name": "A"}]}]
    stats = asyncio.get_event_loop().run_until_complete(
        enrich_citing_authors(citing, db, client, lambda m: None)
    )
    assert stats == {"hit": 0, "fetched": 0, "missed": 0}
    assert [a["name"] for a in citing[0]["authors"]] == ["A"]
    client.get_paper.assert_not_awaited()


def test_enrich_budget_caps_fetches(db):
    client = MagicMock()
    client.get_paper = AsyncMock(return_value={
        "arxiv_id": "1.0", "title": "T", "authors": [{"name": "X"}],
    })
    citing = [
        {"arxiv_id": f"1.{i:04d}", "title": "T", "authors": []}
        for i in range(5)
    ]
    stats = asyncio.get_event_loop().run_until_complete(
        enrich_citing_authors(citing, db, client, lambda m: None, max_fetch=2)
    )
    assert stats["fetched"] == 2
    assert stats["missed"] == 3
    assert client.get_paper.await_count == 2


def test_enrich_no_db_returns_zero(db):
    citing = [{"arxiv_id": "1.0", "authors": []}]
    stats = asyncio.get_event_loop().run_until_complete(
        enrich_citing_authors(citing, None, None, lambda m: None)
    )
    assert stats == {"hit": 0, "fetched": 0, "missed": 0}
