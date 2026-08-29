from dataclasses import dataclass
from types import SimpleNamespace

from connect_hub.config import ProviderSettings
from connect_hub.llm import LLMGateway


class FakeCompletions:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def create(self, **kwargs):
        if self.error:
            raise self.error
        return self.result


class FakeClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def _result(text):
    message = SimpleNamespace(content=text, tool_calls=[])
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=4, completion_tokens=2)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_falls_back_to_second_provider():
    providers = (
        ProviderSettings("primary", "https://one/v1", "key", "m1"),
        ProviderSettings("fallback", "https://two/v1", "key", "m2"),
    )

    def factory(provider):
        if provider.name == "primary":
            return FakeClient(FakeCompletions(error=TimeoutError("slow")))
        return FakeClient(FakeCompletions(result=_result("ok")))

    gateway = LLMGateway(providers, client_factory=factory)
    response = gateway.chat([{"role": "user", "content": "hello"}])

    assert response.content == "ok"
    assert response.provider == "fallback"
    assert response.model == "m2"
