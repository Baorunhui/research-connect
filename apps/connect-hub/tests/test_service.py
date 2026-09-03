from dataclasses import dataclass
from datetime import datetime, timezone

from connect_hub.service import ChatService, _runtime_context_prompt
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


class FakeRemoteStorage:
    configured = True

    def __init__(self):
        self.deleted_sites = []
        self.cleared = False

    def storage_summary(self):
        return {
            "install_id": "install-1",
            "label": "测试用户",
            "site_count": 1,
            "total_bytes": 2048,
            "sites": [
                {
                    "site_id": "daily-paper-test",
                    "title": "Daily Paper",
                    "size_bytes": 2048,
                }
            ],
        }

    def delete_remote_site(self, site_id):
        self.deleted_sites.append(site_id)
        return {"deleted": True, "site_id": site_id}

    def clear_remote_data(self):
        self.cleared = True
        return {"deleted": True, "deleted_sites": 1}


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


def test_runtime_context_injects_current_date_and_search_time_rule(tmp_path):
    prompt = _runtime_context_prompt(datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))
    assert "当前日期：2026-09-01" in prompt
    assert "freshness_days" in prompt
    assert "不要把旧年份写入搜索词" in prompt

    gateway = FakeGateway()
    service = ChatService(gateway, ConversationStore(tmp_path / "date.sqlite3"))
    service.handle("s", "调研最近五天的 RAG")
    system_messages = [m["content"] for m in gateway.messages if m["role"] == "system"]
    assert any("当前日期：" in message for message in system_messages)


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


def test_storage_slash_command_uses_scoped_number_menu(tmp_path):
    gateway = FakeGateway()
    remote = FakeRemoteStorage()
    service = ChatService(
        gateway,
        ConversationStore(tmp_path / "storage.sqlite3"),
        remote_storage=remote,
    )

    opened = service.handle("feishu:chat:user", "/storage").text
    assert "测试用户" in opened
    assert "daily-paper-test" in opened
    assert "2.0 KiB" in opened
    assert "SITE_ID" in service.handle("feishu:chat:user", "2").text
    assert "已删除" in service.handle("feishu:chat:user", "daily-paper-test").text
    assert remote.deleted_sites == ["daily-paper-test"]

    # A plain number outside an active storage flow remains an ordinary LLM message.
    assert service.handle("another-session", "2").text == "answer"


def test_storage_clear_requires_explicit_confirmation(tmp_path):
    remote = FakeRemoteStorage()
    service = ChatService(
        FakeGateway(),
        ConversationStore(tmp_path / "storage-clear.sqlite3"),
        remote_storage=remote,
    )

    service.handle("s", "/storage")
    assert "确认清空" in service.handle("s", "3").text
    assert "确认清空" in service.handle("s", "确认").text
    assert remote.cleared is False
    assert "已清空" in service.handle("s", "确认清空").text
    assert remote.cleared is True


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
