from dataclasses import dataclass

from connect_hub.service import ChatService
from connect_hub.storage import ConversationStore


@dataclass
class FakeResponse:
    content: str
    provider: str = "fake"
    model: str = "fake-model"


class FakeGateway:
    provider_names = ("fake",)

    def __init__(self):
        self.messages = []

    def chat(self, messages, **kwargs):
        self.messages = list(messages)
        return FakeResponse("answer")


def test_commands_and_history(tmp_path):
    gateway = FakeGateway()
    store = ConversationStore(tmp_path / "messages.sqlite3")
    service = ChatService(gateway, store, history_messages=10)

    assert service.handle("s", "/ping").text == "pong"
    assert service.handle("s", "question").text == "answer"
    assert store.history("s") == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    assert service.handle("s", "/reset").text.startswith("已清空当前会话")


def test_module_page_shortcuts_do_not_call_llm(tmp_path):
    gateway = FakeGateway()
    store = ConversationStore(tmp_path / "shortcuts.sqlite3")
    service = ChatService(
        gateway,
        store,
        shortcut_urls={
            "paper_reader": "https://reports.test/papers/",
            "citationclaw": "https://reports.test/citations/",
            "config": "https://reports.test/configure/token/",
        },
    )

    assert service.handle("s", "/paper_reader").text.endswith("https://reports.test/papers/")
    assert service.handle("s", "/citationclaw").text.endswith("https://reports.test/citations/")
    assert "https://reports.test/configure/token/" in service.handle("s", "/config").text
    assert gateway.messages == []


def test_first_use_configuration_notice_is_once_per_feishu_user(tmp_path):
    store = ConversationStore(tmp_path / "notices.sqlite3")
    service = ChatService(
        FakeGateway(), store, shortcut_urls={"config": "https://reports.test/configure/token/"}
    )

    assert "首次使用" in service.first_use_notice("feishu:chat-a:user-1")
    assert service.first_use_notice("feishu:chat-b:user-1") == ""
    assert "首次使用" in service.first_use_notice("feishu:chat-a:user-2")


def test_job_query_commands_are_session_scoped(tmp_path):
    gateway = FakeGateway()
    store = ConversationStore(tmp_path / "jobs.sqlite3")
    store.create_job(
        "job-abcdef123456",
        session_key="s",
        job_type="daily_report",
        module_name="daily-paper",
        module_version="1.0.0",
        input_data={"topic": "3DVG"},
    )
    store.update_job("job-abcdef123456", status="running")
    store.append_job_event(
        event_id="evt-query",
        job_id="job-abcdef123456",
        event_type="job.progress",
        message="正在召回论文",
    )
    store.record_usage(
        "job-abcdef123456",
        provider="test",
        operation="chat",
        input_tokens=10,
        output_tokens=5,
        duration_ms=20,
    )
    service = ChatService(gateway, store)

    jobs_reply = service.handle("s", "/jobs").text
    assert "abcdef12" in jobs_reply
    assert "运行中" in jobs_reply
    detail = service.handle("s", "/job abcdef12").text
    assert "job-abcdef123456" in detail
    assert "正在召回论文" in detail
    assert "输入 10 tokens" in detail
    assert "没有找到" in service.handle("another-session", "/job abcdef12").text


def test_web_mode_commands_persist_per_session(tmp_path):
    gateway = FakeGateway()
    store = ConversationStore(tmp_path / "web-mode.sqlite3")
    service = ChatService(gateway, store)

    assert "开启" in service.handle("s", "/web").text
    assert "开启" in service.handle("s", "/web on").text
    assert store.get_web_mode("s") == "on"
    assert "关闭" in service.handle("s", "关闭联网").text
    assert store.get_web_mode("s") == "off"
    assert store.get_web_mode("another") == "on"


def test_legacy_feishu_web_menu_toggle_still_works(tmp_path):
    store = ConversationStore(tmp_path / "menu-toggle.sqlite3")
    service = ChatService(FakeGateway(), store)

    first = service.toggle_feishu_user_web_mode("ou_user")
    second = service.toggle_feishu_user_web_mode("ou_user")

    assert "关闭" in first.text
    assert "开启" in second.text
    assert store.get_web_mode("feishu:oc_chat:ou_user") == "on"
