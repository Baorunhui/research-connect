from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from connect_hub.tools.base import ToolContext, ToolDefinition


def system_status_tool(registered_tools: Callable[[], tuple[str, ...]]) -> ToolDefinition:
    def handler(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        return {
            "service": "research-connect-hub",
            "status": "ok",
            "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session": context.session_key,
            "registered_tools": list(registered_tools()),
        }

    return ToolDefinition(
        name="system_status",
        description=(
            "检查 Research Connect Hub 服务状态和当前已注册工具。"
            "仅当用户询问系统、机器人、连接或工具是否正常时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=handler,
        timeout_seconds=5,
        track_job=False,
        kind="system",
    )
