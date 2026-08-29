import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio
import httpx
import pytest
from unittest.mock import MagicMock
from openai import RateLimitError, AuthenticationError

from citationclaw.core.scholar_search_agent import ScholarSearchAgent


def _rate_limit_error():
    resp = httpx.Response(429, request=httpx.Request("POST", "http://test/v1"))
    return RateLimitError("Error code: 429 - 余额不足", response=resp, body=None)


def _auth_error():
    resp = httpx.Response(401, request=httpx.Request("POST", "http://test/v1"))
    return AuthenticationError("Error code: 401 - invalid key", response=resp, body=None)


def _ok_response(text="$$$分隔符$$$\n张三\n清华大学\n中国\n教授\nIEEE Fellow\n$$$分隔符$$$"):
    m = MagicMock()
    m.choices = [MagicMock()]
    m.choices[0].message.content = text
    return m


def _agent_with_calls(callables, keys):
    """Build an agent whose client.chat.completions.create returns/raises in order."""
    agent = ScholarSearchAgent(api_keys=keys, base_url="http://test/v1", model="m")
    it = iter(callables)

    class _FakeCompletions:
        async def create(self, **kw):
            fn = next(it)
            if isinstance(fn, Exception):
                raise fn
            return fn

    fake_client = MagicMock()
    fake_client.chat.completions = _FakeCompletions()
    agent._client = fake_client
    agent._client_key = "sentinel"
    agent._ensure_client = lambda key: None  # keep the fake client
    return agent


def _run(agent):
    return asyncio.get_event_loop().run_until_complete(
        agent.search_paper_authors("Some Paper", [{"name": "Zhang San"}])
    )


# ── pool building / round robin ────────────────────────────────────────

def test_pool_from_api_keys_only():
    a = ScholarSearchAgent(api_keys=["k1", "k2", " ", "k3"])
    assert a._api_keys == ["k1", "k2", "k3"]


def test_pool_fallback_to_single_api_key():
    a = ScholarSearchAgent(api_key="kX")
    assert a._api_keys == ["kX"]


def test_pool_api_key_not_duplicated():
    a = ScholarSearchAgent(api_key="k1", api_keys=["k1", "k2"])
    assert a._api_keys == ["k1", "k2"]


def test_pick_key_round_robin():
    a = ScholarSearchAgent(api_keys=["k1", "k2", "k3"])
    picked = [a._pick_key() for _ in range(6)]
    # indices: 0,1,2,0,1,2
    assert picked == [0, 1, 2, 0, 1, 2]


def test_pick_key_skips_exhausted():
    a = ScholarSearchAgent(api_keys=["k1", "k2"])
    a._exhausted.add(0)
    assert a._pick_key() == 1
    a._exhausted.add(1)
    assert a._pick_key() is None


def test_no_keys_returns_empty():
    a = ScholarSearchAgent()
    assert _run(a) == []


# ── 429 / 401 failover ─────────────────────────────────────────────────

def test_429_switches_to_next_key():
    agent = _agent_with_calls(
        [_rate_limit_error(), _ok_response()],
        keys=["bad-key-1111", "good-key-2222"],
    )
    logs = []
    agent._log = logs.append
    results = _run(agent)
    assert len(results) == 1
    assert results[0].name == "张三"
    assert any("429" in l for l in logs)
    assert 0 in agent._exhausted  # first key marked dead


def test_401_switches_to_next_key():
    agent = _agent_with_calls(
        [_auth_error(), _ok_response()],
        keys=["bad-key-1111", "good-key-2222"],
    )
    results = _run(agent)
    assert len(results) == 1
    assert 0 in agent._exhausted


def test_all_keys_exhausted_returns_empty():
    agent = _agent_with_calls(
        [_rate_limit_error(), _rate_limit_error()],
        keys=["bad-1", "bad-2"],
    )
    logs = []
    agent._log = logs.append
    assert _run(agent) == []
    assert any("所有 key" in l for l in logs)


def test_single_key_429_returns_empty_no_loop():
    agent = _agent_with_calls(
        [_rate_limit_error()],
        keys=["only-key"],
    )
    assert _run(agent) == []


def test_non_429_error_returns_empty_immediately():
    agent = _agent_with_calls(
        [RuntimeError("network down"), _ok_response()],
        keys=["k1", "k2"],
    )
    assert _run(agent) == []
    # second key must NOT be consumed
    assert not agent._exhausted


def test_success_on_first_key_no_failover():
    agent = _agent_with_calls(
        [_ok_response()],
        keys=["k1", "k2"],
    )
    results = _run(agent)
    assert len(results) == 1
    assert not agent._exhausted
