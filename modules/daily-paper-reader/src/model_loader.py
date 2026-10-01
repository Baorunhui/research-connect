"""统一模型加载器：按顺序尝试下载源，按重试次数回退。"""

from __future__ import annotations

from contextlib import contextmanager
import os
import time
from typing import Any, Callable, Optional, TYPE_CHECKING

import numpy as np
import requests

if TYPE_CHECKING:
  from sentence_transformers import SentenceTransformer


HUGGINGFACE_ENDPOINT = "https://huggingface.co"
MODELSCOPE_ENDPOINT = "https://modelscope.cn/hf"
_DEFAULT_RETRIES = 3
_DEFAULT_HF_BACKOFF_RETRIES = 1
_DEFAULT_REMOTE_TIMEOUT_SECONDS = 60
_DEFAULT_REMOTE_EMBED_ENDPOINT = os.getenv("DPR_EMBED_API_URL") or "https://zwwen.online/embed"
# 与重排同一把公共服务密钥。没单独配 DPR_EMBED_API_KEY 时用它，否则请求会 401。
_PUBLIC_EMBED_API_KEY = "26932a86d772001af60cbd9d2c162bfda3a90e094f797f3d6806f6077478b27a"
_DEFAULT_REMOTE_EMBED_API_KEY = os.getenv("DPR_EMBED_API_KEY") or _PUBLIC_EMBED_API_KEY
_PAPER_EMBED_DIM = 384
_DEFAULT_CHAT_EMBED_MODEL = "qwen3-embedding"


def _log_default(message: str) -> None:
  print(message, flush=True)


def is_remote_embedding_enabled() -> bool:
  return bool(str(_DEFAULT_REMOTE_EMBED_ENDPOINT or "").strip())


def is_local_embedding_fallback_enabled() -> bool:
  value = str(os.getenv("DPR_EMBED_ALLOW_LOCAL_FALLBACK") or "").strip().lower()
  return value in {"1", "true", "yes", "y", "on"}


