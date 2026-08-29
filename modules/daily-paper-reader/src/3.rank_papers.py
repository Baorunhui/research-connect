#!/usr/bin/env python
# 使用本地 Qwen3 Reranker 对候选论文做重排序（简化版）。

import argparse
import json
import os
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(__file__)
ROOT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
TODAY_STR = str(os.getenv("DPR_RUN_DATE") or "").strip() or datetime.now(timezone.utc).strftime("%Y%m%d")
ARCHIVE_DIR = os.path.join(ROOT_DIR, "archive", TODAY_STR)
FILTERED_DIR = os.path.join(ARCHIVE_DIR, "filtered")
RANKED_DIR = os.path.join(ARCHIVE_DIR, "rank")

MAX_CHARS_PER_DOC = 850
BATCH_SIZE = 100
TOKEN_SAFETY = 29000
RRF_K = 60
LANE_TOP_K_BASE = 30
LANE_TOP_K_STEP = 10
LANE_TOP_K_MAX = 120
GLOBAL_POOL_GUARANTEED_MIN = 5
GLOBAL_POOL_GUARANTEED_MAX = 20
GLOBAL_POOL_RRF_MIN = 60
GLOBAL_POOL_RRF_MAX = 300
DEFAULT_LOCAL_RERANK_MODEL = "Qwen/Qwen3-Reranker-0.6B"
DEFAULT_LOCAL_RERANK_BATCH_SIZE = 8
RERANK_PROFILE_CONFIGS: Dict[str, Dict[str, str]] = {
  "public-zwwen-rerank": {
    "provider": "public_zwwen",
    "model": "Qwen/Qwen3-Reranker-0.6B",
    "base_url": "https://zwwen.online/rerank",
  },
  "public-sinksilk-rerank": {
    "provider": "public_zwwen",
    "model": "qwen3-reranker",
    "base_url": "https://api.sinksilk.com:58443/v1/rerank",
  },
  "local-qwen3-0.6b": {
    "provider": "local",
    "model": "Qwen/Qwen3-Reranker-0.6B",
  },
  "siliconflow-qwen3-0.6b": {
    "provider": "siliconflow",
    "model": "Qwen/Qwen3-Reranker-0.6B",
    "base_url": "https://api.siliconflow.cn/v1/rerank",
  },
}


def _env_int(name: str, default: Optional[int] = None) -> Optional[int]:
  value = str(os.getenv(name) or "").strip()
  if not value:
    return default
  try:
    return int(value)
  except ValueError:
    return default


def _normalize_rerank_profile(value: str) -> str:
  text = str(value or "").strip().lower().replace("_", "-")
  aliases = {
    "local": "local-qwen3-0.6b",
    "local-0.6b": "local-qwen3-0.6b",
    "qwen3-0.6b-local": "local-qwen3-0.6b",
    "siliconflow": "siliconflow-qwen3-0.6b",
    "siliconflow-0.6b": "siliconflow-qwen3-0.6b",
    "sf-0.6b": "siliconflow-qwen3-0.6b",
    "public": "public-zwwen-rerank",
    "public-rerank": "public-zwwen-rerank",
    "public-zwwen": "public-zwwen-rerank",
    "zwwen": "public-zwwen-rerank",
    "sinksilk": "public-sinksilk-rerank",
    "public-sinksilk": "public-sinksilk-rerank",
    "silk-rerank": "public-sinksilk-rerank",
  }
  return aliases.get(text, text)


def _normalize_rerank_provider(value: str) -> str:
  text = str(value or "").strip().lower().replace("_", "-")
  if text in {"", "local", "hf", "huggingface"}:
    return "local"
  if text in {"siliconflow", "sf"}:
    return "siliconflow"
  if text in {"public", "public-zwwen", "public-zwwen-rerank", "zwwen"}:
    return "public_zwwen"
  if text in {"remote"}:
    return "siliconflow"
  return text


def _resolve_rerank_profile_config(profile: str) -> Dict[str, str]:
  normalized = _normalize_rerank_profile(profile)
  return dict(RERANK_PROFILE_CONFIGS.get(normalized) or {})


