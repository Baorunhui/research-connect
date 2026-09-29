import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from citationclaw.core import kaggle_arxiv_meta as km
from citationclaw.core.arxiv_db import ArxivDB
from citationclaw.core.metadata_collector import MetadataCollector


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_index(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "abstract TEXT NOT NULL, authors TEXT, categories TEXT, published TEXT, "
        "update_date TEXT, doi TEXT, journal_ref TEXT, authors_json TEXT, title_norm TEXT)"
    )
    conn.execute(
        "INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("1706.03762", "Attention Is All You Need", "Transformers.",
         "Ashish Vaswani, Noam Shazeer", "cs.CL", "2017-06-12", "2023-08-02",
         "10.5555/attention", "NeurIPS 2017",
         json.dumps(["Ashish Vaswani", "Noam Shazeer"]),
         km.normalize_title("Attention Is All You Need")),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def index(tmp_path, monkeypatch):
    path = tmp_path / "index.sqlite3"
    _make_index(path)
    meta = km.KaggleArxivMeta(path)
    monkeypatch.setattr(km, "_shared", meta)
    yield meta
    meta.close()


def test_lookup_by_id_title_doi_url(index):
    assert index.lookup_by_id("arXiv:1706.03762v5")["title"] == "Attention Is All You Need"
    assert index.lookup_by_title("attention is all you need!")["arxiv_id"] == "1706.03762"
    assert index.lookup_by_doi("https://doi.org/10.5555/ATTENTION")["arxiv_id"] == "1706.03762"
    rec = index.lookup(url="https://arxiv.org/pdf/1706.03762v7.pdf")
    assert rec["year"] == 2017
    assert rec["venue"] == "NeurIPS 2017"
    assert rec["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
    assert index.lookup_by_id("9999.99999") is None


def test_missing_index_is_unavailable(tmp_path):
    meta = km.KaggleArxivMeta(tmp_path / "none.sqlite3")
    assert not meta.available
    assert meta.lookup(title="Attention Is All You Need") is None


def test_collector_local_hit_skips_external(index):
    collector = MetadataCollector()
    collector.s2.search_paper = AsyncMock(side_effect=AssertionError("S2 must not be called"))
    collector.openalex.search_work = AsyncMock(side_effect=AssertionError("OpenAlex must not be called"))
    collector.arxiv.search_paper = AsyncMock(side_effect=AssertionError("arXiv must not be called"))
    result = _run(collector.collect("Attention Is All You Need"))
    assert result["sources"] == ["kaggle_arxiv"]
    assert result["arxiv_id"] == "1706.03762"
    assert result["authors"][0]["name"] == "Ashish Vaswani"


def test_collector_local_miss_falls_back_to_s2(index):
    collector = MetadataCollector()
    collector.s2.search_paper = AsyncMock(return_value=None)
    collector.openalex.search_work = AsyncMock(return_value=None)
    collector.arxiv.search_paper = AsyncMock(return_value=None)
    assert _run(collector.collect("A paper that is not in the snapshot")) is None
    collector.s2.search_paper.assert_awaited_once()


def test_arxiv_db_falls_back_to_kaggle(index, tmp_path):
    db = ArxivDB(db_path=tmp_path / "arxiv.db")
    try:
        assert db.lookup_by_id("1706.03762")["source"] == "kaggle_arxiv"
        assert db.lookup_by_title("Attention is all you need")["arxiv_id"] == "1706.03762"
        assert db.count() == 0
    finally:
        db.close()


def test_enrich_papers_local(index):
    papers = [{"title": "Attention Is All You Need", "year": None, "citations": 1},
              {"title": "Unknown paper title here", "year": 2020}]
    assert km.enrich_papers_local(papers) == 1
    assert papers[0]["arxiv_id"] == "1706.03762"
    assert papers[0]["year"] == 2017
    assert papers[0]["metadata_source"] == "kaggle_arxiv"
    assert "arxiv_id" not in papers[1]
