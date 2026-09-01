import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from connect_hub.llm import ToolCall
from connect_hub.jobs import JobCoordinator
from connect_hub.processes import ManagedProcessRunner
from connect_hub.service import (
    ChatService,
    _format_daily_paper_result,
    _format_xhs_result,
)
from connect_hub.tools.daily_paper import render_daily_paper_markdown
from connect_hub.storage import ConversationStore
from connect_hub.tools import ToolContext, ToolDefinition, ToolRegistry


@dataclass
class FakeResponse:
    content: str
    provider: str = "fake"
    model: str = "fake-model"
    tool_calls: tuple[ToolCall, ...] = ()


class ToolCallingGateway:
    provider_names = ("fake",)

    def __init__(self):
        self.calls = 0
        self.messages = []

    def chat(self, messages, **kwargs):
        self.calls += 1
        self.messages = list(messages)
        if self.calls == 1:
            return FakeResponse(
                "",
                tool_calls=(ToolCall("call-1", "echo", '{"value":"hello"}'),),
            )
        tool_message = next(item for item in messages if item["role"] == "tool")
        payload = json.loads(tool_message["content"])
        return FakeResponse(f"tool said {payload['output']['value']}")


def test_registry_validates_and_audits(tmp_path):
    store = ConversationStore(tmp_path / "messages.sqlite3")
    registry = ToolRegistry(store)
    registry.register(
        ToolDefinition(
            name="echo",
            description="echo",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            handler=lambda args, context: {"value": args["value"]},
        )
    )

    invalid = registry.execute(
        "echo", {}, context=ToolContext("s"), tool_call_id="bad"
    )
    assert invalid.success is False
    assert store.recent_tool_runs("s") == []

    result = registry.execute(
        "echo", {"value": "ok"}, context=ToolContext("s"), tool_call_id="good"
    )
    assert result.success is True
    assert result.output == {"value": "ok"}
    assert store.recent_tool_runs("s")[0]["status"] == "completed"


def test_registry_can_replace_runtime_provider_tool(tmp_path):
    store = ConversationStore(tmp_path / "runtime-tools.sqlite3")
    registry = ToolRegistry(store)
    definition = ToolDefinition(
        name="runtime_search",
        description="runtime",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda _args, _context: {"ok": True},
    )
    registry.register(definition)
    assert "runtime_search" in registry.names
    assert registry.unregister("runtime_search") is True
    assert registry.unregister("runtime_search") is False
    assert "runtime_search" not in registry.names
    registry.register(definition)
    assert "runtime_search" in registry.names


def test_registry_validates_nonempty_business_inputs(tmp_path):
    store = ConversationStore(tmp_path / "nonempty.sqlite3")
    registry = ToolRegistry(store)
    registry.register(
        ToolDefinition(
            name="bounded_input",
            description="bounded",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 2,
                        "items": {"type": "string"},
                    },
                },
                "required": ["title", "items"],
                "additionalProperties": False,
            },
            handler=lambda args, context: args,
        )
    )

    empty = registry.execute(
        "bounded_input",
        {"title": " ", "items": []},
        context=ToolContext("s"),
        tool_call_id="empty",
    )
    too_many = registry.execute(
        "bounded_input",
        {"title": "ok", "items": ["a", "b", "c"]},
        context=ToolContext("s"),
        tool_call_id="many",
    )

    assert empty.success is False
    assert "title" in empty.error
    assert too_many.success is False
    assert "at most 2" in too_many.error


def test_chat_service_executes_registered_tool(tmp_path):
    store = ConversationStore(tmp_path / "messages.sqlite3")
    registry = ToolRegistry(store)
    registry.register(
        ToolDefinition(
            name="echo",
            description="echo",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            handler=lambda args, context: {"value": args["value"]},
        )
    )
    gateway = ToolCallingGateway()
    service = ChatService(gateway, store, tools=registry)

    reply = service.handle("s", "use the echo tool")

    assert reply.text == "tool said hello"
    assert gateway.calls == 2
    assert store.recent_tool_runs("s")[0]["tool_name"] == "echo"


def test_xhs_result_is_deterministic_and_returns_images(tmp_path):
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    first.write_bytes(b"png")
    second.write_bytes(b"png")

    text, attachments = _format_xhs_result(
        {
            "title": "原样标题",
            "content": "第一段\n\n第二段",
            "tags": ["科研", "Agent"],
            "images": [str(first), str(second)],
            "needs_human_check": ["检查链接"],
        }
    )

    assert "标题：原样标题" in text
    assert "第一段\n\n第二段" in text
    assert "#科研 #Agent" in text
    assert [item.path for item in attachments] == [str(first), str(second)]