class RemoteSentenceTransformer:
  """兼容 SentenceTransformer.encode 接口的远程 embedding 包装器。"""

  is_remote = True

  def __init__(
    self,
    model_name: str,
    endpoint: str,
    api_key: str = "",
    timeout: int = _DEFAULT_REMOTE_TIMEOUT_SECONDS,
    default_batch_size: int = 8,
    local_device: str = "cpu",
    local_retries: int | None = None,
    local_providers: tuple[tuple[str, str], ...] = (
      ("huggingface", HUGGINGFACE_ENDPOINT),
      ("modelscope", MODELSCOPE_ENDPOINT),
    ),
    allow_local_fallback: bool = False,
    log: Callable[[str], None] = _log_default,
  ):
    self.model_name = model_name
    self.endpoint = self._normalize_endpoint(endpoint)
    self.api_key = str(api_key or "").strip()
    self.timeout = max(int(timeout or _DEFAULT_REMOTE_TIMEOUT_SECONDS), 1)
    self.default_batch_size = max(int(default_batch_size or 1), 1)
    self.max_seq_length = None
    self.local_device = str(local_device or "cpu")
    self.local_retries = local_retries
    self.local_providers = local_providers
    self.allow_local_fallback = bool(allow_local_fallback)
    self._local_model = None
    self._log = log
    self._remote_available = True
    self._remote_disabled_reason = ""

  @staticmethod
  def _normalize_endpoint(endpoint: str) -> str:
    text = str(endpoint or "").strip().rstrip("/")
    if not text:
      raise ValueError("远程 embedding 服务地址不能为空（DPR_EMBED_API_URL）")
    if text.endswith("/embed"):
      return text
    return f"{text}/embed"

  def _headers(self) -> dict[str, str]:
    headers = {
      "Content-Type": "application/json",
    }
    if self.api_key:
      headers["Authorization"] = f"Bearer {self.api_key}"
    return headers

  def _get_local_model(self):
    if not self.allow_local_fallback:
      reason = self._remote_disabled_reason or "远程 embedding 请求失败"
      raise RuntimeError(
        f"{reason}；当前默认不安装/加载本地 embedding 模型。"
        "请先检查 zwwen embedding 服务，或设置 DPR_EMBED_ALLOW_LOCAL_FALLBACK=1 "
        "并安装 requirements-local-models.txt 后再启用本地 fallback。"
      )
    if self._local_model is None:
      self._log(
        f"[WARN] 远程 embedding 不可用，回退本地模型：{self.model_name} "
        f"(device={self.local_device})"
      )
      self._local_model = _load_local_sentence_transformer(
        self.model_name,
        device=self.local_device,
        retries=self.local_retries,
        log=self._log,
        providers=self.local_providers,
      )
      if self.max_seq_length is not None and hasattr(self._local_model, "max_seq_length"):
        try:
          self._local_model.max_seq_length = self.max_seq_length
        except Exception:
          pass
    return self._local_model

  def _disable_remote(self, reason: Exception | str) -> None:
    self._remote_available = False
    self._remote_disabled_reason = str(reason or "").strip()

  def _encode_via_local(
    self,
    texts,
    *,
    convert_to_numpy: bool,
    normalize_embeddings: bool,
    batch_size: int,
    show_progress_bar: bool,
    **kwargs,
  ):
    local_model = self._get_local_model()
    result = local_model.encode(
      texts,
      convert_to_numpy=convert_to_numpy,
      normalize_embeddings=normalize_embeddings,
      batch_size=batch_size,
      show_progress_bar=show_progress_bar,
      **kwargs,
    )
    if convert_to_numpy and not isinstance(result, np.ndarray):
      try:
        result = np.asarray(result, dtype=np.float32)
      except Exception:
        pass
    return result

  def encode(
    self,
    texts,
    convert_to_numpy: bool = True,
    normalize_embeddings: bool = True,
    batch_size: int = 8,
    show_progress_bar: bool = False,
    **kwargs,
  ):
    if isinstance(texts, str):
      texts = [texts]
    if not isinstance(texts, list):
      texts = list(texts or [])
    if not texts:
      empty = np.zeros((0, 0), dtype=np.float32)
      return empty if convert_to_numpy else empty.tolist()

    safe_batch_size = max(int(batch_size or self.default_batch_size), 1)
    if not self._remote_available:
      return self._encode_via_local(
        texts,
        convert_to_numpy=convert_to_numpy,
        normalize_embeddings=normalize_embeddings,
        batch_size=safe_batch_size,
        show_progress_bar=show_progress_bar,
        **kwargs,
      )
    try:
      chunks = [texts[i : i + safe_batch_size] for i in range(0, len(texts), safe_batch_size)]
      outputs: list[np.ndarray] = []

      self._log(
        f"[INFO] 远程 embedding：model={self.model_name} "
        f"endpoint={self.endpoint} total={len(texts)} batch={safe_batch_size}"
      )

      for chunk_index, chunk in enumerate(chunks, start=1):
        headers = self._headers()
        response = requests.post(
          self.endpoint,
          headers=headers,
          json={"texts": chunk},
          timeout=self.timeout,
        )
        if response.status_code == 401 and headers.get("Authorization"):
          self._log("[WARN] 远程 embedding 鉴权失败，自动回退为无鉴权请求重试一次。")
          headers = {
            "Content-Type": "application/json",
          }
          response = requests.post(
            self.endpoint,
            headers=headers,
            json={"texts": chunk},
            timeout=self.timeout,
          )
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list):
          raise RuntimeError("远程 embedding 服务返回缺少 embeddings 字段")
        try:
          arr = np.asarray(embeddings, dtype=np.float32)
        except Exception as exc:
          raise RuntimeError(f"远程 embedding 返回无法转换为 float32：{exc}") from exc

        if arr.ndim != 2:
          raise RuntimeError(f"远程 embedding 返回维度异常：shape={getattr(arr, 'shape', None)}")
        if arr.shape[0] != len(chunk):
          raise RuntimeError(
            f"远程 embedding 返回条数异常：expected={len(chunk)} actual={arr.shape[0]}"
          )
        if normalize_embeddings:
          norms = np.linalg.norm(arr, axis=1, keepdims=True)
          arr = arr / np.clip(norms, 1e-12, None)
        outputs.append(arr)
        self._log(
          f"[INFO] 远程 embedding 批次完成：{chunk_index}/{len(chunks)} "
          f"count={len(chunk)} dim={arr.shape[1]}"
        )

      merged = np.vstack(outputs) if outputs else np.zeros((0, 0), dtype=np.float32)
      return merged if convert_to_numpy else merged.tolist()
    except Exception as exc:
      if not self.allow_local_fallback:
        raise RuntimeError(
          f"远程 embedding 请求失败：{exc}。当前默认依赖 zwwen 远程 embedding，"
          "不会自动安装/加载本地 Torch 模型；如需本地 fallback，请设置 "
          "DPR_EMBED_ALLOW_LOCAL_FALLBACK=1 并安装 requirements-local-models.txt。"
        ) from exc
      self._log(f"[WARN] 远程 embedding 请求失败，将自动回退本地模型：{exc}")
      self._disable_remote(exc)
      return self._encode_via_local(
        texts,
        convert_to_numpy=convert_to_numpy,
        normalize_embeddings=normalize_embeddings,
        batch_size=safe_batch_size,
        show_progress_bar=show_progress_bar,
        **kwargs,
      )

  def start_multi_process_pool(self, target_devices=None):
    del target_devices
    return None

  def encode_multi_process(
    self,
    texts,
    pool=None,
    batch_size: int = 8,
    normalize_embeddings: bool = True,
    **kwargs,
  ):
    del pool
    return self.encode(
      texts,
      convert_to_numpy=True,
      normalize_embeddings=normalize_embeddings,
      batch_size=batch_size,
      **kwargs,
    )

  def stop_multi_process_pool(self, pool):
    del pool
    return None


