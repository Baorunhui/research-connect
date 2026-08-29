import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from citationclaw.core.pdf_parse_cache import PDFParseCache


def test_cache_store_and_get(tmp_path):
    cache = PDFParseCache(base_dir=tmp_path)
    assert not cache.has("abc123")
    cache.store("abc123", {"title": "Test", "source": "mineru"})
    assert cache.has("abc123")
    meta = cache.get_meta("abc123")
    assert meta["title"] == "Test"


def test_cache_persistence(tmp_path):
    cache1 = PDFParseCache(base_dir=tmp_path)
    cache1.store("key1", {"title": "Paper A"})
    # Reload from disk
    cache2 = PDFParseCache(base_dir=tmp_path)
    assert cache2.has("key1")


def test_cache_authors(tmp_path):
    cache = PDFParseCache(base_dir=tmp_path)
    cache.store("key1", {"title": "Test"})
    cache.store_authors("key1", [{"name": "Alice", "affiliation": "MIT"}])
    authors = cache.get_authors("key1")
    assert len(authors) == 1
    assert authors[0]["name"] == "Alice"


def test_cache_stats(tmp_path):
    cache = PDFParseCache(base_dir=tmp_path)
    cache.store("k1", {"title": "A"})
    cache.store("k2", {"title": "B"})
    cache.store_authors("k1", [])
    stats = cache.stats()
    assert stats["total"] == 2
    assert stats["with_authors"] == 1