def test_daily_paper_result_returns_summary_and_markdown_file(tmp_path):
    report = tmp_path / "daily.md"
    report.write_text("# 日报", encoding="utf-8")
    result = {
        "mode": "standard",
        "generated_at": "2026-08-19T00:00:00Z",
        "deep_dive": [
            {
                "title": "A Paper",
                "link": "https://arxiv.org/abs/1",
                "llm_score": 9,
                "llm_tldr_cn": "一句话总结",
            }
        ],
        "quick_skim": [],
    }

    text, attachments = _format_daily_paper_result(
        {
            "run_id": "pe-test-20260819",
            "result": result,
            "report_path": str(report),
            "public_url": "https://papers.example.test",
        }
    )

    assert "精读 1 篇，速读 0 篇" in text
    assert "A Paper（9分）" in text
    assert "https://papers.example.test" in text
    assert attachments[0].kind == "file"
    assert attachments[0].path == str(report)

    markdown = render_daily_paper_markdown(
        run_id="pe-test-20260819",
        result=result,
        topics=[{"tag": "Pose", "keywords": ["pose estimation"]}],
    )
    assert "# 论文日报" in markdown
    assert "主题：Pose" in markdown
    assert "[A Paper](https://arxiv.org/abs/1)" in markdown


def test_registry_persists_events_and_notifies(tmp_path):
    store = ConversationStore(tmp_path / "events.sqlite3")
    registry = ToolRegistry(store)
    registry.register(
        ToolDefinition(
            name="progress_demo",
            description="demo",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda args, context: (
                context.report_progress("正在处理第二步") or {"ok": True}
            ),
            progress_message="正在处理第一步",
            start_url="https://reports.test/site/",
            module_name="demo",
            module_version="1.0.0",
        )
    )
    notifications = []
    result = registry.execute(
        "progress_demo",
        {},
        context=ToolContext("s1", progress=notifications.append),
        tool_call_id="call-events",
    )

    assert result.success is True
    job = store.get_job(result.job_id)
    assert job["status"] == "completed"
    assert [item["event_type"] for item in store.job_events(result.job_id)] == [
        "job.accepted",
        "job.started",
        "job.progress",
        "job.completed",
    ]
    assert any("正在处理第一步" in item for item in notifications)
    assert any("正在处理第二步" in item for item in notifications)
    assert notifications[0].endswith("网页版：https://reports.test/site/")


def test_cancel_stops_managed_process_and_marks_job_cancelled(tmp_path):
    store = ConversationStore(tmp_path / "cancel.sqlite3")
    registry = ToolRegistry(store)

    def long_handler(args, context):
        context.run_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout=60,
        )
        return {"unexpected": True}

    registry.register(
        ToolDefinition(
            name="long_process",
            description="long process",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=long_handler,
            timeout_seconds=65,
            progress_message="长任务开始",
            module_name="demo",
            module_version="1.0.0",
        )
    )
    result_box = {}

    def execute():
        result_box["result"] = registry.execute(
            "long_process",
            {},
            context=ToolContext("s-cancel"),
            tool_call_id="call-cancel",
        )

    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 5
    active = None
    while time.monotonic() < deadline:
        active = store.latest_active_job("s-cancel")
        if active and registry.jobs.process_runner.active_pid(active["id"]):
            break
        time.sleep(0.05)
    assert active is not None
    recorded = store.get_job(active["id"])
    assert recorded["pid"] == registry.jobs.process_runner.active_pid(active["id"])
    assert recorded["process_executable"]

    cancelled = registry.cancel_latest("s-cancel")
    assert cancelled.found is True
    thread.join(timeout=8)
    assert not thread.is_alive()
    result = result_box["result"]
    assert result.success is False
    assert store.get_job(result.job_id)["status"] == "cancelled"
    assert store.get_job(result.job_id)["pid"] is None
    assert registry.jobs.process_runner.active_pid(result.job_id) is None


def test_startup_interrupts_stale_job_and_terminates_recorded_process(tmp_path):
    if os.name == "nt":
        return
    store = ConversationStore(tmp_path / "restart-cleanup.sqlite3")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        store.create_job(
            "job-stale-process",
            session_key="s-restart",
            job_type="paper_research",
            module_name="daily-paper",
            module_version="1.0.0",
            input_data={},
        )
        store.update_job("job-stale-process", status="running")
        store.set_job_process("job-stale-process", process.pid, sys.executable)

        JobCoordinator(
            store,
            process_runner=ManagedProcessRunner(cancel_grace_seconds=0.1),
            interrupt_stale=True,
        )

        process.wait(timeout=3)
        job = store.get_job("job-stale-process")
        assert job["status"] == "interrupted"
        assert job["error_code"] == "SERVICE_RESTARTED"
        event = store.job_events("job-stale-process")[-1]
        assert event["event_type"] == "job.interrupted"
        assert event["payload"]["cleanup_succeeded"] is True
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