@contextmanager
def _hf_http_backoff(max_retries: int):
  """临时覆盖 huggingface_hub 的 http_backoff 重试次数。

  仅用于抑制单次请求内置重试次数（日志中通常体现为 `Retry x/5`）。
  """
  if max_retries <= 0:
    yield
    return

  try:
    from huggingface_hub.utils import _http as hf_http
  except Exception:
    yield
    return

  origin_http_backoff = hf_http.http_backoff

  def http_backoff_with_retry_limit(*args, **kwargs):
    kwargs.setdefault("max_retries", max_retries)
    return origin_http_backoff(*args, **kwargs)

  hf_http.http_backoff = http_backoff_with_retry_limit
  try:
    yield
  finally:
    hf_http.http_backoff = origin_http_backoff


@contextmanager
def _hf_endpoint(endpoint: Optional[str] = None):
  had_endpoint = "HF_ENDPOINT" in os.environ
  old_endpoint = os.environ.get("HF_ENDPOINT")
  had_base_url = "HF_HUB_BASE_URL" in os.environ
  old_base_url = os.environ.get("HF_HUB_BASE_URL")
  if endpoint:
    os.environ["HF_ENDPOINT"] = endpoint
    os.environ["HF_HUB_BASE_URL"] = endpoint
  elif had_endpoint:
    if "HF_ENDPOINT" in os.environ:
      del os.environ["HF_ENDPOINT"]
    if "HF_HUB_BASE_URL" in os.environ:
      del os.environ["HF_HUB_BASE_URL"]

  try:
    yield
  finally:
    if had_endpoint:
      if old_endpoint is None:
        del os.environ["HF_ENDPOINT"]
      else:
        os.environ["HF_ENDPOINT"] = old_endpoint
    elif "HF_ENDPOINT" in os.environ:
      del os.environ["HF_ENDPOINT"]
    if had_base_url:
      if old_base_url is None:
        del os.environ["HF_HUB_BASE_URL"]
      else:
        os.environ["HF_HUB_BASE_URL"] = old_base_url
    elif "HF_HUB_BASE_URL" in os.environ:
      del os.environ["HF_HUB_BASE_URL"]


