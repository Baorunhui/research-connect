import asyncio
import re
from datetime import datetime
from typing import List, Optional, Set
from collections import deque
from fastapi import WebSocket


_LEAK = re.compile(
    r"https?://|Traceback|\.py\b|/(?:home|app|data|tmp|var|Users)/|[A-Za-z]:\\|\b[A-Za-z]+Error\b|workers|h-index|api[_ ]?key",
    re.I,
)
_CHATTER = re.compile(
    r"文件前缀|保存位置|调试文件|剩余额度|并行查询|并行搜索|查询异常|^\s*[\[│→⚠💾📄]"
)
_PHASES = (
    (re.compile(r"未找到任何施引"), "没有查到施引论文。"),
    (re.compile(r"Phase\s*1.*完成"), "引用列表查完了"),
    (re.compile(r"Phase\s*1"), "正在查这些论文被谁引用"),
    (re.compile(r"Phase\s*2.*完成"), "作者单位对过了"),
    (re.compile(r"Phase\s*2"), "正在核对作者单位"),
    (re.compile(r"Step\s*5"), "正在和学者名单对照"),
    (re.compile(r"Phase\s*3.*完成|导出\s*完成"), "结果整理好了"),
    (re.compile(r"Phase\s*3"), "正在整理结果"),
    (re.compile(r"Phase\s*4"), "正在读引用原文"),
    (re.compile(r"Phase\s*5"), "正在写报告"),
)


def page_log_message(message: str) -> Optional[str]:
    """页面上只留读得懂的一句。内部步骤、路径和报错原文仍打印在服务器上。"""
    text = " ".join(str(message).split())
    if not text:
        return None
    if "未提供学者主页" in text:
        return "请先填写学者主页链接，或上传保存的主页。"
    for pattern, replacement in _PHASES:
        if pattern.search(text):
            return replacement
    if _LEAK.search(text) or _CHATTER.search(text):
        return None
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    if chinese < 4:
        return None
    return text


class LogManager:
    def __init__(self, max_logs: int = 1000):
        """
        日志管理器,负责日志记录和WebSocket广播

        Args:
            max_logs: 最大保留日志条数
        """
        self.logs = deque(maxlen=max_logs)
        self.websocket_connections: Set[WebSocket] = set()
        self.current_progress = {"current": 0, "total": 100, "percentage": 0}
        self.suppress_task_logs = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_page_message: Optional[str] = None

    def set_task_log_suppressed(self, suppressed: bool):
        """Suppress normal log printing/broadcasting after user-visible cancellation."""
        self.suppress_task_logs = suppressed

    def add_websocket(self, websocket: WebSocket):
        """添加WebSocket连接"""
        self.websocket_connections.add(websocket)

    def remove_websocket(self, websocket: WebSocket):
        """移除WebSocket连接"""
        self.websocket_connections.discard(websocket)

    async def _broadcast(self, message: dict):
        """
        广播消息到所有连接的WebSocket

        Args:
            message: 要广播的消息
        """
        disconnected = set()
        for ws in self.websocket_connections:
            try:
                await ws.send_json(message)
            except Exception as e:
                print(f"WebSocket发送失败: {e}")
                disconnected.add(ws)

        # 清理断开的连接
        self.websocket_connections -= disconnected

    def _schedule_broadcast(self, message: dict):
        """
        Schedule an async broadcast.  Works from both the main event-loop
        thread (via asyncio.create_task) and worker threads (via
        run_coroutine_threadsafe on the cached main loop).
        """
        try:
            loop = asyncio.get_running_loop()
            self._loop = loop  # cache for cross-thread scheduling
            asyncio.create_task(self._broadcast(message))
        except RuntimeError:
            # No running event loop in *this* thread — we're inside a
            # worker thread (e.g. asyncio.to_thread).  Schedule the
            # broadcast on the main event loop so WebSocket clients still
            # receive it.
            if self._loop is not None and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._broadcast(message), self._loop
                )
            # else: no loop available yet (startup), skip broadcast.
            # The log entry is already in self.logs so it won't be lost.

    def _log(self, level: str, message: str):
        """
        记录日志

        Args:
            level: 日志级别(INFO, SUCCESS, WARNING, ERROR)
            message: 日志消息
        """
        message = str(message)
        if self.suppress_task_logs:
            return
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {message.lstrip()}", flush=True)
        shown = page_log_message(message)
        if shown is None or shown == self._last_page_message:
            return
        self._last_page_message = shown
        log_entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "message": shown
        }
        self.logs.append(log_entry)
        self._schedule_broadcast({
            "type": "log",
            "data": log_entry
        })

    def info(self, message: str):
        """记录INFO级别日志"""
        self._log("INFO", message)

    def success(self, message: str):
        """记录SUCCESS级别日志"""
        self._log("SUCCESS", message)

    def warning(self, message: str):
        """记录WARNING级别日志"""
        self._log("WARNING", message)

    def error(self, message: str):
        """记录ERROR级别日志"""
        self._log("ERROR", message)

    def broadcast_event(self, event_type: str, payload: dict):
        """向所有 WebSocket 连接广播自定义事件（非日志型消息）"""
        self._schedule_broadcast({
            "type": event_type,
            "data": payload
        })

    def update_progress(self, current: int, total: int):
        """
        更新进度

        Args:
            current: 当前进度
            total: 总进度
        """
        percentage = int((current / total) * 100) if total > 0 else 0
        self.current_progress = {
            "current": current,
            "total": total,
            "percentage": percentage
        }

        # 异步广播进度
        self._schedule_broadcast({
            "type": "progress",
            "data": self.current_progress
        })

    def get_recent_logs(self, count: int = 100) -> List[dict]:
        """
        获取最近的日志

        Args:
            count: 返回的日志条数

        Returns:
            日志列表
        """
        return list(self.logs)[-count:]

    def clear_logs(self):
        """清空日志"""
        self.logs.clear()
        self._last_page_message = None
