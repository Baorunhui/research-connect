# -*- coding: utf-8 -*-
"""recall_local 测试：lane 输出契约 + 与 Step 2.3 RRF 的下游兼容。

契约锚点（来自 2.1/2.2 实际输出，勿随意改动）：
- lane 文件：{ top_k, generated_at, papers[], queries[] }
- papers 元数据字段：id/source/title/abstract/authors/primary_category/categories/published/link/pdf_url/tags
- queries[].sim_scores：{pid: {score, rank}}，rank 从 1 连续递增
- 2.3 只消费 queries[].sim_scores 的 rank 做 RRF 融合
"""
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
  sys.path.insert(0, str(SRC_DIR))

from local_paper_store import LocalPaperStore  # noqa: E402


def _load_module(name: str, filename: str):
  spec = importlib.util.spec_from_file_location(name, SRC_DIR / filename)
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


recall_local = _load_module("recall_local_under_test", "recall_local.py")

PAPER_FIELDS = {
  "id", "source", "title", "abstract", "authors", "primary_category",
  "categories", "published", "updated", "link", "pdf_url",
}

CONFIG = {
  "arxiv_paper_setting": {"days_window": 9},
  "subscriptions": {
    "intent_profiles": [
      {
        "tag": "ASM",
        "enabled": True,
        "keywords": [{"query": "robot assembly", "keyword": "robot assembly"}],
        "intent_queries": [{"query": "Find recent papers on category-level pose estimation for object assembly"}],
      }
    ]
  },
}


@pytest.fixture()
def seeded_store(tmp_path):
  with LocalPaperStore(tmp_path / "index.sqlite3") as store:
    store.upsert_papers([
      {
        "id": "2601.00001", "source": "arxiv",
        "title": "Category-level pose estimation for robot assembly",
        "abstract": "We estimate 6D pose of unseen objects in assembly scenes.",
        "authors": ["A"], "primary_category": "cs.CV", "categories": ["cs.CV"],
        "published": "2026-08-25T10:00:00+00:00", "updated": "2026-08-25T10:00:00+00:00",
        "link": "", "pdf_url": "",
      },
      {
        "id": "2601.00002", "source": "arxiv",
        "title": "Unrelated protein folding work",
        "abstract": "AlphaFold style structure prediction.",
        "authors": ["B"], "primary_category": "q-bio.BM", "categories": ["q-bio.BM"],
        "published": "2026-08-26T10:00:00+00:00", "updated": "2026-08-26T10:00:00+00:00",
        "link": "", "pdf_url": "",
      },
    ])
    yield store


def _patch_paths(monkeypatch, tmp_path):
  db = tmp_path / "index.sqlite3"
  (tmp_path / "out").mkdir(parents=True, exist_ok=True)  # 生产环境由 output_paths() 建，测试里补上
  monkeypatch.setenv("DPR_RUN_DATE", "20260820-20260829")
  # 输出与库都指到 tmp
  monkeypatch.setattr(recall_local, "output_paths", lambda: (
    tmp_path / "out" / "arxiv_papers_20260820-20260829.bm25.json",
    tmp_path / "out" / "arxiv_papers_20260820-20260829.embedding.json",
    tmp_path / "out",
  ))
  return str(db)


def _patch_fake_embed(monkeypatch):
  """确定性假 embedding：含 'pose' 的文本互相接近，其余远离。"""
  calls = {"n": 0}

  def fake_embed(texts, *, provider=None, allow_local_fallback=True):
    calls["n"] += 1
    vecs = []
    for t in texts:
      vecs.append([1.0, 0.0] if "pose" in t.lower() else [0.0, 1.0])
    arr = np.asarray(vecs, dtype=np.float32)
    return arr, "fake"

  monkeypatch.setattr(recall_local, "embed_texts", fake_embed)
  return calls


