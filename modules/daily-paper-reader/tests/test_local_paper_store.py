# -*- coding: utf-8 -*-
"""本地论文库（local_paper_store）测试：upsert 幂等、窗口过滤、FTS 检索。"""
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
  sys.path.insert(0, str(SRC_DIR))

import pytest

from local_paper_store import LocalPaperStore


@pytest.fixture()
def store(tmp_path):
  with LocalPaperStore(tmp_path / "index.sqlite3") as s:
    yield s


def _paper(pid, title="Deep Assembly Learning", abstract="robot assembly via neural nets", published="2026-08-25T10:00:00+00:00"):
  return {
    "id": pid,
    "source": "arxiv",
    "title": title,
    "abstract": abstract,
    "authors": ["Alice", "Bob"],
    "primary_category": "cs.CV",
    "categories": ["cs.CV", "cs.RO"],
    "published": published,
    "updated": published,
    "link": f"https://arxiv.org/abs/{pid}",
    "pdf_url": f"https://arxiv.org/pdf/{pid}",
  }


def test_upsert_dedupe_and_roundtrip(store):
  assert store.upsert_papers([_paper("2601.00001", published="2026-08-25T10:00:00+00:00")]) == 1
  # 同 id 重复 upsert 幂等（更新而非新增）
  assert store.upsert_papers([_paper("2601.00001", title="Updated Title", published="2026-08-25T10:00:00+00:00")]) == 1
  papers = store.get_papers(["2601.00001"])
  assert papers["2601.00001"]["title"] == "Updated Title"
  assert papers["2601.00001"]["authors"] == ["Alice", "Bob"]
  # 无 id 的行被忽略
  assert store.upsert_papers([{"title": "no id"}]) == 0


def test_count_window_is_left_closed_right_open(store):
  store.upsert_papers([
    _paper("a", published="2026-08-19T23:00:00+00:00"),
    _paper("b", published="2026-08-20T00:00:00+00:00"),
    _paper("c", published="2026-08-27T00:00:00+00:00"),
  ])
  assert store.count_window("2026-08-20T00:00:00", "2026-08-27T00:00:00") == 1  # b
  assert store.count_window("2026-08-19T00:00:00", "2026-08-28T00:00:00") == 3  # a(08-19T23), b, c


def test_search_fts_matches_and_orders(store):
  store.upsert_papers([
    _paper("hit1", title="3D assembly robot learning", abstract="pose estimation"),
    _paper("hit2", title="Assembly line survey", abstract="factory automation"),
    _paper("miss", title="Protein folding", abstract="bio chemistry"),
  ])
  # 词命中
  hits = store.search_fts("assembly", "2026-08-01T00:00:00", "2026-09-01T00:00:00", limit=10)
  assert {h["id"] for h in hits} == {"hit1", "hit2"}
  # AND 语义：双词都出现
  hits = store.search_fts("assembly robot", "2026-08-01T00:00:00", "2026-09-01T00:00:00", limit=10)
  assert {h["id"] for h in hits} == {"hit1"}
  # 窗口过滤生效
  hits = store.search_fts("assembly", "2026-09-01T00:00:00", "2026-09-09T00:00:00", limit=10)
  assert hits == []


def test_search_fts_or_expression(store):
  store.upsert_papers([
    _paper("x", title="vision language model", abstract="VLM grounding"),
    _paper("y", title="object detection", abstract="YOLO detector"),
  ])
  hits = store.search_fts("vision OR detection", "2026-08-01T00:00:00", "2026-09-01T00:00:00", limit=10)
  assert {h["id"] for h in hits} == {"x", "y"}


def test_search_fts_rank_is_contiguous(store):
  store.upsert_papers([
    _paper("r1", title="robot assembly", abstract="assembly assembly"),
    _paper("r2", title="robot arm", abstract="kinematics"),
  ])
  hits = store.search_fts("robot", "2026-08-01T00:00:00", "2026-09-01T00:00:00", limit=10)
  assert [h["id"] for h in hits]  # 有结果
  # 元数据字段与契约一致（lane JSON 的 paper 字段来源）
  expected = {"id", "source", "title", "abstract", "authors", "primary_category", "categories", "published", "updated", "link", "pdf_url"}
  assert expected <= set(hits[0].keys())
