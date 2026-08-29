# -*- coding: utf-8 -*-
"""local_embed 测试：openai 通道批量/解析/重试，provider 解析与降级。"""
import sys
from pathlib import Path

import numpy as np

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
  sys.path.insert(0, str(SRC_DIR))

import local_embed as LE


def test_resolve_embedding_target_openai_defaults(monkeypatch):
  monkeypatch.delenv("DPR_LOCAL_EMBED_PROVIDER", raising=False)
  monkeypatch.delenv("DPR_LOCAL_EMBED_BASE_URL", raising=False)
  monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
  monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
  monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.com/v1")
  provider, opts = LE.resolve_embedding_target()
  assert provider == "openai"
  assert opts["base_url"] == "https://api.example.com/v1"
  assert opts["api_key"] == "sk-test"
  assert opts["model"] == LE.DEFAULT_OPENAI_EMBED_MODEL


def test_resolve_embedding_target_local(monkeypatch):
  monkeypatch.setenv("DPR_LOCAL_EMBED_PROVIDER", "local")
  provider, opts = LE.resolve_embedding_target()
  assert provider == "local"
  assert opts["model"] == LE.DEFAULT_LOCAL_EMBED_MODEL


def test_embed_openai_batches_and_orders(monkeypatch):
  captured = []

  class FakeResp:
    def __init__(self, payload):
      self._payload = payload

    def raise_for_status(self):
      return None

    def json(self):
      return self._payload

  def fake_post(url, json=None, headers=None, timeout=0):
    start = sum(len(c["input"]) for c in captured)
    rows = [{"index": i, "embedding": [float(start + i), 1.0]} for i in range(len(json["input"]))]
    captured.append({"input": list(json["input"]), "model": json["model"]})
    return FakeResp({"data": rows})

  # 必须给 openai 通道端点/密钥，否则 resolve 阶段就抛 ValueError 走真·本地降级
  monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
  monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.com/v1")
  monkeypatch.setattr(LE.requests, "post", fake_post)
  texts = [f"t{i}" for i in range(70)]  # 70 条 → 32+32+6 三批
  vectors, provider = LE.embed_texts(texts)
  assert provider == "openai"
  assert vectors.shape == (70, 2)
  assert [c["model"] for c in captured] == ["qwen3-embedding"] * 3
  assert [len(c["input"]) for c in captured] == [32, 32, 6]
  # index 排序后与输入顺序一致
  assert vectors[0][0] == 0.0 and vectors[69][0] == 69.0


def test_embed_openai_failure_falls_back_to_local(monkeypatch):
  state = {"openai": 0, "local": 0}

  def fake_post(*a, **k):
    state["openai"] += 1
    raise RuntimeError("boom")

  def fake_embed_local(texts, opts):
    state["local"] += 1
    return np.ones((len(texts), 4), dtype=np.float32)

  monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
  monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.com/v1")
  monkeypatch.setattr(LE.requests, "post", fake_post)
  monkeypatch.setattr(LE, "_embed_local", fake_embed_local)
  monkeypatch.setattr(LE.time, "sleep", lambda s: None)
  vectors, provider = LE.embed_texts(["hello"])
  assert provider == "local"  # openai 失败自动降级
  assert state["openai"] == 5  # 重试 5 次（中转站通道随机 500，靠次数堆过不稳定窗口）
  assert state["local"] == 1
  assert vectors.shape == (1, 4)


def test_embed_empty_input_returns_empty():
  vectors, provider = LE.embed_texts([])
  assert vectors.shape[0] == 0
  assert provider
