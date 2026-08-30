from __future__ import annotations

import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import httpx
from openai import APITimeoutError

from research_connect_core import JsonLineEventSink, RetryPolicy, runtime_from_env
from research_connect_core.llm import run_with_llm_policy


class ProviderError(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = SimpleNamespace(status_code=status_code, headers={"Retry-After": "0"})


def test_429_is_retried_and_provider_exception_is_preserved():
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ProviderError(429)
        return "ok"

    result = run_with_llm_policy(
        operation,
        api_key="test",
        base_url="https://example.invalid/v1",
        retry_policy=RetryPolicy(max_attempts=4, jitter_ratio=0),
    )
    assert result == "ok"
    assert calls == 3


def test_openai_sdk_timeout_is_retried():
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise APITimeoutError(httpx.Request("POST", "https://example.invalid/v1/chat"))
        return "ok"

    result = run_with_llm_policy(
        operation,
        api_key="timeout-test",
        base_url="https://timeout.invalid/v1",
        retry_policy=RetryPolicy(max_attempts=2, jitter_ratio=0),
    )

    assert result == "ok"
    assert calls == 2


def test_non_retryable_error_is_not_wrapped():
    with pytest.raises(ProviderError) as error:
        run_with_llm_policy(
            lambda: (_ for _ in ()).throw(ProviderError(401)),
            api_key="test",
            base_url="https://example.invalid/v1",
        )
    assert error.value.status_code == 401


def test_provider_concurrency_is_capped_at_four():
    active = 0
    observed = 0
    lock = threading.Lock()

    def operation():
        nonlocal active, observed
        with lock:
            active += 1
            observed = max(observed, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return "ok"

    def invoke(_index: int):
        return run_with_llm_policy(
            operation,
            api_key="shared-key",
            base_url="https://concurrency.invalid/v1",
            max_concurrency=4,
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert list(pool.map(invoke, range(12))) == ["ok"] * 12
    assert observed == 4


def test_cli_events_are_opt_in(monkeypatch):
    monkeypatch.setenv("CONNECT_EMIT_EVENTS", "1")
    monkeypatch.setenv("CONNECT_JOB_ID", "job-known")
    stream = io.StringIO()
    runtime = runtime_from_env("demo", "demo.run")
    runtime.sink = JsonLineEventSink(stream)
    with runtime:
        runtime.progress("working", stage="test")
    text = stream.getvalue()
    assert '"job_id":"job-known"' in text
    assert '"event_type":"job.progress"' in text
