from __future__ import annotations

import logging
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
        self._providers = tuple(provider for provider in providers if provider.configured)
        if not self._providers:
            raise ValueError("at least one configured LLM provider is required")
        self._clients = {
            provider.name: client_factory(provider) for provider in self._providers
        }

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self._providers)

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
        for provider in self._providers:
            client = self._clients[provider.name]
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
        raise LLMGatewayError("all LLM providers failed; " + " | ".join(errors))
