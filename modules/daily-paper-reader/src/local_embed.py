# -*- coding: utf-8 -*-
"""本地召回模式的 embedding 客户端。

两个通道：
- openai：任意 OpenAI 兼容 /v1/embeddings 端点（默认用中转站上的 qwen3-embedding，
  复用问答链路的 base_url/key），批量编码 1-2k 条粗筛候选约 1-3 分钟；
- local：sentence-transformers 本地模型（默认 bge-small-en-v1.5），完全离线兜底。

本地召回模式没有预存向量：查询与候选每次运行用同一模型现场编码、天然自洽，
因此 embedding 模型/维度可自由切换，不受 Supabase vector(384) 约束。

环境变量（优先级从高到低）：
- DPR_LOCAL_EMBED_PROVIDER：openai | local（默认 openai）
- DPR_LOCAL_EMBED_BASE_URL：embeddings 端点 → 回退 DEEPSEEK_BASE_URL → SUMMARY_BASE_URL
- DPR_LOCAL_EMBED_API_KEY：→ 回退 DEEPSEEK_API_KEY → SUMMARY_API_KEY
- DPR_LOCAL_EMBED_MODEL：openai 默认 qwen3-embedding；local 默认 BAAI/bge-small-en-v1.5
- DPR_LOCAL_EMBED_DEVICE：local 通道设备，默认 cpu
- DPR_LOCAL_EMBED_BATCH_SIZE：openai 单请求文本数，默认 32
"""
from __future__ import annotations

import os
import time
from typing import Any, List, Optional, Tuple

import numpy as np
import requests

DEFAULT_OPENAI_EMBED_MODEL = "qwen3-embedding"
DEFAULT_LOCAL_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_BATCH_SIZE = 32

_local_model_cache: dict = {}


def _env(name: str, default: str = "") -> str:
  return str(os.getenv(name) or default).strip()


def resolve_embedding_target() -> Tuple[str, Dict[str, Any]]:
  """解析 embedding 通道，返回 (provider, options)。provider ∈ {openai, local}。"""
  provider = _env("DPR_LOCAL_EMBED_PROVIDER", "openai").lower()
  if provider not in ("openai", "local"):
    provider = "openai"
  if provider == "local":
    return "local", {
      "model": _env("DPR_LOCAL_EMBED_MODEL", DEFAULT_LOCAL_EMBED_MODEL),
      "device": _env("DPR_LOCAL_EMBED_DEVICE", "cpu"),
      "batch_size": int(_env("DPR_LOCAL_EMBED_BATCH_SIZE", "32") or 32),
    }
  base_url = (
    _env("DPR_LOCAL_EMBED_BASE_URL")
    or _env("DEEPSEEK_BASE_URL")
    or _env("SUMMARY_BASE_URL")
  ).rstrip("/")
  api_key = _env("DPR_LOCAL_EMBED_API_KEY") or _env("DEEPSEEK_API_KEY") or _env("SUMMARY_API_KEY")
  return "openai", {
    "base_url": base_url,
    "api_key": api_key,
    "model": _env("DPR_LOCAL_EMBED_MODEL", DEFAULT_OPENAI_EMBED_MODEL),
    "batch_size": int(_env("DPR_LOCAL_EMBED_BATCH_SIZE", str(DEFAULT_BATCH_SIZE)) or DEFAULT_BATCH_SIZE),
  }


