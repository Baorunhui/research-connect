"""Web search parameter compatibility across OpenAI-compatible providers.

Different providers enable in-chat web search via different request fields:
- Gemini / gpt.ge style: ``extra_body={"web_search_options": {}}``
- Zhipu BigModel: ``extra_body={"tools": [{"type": "web_search", "web_search": {"enable": true, "search_engine": "search_pro"}}]}``

This helper lets the rest of the codebase keep a single call site and pick the
right shape based on the configured ``base_url``.
"""


def web_search_extra(base_url: str) -> dict:
    """Return the ``extra_body`` payload that enables web search for the given provider.

    Falls back to the Gemini-style ``web_search_options`` for any non-Zhipu endpoint,
    preserving the original behavior for gpt.ge / Gemini grounding users.
    """
    if base_url and "bigmodel" in base_url:
        return {
            "tools": [
                {
                    "type": "web_search",
                    "web_search": {
                        "enable": True,
                        "search_engine": "search_pro",
                    },
                }
            ]
        }
    return {"web_search_options": {}}


def is_web_search_request(extra_body: dict) -> bool:
    """True when an ``extra_body`` is asking for web search (either shape)."""
    if not extra_body:
        return False
    return "web_search_options" in extra_body or any(
        isinstance(t, dict) and t.get("type") == "web_search"
        for t in extra_body.get("tools", [])
    )