class OpenAICompatibleEmbedder:
  """问答接口站的 OpenAI 兼容 /embeddings。"""

  is_remote = True

  def __init__(
    self,
    model_name: str,
    base_url: str,
    api_key: str,
    timeout: int = _DEFAULT_REMOTE_TIMEOUT_SECONDS,
    log: Callable[[str], None] = _log_default,
  ):
    self.model_name = str(model_name or _DEFAULT_CHAT_EMBED_MODEL).strip()
    self.endpoint = self._normalize_endpoint(base_url)
    self.api_key = str(api_key or "").strip()
    self.timeout = max(int(timeout or _DEFAULT_REMOTE_TIMEOUT_SECONDS), 1)
    self.max_seq_length = None
    self._log = log

  @staticmethod
  def _normalize_endpoint(base_url: str) -> str:
    text = str(base_url or "").strip().rstrip("/")
    if not text:
      raise ValueError("问答接口站地址为空")
    if text.lower().endswith("/embeddings"):
      return text
    return f"{text}/embeddings"

  def encode(
    self,
    texts,
    convert_to_numpy: bool = True,
    normalize_embeddings: bool = True,
    batch_size: int = 8,
    show_progress_bar: bool = False,
    **kwargs,
  ):
    del show_progress_bar, kwargs
    if isinstance(texts, str):
      texts = [texts]
    if not isinstance(texts, list):
      texts = list(texts or [])
    if not texts:
      empty = np.zeros((0, 0), dtype=np.float32)
      return empty if convert_to_numpy else empty.tolist()
    if not self.api_key:
      raise RuntimeError("问答接口站的 embedding 缺少密钥")

    safe_batch_size = max(int(batch_size or 8), 1)
    outputs: list[np.ndarray] = []
    headers = {
      "Content-Type": "application/json",
      "Authorization": f"Bearer {self.api_key}",
    }
    self._log(
      f"[INFO] 问答接口站 embedding：model={self.model_name} "
      f"endpoint={self.endpoint} total={len(texts)} batch={safe_batch_size}"
    )
    for start in range(0, len(texts), safe_batch_size):
      chunk = texts[start : start + safe_batch_size]
      response = requests.post(
        self.endpoint,
        headers=headers,
        json={"model": self.model_name, "input": chunk},
        timeout=self.timeout,
      )
      if response.status_code >= 300:
        detail = ""
        try:
          err = response.json().get("error") or {}
          detail = str(err.get("message") or err.get("code") or "")[:180]
        except Exception:
          detail = (response.text or "")[:180]
        raise RuntimeError(f"HTTP {response.status_code} {detail}".strip())
      data = response.json().get("data") or []
      if not isinstance(data, list) or len(data) != len(chunk):
        raise RuntimeError("问答接口站的 embedding 返回条数不对")
      data.sort(key=lambda item: item.get("index", 0))
      try:
        arr = np.asarray([item["embedding"] for item in data], dtype=np.float32)
      except Exception as exc:
        raise RuntimeError(f"问答接口站的 embedding 无法解析：{exc}") from exc
      if arr.ndim != 2 or arr.shape[0] != len(chunk):
        raise RuntimeError("问答接口站的 embedding 维度异常")
      if normalize_embeddings:
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        arr = arr / np.clip(norms, 1e-12, None)
      outputs.append(arr)
    merged = np.vstack(outputs) if outputs else np.zeros((0, 0), dtype=np.float32)
    return merged if convert_to_numpy else merged.tolist()


