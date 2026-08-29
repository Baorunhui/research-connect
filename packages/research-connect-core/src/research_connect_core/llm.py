from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Sequence

import httpx
from openai import AsyncOpenAI, OpenAI

logger = logging.getLogger(__name__)

EventCallback = Callable[[Mapping[str, Any]], None]
UsageCallback = Callable[[Mapping[str, Any]], None]
RuleFallback = Callable[[Exception], str]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    jitter_ratio: float = 0.2

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None and retry_after >= 0:
            base = min(retry_after, self.max_delay_seconds)
        else:
            base = min(
                self.base_delay_seconds * (2 ** max(attempt - 1, 0)),
                self.max_delay_seconds,
            )
        jitter = base * self.jitter_ratio
        return max(0.0, base + random.uniform(-jitter, jitter))


@dataclass(frozen=True)
class LLMProvider:
    name: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 120.0
    max_concurrency: int = 4
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.api_key.strip() and self.model.strip())


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMResult:
    content: str
    provider: str
    model: str
    finish_reason: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    elapsed_seconds: float = 0.0
    degraded: bool = False
    raw: Any = None


class LLMRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _status_code(exc: BaseException) -> int | None:
    raw = getattr(exc, "status_code", None)
    if isinstance(raw, int):
        return raw
    response = getattr(exc, "response", None)
    raw = getattr(response, "status_code", None)
    return int(raw) if isinstance(raw, int) else None


def _headers(exc: BaseException) -> Mapping[str, Any]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def _retry_after_seconds(exc: BaseException) -> float | None:
    value = _headers(exc).get("retry-after") or _headers(exc).get("Retry-After")
    if value is None:
        return None
    text = str(value).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        try:
            target = parsedate_to_datetime(text)
            now = datetime.now(target.tzinfo or timezone.utc)
            return max(0.0, (target - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _is_retryable(exc: BaseException) -> bool:
    status = _status_code(exc)
    if status in {408, 409, 425, 429} or (status is not None and status >= 500):
        return True
    return isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            httpx.TimeoutException,
            httpx.NetworkError,
        ),
    )


