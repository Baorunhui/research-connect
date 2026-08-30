from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from connect_hub.config import Settings
from connect_hub.service import ChatService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    sender_type: str
    message_type: str
    text: str
    mentioned: bool
    file_key: str = ""
    file_name: str = ""

    @property
    def session_key(self) -> str:
        return f"feishu:{self.chat_id}:{self.sender_open_id}"


@dataclass(frozen=True)
class BotMenuEvent:
    event_key: str
    operator_open_id: str


BOT_MENU_WEB_MODES = {
    "connect_web_auto": "auto",
    "connect_web_on": "on",
    "connect_web_off": "off",
}
BOT_MENU_WEB_TOGGLE = "connect_web_toggle"


def _nested(data: Mapping[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def parse_message_event(payload: Mapping[str, Any]) -> InboundMessage:
    event = payload.get("event") if isinstance(payload.get("event"), Mapping) else payload
    message = event.get("message") if isinstance(event, Mapping) else None
    sender = event.get("sender") if isinstance(event, Mapping) else None
    if not isinstance(message, Mapping) or not isinstance(sender, Mapping):
        raise ValueError("invalid Feishu message event")

    raw_content = str(message.get("content") or "")
    try:
        content = json.loads(raw_content) if raw_content else {}
    except json.JSONDecodeError:
        content = {"text": raw_content}
    text = str(content.get("text") or "") if isinstance(content, Mapping) else ""
    file_key = str(content.get("file_key") or "") if isinstance(content, Mapping) else ""
    file_name = str(content.get("file_name") or "") if isinstance(content, Mapping) else ""
    mentions = message.get("mentions") if isinstance(message.get("mentions"), list) else []
    for mention in mentions:
        if isinstance(mention, Mapping):
            key = str(mention.get("key") or "")
            if key:
                text = text.replace(key, "")

    return InboundMessage(
        message_id=str(message.get("message_id") or ""),
        chat_id=str(message.get("chat_id") or ""),
        chat_type=str(message.get("chat_type") or ""),
        sender_open_id=str(_nested(sender, "sender_id", "open_id") or ""),
        sender_type=str(sender.get("sender_type") or ""),
        message_type=str(message.get("message_type") or ""),
        text=text.strip(),
        mentioned=bool(mentions),
        file_key=file_key.strip(),
        file_name=file_name.strip(),
    )


def parse_bot_menu_event(payload: Mapping[str, Any]) -> BotMenuEvent:
    event = payload.get("event") if isinstance(payload.get("event"), Mapping) else payload
    operator = event.get("operator") if isinstance(event, Mapping) else None
    operator_id = operator.get("operator_id") if isinstance(operator, Mapping) else None
    if not isinstance(operator_id, Mapping):
        raise ValueError("invalid Feishu bot menu event")
    event_key = str(event.get("event_key") or "").strip()
    open_id = str(operator_id.get("open_id") or "").strip()
    if not event_key or not open_id:
        raise ValueError("Feishu bot menu event is missing event_key or open_id")
    return BotMenuEvent(event_key=event_key, operator_open_id=open_id)


class FeishuConnector:
    """Official Feishu SDK WebSocket connector.

    The SDK event callback only parses, validates and queues work so that it
    returns within Feishu's three-second acknowledgement window.
    """

    def __init__(
        self,
        settings: Settings,
        service: ChatService,
        *,
        inbound_dir: str | Path | None = None,
    ) -> None:
        self.settings = settings
        self.service = service
        self._executor = ThreadPoolExecutor(
            max_workers=settings.workers, thread_name_prefix="feishu-message"
        )
        self._seen: deque[str] = deque(maxlen=2048)
        self._seen_set: set[str] = set()
        self._seen_lock = threading.Lock()
        self._api_client: Any = None
        self._inbound_dir = Path(inbound_dir or ".").expanduser().resolve()
        self._inbound_dir.mkdir(parents=True, exist_ok=True)

    def _mark_seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._seen_lock:
            if message_id in self._seen_set:
                return False
            if len(self._seen) == self._seen.maxlen:
                expired = self._seen.popleft()
                self._seen_set.discard(expired)
            self._seen.append(message_id)
            self._seen_set.add(message_id)
            return True

    def _authorized(self, message: InboundMessage) -> bool:
        return self._authorized_open_id(message.sender_open_id)

    def _authorized_open_id(self, open_id: str) -> bool:
        allowed = self.settings.feishu_allowed_open_ids
        return not allowed or open_id in allowed

    def _on_message(self, data: Any) -> None:
        try:
            import lark_oapi as lark

            payload = json.loads(lark.JSON.marshal(data))
            message = parse_message_event(payload)
        except Exception:
            logger.exception("failed to parse Feishu event")
            return

        if message.sender_type == "app":
            return
        if message.chat_type == "group" and self.settings.feishu_require_mention and not message.mentioned:
            return
        if not self._authorized(message):
            logger.warning("rejected Feishu sender open_id=%s", message.sender_open_id)
            return
        if not self._mark_seen(message.message_id):
            return
        if message.message_type == "text":
            self._executor.submit(self._process_message, message)
        elif message.message_type == "file":
            self._executor.submit(self._process_file_message, message)
        else:
            self._executor.submit(
                self._reply_text,
                message.message_id,
                "目前支持文字和 PDF 文件；其他附件类型暂不处理。",
            )

    def _on_bot_menu(self, data: Any) -> None:
        try:
            import lark_oapi as lark

            payload = json.loads(lark.JSON.marshal(data))
            menu_event = parse_bot_menu_event(payload)
        except Exception:
            logger.exception("failed to parse Feishu bot menu event")
            return

        mode = BOT_MENU_WEB_MODES.get(menu_event.event_key)
        if mode is None and menu_event.event_key != BOT_MENU_WEB_TOGGLE:
            logger.info("ignored unknown Feishu bot menu event_key=%s", menu_event.event_key)
            return
        if not self._authorized_open_id(menu_event.operator_open_id):
            logger.warning(
                "rejected Feishu bot menu operator open_id=%s",
                menu_event.operator_open_id,
            )
            return
        if menu_event.event_key == BOT_MENU_WEB_TOGGLE:
            self._executor.submit(
                self._process_bot_menu_toggle,
                menu_event.operator_open_id,
            )
        else:
            self._executor.submit(
                self._process_bot_menu,
                menu_event.operator_open_id,
                mode,
            )

    def _process_bot_menu_toggle(self, open_id: str) -> None:
        try:
            reply = self.service.toggle_feishu_user_web_mode(open_id)
            self._send_text_to_open_id(open_id, reply.text)
        except Exception:
            logger.exception("failed to toggle Feishu bot menu web mode")
            self._send_text_to_open_id(open_id, "联网模式切换失败，请改用 /web 命令。")

    def _process_bot_menu(self, open_id: str, mode: str) -> None:
        try:
            reply = self.service.set_feishu_user_web_mode(open_id, mode)
            self._send_text_to_open_id(open_id, reply.text)
        except Exception:
            logger.exception("failed to apply Feishu bot menu web mode")
            self._send_text_to_open_id(open_id, "联网模式切换失败，请改用 /web 命令。")

    def _process_message(self, message: InboundMessage) -> None:
        try:
            reply = self.service.handle(
                message.session_key,
                message.text,
                progress=lambda text: self._reply_text(message.message_id, text),
            )
            self._reply_text(message.message_id, reply.text)
            attachment_errors: list[str] = []
            for attachment in reply.attachments[:8]:
                try:
                    if attachment.kind == "image":
                        self._reply_image(message.message_id, attachment.path)
                    elif attachment.kind == "file":
                        self._reply_file(
                            message.message_id,
                            attachment.path,
                            attachment.name,
                        )
                except Exception as exc:
                    logger.exception("attachment delivery failed: %s", attachment.path)
                    attachment_errors.append(f"{attachment.name or Path(attachment.path).name}: {exc}")
                time.sleep(0.25)
            if attachment_errors:
                self._reply_text(
                    message.message_id,
                    "内容已经生成，但部分附件发送失败。请确认机器人已开通并发布 "
                    "im:resource 权限。服务端产物仍然保留。",
                )
        except Exception as exc:
            logger.exception("message processing failed")
            self._reply_text(message.message_id, f"处理失败：{type(exc).__name__}")

    def _process_file_message(self, message: InboundMessage) -> None:
        try:
            path = self._download_inbound_pdf(message)
        except Exception as exc:
            logger.exception("failed to download inbound Feishu file")
            self._reply_text(
                message.message_id,
                f"PDF 接收失败：{exc}。请确认文件不超过 30 MiB，并检查机器人 im:resource 权限。",
            )
            return
        synthetic = InboundMessage(
            message_id=message.message_id,
            chat_id=message.chat_id,
            chat_type=message.chat_type,
            sender_open_id=message.sender_open_id,
            sender_type=message.sender_type,
            message_type="text",
            text=(
                "[用户刚刚上传了一个 PDF 附件]\n"
                f"文件名：{message.file_name or path.name}\n"
                f"系统保存路径：{path}\n"
                "请结合完整对话判断用户用途：若用户已要求总结这篇论文，调用 summarize_paper；"
                "若用户要求把它作为种子论文生成领域综述，调用 generate_paper_survey；"
                "若用途尚不明确，只需自然询问用户想总结单篇论文还是生成领域综述。"
                "不要在对用户的回复中暴露系统保存路径。"
            ),
            mentioned=message.mentioned,
            file_key=message.file_key,
            file_name=message.file_name,
        )
        self._process_message(synthetic)

    def _download_inbound_pdf(self, message: InboundMessage) -> Path:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        raw_name = Path(message.file_name or "paper.pdf").name
        if Path(raw_name).suffix.lower() != ".pdf":
            raise ValueError("只接受 .pdf 文件")
        if not message.file_key:
            raise ValueError("飞书事件没有提供 file_key")
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message.message_id)
            .file_key(message.file_key)
            .type("file")
            .build()
        )
        response = self._api_client.im.v1.message_resource.get(request)
        if not response.success() or response.file is None:
            raise RuntimeError(
                f"飞书资源下载失败 code={response.code} msg={response.msg}"
            )
        data = response.file.read(30 * 1024 * 1024 + 1)
        if len(data) > 30 * 1024 * 1024:
            raise ValueError("PDF 超过 30 MiB")
        safe_name = "".join(
            char if (char.isalnum() or char in "._- ") else "_"
            for char in raw_name
        ).strip(" .") or "paper.pdf"
        path = self._inbound_dir / f"{uuid.uuid4().hex[:12]}-{safe_name}"
        path.write_bytes(data)
        return path

    def _reply_text(self, message_id: str, text: str) -> None:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        chunks = [text[i : i + 3500] for i in range(0, len(text), 3500)] or [""]
        for chunk in chunks:
            body = (
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": chunk}, ensure_ascii=False))
                .build()
            )
            request = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(body)
                .build()
            )
            response = self._api_client.im.v1.message.reply(request)
            if not response.success():
                logger.error(
                    "Feishu reply failed code=%s msg=%s log_id=%s",
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )

    def _send_text_to_open_id(self, open_id: str, text: str) -> None:
        """Send a new P2P message for events that have no message_id."""

        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        chunks = [text[i : i + 3500] for i in range(0, len(text), 3500)] or [""]
        for chunk in chunks:
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": chunk}, ensure_ascii=False))
                .build()
            )
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body(body)
                .build()
            )
            response = self._api_client.im.v1.message.create(request)
            if not response.success():
                raise RuntimeError(
                    f"Feishu send failed code={response.code} msg={response.msg} "
                    f"log_id={response.get_log_id()}"
                )

    def _reply_image(self, message_id: str, path: str) -> None:
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        image_path = Path(path).resolve()
        if (
            not image_path.is_file()
            or image_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        ):
            raise ValueError(f"invalid outbound image: {image_path}")
        if image_path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError(f"outbound image exceeds 10 MiB: {image_path.name}")
        with image_path.open("rb") as image_file:
            body = (
                CreateImageRequestBody.builder()
                .image_type("message")
                .image(image_file)
                .build()
            )
            request = CreateImageRequest.builder().request_body(body).build()
            response = self._api_client.im.v1.image.create(request)
        if not response.success() or response.data is None:
            raise RuntimeError(
                f"Feishu image upload failed code={response.code} msg={response.msg}"
            )
        image_key = str(response.data.image_key or "")
        if not image_key:
            raise RuntimeError("Feishu image upload returned no image_key")
        self._reply_message(message_id, "image", {"image_key": image_key})

    def _reply_file(self, message_id: str, path: str, name: str = "") -> None:
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        file_path = Path(path).resolve()
        if not file_path.is_file():
            raise ValueError(f"invalid outbound file: {file_path}")
        if file_path.stat().st_size > 30 * 1024 * 1024:
            raise ValueError(f"outbound file exceeds 30 MiB: {file_path.name}")
        file_name = (name or file_path.name).strip() or file_path.name
        with file_path.open("rb") as upload_file:
            body = (
                CreateFileRequestBody.builder()
                .file_type("stream")
                .file_name(file_name)
                .file(upload_file)
                .build()
            )
            request = CreateFileRequest.builder().request_body(body).build()
            response = self._api_client.im.v1.file.create(request)
        if not response.success() or response.data is None:
            raise RuntimeError(
                f"Feishu file upload failed code={response.code} msg={response.msg}"
            )
        file_key = str(response.data.file_key or "")
        if not file_key:
            raise RuntimeError("Feishu file upload returned no file_key")
        self._reply_message(message_id, "file", {"file_key": file_key})

    def _reply_message(self, message_id: str, msg_type: str, content: Mapping[str, Any]) -> None:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        body = (
            ReplyMessageRequestBody.builder()
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
            .build()
        )
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        response = self._api_client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(
                f"Feishu reply failed code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}"
            )

    def start(self) -> None:
        if not self.settings.feishu_configured:
            raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")

        import lark_oapi as lark

        builder = (
            lark.Client.builder()
            .app_id(self.settings.feishu_app_id)
            .app_secret(self.settings.feishu_app_secret)
            .log_level(lark.LogLevel.INFO)
        )
        if self.settings.feishu_domain == "lark":
            builder = builder.domain(lark.LARK_DOMAIN)
        self._api_client = builder.build()

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_application_bot_menu_v6(self._on_bot_menu)
            .build()
        )
        ws_client = lark.ws.Client(
            self.settings.feishu_app_id,
            self.settings.feishu_app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            domain=(lark.LARK_DOMAIN if self.settings.feishu_domain == "lark" else lark.FEISHU_DOMAIN),
        )
        logger.info("starting Feishu WebSocket long connection")
        ws_client.start()