def resolve_default_rerank_model() -> str:
  profile_config = _resolve_rerank_profile_config(os.getenv("RERANK_PROFILE", ""))
  if profile_config.get("model"):
    return profile_config["model"]
  provider = _normalize_rerank_provider(os.getenv("RERANK_PROVIDER") or "public_zwwen")
  if provider == "local":
    return os.getenv("LOCAL_RERANK_MODEL") or os.getenv("RERANK_MODEL") or DEFAULT_LOCAL_RERANK_MODEL
  return os.getenv("RERANK_MODEL") or DEFAULT_LOCAL_RERANK_MODEL


def _resolve_remote_api_key(provider: str) -> str:
  if provider in {"siliconflow", "public_zwwen"}:
    return (
      os.getenv("SILICONFLOW_API_KEY")
      or os.getenv("RERANK_API_KEY")
      or os.getenv("PUBLIC_RERANK_API_KEY")
      or os.getenv("DPR_PUBLIC_SERVICE_API_KEY")
      or "26932a86d772001af60cbd9d2c162bfda3a90e094f797f3d6806f6077478b27a"
      or ""
    ).strip()
  return (os.getenv("RERANK_API_KEY") or "").strip()


def _resolve_remote_base_url(provider: str, profile_config: Dict[str, str], explicit: str = "") -> str:
  if explicit:
    return explicit
  if provider == "siliconflow":
    return (
      os.getenv("SILICONFLOW_RERANK_URL")
      or os.getenv("RERANK_API_BASE_URL")
      or profile_config.get("base_url")
      or "https://api.siliconflow.cn/v1/rerank"
    ).strip()
  if provider == "public_zwwen":
    return (
      os.getenv("PUBLIC_RERANK_API_BASE_URL")
      or os.getenv("RERANK_API_BASE_URL")
      or profile_config.get("base_url")
      or "https://zwwen.online/rerank"
    ).strip()
  return (os.getenv("RERANK_API_BASE_URL") or profile_config.get("base_url") or "").strip()


def log(message: str) -> None:
  ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
  print(f"[{ts}] {message}", flush=True)


def group_start(title: str) -> None:
  print(f"::group::{title}", flush=True)


def group_end() -> None:
  print("::endgroup::", flush=True)


