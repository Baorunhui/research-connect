# -*- coding: utf-8 -*-
"""Step 2L（本地召回模式）：本地 SQLite FTS5 词法召回 + embedding 语义精排。

输出与 Step 2.1（.bm25.json）/ 2.2（.embedding.json）**同构**的两个 lane 文件：
  { top_k, generated_at, papers: [...], queries: [{...查询定义, sim_scores: {pid: {score, rank}}}] }
因此既有 Step 2.3（RRF 融合）及下游 Step 3-6 全部原样复用，不需要任何改动。

召回漏斗（复用综述「Kaggle 粗筛 + 本地语义粗排」的成熟套路）：
  ① FTS5 词法粗筛：keyword 查询走 AND 表达式（不足回退 OR），intent 句走 OR 表达式
     放宽召回，每查询 coarse_per_query 条
  ② embedding 语义精排：粗筛并集（上限 semantic_cap）现场编码（默认中转站
     qwen3-embedding，失败自动降级本地 bge-small），按余弦取每查询 top_k
词法/语义两路的排名经 2.3 RRF 融合成统一候选池。

时间窗解析与 2.1 完全一致（importlib 复用其 resolve_supabase_recall_window，
DPR_RUN_DATE 区间 token 优先，其次 arxiv_paper_setting.days_window）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
  sys.path.insert(0, str(SRC_DIR))

from local_embed import embed_texts  # noqa: E402
from local_paper_store import LocalPaperStore  # noqa: E402

CONFIG_PATH = ROOT_DIR / "config.yaml"


def log(message: str) -> None:
  ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
  print(f"[{ts}] {message}", flush=True)


def _load_rank_module():
  """importlib 复用 2.1 的窗口解析（模块名含点，无法常规 import）。"""
  spec = importlib.util.spec_from_file_location("dpr_bm25_for_local_recall", SRC_DIR / "2.1.retrieval_papers_bm25.py")
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


def load_fts_helpers():
  """复用 Kaggle 索引的 FTS 查询构造（AND 严格式 + OR 放宽式）。"""
  spec = importlib.util.spec_from_file_location("dpr_kaggle_arxiv_for_recall", SRC_DIR / "kaggle_arxiv.py")
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module._query_to_fts


def output_paths() -> Tuple[Path, Path, Path]:
  """与 2.1/2.2 相同的 lane 输出路径（archive/<token>/filtered/）。"""
  token = str(os.getenv("DPR_RUN_DATE") or "").strip() or datetime.now(timezone.utc).strftime("%Y%m%d")
  filtered_dir = ROOT_DIR / "archive" / token / "filtered"
  filtered_dir.mkdir(parents=True, exist_ok=True)
  base = filtered_dir / f"arxiv_papers_{token}"
  return base.with_suffix(".bm25.json"), base.with_suffix(".embedding.json"), filtered_dir


def lane_query(query: Dict[str, Any]) -> Dict[str, Any]:
  """只保留 2.1/2.2 输出里出现过的查询字段（多余字段对 2.3 无害但保持整洁）。"""
  keys = ("type", "tag", "paper_tag", "paper_sources", "query_text", "logic_cn", "boolean_expr", "bm25_mode")
  out = {k: query.get(k) for k in keys if k in query}
  out.setdefault("type", query.get("type") or "keyword")
  out.setdefault("query_text", query.get("query_text") or "")
  return out


def make_lane(papers: List[Dict[str, Any]], queries: List[Dict[str, Any]], top_k: int) -> Dict[str, Any]:
  return {
    "top_k": top_k,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "papers": papers,
    "queries": queries,
  }


def fuse_paper_list(
  hits_by_query: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]],
  paper_meta: Dict[str, Dict[str, Any]],
  tag_key: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
  """把每查询的命中列表折成 lane 文件的 papers + queries（含 sim_scores）。"""
  queries_out: List[Dict[str, Any]] = []
  tags_by_pid: Dict[str, set] = {}
  for query, hits in hits_by_query:
    q_out = lane_query(query)
    sim_scores: Dict[str, Dict[str, float | int]] = {}
    for rank, paper in enumerate(hits, start=1):
      pid = paper["id"]
      sim_scores[pid] = {"score": round(1.0 / rank, 6), "rank": rank}
      tags_by_pid.setdefault(pid, set()).add(str(query.get("paper_tag") or tag_key))
    q_out["sim_scores"] = sim_scores
    queries_out.append(q_out)

  papers_out: List[Dict[str, Any]] = []
  for pid, tags in tags_by_pid.items():
    meta = paper_meta.get(pid)
    if not meta:
      continue
    entry = dict(meta)
    entry["tags"] = sorted(tags)
    papers_out.append(entry)
  return papers_out, queries_out


def semantic_rerank(
  intent_queries: List[Dict[str, Any]],
  candidates: List[Dict[str, Any]],
  top_k: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
  """embedding 语义精排：候选现场编码 + 每查询余弦 top_k。返回 (hits_by_query, paper_meta, provider)。

  查询与候选必须合并成一次 embed_texts 调用——拆开两次调用可能因其中一次
  失败降级到不同模型（维度都不同），向量空间错位无法比较。
  """
  if not intent_queries or not candidates:
    return [(q, []) for q in intent_queries], {}, ""
  cand_texts = [f"{c.get('title') or ''}\n\n{c.get('abstract') or ''}".strip() for c in candidates]
  query_texts = [str(q.get("query_text") or "") for q in intent_queries]
  all_vectors, provider = embed_texts(cand_texts + query_texts)
  if len(all_vectors) == 0:
    return [(q, []) for q in intent_queries], {}, provider

  vectors = all_vectors[: len(cand_texts)]
  query_vectors = all_vectors[len(cand_texts) :]
  norms_v = np.linalg.norm(vectors, axis=1, keepdims=True)
  norms_q = np.linalg.norm(query_vectors, axis=1, keepdims=True)
  mat = vectors / np.clip(norms_v, 1e-9, None)
  qmat = query_vectors / np.clip(norms_q, 1e-9, None)
  sims = qmat @ mat.T  # [n_queries, n_candidates]

  hits_by_query: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
  paper_meta: Dict[str, Dict[str, Any]] = {}
  for idx, query in enumerate(intent_queries):
    order = np.argsort(-sims[idx])[: max(int(top_k), 1)]
    hits = []
    for pos in order:
      cand = candidates[int(pos)]
      entry = dict(cand)
      entry["_semantic_score"] = float(sims[idx][int(pos)])
      hits.append(entry)
      paper_meta.setdefault(cand["id"], cand)
    hits_by_query.append((query, hits))
  return hits_by_query, paper_meta, provider


def main() -> None:
  parser = argparse.ArgumentParser(description="Step 2L：本地 FTS 召回 + embedding 语义精排（输出与 2.1/2.2 同构）")
  parser.add_argument("--config", type=str, default=str(CONFIG_PATH))
  parser.add_argument("--db", type=str, default="", help="本地库路径；默认 data/local_recall/index.sqlite3")
  parser.add_argument("--bm25-top-k", type=int, default=int(os.getenv("DPR_LOCAL_BM25_TOP_K") or "400"))
  parser.add_argument("--semantic-top-k", type=int, default=int(os.getenv("DPR_LOCAL_SEMANTIC_TOP_K") or "400"))
  parser.add_argument("--coarse-per-query", type=int, default=int(os.getenv("DPR_LOCAL_SEMANTIC_COARSE") or "500"))
  parser.add_argument("--semantic-cap", type=int, default=int(os.getenv("DPR_LOCAL_SEMANTIC_CAP") or "2000"))
  args = parser.parse_args()

  config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) if Path(args.config).exists() else {}
  config = config if isinstance(config, dict) else {}

  from subscription_plan import build_pipeline_inputs

  inputs = build_pipeline_inputs(config)
  bm25_queries: List[Dict[str, Any]] = list(inputs.get("bm25_queries") or [])
  embedding_queries: List[Dict[str, Any]] = list(inputs.get("embedding_queries") or [])
  if not bm25_queries and not embedding_queries:
    log("[WARN] 未解析到任何订阅查询（intent_profiles），本地召回输出空结果。")

  rank_mod = _load_rank_module()
  start_dt, end_dt = rank_mod.resolve_supabase_recall_window(config)
  date_start = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
  date_end = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
  query_to_fts = load_fts_helpers()

  bm25_path, embedding_path, _ = output_paths()

  with LocalPaperStore(args.db or None) as store:
    window_count = store.count_window(date_start, date_end)
    log(f"[INFO] 本地召回窗口 {date_start} ~ {date_end}：库内 {window_count} 篇")
    if window_count == 0:
      log("[WARN] 本地库窗口为空——先跑 Step 1L（fetch_arxiv_window.py）增量抓取。")
      log("[INFO] Step 2L 完成：输出空 lane 结果。")
      for path in (bm25_path, embedding_path):
        Path(path).write_text(
          json.dumps(make_lane([], [], int(args.bm25_top_k)), ensure_ascii=False, indent=2),
          encoding="utf-8",
        )
      return

    # ---- 路一：词法召回（keyword AND 式，命中不足回退 OR 式） ----
    lexical_hits: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    for query in bm25_queries:
      text = str(query.get("query_text") or "").strip()
      if not text:
        continue
      and_expr, or_expr = query_to_fts(text)
      hits = store.search_fts(and_expr, date_start, date_end, limit=int(args.bm25_top_k))
      if len(hits) < 5 and or_expr and or_expr != and_expr:
        hits = store.search_fts(or_expr, date_start, date_end, limit=int(args.bm25_top_k))
      lexical_hits.append((query, hits))
      log(f"[Supabase BM25 → 本地FTS] tag={query.get('tag') or ''} type={query.get('type') or ''} 命中 {len(hits)} 条")
    paper_meta: Dict[str, Dict[str, Any]] = {}
    for _, hits in lexical_hits:
      for paper in hits:
        paper_meta.setdefault(paper["id"], paper)
    lexical_papers, lexical_queries = fuse_paper_list(lexical_hits, paper_meta, "keyword")
    bm25_lane = make_lane(lexical_papers, lexical_queries, int(args.bm25_top_k))

    # ---- 路二：语义召回（intent 句 OR 式粗筛 → embedding 精排） ----
    coarse_ids: List[str] = []
    coarse_set: set = set()
    for query in embedding_queries:
      text = str(query.get("query_text") or "").strip()
      if not text:
        continue
      and_expr, or_expr = query_to_fts(text)
      for hit in store.search_fts(or_expr or and_expr, date_start, date_end, limit=int(args.coarse_per_query)):
        if hit["id"] not in coarse_set:
          coarse_set.add(hit["id"])
          coarse_ids.append(hit["id"])
    coarse_candidates = []
    meta_map = store.get_papers(coarse_ids)
    coarse_candidates = [meta_map[pid] for pid in coarse_ids if pid in meta_map]
    if len(coarse_candidates) > int(args.semantic_cap):
      coarse_candidates = coarse_candidates[: int(args.semantic_cap)]
    log(f"[INFO] 语义精排候选：粗筛并集 {len(coarse_candidates)} 条（每查询粗筛上限 {args.coarse_per_query}，总上限 {args.semantic_cap}）")

    semantic_hits, semantic_meta, provider = semantic_rerank(
      embedding_queries, coarse_candidates, int(args.semantic_top_k)
    )
    for paper in coarse_candidates:
      semantic_meta.setdefault(paper["id"], paper)
    semantic_papers, semantic_queries = fuse_paper_list(semantic_hits, semantic_meta, "query")
    embedding_lane = make_lane(semantic_papers, semantic_queries, int(args.semantic_top_k))
    log(f"[INFO] embedding 通道：{provider or '未启用（无 intent 查询）'}")

  for path, lane in ((bm25_path, bm25_lane), (embedding_path, embedding_lane)):
    Path(path).write_text(json.dumps(lane, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[INFO] lane 输出：{path}（papers={len(lane['papers'])}, queries={len(lane['queries'])}）")
  log("[INFO] Step 2L 完成。后续 Step 2.3 RRF / Step 3 rerank 原样执行。")


def main_with_args(argv: List[str]) -> None:
  """测试入口：显式传参跑 main（等价命令行调用）。"""
  sys.argv = ["recall_local.py"] + [str(a) for a in argv]
  main()


if __name__ == "__main__":
  main()
