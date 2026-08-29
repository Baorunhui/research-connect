"""Tests for S2-first metadata collector."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from citationclaw.core.metadata_collector import MetadataCollector


def test_build_from_s2():
    collector = MetadataCollector()
    s2 = {
        "title": "Paper A", "year": 2020, "doi": "10.1234",
        "arxiv_id": "2001.00001",
        "cited_by_count": 480, "influential_citation_count": 50, "s2_id": "P1",
        "authors": [{"name": "Alice", "s2_id": "SA1", "affiliation": "MIT"}],
        "pdf_url": "https://example.com/paper.pdf",
        "venue": "NeurIPS",
        "_external_ids": {"ArXiv": "2001.00001", "DOI": "10.1234"},
        "source": "s2",
    }
    oa = {
        "title": "Paper A", "year": 2020, "doi": "10.1234",
        "cited_by_count": 500, "openalex_id": "W1",
        "authors": [{"name": "Alice", "affiliation": "MIT", "country": "US", "openalex_id": "A1"}],
        "oa_pdf_url": "https://oa.example.com/paper.pdf",
        "venue": "NeurIPS 2020",
        "source": "openalex",
    }
    result = collector._build_from_s2(s2, oa_supplement=oa)
    assert result["title"] == "Paper A"
    assert result["s2_id"] == "P1"
    assert result["arxiv_id"] == "2001.00001"
    assert result["pdf_url"] == "https://example.com/paper.pdf"
    assert result["oa_pdf_url"] == "https://oa.example.com/paper.pdf"
    assert "s2" in result["sources"]
    assert "openalex" in result["sources"]


def test_build_from_s2_no_oa():
    collector = MetadataCollector()
    s2 = {
        "title": "Paper B", "year": 2022, "doi": "",
        "arxiv_id": "2201.54321",
        "cited_by_count": 200, "influential_citation_count": 20, "s2_id": "P2",
        "authors": [{"name": "Carol", "s2_id": "SA2", "affiliation": "Stanford"}],
        "pdf_url": "https://arxiv.org/pdf/2201.54321",  # _parse_paper fills arXiv fallback
        "venue": "ICML",
        "_external_ids": {"ArXiv": "2201.54321"},
        "source": "s2",
    }
    result = collector._build_from_s2(s2, oa_supplement=None)
    assert result["title"] == "Paper B"
    assert result["arxiv_id"] == "2201.54321"
    # pdf_url should fallback to arxiv
    assert "arxiv.org" in result["pdf_url"]


def test_build_from_fallback_oa_only():
    collector = MetadataCollector()
    oa = {
        "title": "Paper C", "year": 2021, "doi": "10.5678",
        "cited_by_count": 100, "openalex_id": "W2",
        "authors": [{"name": "Bob", "affiliation": "Stanford", "country": "US", "openalex_id": "A2"}],
        "oa_pdf_url": "",
        "venue": "",
        "source": "openalex",
    }
    result = collector._build_from_fallback(oa, None)
    assert result["title"] == "Paper C"
    assert result["influential_citation_count"] == 0
    assert result["s2_id"] == ""


def test_build_from_fallback_arxiv_only():
    collector = MetadataCollector()
    arxiv = {
        "title": "Paper D", "arxiv_id": "2301.00001", "year": 2023,
        "authors": [{"name": "Dave", "source": "arxiv"}],
        "pdf_url": "https://arxiv.org/pdf/2301.00001",
        "source": "arxiv",
    }
    result = collector._build_from_fallback(None, arxiv)
    assert result["arxiv_id"] == "2301.00001"
    assert "arxiv.org" in result["pdf_url"]


def test_enrich_s2_authors():
    s2_authors = [
        {"name": "Alice", "s2_id": "SA1", "affiliation": ""},
    ]
    oa_authors = [
        {"name": "Alice", "openalex_id": "A1", "affiliation": "MIT", "country": "US"},
    ]
    MetadataCollector._enrich_s2_authors(s2_authors, oa_authors)
    assert s2_authors[0]["affiliation"] == "MIT"
    assert s2_authors[0]["openalex_id"] == "A1"
