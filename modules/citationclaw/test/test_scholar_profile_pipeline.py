import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from citationclaw.core.scholar_profile_pipeline import (
    filter_top_papers,
    db_hit_to_renowned,
    match_authors_with_db,
    fetch_target_citations,
    build_citing_record,
    collect_target_authors,
    is_self_citation,
)
from citationclaw.core.pipeline_adapter import PipelineAdapter
from citationclaw.core.self_citation import SelfCitationDetector


# ── filter_top_papers ──────────────────────────────────────────────────

def test_filter_top_n_cap():
    papers = [
        {"title": "A", "citations": 100},
        {"title": "B", "citations": 50},
        {"title": "C", "citations": 10},
    ]
    result = filter_top_papers(papers, top_n=2)
    assert len(result) == 2
    assert result[0]["title"] == "A"
    assert result[1]["title"] == "B"


def test_filter_min_citations():
    papers = [
        {"title": "A", "citations": 100},
        {"title": "B", "citations": 50},
        {"title": "C", "citations": 10},
    ]
    result = filter_top_papers(papers, min_citations=50)
    assert len(result) == 2
    assert result[0]["title"] == "A"
    assert result[1]["title"] == "B"


def test_filter_top_n_and_min():
    papers = [
        {"title": "A", "citations": 100},
        {"title": "B", "citations": 50},
        {"title": "C", "citations": 10},
    ]
    result = filter_top_papers(papers, top_n=1, min_citations=50)
    assert len(result) == 1
    assert result[0]["title"] == "A"


def test_filter_no_limits():
    papers = [{"title": "A", "citations": 0}]
    result = filter_top_papers(papers, top_n=0, min_citations=0)
    assert len(result) == 1


def test_filter_empty():
    assert filter_top_papers([], top_n=10) == []


def test_filter_no_citation_key():
    papers = [{"title": "A"}, {"title": "B", "citations": 5}]
    result = filter_top_papers(papers, min_citations=1)
    assert len(result) == 1
    assert result[0]["title"] == "B"


# ── db_hit_to_renowned ─────────────────────────────────────────────────

def test_db_hit_to_renowned_list_honors():
    db_hit = {
        "name": "张三", "name_en": "San Zhang",
        "affiliation": "清华大学", "country": "中国",
        "title": "教授", "honors": ["长江学者", "杰青"],
    }
    r = db_hit_to_renowned(db_hit, "San Zhang")
    assert r["name"] == "San Zhang"
    assert r["tier"] == "教授"
    assert r["honors"] == ["长江学者", "杰青"]
    assert r["affiliation"] == "清华大学"
    assert r["country"] == "中国"
    assert r["position"] == "教授"


def test_db_hit_to_renowned_str_honors():
    db_hit = {
        "name": "李四", "name_en": "Si Li",
        "affiliation": "北京大学", "country": "中国",
        "title": "院士", "honors": "IEEE Fellow,AAAI Fellow",
    }
    r = db_hit_to_renowned(db_hit, "Si Li")
    assert r["honors"] == ["IEEE Fellow", "AAAI Fellow"]


def test_db_hit_to_renowned_empty_honors():
    db_hit = {
        "name": "王五", "name_en": "",
        "affiliation": "", "country": "",
        "title": "", "honors": [],
    }
    r = db_hit_to_renowned(db_hit, "Wang Wu")
    assert r["honors"] == []
    assert r["tier"] == ""


# ── match_authors_with_db ──────────────────────────────────────────────

def test_match_authors_with_hit():
    db = MagicMock()
    hit = {
        "name": "张三", "name_en": "San Zhang",
        "affiliation": "清华大学", "country": "中国",
        "title": "教授", "honors": ["长江学者"],
    }
    db.lookup = MagicMock(side_effect=lambda n: hit if n == "San Zhang" else None)
    authors = [{"name": "San Zhang"}, {"name": "Nobody"}]
    result = match_authors_with_db(authors, db)
    assert len(result) == 1
    assert result[0]["name"] == "San Zhang"
    assert result[0]["tier"] == "教授"