def _chat_embedding_settings() -> tuple[str, str, str]:
  base = (
    os.getenv("DPR_CHAT_EMBED_BASE_URL")
    or os.getenv("DEEPSEEK_BASE_URL")
    or os.getenv("SUMMARY_BASE_URL")
    or ""
  ).strip().rstrip("/")
  key = (
    os.getenv("DPR_CHAT_EMBED_API_KEY")
    or os.getenv("DEEPSEEK_API_KEY")
    or os.getenv("SUMMARY_API_KEY")
    or ""
  ).strip()
  model = (os.getenv("DPR_EMBED_MODEL") or _DEFAULT_CHAT_EMBED_MODEL).strip()
  return base, key, model


def _try_chat_site_embedder(
  log: Callable[[str], None],
  timeout: int,
) -> Optional[OpenAICompatibleEmbedder]:
  """优先用问答同一个接口站的 embedding。维度必须和论文库一致，否则检索会对不上。"""
  if str(os.getenv("DPR_EMBED_API_URL") or "").strip():
    return None
  base, key, model = _chat_embedding_settings()
  if not base or not key or not model:
    return None
  embedder = OpenAICompatibleEmbedder(
    model_name=model,
    base_url=base,
    api_key=key,
    timeout=timeout,
    log=log,
  )
  try:
    sample = embedder.encode(
      ["probe"],
      convert_to_numpy=True,
      normalize_embeddings=False,
      batch_size=1,
    )
  except Exception as exc:
    log(f"[WARN] 问答接口站的 embedding 现在不可用，改用和论文库一致的向量服务：{exc}")
    return None
  dim = int(sample.shape[1]) if getattr(sample, "ndim", 0) == 2 and sample.shape[0] else 0
  if dim != _PAPER_EMBED_DIM:
    log(
      f"[WARN] 问答接口站的 embedding（{model}）维度是 {dim}，论文库是 {_PAPER_EMBED_DIM}。"
      "维度不一致不能拿来检索，改用和论文库一致的向量服务。"
    )
    return None
  log(
    f"[INFO] 使用问答接口站的 embedding：model={model} "
    f"endpoint={embedder.endpoint} dim={dim}"
  )
  return embedder


def load_sentence_transformer(
  model_name: str,
  *,
  device: str,
  allow_remote: bool = True,
  remote_endpoint: str | None = None,
  remote_api_key: str | None = None,
  retries: int | None = None,
  log: Callable[[str], None] = _log_default,
  providers: tuple[tuple[str, str], ...] = (
    ("huggingface", HUGGINGFACE_ENDPOINT),
    ("modelscope", MODELSCOPE_ENDPOINT),
  ),
):
  effective_remote_endpoint = str(
    remote_endpoint
    if remote_endpoint is not None
    else (os.getenv("DPR_EMBED_API_URL") or _DEFAULT_REMOTE_EMBED_ENDPOINT)
  ).strip()
  effective_remote_api_key = str(
    remote_api_key
    if remote_api_key is not None
    else (
      os.getenv("DPR_EMBED_API_KEY")
      or os.getenv("DPR_PUBLIC_SERVICE_API_KEY")
      or _PUBLIC_EMBED_API_KEY
    )
  ).strip()
  if allow_remote and remote_endpoint is None:
    chat_embedder = _try_chat_site_embedder(log, _DEFAULT_REMOTE_TIMEOUT_SECONDS)
    if chat_embedder is not None:
      return chat_embedder

  if allow_remote and effective_remote_endpoint:
    remote_timeout_text = os.getenv("DPR_EMBED_API_TIMEOUT", str(_DEFAULT_REMOTE_TIMEOUT_SECONDS))
    try:
      remote_timeout = int(remote_timeout_text)
    except ValueError:
      log(
        f"[WARN] 环境变量 DPR_EMBED_API_TIMEOUT 无效：{remote_timeout_text}，"
        f"回退默认 {_DEFAULT_REMOTE_TIMEOUT_SECONDS}"
      )
      remote_timeout = _DEFAULT_REMOTE_TIMEOUT_SECONDS
    log(
      f"[INFO] 使用远程 embedding 服务：model={model_name} "
      f"endpoint={effective_remote_endpoint} timeout={remote_timeout}s device={device}"
    )
    return RemoteSentenceTransformer(
      model_name=model_name,
      endpoint=effective_remote_endpoint,
      api_key=effective_remote_api_key,
      timeout=remote_timeout,
      local_device=device,
      local_retries=retries,
      local_providers=providers,
      allow_local_fallback=is_local_embedding_fallback_enabled(),
      log=log,
    )

  if effective_remote_endpoint and not allow_remote:
    log(f"[INFO] 已禁用远程 embedding，强制使用本地模型：{model_name} (device={device})")

  return _load_local_sentence_transformer(
    model_name,
    device=device,
    retries=retries,
    log=log,
    providers=providers,
  )


