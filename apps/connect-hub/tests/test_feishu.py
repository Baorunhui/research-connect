from types import SimpleNamespace

from connect_hub.connectors.feishu import (
    FeishuConnector,
    parse_bot_menu_event,
    parse_message_event,
)


def test_parse_text_and_remove_mention_placeholder():
    event = {
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user"},
                "sender_type": "user",
            },
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": "group",
                "message_type": "text",
                "content": '{"text":"@_user_1 你好"}',
                "mentions": [{"key": "@_user_1"}],
            },
        }
    }

    message = parse_message_event(event)
    assert message.text == "你好"
    assert message.mentioned is True
    assert message.session_key == "feishu:oc_1:ou_user"


def test_parse_pdf_file_message():
    event = {
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user"}, "sender_type": "user"},
            "message": {
                "message_id": "om_pdf",
                "chat_id": "oc_1",
                "chat_type": "p2p",
                "message_type": "file",
                "content": '{"file_key":"file_abc","file_name":"paper.pdf"}',
            },
        }
    }

    message = parse_message_event(event)

    assert message.file_key == "file_abc"
    assert message.file_name == "paper.pdf"
    assert message.message_type == "file"


def test_parse_bot_menu_event():
    event = {
        "event": {
            "operator": {
                "operator_name": "Test User",
                "operator_id": {"open_id": "ou_user"},
            },
            "event_key": "connect_web_toggle",
            "timestamp": 123,
        }
    }

    parsed = parse_bot_menu_event(event)
    assert parsed.operator_open_id == "ou_user"
    assert parsed.event_key == "connect_web_toggle"


def test_sends_new_text_message_to_menu_operator():
    created = []

    class Messages:
        def create(self, request):
            created.append(request)
            return SimpleNamespace(
                success=lambda: True,
                code=0,
                msg="ok",
                get_log_id=lambda: "log",
            )

    connector = FeishuConnector(SimpleNamespace(workers=1), service=None)
    connector._api_client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=Messages()))
    )
    connector._send_text_to_open_id("ou_user", "联网已开启")

    connector._executor.shutdown(wait=True)
    assert created[0].receive_id_type == "open_id"
    assert created[0].request_body.receive_id == "ou_user"
    assert created[0].request_body.msg_type == "text"


def test_uploads_and_replies_with_image(tmp_path):
    image_path = tmp_path / "card.png"
    image_path.write_bytes(b"not-a-real-png-but-sdk-does-not-parse-it")

    uploaded = []
    replied = []

    class Images:
        def create(self, request):
            uploaded.append(request)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(image_key="img_test"),
                code=0,
                msg="ok",
            )

    class Messages:
        def reply(self, request):
            replied.append(request)
            return SimpleNamespace(
                success=lambda: True,
                code=0,
                msg="ok",
                get_log_id=lambda: "log",
            )

    settings = SimpleNamespace(workers=1)
    connector = FeishuConnector(settings, service=None)
    connector._api_client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(image=Images(), message=Messages()))
    )

    connector._reply_image("om_test", str(image_path))

    connector._executor.shutdown(wait=True)
    assert len(uploaded) == 1
    assert len(replied) == 1
    assert replied[0].request_body.msg_type == "image"


def test_uploads_and_replies_with_file(tmp_path):
    file_path = tmp_path / "daily.md"
    file_path.write_text("# Daily Paper", encoding="utf-8")
    uploaded = []
    replied = []

    class Files:
        def create(self, request):
            uploaded.append(request)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(file_key="file_test"),
                code=0,
                msg="ok",
            )

    class Messages:
        def reply(self, request):
            replied.append(request)
            return SimpleNamespace(
                success=lambda: True,
                code=0,
                msg="ok",
                get_log_id=lambda: "log",
            )

    connector = FeishuConnector(SimpleNamespace(workers=1), service=None)
    connector._api_client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(file=Files(), message=Messages()))
    )

    connector._reply_file("om_test", str(file_path))

    connector._executor.shutdown(wait=True)
    assert len(uploaded) == 1
    assert uploaded[0].request_body.file_type == "stream"
    assert uploaded[0].request_body.file_name == "daily.md"
    assert replied[0].request_body.msg_type == "file"


def test_downloads_inbound_pdf_to_configured_directory(tmp_path):
    class Resources:
        def get(self, request):
            import io

            return SimpleNamespace(
                success=lambda: True,
                file=io.BytesIO(b"%PDF-test"),
                code=0,
                msg="ok",
            )

    connector = FeishuConnector(
        SimpleNamespace(workers=1), service=None, inbound_dir=tmp_path
    )
    connector._api_client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message_resource=Resources()))
    )
    message = SimpleNamespace(
        message_id="om_pdf",
        file_key="file_abc",
        file_name="paper.pdf",
    )

    path = connector._download_inbound_pdf(message)

    connector._executor.shutdown(wait=True)
    assert path.parent == tmp_path.resolve()
    assert path.read_bytes() == b"%PDF-test"