class LocalQwenReranker:
  """本地 Qwen3 reranker，按 yes/no token 概率为 query-document 打分。"""

  def __init__(
    self,
    model_name: str = DEFAULT_LOCAL_RERANK_MODEL,
    *,
    device: str = "",
    batch_size: int = DEFAULT_LOCAL_RERANK_BATCH_SIZE,
    max_length: int = 8192,
  ) -> None:
    self.model_name = str(model_name or DEFAULT_LOCAL_RERANK_MODEL).strip()
    self.batch_size = max(int(batch_size or DEFAULT_LOCAL_RERANK_BATCH_SIZE), 1)
    self.max_length = max(int(max_length or 8192), 256)
    try:
      import torch  # type: ignore
      from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore
    except Exception as exc:
      raise RuntimeError(
        "本地 reranker 需要 torch 与 transformers，请先安装 requirements.txt。"
      ) from exc

    self.torch = torch
    self.device = str(device or "").strip() or ("cuda" if torch.cuda.is_available() else "cpu")
    self.tokenizer = AutoTokenizer.from_pretrained(
      self.model_name,
      padding_side="left",
      trust_remote_code=True,
    )

    model_kwargs: Dict[str, Any] = {"trust_remote_code": True}
    if self.device == "cpu":
      model_kwargs["dtype"] = torch.float32
    else:
      # GPU 用 fp16 加载：权重先整份读进系统内存再复制到显存，fp32 会在 CPU 侧滞留
      # ~2.4GB（Windows 不把这份副本还给 OS，任务管理器里看到的 3GB 内存主要是它）；
      # 半精度把这份副本和显存占用都减半，对小显存卡是实质性改善。
      # rerank 只取 yes/no 两个 token 的 softmax 相对值，fp16 波动可忽略。
      model_kwargs["dtype"] = torch.float16
      try:  # low_cpu_mem_usage 让权重流式上卡，避免加载期的双份峰值（需要 accelerate）
        import accelerate  # noqa: F401
        model_kwargs["low_cpu_mem_usage"] = True
      except ImportError:
        pass
    try:
      self.model = AutoModelForCausalLM.from_pretrained(self.model_name, **model_kwargs)
    except TypeError:
      if "dtype" in model_kwargs:
        model_kwargs["torch_dtype"] = model_kwargs.pop("dtype")
      else:
        model_kwargs.pop("torch_dtype", None)
      self.model = AutoModelForCausalLM.from_pretrained(self.model_name, **model_kwargs)
    self.model.to(self.device)
    self.model.eval()

    self.token_false_id = self.tokenizer.convert_tokens_to_ids("no")
    self.token_true_id = self.tokenizer.convert_tokens_to_ids("yes")
    if self.token_false_id is None or self.token_true_id is None:
      raise RuntimeError("Qwen3 reranker tokenizer 缺少 yes/no token，无法计算相关性。")

    self.prefix = (
      "<|im_start|>system\n"
      "Judge whether the Document meets the requirements based on the Query and the Instruct provided. "
      "Note that the answer can only be \"yes\" or \"no\"."
      "<|im_end|>\n"
      "<|im_start|>user\n"
    )
    self.suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    self.prefix_tokens = self.tokenizer.encode(self.prefix, add_special_tokens=False)
    self.suffix_tokens = self.tokenizer.encode(self.suffix, add_special_tokens=False)

  @staticmethod
  def _format_pair(query: str, document: str) -> str:
    instruction = "Given an academic search query, retrieve papers that best satisfy the query."
    return f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}"

  def _score_batch(self, query: str, documents: List[str]) -> List[float]:
    pair_texts = [self._format_pair(query, doc) for doc in documents]
    content_max_length = max(
      self.max_length - len(self.prefix_tokens) - len(self.suffix_tokens),
      32,
    )
    encoded = self.tokenizer(
      pair_texts,
      padding=False,
      truncation=True,
      max_length=content_max_length,
      return_attention_mask=False,
    )
    input_ids = [
      self.prefix_tokens + item + self.suffix_tokens
      for item in encoded.get("input_ids", [])
    ]
    inputs = self.tokenizer.pad(
      {"input_ids": input_ids},
      padding=True,
      return_tensors="pt",
    )
    inputs = {key: value.to(self.model.device) for key, value in inputs.items()}
    with self.torch.no_grad():
      logits = self.model(**inputs).logits[:, -1, :]
      false_scores = logits[:, self.token_false_id]
      true_scores = logits[:, self.token_true_id]
      yes_no_scores = self.torch.stack([false_scores, true_scores], dim=1)
      probs = self.torch.nn.functional.softmax(yes_no_scores, dim=1)[:, 1]
    return [float(score) for score in probs.detach().cpu().tolist()]

  def rerank(
    self,
    *,
    query: str,
    documents: List[str],
    top_n: Optional[int] = None,
    model: Optional[str] = None,
  ) -> Dict[str, Any]:
    query_text = str(query or "").strip()
    if not query_text:
      raise ValueError("rerank: query 不能为空")
    if not documents:
      raise ValueError("rerank: documents 不能为空")

    results: List[Dict[str, Any]] = []
    for start in range(0, len(documents), self.batch_size):
      batch_docs = [str(doc or "") for doc in documents[start : start + self.batch_size]]
      for offset, score in enumerate(self._score_batch(query_text, batch_docs)):
        results.append({"index": start + offset, "relevance_score": float(score)})

    results.sort(key=lambda item: item["relevance_score"], reverse=True)
    if top_n is not None:
      results = results[: max(int(top_n), 0)]
    return {"results": results, "model": model or self.model_name}


def build_token_encoder():
  try:
    import tiktoken  # type: ignore
    return tiktoken.get_encoding("cl100k_base")
  except Exception:
    return None