def test_match_authors_no_hit():
    db = MagicMock()
    db.lookup = MagicMock(return_value=None)
    authors = [{"name": "Nobody"}, {"name": "Someone"}]
    result = match_authors_with_db(authors, db)
    assert result == []


def test_match_authors_empty():
    db = MagicMock()
    assert match_authors_with_db([], db) == []


def test_match_authors_dedup():
    db = MagicMock()
    db.lookup = MagicMock(return_value={
        "name": "张三", "name_en": "San Zhang",
        "affiliation": "清华", "country": "中国",
        "title": "教授", "honors": [],
    })
    authors = [{"name": "San Zhang"}, {"name": "San Zhang"}]
    result = match_authors_with_db(authors, db)
    assert len(result) == 1


def test_match_authors_skips_empty_name():
    db = MagicMock()
    db.lookup = MagicMock(return_value=None)
    authors = [{"name": ""}, {"name": "  "}, {"name": "Real Person"}]
    result = match_authors_with_db(authors, db)
    assert result == []
    assert db.lookup.call_count == 1  # only "Real Person" queried


# ── collect_target_authors ─────────────────────────────────────────────

def test_collect_target_authors():
    meta = {"authors": [{"name": "Alice"}, {"name": "Bob"}]}
    result = collect_target_authors(meta)
    assert len(result) == 2
    assert result[0]["name"] == "Alice"


def test_collect_target_authors_none():
    assert collect_target_authors(None) == []


def test_collect_target_authors_no_authors_key():
    assert collect_target_authors({"title": "X"}) == []


# ── is_self_citation ───────────────────────────────────────────────────

def test_is_self_citation_true():
    detector = SelfCitationDetector()
    target = [{"name": "Alice Smith"}]
    citing = [{"name": "Alice Smith"}]
    result = is_self_citation(detector, target, citing)
    assert result["is_self_citation"] is True


def test_is_self_citation_false():
    detector = SelfCitationDetector()
    target = [{"name": "Alice Smith"}]
    citing = [{"name": "Bob Jones"}]
    result = is_self_citation(detector, target, citing)
    assert result["is_self_citation"] is False


# ── build_citing_record ────────────────────────────────────────────────

def test_build_citing_record():
    adapter = PipelineAdapter()
    citing = {
        "title": "Citing Paper X",
        "year": 2022,
        "authors": [
            {"name": "Alice Smith", "affiliation": "MIT", "s2_id": "A1"},
            {"name": "Bob Jones", "affiliation": "Stanford", "s2_id": "A2"},
        ],
        "cited_by_count": 15,
        "doi": "10.1234/xyz",
        "s2_id": "S2ID123",
        "venue": "NeurIPS",
        "pdf_url": "https://arxiv.org/pdf/2201.00001",
    }
    self_cite = {"is_self_citation": False, "method": "none", "matched_pair": None}
    renowned = [{"name": "Alice Smith", "tier": "Fellow", "honors": ["IEEE Fellow"],
                 "affiliation": "MIT", "country": "US"}]
    record = build_citing_record(adapter, citing, "Target Paper T",
                                  self_cite, renowned, 0)
    assert "0" in record
    inner = record["0"]
    assert inner["Paper_Title"] == "Citing Paper X"
    assert inner["Citing_Paper"] == "Target Paper T"
    assert inner["Is_Self_Citation"] is False
    assert "MIT" in inner.get("First_Author_Institution", "")
    assert "Alice Smith" in inner.get("Authors_Affiliation", "")
    assert "IEEE Fellow" in str(inner.get("Renowned Scholar", ""))
    assert inner.get("PDF_Download") is False
    assert inner.get("PDF_Path") == ""