def _safe_error(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ").strip()
    return f"{type(exc).__name__}: {text[:500]}"


class _ProviderRuntime:
    def __init__(self, identity: str, max_concurrency: int) -> None:
        self.identity = identity
        self.max_concurrency = max(1, int(max_concurrency))
        self.gate = threading.BoundedSemaphore(self.max_concurrency)

    @staticmethod
    def _emit(callback: EventCallback | None, event_type: str, **payload: Any) -> None:
        if callback is None:
            return
        callback({"event_type": event_type, **payload})

    def call_sync(
        self,
        operation: Callable[[], Any],
        *,
        provider: str,
        policy: RetryPolicy,
        event_callback: EventCallback | None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, max(1, policy.max_attempts) + 1):
            self._emit(
                event_callback,
                "llm.request.started",
                provider=provider,
                attempt=attempt,
                max_attempts=policy.max_attempts,
                concurrency=self.max_concurrency,
            )
            self.gate.acquire()
            try:
                return operation()
            except Exception as exc:
                last_error = exc
            finally:
                self.gate.release()
            status = _status_code(last_error)
            retryable = _is_retryable(last_error)
            if not retryable or attempt >= policy.max_attempts:
                break
            delay = policy.delay(attempt, _retry_after_seconds(last_error))
            self._emit(
                event_callback,
                "llm.request.retrying",
                provider=provider,
                attempt=attempt,
                status_code=status,
                delay_seconds=round(delay, 3),
                rate_limited=status == 429,
            )
            time.sleep(delay)
        assert last_error is not None
        # Compatibility facades deliberately preserve the provider exception.
        # Existing modules use SDK exception types/status codes for quota and
        # fallback decisions; retrying here must not erase that information.
        raise last_error

    async def call_async(
        self,
        operation: Callable[[], Any],
        *,
        provider: str,
        policy: RetryPolicy,
        event_callback: EventCallback | None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, max(1, policy.max_attempts) + 1):
            self._emit(
                event_callback,
                "llm.request.started",
                provider=provider,
                attempt=attempt,
                max_attempts=policy.max_attempts,
                concurrency=self.max_concurrency,
            )
            while not self.gate.acquire(blocking=False):
                await asyncio.sleep(0.01)
            try:
                return await operation()
            except Exception as exc:
                last_error = exc
            finally:
                self.gate.release()
            status = _status_code(last_error)
            retryable = _is_retryable(last_error)
            if not retryable or attempt >= policy.max_attempts:
                break
            delay = policy.delay(attempt, _retry_after_seconds(last_error))
            self._emit(
                event_callback,
                "llm.request.retrying",
                provider=provider,
                attempt=attempt,
                status_code=status,
                delay_seconds=round(delay, 3),
                rate_limited=status == 429,
            )
            await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error


_RUNTIME_LOCK = threading.Lock()
_RUNTIMES: dict[str, _ProviderRuntime] = {}


def _runtime_key(base_url: str, api_key: str, max_concurrency: int) -> str:
    secret_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    return f"{base_url.rstrip('/')}|{secret_hash}|{max(1, max_concurrency)}"


def _get_runtime(base_url: str, api_key: str, max_concurrency: int) -> _ProviderRuntime:
    key = _runtime_key(base_url, api_key, max_concurrency)
    with _RUNTIME_LOCK:
        runtime = _RUNTIMES.get(key)
        if runtime is None:
            runtime = _ProviderRuntime(key, max_concurrency)
            _RUNTIMES[key] = runtime
        return runtime


def run_with_llm_policy(
    operation: Callable[[], Any],
    *,
    api_key: str,
    base_url: str,
    provider_name: str = "llm",
    max_concurrency: int = 4,
    retry_policy: RetryPolicy | None = None,
    event_callback: EventCallback | None = None,
) -> Any:
    """Apply the shared concurrency and transport retry policy to any client.

    This keeps older modules that need custom response parsing on the same
    limiter without forcing them to adopt a particular SDK response object.
    """

    return _get_runtime(base_url, api_key, max_concurrency).call_sync(
        operation,
        provider=provider_name,
        policy=retry_policy or RetryPolicy(),
        event_callback=event_callback,
    )


class _SyncCompletionsFacade:
    def __init__(self, owner: "OpenAIClientFacade") -> None:
        self.owner = owner

    def create(self, **kwargs: Any) -> Any:
        return self.owner._runtime.call_sync(
            lambda: self.owner._raw.chat.completions.create(**kwargs),
            provider=self.owner.provider_name,
            policy=self.owner.retry_policy,
            event_callback=self.owner.event_callback,
        )


class _SyncChatFacade:
    def __init__(self, owner: "OpenAIClientFacade") -> None:
        self.completions = _SyncCompletionsFacade(owner)


class OpenAIClientFacade:
    def __init__(
        self,
        raw: OpenAI,
        runtime: _ProviderRuntime,
        *,
        provider_name: str,
        retry_policy: RetryPolicy,
        event_callback: EventCallback | None,
    ) -> None:
        self._raw = raw
        self._runtime = runtime
        self.provider_name = provider_name
        self.retry_policy = retry_policy
        self.event_callback = event_callback
        self.chat = _SyncChatFacade(self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class _AsyncCompletionsFacade:
    def __init__(self, owner: "AsyncOpenAIClientFacade") -> None:
        self.owner = owner

    async def create(self, **kwargs: Any) -> Any:
        return await self.owner._runtime.call_async(
            lambda: self.owner._raw.chat.completions.create(**kwargs),
            provider=self.owner.provider_name,
            policy=self.owner.retry_policy,
            event_callback=self.owner.event_callback,
        )


class _AsyncChatFacade:
    def __init__(self, owner: "AsyncOpenAIClientFacade") -> None:
        self.completions = _AsyncCompletionsFacade(owner)


class AsyncOpenAIClientFacade:
    def __init__(
        self,
        raw: AsyncOpenAI,
        runtime: _ProviderRuntime,
        *,
        provider_name: str,
        retry_policy: RetryPolicy,
        event_callback: EventCallback | None,
    ) -> None:
        self._raw = raw
        self._runtime = runtime
        self.provider_name = provider_name
        self.retry_policy = retry_policy
        self.event_callback = event_callback
        self.chat = _AsyncChatFacade(self)

    async def close(self) -> None:
        await self._raw.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


def create_openai_client(
    *,
    api_key: str,
    base_url: str,
    timeout: float = 120.0,
    max_concurrency: int = 4,
    retry_policy: RetryPolicy | None = None,
    max_retries: int | None = None,
    provider_name: str = "llm",
    event_callback: EventCallback | None = None,
    http_client: httpx.Client | None = None,
) -> OpenAIClientFacade:
    policy = retry_policy or (
        RetryPolicy() if max_retries is None
        else RetryPolicy(max_attempts=max(1, max_retries + 1))
    )
    client_options: dict[str, Any] = dict(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        timeout=timeout,
        max_retries=0,
    )
    if http_client is not None:
        client_options["http_client"] = http_client
    client = OpenAI(**client_options)
    return OpenAIClientFacade(
        client,
        _get_runtime(base_url, api_key, max_concurrency),
        provider_name=provider_name,
        retry_policy=policy,
        event_callback=event_callback,
    )


def create_async_openai_client(
    *,
    api_key: str,
    base_url: str,
    timeout: float = 120.0,
    max_concurrency: int = 4,
    retry_policy: RetryPolicy | None = None,
    max_retries: int | None = None,
    provider_name: str = "llm",
    event_callback: EventCallback | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> AsyncOpenAIClientFacade:
    policy = retry_policy or (
        RetryPolicy() if max_retries is None
        else RetryPolicy(max_attempts=max(1, max_retries + 1))
    )
    client_options: dict[str, Any] = dict(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        timeout=timeout,
        max_retries=0,
    )
    if http_client is not None:
        client_options["http_client"] = http_client
    client = AsyncOpenAI(**client_options)
    return AsyncOpenAIClientFacade(
        client,
        _get_runtime(base_url, api_key, max_concurrency),
        provider_name=provider_name,
        retry_policy=policy,
        event_callback=event_callback,
    )


def _normalize_response(
    raw: Any,
    provider: LLMProvider,
    elapsed: float,
    *,
    requested_model: str | None = None,
) -> LLMResult:
    choice = raw.choices[0]
    message = choice.message
    calls = tuple(
        ToolCall(
            id=str(call.id),
            name=str(call.function.name),
            arguments=str(call.function.arguments or "{}"),
        )
        for call in (getattr(message, "tool_calls", None) or [])
    )
    usage = getattr(raw, "usage", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(completion_details, "reasoning_tokens", None)
    return LLMResult(
        content=str(getattr(message, "content", None) or ""),
        provider=provider.name,
        model=requested_model or provider.model,
        finish_reason=str(getattr(choice, "finish_reason", None) or ""),
        tool_calls=calls,
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
        reasoning_tokens=reasoning,
        elapsed_seconds=elapsed,
        raw=raw,
    )


class UnifiedLLM:
    def __init__(
        self,
        providers: Sequence[LLMProvider],
        *,
        event_callback: EventCallback | None = None,
        usage_callback: UsageCallback | None = None,
    ) -> None:
        self.providers = tuple(provider for provider in providers if provider.configured)
        if not self.providers:
            raise ValueError("at least one configured LLM provider is required")
        self.event_callback = event_callback
        self.usage_callback = usage_callback
        self.clients = {
            provider.name: create_openai_client(
                api_key=provider.api_key,
                base_url=provider.base_url,
                timeout=provider.timeout_seconds,
                max_concurrency=provider.max_concurrency,
                retry_policy=provider.retry,
                provider_name=provider.name,
                event_callback=event_callback,
            )
            for provider in self.providers
        }

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        response_format: Mapping[str, Any] | None = None,
        extra_body: Mapping[str, Any] | None = None,
        model: str | None = None,
        rule_fallback: RuleFallback | None = None,
    ) -> LLMResult:
        errors: list[Exception] = []
        for provider in self.providers:
            request: dict[str, Any] = {
                "model": model or provider.model,
                "messages": list(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if tools:
                request["tools"] = list(tools)
            if tool_choice is not None:
                request["tool_choice"] = tool_choice
            if response_format is not None:
                request["response_format"] = dict(response_format)
            if extra_body is not None:
                request["extra_body"] = dict(extra_body)
            started = time.monotonic()
            try:
                raw = self.clients[provider.name].chat.completions.create(**request)
                result = _normalize_response(
                    raw,
                    provider,
                    time.monotonic() - started,
                    requested_model=model,
                )
                self._record_usage(result, 200)
                return result
            except Exception as exc:
                errors.append(exc)
                status = _status_code(exc)
                self._record_failed_usage(provider, time.monotonic() - started, status)
                if self.event_callback:
                    self.event_callback(
                        {
                            "event_type": "llm.provider.failed",
                            "provider": provider.name,
                            "status_code": status,
                            "error": _safe_error(exc),
                        }
                    )
        last = errors[-1]
        if rule_fallback is not None:
            content = rule_fallback(last)
            if self.event_callback:
                self.event_callback(
                    {"event_type": "llm.rule_fallback", "error": _safe_error(last)}
                )
            return LLMResult(
                content=content,
                provider="rule-fallback",
                model="deterministic",
                degraded=True,
            )
        raise LLMRequestError(
            "all LLM providers failed: " + " | ".join(_safe_error(exc) for exc in errors),
            status_code=_status_code(last),
            retryable=_is_retryable(last),
        ) from last

    def complete_json(self, *args: Any, **kwargs: Any) -> tuple[Any, LLMResult]:
        kwargs.setdefault("response_format", {"type": "json_object"})
        result = self.complete(*args, **kwargs)
        return json.loads(result.content), result

    def _record_usage(self, result: LLMResult, status_code: int) -> None:
        if self.usage_callback is None:
            return
        self.usage_callback(
            {
                "provider": result.provider,
                "model": result.model,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.total_tokens,
                "elapsed_seconds": result.elapsed_seconds,
                "status_code": status_code,
            }
        )

    def _record_failed_usage(self, provider: LLMProvider, elapsed: float, status: int | None) -> None:
        if self.usage_callback:
            self.usage_callback(
                {
                    "provider": provider.name,
                    "model": provider.model,
                    "elapsed_seconds": elapsed,
                    "status_code": status,
                }
            )


class UnifiedAsyncLLM:
    def __init__(
        self,
        providers: Sequence[LLMProvider],
        *,
        event_callback: EventCallback | None = None,
        usage_callback: UsageCallback | None = None,
    ) -> None:
        self.providers = tuple(provider for provider in providers if provider.configured)
        if not self.providers:
            raise ValueError("at least one configured LLM provider is required")
        self.event_callback = event_callback
        self.usage_callback = usage_callback
        self.clients = {
            provider.name: create_async_openai_client(
                api_key=provider.api_key,
                base_url=provider.base_url,
                timeout=provider.timeout_seconds,
                max_concurrency=provider.max_concurrency,
                retry_policy=provider.retry,
                provider_name=provider.name,
                event_callback=event_callback,
            )
            for provider in self.providers
        }

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
        response_format: Mapping[str, Any] | None = None,
        extra_body: Mapping[str, Any] | None = None,
        model: str | None = None,
        rule_fallback: RuleFallback | None = None,
    ) -> LLMResult:
        errors: list[Exception] = []
        for provider in self.providers:
            request: dict[str, Any] = {
                "model": model or provider.model,
                "messages": list(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if tools:
                request["tools"] = list(tools)
            if tool_choice is not None:
                request["tool_choice"] = tool_choice
            if response_format is not None:
                request["response_format"] = dict(response_format)
            if extra_body is not None:
                request["extra_body"] = dict(extra_body)
            started = time.monotonic()
            try:
                raw = await self.clients[provider.name].chat.completions.create(**request)
                result = _normalize_response(
                    raw,
                    provider,
                    time.monotonic() - started,
                    requested_model=model,
                )
                if self.usage_callback:
                    self.usage_callback(
                        {
                            "provider": result.provider,
                            "model": result.model,
                            "prompt_tokens": result.prompt_tokens,
                            "completion_tokens": result.completion_tokens,
                            "total_tokens": result.total_tokens,
                            "elapsed_seconds": result.elapsed_seconds,
                            "status_code": 200,
                        }
                    )
                return result
            except Exception as exc:
                errors.append(exc)
                if self.usage_callback:
                    self.usage_callback(
                        {
                            "provider": provider.name,
                            "model": provider.model,
                            "elapsed_seconds": time.monotonic() - started,
                            "status_code": _status_code(exc),
                        }
                    )
                if self.event_callback:
                    self.event_callback(
                        {
                            "event_type": "llm.provider.failed",
                            "provider": provider.name,
                            "status_code": _status_code(exc),
                            "error": _safe_error(exc),
                        }
                    )
        last = errors[-1]
        if rule_fallback is not None:
            return LLMResult(
                content=rule_fallback(last),
                provider="rule-fallback",
                model="deterministic",
                degraded=True,
            )
        raise LLMRequestError(
            "all LLM providers failed: " + " | ".join(_safe_error(exc) for exc in errors),
            status_code=_status_code(last),
            retryable=_is_retryable(last),
        ) from last

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for client in self.clients.values()))
