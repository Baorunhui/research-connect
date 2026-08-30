import json
from dataclasses import dataclass

from connect_hub.llm import ToolCall
from connect_hub.service import ChatService
from connect_hub.storage import ConversationStore
from connect_hub.tools import ToolContext, ToolDefinition, ToolRegistry
from connect_hub.tools.web import web_tools
from connect_hub.websearch import (
    SearchResponse,
    SearchResult,
    _decode_mcp_body,
    _parse_exa_text,
)


@dataclass
class FakeResponse:
    content: str = ""
    provider: str = "fake"
    model: str = "fake-model"
    finish_reason: str = "stop"
    tool_calls: tuple[ToolCall, ...] = ()
    prompt_tokens: int = 10
    completion_tokens: int = 4


class ScriptedGateway:
    provider_names = ("fake",)

    def __init__(self, responses):
        self.responses = list(responses)
        self.messages_seen = []
        self.kwargs_seen = []

    def chat(self, messages, **kwargs):
        self.messages_seen.append(list(messages))
        self.kwargs_seen.append(dict(kwargs))
        return self.responses.pop(0)


class FakeSearch:
    name = "fake-search"

    def __init__(self):
        self.calls = 0

    def search(self, query, *, max_results, include_domains=None, freshness_days=None):
        self.calls += 1
        return SearchResponse(
            query=query,
            provider=self.name,
            duration_ms=10,
            results=(
                SearchResult(
                    title="Occupancy Networks for 3D perception",
                    url="https://example.test/occupancy",
                    snippet="Occupancy commonly refers to 3D scene occupancy prediction.",
                    provider=self.name,
                ),
            ),
        )


class FakeReader:
    name = "fake-reader"

    def fetch(self, url, *, max_chars=12000):
        return "page content"


def _registry(store, *definitions):
    registry = ToolRegistry(store)
    for definition in definitions:
        registry.register(definition)
    return registry


def _daily_tool(handler):
    return ToolDefinition(
        name="generate_daily_paper_report",
        description="Generate an expensive paper report only after an explicit request.",
        parameters={
            "type": "object",
            "properties": {
                "topics": {"type": "array", "items": {"type": "object"}},
                "mode": {"type": "string", "enum": ["standard", "skims"]},
                "fetch_days": {"type": "integer", "minimum": 1, "maximum": 30},
                "publish_web": {"type": "boolean"},
            },
            "required": ["topics"],
            "additionalProperties": False,
        },
        handler=handler,
        kind="business",
        track_job=False,
    )


def test_followup_uses_conversation_history_without_task_draft(tmp_path):
    store = ConversationStore(tmp_path / "conversation.sqlite3")
    gateway = ScriptedGateway(
        [
            FakeResponse("你说的 OCC 是 3D occupancy prediction 吗？"),
            FakeResponse("明白了，主题按 3D occupancy prediction 处理。"),
        ]
    )
    service = ChatService(gateway, store)

    first = service.handle("s", "帮我生成一份论文日报，方向是 OCC")
    second = service.handle("s", "对，就是这个")

    assert "occupancy" in first.text
    assert "3D occupancy" in second.text
    second_messages = gateway.messages_seen[1]
    assert {"role": "user", "content": "帮我生成一份论文日报，方向是 OCC"} in second_messages
    assert {"role": "assistant", "content": first.text} in second_messages
    assert store.get_task_draft("s") is None


def test_model_can_search_once_then_answer_with_source(tmp_path):
    store = ConversationStore(tmp_path / "search.sqlite3")
    search = FakeSearch()
    registry = _registry(
        store,
        *web_tools(store, search_provider=search, url_provider=None),
    )
    gateway = ScriptedGateway(
        [
            FakeResponse(
                tool_calls=(
                    ToolCall(
                        "search-1",
                        "web_search",
                        json.dumps({"query": "OCC 3D vision meaning"}),
                    ),
                )
            ),
            FakeResponse("我查到这里更可能指 3D occupancy prediction。"),
        ]
    )
    service = ChatService(gateway, store, tools=registry)

    progress = []
    reply = service.handle("s", "OCC 是什么方向？", progress=progress.append)

    assert search.calls == 1
    assert progress == ["🔎 正在联网搜索：OCC 3D vision meaning"]
    assert "3D occupancy" in reply.text
    assert "https://example.test/occupancy" in reply.text
    run = store.recent_agent_runs("s", 1)[0]
    assert (run["model_calls"], run["search_calls"]) == (2, 1)
    assert [step["step_type"] for step in store.agent_steps(run["id"])] == [
        "model",
        "tool",
        "model",
    ]


