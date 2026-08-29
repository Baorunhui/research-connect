from __future__ import annotations

import json

from research_connect_core import FileCacheIndex


def test_payload_is_file_and_sqlite_is_only_index(tmp_path):
    cache = FileCacheIndex(tmp_path)
    path = cache.put_json("papers", "arxiv:1234", {"title": "demo", "items": [1, 2]})

    assert path.suffix == ".json"
    assert json.loads(path.read_text(encoding="utf-8"))["title"] == "demo"
    assert cache.get_json("papers", "arxiv:1234")["items"] == [1, 2]
    assert cache.count("papers") == 1
    assert cache.db_path.is_file()


def test_cache_keys_cannot_escape_data_root(tmp_path):
    cache = FileCacheIndex(tmp_path)
    path = cache.put_json("pdf", "../../outside", {"ok": True})
    assert path.is_relative_to(cache.files_dir)