def test_recall_local_lane_contract(tmp_path, monkeypatch, seeded_store):
  db = _patch_paths(monkeypatch, tmp_path)
  _patch_fake_embed(monkeypatch)
  config_path = tmp_path / "config.yaml"
  config_path.write_text(yaml.safe_dump(CONFIG, allow_unicode=True), encoding="utf-8")

  recall_local.main_with_args(["--config", str(config_path), "--db", db])

  bm25 = json.loads((tmp_path / "out" / "arxiv_papers_20260820-20260829.bm25.json").read_text(encoding="utf-8"))
  emb = json.loads((tmp_path / "out" / "arxiv_papers_20260820-20260829.embedding.json").read_text(encoding="utf-8"))

  for lane, name in ((bm25, "bm25"), (emb, "embedding")):
    assert {"top_k", "generated_at", "papers", "queries"} <= set(lane.keys()), name
    for paper in lane["papers"]:
      assert PAPER_FIELDS <= set(paper.keys()), f"{name} paper 字段缺失"
      assert isinstance(paper["tags"], list) and paper["tags"]
    for query in lane["queries"]:
      assert query["query_text"], name
      ranks = [meta["rank"] for meta in query["sim_scores"].values()]
      assert ranks == list(range(1, len(ranks) + 1)), f"{name} rank 必须从 1 连续"

  # bm25 lane：AND 语义只命中相关论文（Folding 不含 assembly/robot）
  bm25_ids = {p["id"] for p in bm25["papers"]}
  assert "2601.00002" not in bm25_ids
  # 语义 lane：粗筛是 OR 式会带上 Folding，但精排 top 应以 pose 相关论文靠前
  assert emb["queries"][0]["sim_scores"]["2601.00001"]["rank"] == 1


def test_recall_local_empty_window_writes_empty_lanes(tmp_path, monkeypatch):
  db = str(tmp_path / "empty.sqlite3")
  _patch_paths(monkeypatch, tmp_path)
  config_path = tmp_path / "config.yaml"
  config_path.write_text(yaml.safe_dump(CONFIG, allow_unicode=True), encoding="utf-8")

  recall_local.main_with_args(["--config", str(config_path), "--db", db])

  bm25 = json.loads((tmp_path / "out" / "arxiv_papers_20260820-20260829.bm25.json").read_text(encoding="utf-8"))
  assert bm25["papers"] == [] and bm25["queries"] == []


def test_recall_lanes_fuse_through_step_2_3_rrf(tmp_path, monkeypatch, seeded_store):
  """下游契约：本地召回的 lane 文件喂给既有 2.3 RRF，产出与云端链路同构的合并文件。"""
  db = _patch_paths(monkeypatch, tmp_path)
  _patch_fake_embed(monkeypatch)
  config_path = tmp_path / "config.yaml"
  config_path.write_text(yaml.safe_dump(CONFIG, allow_unicode=True), encoding="utf-8")

  recall_local.main_with_args(["--config", str(config_path), "--db", db])

  bm25_path = tmp_path / "out" / "arxiv_papers_20260820-20260829.bm25.json"
  emb_path = tmp_path / "out" / "arxiv_papers_20260820-20260829.embedding.json"
  merged_path = tmp_path / "out" / "arxiv_papers_20260820-20260829.json"

  rrf = _load_module("rrf_under_test", "2.3.retrieval_papers_rrf.py")
  monkeypatch.setattr(
    sys, "argv",
    [
      "2.3.retrieval_papers_rrf.py",
      "--bm25-input", str(bm25_path),
      "--embedding-input", str(emb_path),
      "--output", str(merged_path),
      "--top-n", "50",
    ],
  )
  rrf.main()

  assert merged_path.exists(), "2.3 必须能消费本地召回的 lane 文件"
  merged = json.loads(merged_path.read_text(encoding="utf-8"))
  assert {"top_k", "generated_at", "papers", "queries"} <= set(merged.keys())
  merged_ids = {p["id"] for p in merged["papers"]}
  assert "2601.00001" in merged_ids  # 相关论文进入候选池