def test_web_tools_report_cache_and_page_read_progress(tmp_path):
    store = ConversationStore(tmp_path / "web-progress.sqlite3")
    search = FakeSearch()
    registry = _registry(
        store,
        *web_tools(store, search_provider=search, url_provider=FakeReader()),
    )
    progress = []
    context = ToolContext("s", progress=progress.append)

    first = registry.execute(
        "web_search",
        {"query": "OCC meaning"},
        context=context,
        tool_call_id="search-first",
    )
    cached = registry.execute(
        "web_search",
        {"query": "OCC meaning"},
        context=context,
        tool_call_id="search-cached",
    )
    page = registry.execute(
        "read_web_page",
        {"url": "https://example.test/paper"},
        context=context,
        tool_call_id="read-page",
    )

    assert first.success and cached.success and page.success
    assert search.calls == 1
    assert progress == [
        "🔎 正在联网搜索：OCC meaning",
        "🔎 已使用联网搜索缓存：OCC meaning",
        "📖 正在读取网页：https://example.test/paper",
    ]


def test_business_tool_is_blocked_without_explicit_user_request(tmp_path):
    store = ConversationStore(tmp_path / "gate.sqlite3")
    calls = []
    registry = _registry(
        store,
        _daily_tool(lambda arguments, context: calls.append(arguments) or {"ok": True}),
    )
    gateway = ScriptedGateway(
        [
            FakeResponse(
                tool_calls=(
                    ToolCall(
                        "daily-unsafe",
                        "generate_daily_paper_report",
                        json.dumps({"topics": [{"tag": "3DVG"}]}),
                    ),
                )
            ),
            FakeResponse("你还没有要求生成日报，我先不启动。"),
        ]
    )
    service = ChatService(gateway, store, tools=registry)

    reply = service.handle("s", "3DVG 是什么？")

    assert calls == []
    assert "不启动" in reply.text
    run = store.recent_agent_runs("s", 1)[0]
    assert run["business_tool_calls"] == 0
    tool_step = store.agent_steps(run["id"])[1]
    assert tool_step["status"] == "blocked"


def test_explicit_business_request_executes_once_without_extra_model_call(tmp_path):
    store = ConversationStore(tmp_path / "business.sqlite3")
    calls = []

    def generate(arguments, context: ToolContext):
        calls.append(arguments)
        return {
            "run_id": "daily-test",
            "status": "completed",
            "result": {
                "mode": "standard",
                "generated_at": "2026-08-28T00:00:00Z",
                "deep_dive": [],
                "quick_skim": [],
            },
            "report_path": "",
            "public_url": "",
        }

    registry = _registry(store, _daily_tool(generate))
    store.append(
        "s",
        "assistant",
        "联网生成的 Intent 候选：\n1. Find recent papers on grounded 3D visual understanding.",
    )
    gateway = ScriptedGateway(
        [
            FakeResponse(
                tool_calls=(
                    ToolCall(
                        "daily-1",
                        "generate_daily_paper_report",
                        json.dumps(
                            {
                                "topics": [
                                    {
                                        "tag": "3DVG",
                                        "intent_queries": [
                                            "Find recent papers on grounded 3D visual understanding.",
                                            "Find recent work on language-guided 3D scene reasoning benchmarks.",
                                        ],
                                    }
                                ]
                            }
                        ),
                    ),
                )
            )
        ]
    )
    service = ChatService(gateway, store, tools=registry)

    reply = service.handle("s", "帮我生成一份 3DVG 论文日报")

    assert calls == [
        {
            "topics": [
                {
                    "tag": "3DVG",
                    "intent_queries": [
                        "Find recent papers on grounded 3D visual understanding.",
                        "Find recent work on language-guided 3D scene reasoning benchmarks.",
                    ],
                }
            ],
            "publish_web": True,
        }
    ]
    assert "论文日报已生成" in reply.text
    run = store.recent_agent_runs("s", 1)[0]
    assert (run["model_calls"], run["business_tool_calls"]) == (1, 1)


def test_daily_report_requires_web_grounded_intent_proposal(tmp_path):
    store = ConversationStore(tmp_path / "intent-gate.sqlite3")
    calls = []
    registry = _registry(
        store,
        _daily_tool(lambda arguments, context: calls.append(arguments) or {"ok": True}),
    )
    gateway = ScriptedGateway(
        [
            FakeResponse(
                tool_calls=(
                    ToolCall(
                        "daily-too-early",
                        "generate_daily_paper_report",
                        json.dumps(
                            {
                                "topics": [
                                    {
                                        "tag": "RAG",
                                        "intent_queries": ["Find recent papers on advanced RAG evaluation."],
                                    }
                                ]
                            }
                        ),
                    ),
                )
            ),
            FakeResponse("我会先联网查询，再给出 Intent 候选供你确认。"),
        ]
    )
    service = ChatService(gateway, store, tools=registry)

    reply = service.handle("s", "帮我生成 RAG 论文日报")

    assert calls == []
    assert "Intent 候选" in reply.text
    run = store.recent_agent_runs("s", 1)[0]
    assert run["business_tool_calls"] == 0