def estimate_tokens(text: str, encoder) -> int:
  if encoder is None:
    return max(1, len(text) // 3)
  return len(encoder.encode(text))


def score_to_stars(score: float) -> int:
  if score >= 0.9:
    return 5
  if score >= 0.5:
    return 4
  if score >= 0.1:
    return 3
  if score >= 0.01:
    return 2
  return 1


def load_json(path: str) -> Dict[str, Any]:
  if not os.path.exists(path):
    raise FileNotFoundError(f"找不到文件：{path}")
  with open(path, "r", encoding="utf-8") as f:
    return json.load(f)


def save_json(data: Dict[str, Any], path: str) -> None:
  os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
  with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
  log(f"[INFO] 已将打分结果写入：{path}")


def format_doc(title: str, abstract: str) -> str:
  content = f"Title: {title}\nAbstract: {abstract}".strip()
  if len(content) > MAX_CHARS_PER_DOC:
    content = content[:MAX_CHARS_PER_DOC]
  return content


def build_documents(papers_by_id: Dict[str, Dict[str, Any]], paper_ids: List[str]) -> List[str]:
  docs: List[str] = []
  for pid in paper_ids:
    p = papers_by_id.get(pid)
    if not p:
      docs.append(f"[Missing paper {pid}]")
      continue
    title = (p.get("title") or "").strip()
    abstract = (p.get("abstract") or "").strip()
    if title or abstract:
      docs.append(format_doc(title, abstract))
    else:
      docs.append(f"[Empty paper {pid}]")
  return docs


def get_top_ids(query_obj: Dict[str, Any]) -> List[str]:
  sim_scores = query_obj.get("sim_scores") or {}
  top_ids = query_obj.get("top_ids") or []
  if not top_ids and isinstance(sim_scores, dict) and sim_scores:
    top_ids = sorted(sim_scores.keys(), key=lambda pid: sim_scores[pid].get("rank", 1e9))
  return list(top_ids)


def _unique_keep_order(items: List[str]) -> List[str]:
  seen = set()
  out: List[str] = []
  for item in items:
    pid = str(item or "").strip()
    if not pid or pid in seen:
      continue
    seen.add(pid)
    out.append(pid)
  return out


def _clamp_int(value: float | int, min_value: int, max_value: int) -> int:
  return max(min_value, min(int(value), max_value))


def resolve_global_pool_budget(
  total_papers: int,
  intent_query_count: int,
  *,
  lane_top_k_override: Optional[int] = None,
  guaranteed_per_lane_override: Optional[int] = None,
  global_limit_override: Optional[int] = None,
) -> Tuple[int, int, int]:
  """
  统一候选池预算：
  - lane_top_k 随论文总数递增：1000 篇内 30，每增加 1000 篇 +10，上限 120；
  - guaranteed_per_lane = lane_top_k 的 25%，限制在 [5, 20]；
  - global_rrf_top = lane_top_k * intent_query_count，限制在 [60, 300]。
  """
  total = max(int(total_papers or 0), 0)
  intent_count = max(int(intent_query_count or 0), 1)
  if lane_top_k_override is not None and int(lane_top_k_override) > 0:
    lane_top_k = int(lane_top_k_override)
  elif total <= 0:
    lane_top_k = LANE_TOP_K_BASE
  else:
    blocks = (total - 1) // 1000
    lane_top_k = min(LANE_TOP_K_BASE + LANE_TOP_K_STEP * blocks, LANE_TOP_K_MAX)
  if guaranteed_per_lane_override is not None and int(guaranteed_per_lane_override) >= 0:
    guaranteed_per_lane = int(guaranteed_per_lane_override)
  else:
    guaranteed_per_lane = _clamp_int(
      round(lane_top_k * 0.25),
      GLOBAL_POOL_GUARANTEED_MIN,
      GLOBAL_POOL_GUARANTEED_MAX,
    )
  if global_limit_override is not None and int(global_limit_override) > 0:
    global_rrf_top = int(global_limit_override)
  else:
    global_rrf_top = _clamp_int(
      lane_top_k * intent_count,
      GLOBAL_POOL_RRF_MIN,
      GLOBAL_POOL_RRF_MAX,
    )
  return lane_top_k, guaranteed_per_lane, global_rrf_top


def build_global_candidate_ids(
  queries: List[Dict[str, Any]],
  *,
  guaranteed_per_lane: int,
  global_limit: int,
) -> List[str]:
  """
  将所有 query lane 的候选论文合并成统一候选池。
  - 不区分 keyword / intent_query 来源；
  - 使用 rank-based RRF 做全局聚合，避免不同分数量纲直接混用；
  - 每条 lane 的前 guaranteed_per_lane 固定保留；
  - 再加入全局 RRF 前 global_limit 篇；
  - 最终按“固定保留 + 全局排序”去重合并。
  """
  score_map: Dict[str, float] = {}
  hit_count: Dict[str, int] = {}
  guaranteed_ids: List[str] = []

  for q in queries or []:
    top_ids = get_top_ids(q)
    if not top_ids:
      continue
    if guaranteed_per_lane > 0:
      guaranteed_ids.extend(top_ids[:guaranteed_per_lane])
    for rank_idx, pid in enumerate(top_ids, start=1):
      paper_id = str(pid or "").strip()
      if not paper_id:
        continue
      score_map[paper_id] = score_map.get(paper_id, 0.0) + 1.0 / (RRF_K + rank_idx)
      hit_count[paper_id] = hit_count.get(paper_id, 0) + 1

  ranked = sorted(
    score_map.items(),
    key=lambda item: (
      -item[1],
      -hit_count.get(item[0], 0),
      item[0],
    ),
  )
  global_ids = [pid for pid, _score in ranked]
  if global_limit > 0:
    global_ids = global_ids[:global_limit]
  return _unique_keep_order(list(guaranteed_ids) + list(global_ids))


def iter_batches(
  docs_with_idx: List[Tuple[int, str]],
  query_tokens: int,
  encoder,
  max_docs_per_batch: Optional[int] = None,
) -> List[Tuple[List[int], List[str]]]:
  batches: List[Tuple[List[int], List[str]]] = []
  batch_limit = max(int(max_docs_per_batch or BATCH_SIZE), 1)
  pos = 0
  while pos < len(docs_with_idx):
    total_tokens = query_tokens
    batch_docs: List[str] = []
    batch_indices: List[int] = []

    while pos < len(docs_with_idx) and len(batch_docs) < batch_limit:
      orig_idx, doc = docs_with_idx[pos]
      doc_tokens = estimate_tokens(doc, encoder)
      if total_tokens + doc_tokens > TOKEN_SAFETY and batch_docs:
        break
      batch_docs.append(doc)
      batch_indices.append(orig_idx)
      total_tokens += doc_tokens
      pos += 1

    if not batch_docs:
      pos += 1
      continue
    batches.append((batch_indices, batch_docs))
  return batches


def resolve_effective_rerank_batch_size(reranker: Any) -> int:
  batch_size = BATCH_SIZE
  max_documents = getattr(reranker, "max_documents_per_request", None)
  if max_documents is None:
    return batch_size
  try:
    remote_limit = max(int(max_documents), 1)
  except (TypeError, ValueError):
    return batch_size
  return min(batch_size, remote_limit)


def rrf_merge(scores: Dict[int, float], rank_idx: int, orig_idx: int) -> None:
  scores[orig_idx] = scores.get(orig_idx, 0.0) + 1.0 / (RRF_K + rank_idx)


def process_file(
  reranker: Any,
  input_path: str,
  output_path: str,
  top_n: Optional[int],
  rerank_model: str,
  rerank_lane_top_k: Optional[int] = None,
  rerank_guaranteed_per_lane: Optional[int] = None,
  rerank_global_pool_limit: Optional[int] = None,
) -> None:
  data = load_json(input_path)
  papers_list = data.get("papers") or []
  all_queries = data.get("queries") or []
  if not papers_list or not all_queries:
    log(f"[WARN] 文件 {os.path.basename(input_path)} 中缺少 papers 或 queries，跳过。")
    return

  # 仅使用语义查询（intent_query 或兼容旧的 llm_query）进行 rerank。
  def _is_intent_rerank_query(q: Dict[str, Any]) -> bool:
    q_type = str(q.get("type") or "").strip().lower()
    return q_type in {"intent_query", "llm_query"}

  queries = [q for q in all_queries if _is_intent_rerank_query(q)]
  if not queries:
    log("[WARN] 当前输入中没有可用于 rerank 的意图查询，跳过 rerank。")
    # 保持输出结构一致，避免后续步骤读不到文件
    meta_generated_at = data.get("generated_at") or ""
    data["reranked_at"] = datetime.now(timezone.utc).isoformat()
    data["generated_at"] = meta_generated_at
    save_json(data, output_path)
    return

  papers_by_id = {str(p.get("id")): p for p in papers_list if p.get("id")}
  lane_top_k, guaranteed_per_lane, global_rrf_top = resolve_global_pool_budget(
    len(papers_list),
    len(queries),
    lane_top_k_override=rerank_lane_top_k,
    guaranteed_per_lane_override=rerank_guaranteed_per_lane,
    global_limit_override=rerank_global_pool_limit,
  )
  global_candidate_ids = build_global_candidate_ids(
    all_queries,
    guaranteed_per_lane=guaranteed_per_lane,
    global_limit=global_rrf_top,
  )
  data["global_candidate_ids"] = global_candidate_ids
  data["global_pool_lane_top_k"] = lane_top_k
  data["global_pool_limit"] = global_rrf_top
  data["global_pool_guaranteed_per_lane"] = guaranteed_per_lane
  data["global_pool_effective_size"] = len(global_candidate_ids)
  if not global_candidate_ids:
    log("[WARN] 未能从任意 query 中构建统一候选池，跳过 rerank。")
    meta_generated_at = data.get("generated_at") or ""
    data["reranked_at"] = datetime.now(timezone.utc).isoformat()
    data["generated_at"] = meta_generated_at
    save_json(data, output_path)
    return
  encoder = build_token_encoder()
  effective_batch_size = resolve_effective_rerank_batch_size(reranker)
  group_start(f"Step 3 - rerank {os.path.basename(input_path)}")
  log(
    f"[INFO] 开始 rerank：queries={len(queries)}（仅 intent/语义查询），papers={len(papers_list)}，"
    f"global_pool={len(global_candidate_ids)}（lane_top_k={lane_top_k}, "
    f"guaranteed_per_lane={guaranteed_per_lane}, global_top={global_rrf_top}），"
    f"batch_size={effective_batch_size}，"
    f"max_chars={MAX_CHARS_PER_DOC}，token_safety={TOKEN_SAFETY}"
  )

  for q_idx, q in enumerate(queries, start=1):
    q_text = (q.get("rewrite") or q.get("query_text") or "").strip()
    top_ids = list(global_candidate_ids)
    if not q_text or not top_ids:
      continue

    group_start(f"Query {q_idx}/{len(queries)} tag={q.get('tag') or ''}")
    documents = build_documents(papers_by_id, top_ids)
    docs_with_idx = list(enumerate(documents))
    random.shuffle(docs_with_idx)

    query_tokens = estimate_tokens(q_text, encoder)
    batches = iter_batches(
      docs_with_idx,
      query_tokens,
      encoder,
      max_docs_per_batch=effective_batch_size,
    )
    log(
      f"[INFO] Query {q_idx}/{len(queries)} tag={q.get('tag') or ''} | candidates={len(top_ids)} "
      f"| batches={len(batches)} | query_tokens≈{query_tokens}"
    )

    rrf_scores: Dict[int, float] = {}

    try:
      for batch_idx, (batch_indices, batch_docs) in enumerate(batches, 1):
        log(
          f"[INFO] 发送批次 {batch_idx}/{len(batches)} | docs={len(batch_docs)}"
        )
        response = reranker.rerank(
          query=q_text,
          documents=batch_docs,
          # 不传 top_n：原值 len(batch_docs) 本就是全量、截断无意义；且部分
          # OpenAI 兼容中转（new-api 的 qwen3-reranker 渠道）不认 top_n，传了 500。
          # 客户端拿到全量 results 后自行排序截断。
          model=rerank_model,
        )
        if isinstance(response, dict) and "output" in response:
          results = response.get("output", {}).get("results", [])
        else:
          results = response.get("results", [])

        ranked = sorted(
          results or [],
          key=lambda x: x.get("relevance_score", x.get("score", 0.0)),
          reverse=True,
        )
        for rank_idx, item in enumerate(ranked, start=1):
          idx = int(item.get("index", -1))
          if idx < 0 or idx >= len(batch_indices):
            continue
          orig_idx = batch_indices[idx]
          rrf_merge(rrf_scores, rank_idx, orig_idx)

      if not rrf_scores:
        log("[WARN] 本次 query 未得到有效 rerank 结果，跳过。")
        continue
    finally:
      group_end()

    if not rrf_scores:
      continue

    sorted_items = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    if top_n is not None:
      sorted_items = sorted_items[:top_n]

    rrf_values = [v for _, v in sorted_items]
    min_rrf = min(rrf_values)
    max_rrf = max(rrf_values)
    denom = max_rrf - min_rrf if max_rrf > min_rrf else 1.0

    ranked_for_query: List[Dict[str, Any]] = []
    for idx, rrf_score in sorted_items:
      norm_score = (rrf_score - min_rrf) / denom
      paper_id = top_ids[idx]
      ranked_for_query.append(
        {
          "paper_id": paper_id,
          "score": norm_score,
          "star_rating": score_to_stars(norm_score),
        }
      )

    ranked_for_query.sort(key=lambda x: x["score"], reverse=True)
    q["ranked"] = ranked_for_query

  meta_generated_at = data.get("generated_at") or ""
  data["reranked_at"] = datetime.now(timezone.utc).isoformat()
  data["generated_at"] = meta_generated_at

  save_json(data, output_path)
  group_end()


def main() -> None:
  parser = argparse.ArgumentParser(
    description="步骤 3：使用本地 Qwen3 Reranker 对候选论文做重排序（简化版）。",
  )
  parser.add_argument(
    "--input",
    type=str,
    default=os.path.join(FILTERED_DIR, f"arxiv_papers_{TODAY_STR}.json"),
    help="筛选结果 JSON 路径。",
  )
  parser.add_argument(
    "--output",
    type=str,
    default=os.path.join(RANKED_DIR, f"arxiv_papers_{TODAY_STR}.json"),
    help="打分后的输出 JSON 路径。",
  )
  parser.add_argument(
    "--top-n",
    type=int,
    default=None,
    help="最终保留的 Top N（默认保留全部候选）。",
  )
  parser.add_argument(
    "--rerank-profile",
    type=str,
    default=os.getenv("RERANK_PROFILE", ""),
    help="Rerank 预设：public-sinksilk-rerank / public-zwwen-rerank / local-qwen3-0.6b / siliconflow-qwen3-0.6b。",
  )
  parser.add_argument(
    "--rerank-provider",
    type=str,
    default=os.getenv("RERANK_PROVIDER", ""),
    help="Rerank provider：public_zwwen / local / siliconflow；默认由 --rerank-profile 推断。",
  )
  parser.add_argument(
    "--rerank-model",
    type=str,
    default=os.getenv("RERANK_MODEL", ""),
    help=f"Rerank 模型名称（默认 {DEFAULT_LOCAL_RERANK_MODEL}）。",
  )
  parser.add_argument(
    "--rerank-api-base-url",
    type=str,
    default=os.getenv("RERANK_API_BASE_URL", ""),
    help="远端 rerank API 地址；本地 reranker 会忽略。",
  )
  parser.add_argument(
    "--rerank-device",
    type=str,
    default=os.getenv("LOCAL_RERANK_DEVICE", ""),
    help="本地 Rerank 运行设备，例如 cpu/cuda；默认交给 sentence-transformers 自动判断。",
  )
  parser.add_argument(
    "--rerank-batch-size",
    type=int,
    default=int(os.getenv("LOCAL_RERANK_BATCH_SIZE") or DEFAULT_LOCAL_RERANK_BATCH_SIZE),
    help=f"本地 Rerank 推理 batch size（默认 {DEFAULT_LOCAL_RERANK_BATCH_SIZE}）。",
  )
  parser.add_argument(
    "--rerank-lane-top-k",
    type=int,
    default=_env_int("DPR_RERANK_LANE_TOP_K"),
    help="覆盖候选池 lane_top_k；默认按论文总量自动估算。",
  )
  parser.add_argument(
    "--rerank-guaranteed-per-lane",
    type=int,
    default=_env_int("DPR_RERANK_GUARANTEED_PER_LANE"),
    help="每条召回 lane 固定保留的候选数；可设 1 加速。",
  )
  parser.add_argument(
    "--rerank-global-pool-limit",
    type=int,
    default=_env_int("DPR_RERANK_GLOBAL_POOL_LIMIT"),
    help="全局 RRF 候选池上限；可设 80 加速。",
  )

  args = parser.parse_args()

  input_path = args.input
  if not os.path.isabs(input_path):
    input_path = os.path.abspath(os.path.join(ROOT_DIR, input_path))

  output_path = args.output
  if not os.path.isabs(output_path):
    output_path = os.path.abspath(os.path.join(ROOT_DIR, output_path))

  if not os.path.exists(input_path):
    log(f"[WARN] 输入文件不存在（今天可能没有新论文）：{input_path}，将跳过 Step 3。")
    return

  profile_config = _resolve_rerank_profile_config(args.rerank_profile)
  provider = _normalize_rerank_provider(
    args.rerank_provider or profile_config.get("provider") or os.getenv("RERANK_PROVIDER") or "public_zwwen"
  )
  rerank_model = (
    args.rerank_model
    or profile_config.get("model")
    or (
      os.getenv("LOCAL_RERANK_MODEL")
      if provider == "local"
      else os.getenv("RERANK_MODEL")
    )
    or DEFAULT_LOCAL_RERANK_MODEL
  )
  log(
    f"[INFO] reranker 配置：profile={args.rerank_profile or 'custom'}，provider={provider}，"
    f"model={rerank_model}，global_pool_limit={args.rerank_global_pool_limit or 'auto'}，"
    f"guaranteed_per_lane={args.rerank_guaranteed_per_lane if args.rerank_guaranteed_per_lane is not None else 'auto'}"
  )

  if provider == "local":
    log(
      f"[INFO] 加载本地 reranker：device={args.rerank_device or 'auto'}，"
      f"batch_size={args.rerank_batch_size}"
    )
    reranker = LocalQwenReranker(
      model_name=rerank_model,
      device=args.rerank_device,
      batch_size=args.rerank_batch_size,
    )
  elif provider in {"siliconflow", "public_zwwen"}:
    try:
      from reranker_api import SiliconFlowReranker  # type: ignore
    except Exception as exc:
      raise RuntimeError("远端 reranker 需要 src/reranker_api.py 可导入。") from exc

    api_key = _resolve_remote_api_key(provider)
    base_url = _resolve_remote_base_url(provider, profile_config, args.rerank_api_base_url)
    if not api_key:
      raise RuntimeError("远端 reranker 缺少 SILICONFLOW_API_KEY、PUBLIC_RERANK_API_KEY 或 RERANK_API_KEY。")
    if not base_url:
      raise RuntimeError("远端 reranker 缺少 API Base URL。")
    log(f"[INFO] 使用远端 reranker：provider={provider} base_url={base_url}")
    reranker = SiliconFlowReranker(
      api_key=api_key,
      base_url=base_url,
    )
  else:
    raise RuntimeError(f"不支持的 reranker provider：{provider}")
  process_file(
    reranker=reranker,
    input_path=input_path,
    output_path=output_path,
    top_n=args.top_n,
    rerank_model=rerank_model,
    rerank_lane_top_k=args.rerank_lane_top_k,
    rerank_guaranteed_per_lane=args.rerank_guaranteed_per_lane,
    rerank_global_pool_limit=args.rerank_global_pool_limit,
  )


if __name__ == "__main__":
  main()
