from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from research_connect_core.llm import RetryPolicy, create_openai_client

from connect_hub.config import ProviderSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMResponse:
    content: str
    provider: str
    model: str
    finish_reason: str
    tool_calls: tuple[ToolCall, ...] = ()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMGatewayError(RuntimeError):
    pass


ClientFactory = Callable[[ProviderSettings], Any]


def _default_client_factory(provider: ProviderSettings) -> Any:
    return create_openai_client(
        api_key=provider.api_key,
        base_url=provider.base_url,
        timeout=provider.timeout_seconds,
        max_concurrency=4,
        retry_policy=RetryPolicy(max_attempts=max(1, provider.max_retries + 1)),
        provider_name=provider.name,
    )


class LLMGateway:
    """Normalize one or more OpenAI-compatible providers behind one API."""

    def __init__(
        self,
        providers: Iterable[ProviderSettings],
        *,
        client_factory: ClientFactory = _default_client_factory,
    ) -> None:
        self._client_factory = client_factory
        self._lock = threading.RLock()
        self._providers: tuple[ProviderSettings, ...] = ()
        self._clients: dict[str, Any] = {}
        self.update_providers(providers)

    def update_providers(self, providers: Iterable[ProviderSettings]) -> None:
        configured = tuple(provider for provider in providers if provider.configured)
        clients = {provider.name: self._client_factory(provider) for provider in configured}
        with self._lock:
            self._providers = configured
            self._clients = clients

    @property
    def provider_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(provider.name for provider in self._providers)

    @property
    def primary_provider(self) -> ProviderSettings:
        with self._lock:
            if not self._providers:
                raise LLMGatewayError("no configured LLM provider; open /config first")
            return self._providers[0]

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        errors: list[str] = []
        with self._lock:
            providers = self._providers
            clients = dict(self._clients)
        for provider in providers:
            client = clients[provider.name]
            request: dict[str, Any] = {
                "model": provider.model,
                "messages": list(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if tools:
                request["tools"] = list(tools)
            if tool_choice is not None:
                request["tool_choice"] = tool_choice
            try:
                raw = client.chat.completions.create(**request)
                choice = raw.choices[0]
                message = choice.message
                normalized_calls = tuple(
                    ToolCall(
                        id=str(call.id),
                        name=str(call.function.name),
                        arguments=str(call.function.arguments or "{}"),
                    )
                    for call in (message.tool_calls or [])
                )
                usage = getattr(raw, "usage", None)
                return LLMResponse(
                    content=str(message.content or ""),
                    provider=provider.name,
                    model=provider.model,
                    finish_reason=str(choice.finish_reason or ""),
                    tool_calls=normalized_calls,
                    prompt_tokens=getattr(usage, "prompt_tokens", None),
                    completion_tokens=getattr(usage, "completion_tokens", None),
                )
            except Exception as exc:  # provider errors vary between compatible APIs
                logger.warning("LLM provider %s failed: %s", provider.name, exc)
                errors.append(f"{provider.name}: {type(exc).__name__}: {exc}")
        if not providers:
            raise LLMGatewayError("no configured LLM provider; open /config first")
        raise LLMGatewayError("all LLM providers failed; " + " | ".join(errors))