def test_intent_confirmation_forces_real_daily_tool_and_rejects_stale_text(tmp_path):
    store = ConversationStore(tmp_path / "confirmed-intent.sqlite3")
    calls = []

    def generate(arguments, context):
        calls.append(arguments)
        return {
            "run_id": "new-rag-run",
            "status": "completed",
            "result": {
                "mode": "skims",
                "generated_at": "2026-08-30T11:00:00Z",
                "deep_dive": [],
                "quick_skim": [{"id": "new-paper", "title": "New RAG paper"}],
            },
            "report_path": "",
            "public_url": "https://reports.test/rag/",
        }

    registry = _registry(store, _daily_tool(generate))
    store.append("s", "user", "帮我生成 RAG 方向最近15天的 skims 日报")
    store.append(
        "s",
        "assistant",
        "联网生成的 Intent 候选：\n"
        "1. Find recent papers on robust RAG readers.\n"
        "2. Find recent papers on RAG evaluation benchmarks.\n"
        "回复确认或补充。",
    )
    arguments = {
        "topics": [
            {
                "tag": "RAG",
                "intent_queries": [
                    "Find recent papers on robust RAG readers.",
                    "Find recent papers on RAG evaluation benchmarks.",
                ],
            }
        ]
    }
    gateway = ScriptedGateway(
        [
            FakeResponse(
                "论文日报已生成。任务：old-stale-run，结果：0篇。",
                tool_calls=(),
            ),
            FakeResponse(
                tool_calls=(
                    ToolCall(
                        "daily-confirmed",
                        "generate_daily_paper_report",
                        json.dumps(arguments),
                    ),
                )
            ),
        ]
    )
    service = ChatService(gateway, store, tools=registry)

    reply = service.handle("s", "确认")

    assert calls == [
        {
            **arguments,
            "fetch_days": 15,
            "mode": "skims",
            "publish_web": True,
        }
    ]
    assert "new-rag-run" in reply.text
    assert "New RAG paper" in reply.text
    assert "old-stale-run" not in reply.text
    forced = {
        "type": "function",
        "function": {"name": "generate_daily_paper_report"},
    }
    assert gateway.kwargs_seen[0]["tool_choice"] == forced
    assert gateway.kwargs_seen[1]["tool_choice"] == forced


def test_confirmation_preserves_explicit_daily_options_over_model_omission(tmp_path):
    store = ConversationStore(tmp_path / "confirmed-options.sqlite3")
    calls = []
    registry = _registry(
        store,
        _daily_tool(lambda arguments, context: calls.append(arguments) or {"ok": True}),
    )
    store.append("s", "user", "帮我生成 RAG 最近15天的 skims 日报，并发布网页")
    store.append(
        "s",
        "assistant",
        "联网生成的 Intent 候选：\n1. Find papers on A.\n2. Find papers on B.",
    )
    gateway = ScriptedGateway(
        [
            FakeResponse(
                tool_calls=(
                    ToolCall(
                        "daily-options",
                        "generate_daily_paper_report",
                        json.dumps(
                            {
                                "topics": [
                                    {
                                        "tag": "RAG",
                                        "intent_queries": [
                                            "Find recent papers on advanced RAG planning.",
                                            "Find recent papers on robust RAG evaluation.",
                                        ],
                                    }
                                ],
                                "mode": "standard",
                                "publish_web": False,
                            }
                        ),
                    ),
                )
            )
        ]
    )
    service = ChatService(gateway, store, tools=registry)

    service.handle("s", "确认")

    assert calls[0]["fetch_days"] == 15
    assert calls[0]["mode"] == "skims"
    assert calls[0]["publish_web"] is True


def test_confirmation_without_tool_can_never_claim_report_generated(tmp_path):
    store = ConversationStore(tmp_path / "no-fake-result.sqlite3")
    registry = _registry(store, _daily_tool(lambda arguments, context: {"ok": True}))
    store.append("s", "user", "帮我生成 RAG 日报")
    store.append(
        "s",
        "assistant",
        "联网生成的 Intent 候选：\n1. Find papers on A.\n2. Find papers on B.",
    )
    gateway = ScriptedGateway(
        [
            FakeResponse("论文日报已生成。任务：stale-1"),
            FakeResponse("论文日报已生成。任务：stale-2"),
            FakeResponse("论文日报已生成。任务：stale-3"),
        ]
    )
    service = ChatService(gateway, store, tools=registry)

    reply = service.handle("s", "确认")

    assert "日报尚未启动" in reply.text
    assert "stale-" not in reply.text
    assert store.recent_agent_runs("s", 1)[0]["business_tool_calls"] == 0


def test_mcp_sse_and_exa_result_parsing():
    decoded = _decode_mcp_body(
        b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
    )
    assert decoded["result"]["ok"] is True
    parsed = _parse_exa_text(
        "Title: A Paper\nURL: https://arxiv.org/abs/1\n"
        "Published: 2026-08-20T00:00:00Z\nHighlights:\nUseful summary",
        "exa-mcp",
    )
    assert parsed[0].title == "A Paper"
    assert parsed[0].snippet == "Useful summary"
    assert parsed[0].provider == "exa-mcp"