def _load_local_sentence_transformer(
  model_name: str,
  *,
  device: str,
  retries: int | None = None,
  log: Callable[[str], None] = _log_default,
  providers: tuple[tuple[str, str], ...] = (
    ("huggingface", HUGGINGFACE_ENDPOINT),
    ("modelscope", MODELSCOPE_ENDPOINT),
  ),
):
  if retries is None:
    env_retries = os.getenv("LLM_EMBED_MODEL_RETRIES")
    if env_retries is None:
      retries = _DEFAULT_RETRIES
    else:
      try:
        retries = int(env_retries)
      except ValueError:
        print(f"[WARN] 环境变量 LLM_EMBED_MODEL_RETRIES 无效：{env_retries}，回退默认 {_DEFAULT_RETRIES}")
        retries = _DEFAULT_RETRIES
  hf_backoff_retries = _DEFAULT_HF_BACKOFF_RETRIES
  env_hf_backoff_retries = os.getenv("HF_HUB_HTTP_BACKOFF_RETRIES")
  if env_hf_backoff_retries is not None:
    try:
      hf_backoff_retries = int(env_hf_backoff_retries)
    except ValueError:
      print(
        f"[WARN] 环境变量 HF_HUB_HTTP_BACKOFF_RETRIES 无效：{env_hf_backoff_retries}，"
        f"回退默认 {_DEFAULT_HF_BACKOFF_RETRIES}"
      )
      hf_backoff_retries = _DEFAULT_HF_BACKOFF_RETRIES
    if hf_backoff_retries < 0:
      hf_backoff_retries = 0

  attempts = max(int(retries or _DEFAULT_RETRIES), 1)
  last_err: Exception | None = None

  for round_idx in range(1, attempts + 1):
    for provider_name, endpoint in providers:
      try:
        log(
          f"[INFO] 尝试加载模型（第 {round_idx}/{attempts} 轮）：{model_name}"
          f"（provider={provider_name}，device={device}）"
        )
        with _hf_endpoint(endpoint), _hf_http_backoff(max_retries=hf_backoff_retries):
          from sentence_transformers import SentenceTransformer
          return SentenceTransformer(model_name, device=device)
      except Exception as e:  # pragma: no cover - 仅异常路径
        last_err = e
        msg = str(e)
        if len(msg) > 260:
          msg = msg[:260]
        log(
          f"[WARN] 模型加载失败（provider={provider_name}，round={round_idx}/{attempts}）："
          f"{msg}"
        )

    if round_idx < attempts:
      wait_seconds = 1
      log(f"[INFO] 重试间隔：{wait_seconds}s")
      time.sleep(wait_seconds)

  if last_err is not None:
    raise last_err
  raise RuntimeError(f"加载模型失败：{model_name}")