def test_build_citing_record_no_renowned():
    adapter = PipelineAdapter()
    citing = {
        "title": "Plain Paper",
        "year": 2021,
        "authors": [{"name": "Nobody", "affiliation": ""}],
        "cited_by_count": 0,
        "s2_id": "",
    }
    self_cite = {"is_self_citation": False, "method": "none"}
    record = build_citing_record(adapter, citing, "Target", self_cite, [], 5)
    inner = record["5"]
    assert inner["Paper_Title"] == "Plain Paper"
    assert inner.get("Formated Renowned Scholar", []) == []


def test_build_citing_record_self_cite():
    adapter = PipelineAdapter()
    citing = {
        "title": "Self Cite Paper",
        "year": 2020,
        "authors": [{"name": "Alice Smith", "affiliation": "MIT"}],
        "cited_by_count": 3,
        "s2_id": "S2X",
    }
    self_cite = {"is_self_citation": True, "method": "exact", "matched_pair": ("Alice Smith", "Alice Smith")}
    record = build_citing_record(adapter, citing, "Target T", self_cite, [], 3)
    inner = record["3"]
    assert inner["Is_Self_Citation"] is True


# ── fetch_target_citations ─────────────────────────────────────────────

def test_fetch_target_citations():
    s2 = MagicMock()
    s2.search_paper = AsyncMock(return_value={"s2_id": "ABC", "title": "Target 1"})
    s2.get_paper_citations = AsyncMock(return_value=[
        {"title": "Citing A", "authors": [{"name": "X"}], "s2_id": "c1"},
        {"title": "Citing B", "authors": [{"name": "Y"}], "s2_id": "c2"},
    ])
    target_papers = [{"title": "Target 1", "citations": 100}]
    results = asyncio.get_event_loop().run_until_complete(
        fetch_target_citations(s2, target_papers, log=lambda msg: None)
    )
    assert len(results) == 1
    tp, meta, citing = results[0]
    assert tp["title"] == "Target 1"
    assert meta["s2_id"] == "ABC"
    assert len(citing) == 2
    assert citing[0]["title"] == "Citing A"


def test_fetch_target_citations_unresolved():
    s2 = MagicMock()
    s2.search_paper = AsyncMock(return_value=None)
    s2.get_paper_citations = AsyncMock(return_value=[])
    target_papers = [{"title": "Unknown Paper", "citations": 1}]
    results = asyncio.get_event_loop().run_until_complete(
        fetch_target_citations(s2, target_papers, log=lambda msg: None)
    )
    assert len(results) == 1
    tp, meta, citing = results[0]
    assert meta is None
    assert citing == []


def test_fetch_target_citations_cancel():
    s2 = MagicMock()
    s2.search_paper = AsyncMock(return_value={"s2_id": "X", "title": "T"})
    s2.get_paper_citations = AsyncMock(return_value=[])
    target_papers = [{"title": "T", "citations": 1}]
    results = asyncio.get_event_loop().run_until_complete(
        fetch_target_citations(s2, target_papers, log=lambda msg: None,
                               cancel_check=lambda: True)
    )
    assert len(results) == 1
    tp, meta, citing = results[0]
    assert meta is None
    assert citing == []


def test_fetch_target_citations_multiple():
    s2 = MagicMock()

    async def _search(title):
        return {"s2_id": f"id_{title}", "title": title}

    async def _citations(pid, max_papers=5000):
        return [{"title": f"cite of {pid}", "authors": [], "s2_id": "x"}]

    s2.search_paper = _search
    s2.get_paper_citations = _citations
    target_papers = [
        {"title": "A", "citations": 100},
        {"title": "B", "citations": 50},
    ]
    results = asyncio.get_event_loop().run_until_complete(
        fetch_target_citations(s2, target_papers, log=lambda msg: None)
    )
    assert len(results) == 2
    assert results[0][1]["s2_id"] == "id_A"
    assert results[1][1]["s2_id"] == "id_B"
    assert len(results[0][2]) == 1