def _embed_openai(texts: List[str], opts: Dict[str, Any]) -> np.ndarray:
  base_url = str(opts.get("base_url") or "").rstrip("/")
  api_key = str(opts.get("api_key") or "")
  model = str(opts.get("model") or DEFAULT_OPENAI_EMBED_MODEL)
  batch_size = int(opts.get("batch_size") or DEFAULT_BATCH_SIZE)
  if not base_url:
    raise ValueError("缺少 embeddings 端点（DPR_LOCAL_EMBED_BASE_URL / DEEPSEEK_BASE_URL）")
  if not api_key:
    raise ValueError("缺少 embeddings API Key（DEEPSEEK_API_KEY）")
  if not base_url.lower().endswith("/embeddings"):
    url = f"{base_url}/embeddings"
  else:
    url = base_url

  vectors: List[List[float]] = []
  for start in range(0, len(texts), batch_size):
    batch = texts[start : start + batch_size]
    if start > 0:
      time.sleep(0.2)  # 批间节流，避免触发中转站限速
    last_exc: Optional[Exception] = None
    for attempt in range(1, 6):
      try:
        resp = requests.post(
          url,
          json={"model": model, "input": batch},
          headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
          timeout=120,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        data.sort(key=lambda item: item.get("index", 0))
        vectors.extend([item["embedding"] for item in data])
        last_exc = None
        break
      except Exception as exc:  # noqa: BLE001
        last_exc = exc
        if attempt < 5:
          time.sleep(min(2 ** (attempt - 1), 8))
    if last_exc is not None:
      raise RuntimeError(f"openai embeddings 请求失败（{url}，第 {start // batch_size + 1} 批）：{last_exc}") from last_exc
  return np.asarray(vectors, dtype=np.float32)


def _embed_local(texts: List[str], opts: Dict[str, Any]) -> np.ndarray:
  try:
    from sentence_transformers import SentenceTransformer
  except ImportError as exc:
    raise RuntimeError(
      "本地 bge 兜底需要重依赖：pip install -r requirements-local-models.txt；"
      "或者把 DPR_LOCAL_EMBED_PROVIDER 保持为 openai 并使用带 /v1/embeddings 的端点"
      "（初始配置无需安装任何本地模型）。"
    ) from exc

  model_name = str(opts.get("model") or DEFAULT_LOCAL_EMBED_MODEL)
  device = str(opts.get("device") or "cpu")
  cache_key = f"{model_name}@{device}"
  model = _local_model_cache.get(cache_key)
  if model is None:
    model = SentenceTransformer(model_name, device=device)
    _local_model_cache[cache_key] = model
  vectors = model.encode(
    texts,
    convert_to_numpy=True,
    normalize_embeddings=True,
    show_progress_bar=False,
    batch_size=int(opts.get("batch_size") or 32),
  )
  return np.asarray(vectors, dtype=np.float32)


def embed_texts(texts: List[str], *, provider: Optional[str] = None, allow_local_fallback: bool = False) -> Tuple[np.ndarray, str]:
  """编码文本为 L2 归一化向量。返回 (vectors, 实际使用的 provider)。

  openai 通道失败且 allow_local_fallback 时自动降级到 local。
  """
  clean = [str(t or "") for t in texts]
  if not clean:
    return np.zeros((0, 1), dtype=np.float32), provider or resolve_embedding_target()[0]

  target_provider, opts = resolve_embedding_target()
  if provider:
    target_provider = provider
  try:
    if target_provider == "openai":
      return _embed_openai(clean, opts), "openai"
    return _embed_local(clean, opts), "local"
  except Exception as exc:
    if target_provider == "openai" and allow_local_fallback:
      # 降级必须留痕：否则日志只见「通道：local」，无法定位 API 失败原因
      # （2026-08-29 实测：中转站 qwen3-embedding 通道随机 500 not implemented，
      #   单批失败率 ~40%，重试 3 次内 38 批几乎必有一批失败 → 整段静默降级）。
      print(f"[local-embed][WARN] openai embedding 失败，降级本地 bge-small：{exc}", flush=True)
      fallback_opts = {
        "model": DEFAULT_LOCAL_EMBED_MODEL,
        "device": _env("DPR_LOCAL_EMBED_DEVICE", "cpu"),
        "batch_size": DEFAULT_BATCH_SIZE,
      }
      return _embed_local(clean, fallback_opts), "local"
    raise
