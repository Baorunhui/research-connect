"""本地定时调度器：在指定本地时刻触发一次回调，当日只触发一次。"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional


class LocalScheduler:
    def __init__(self, time_str: str, trigger_fn: Callable[[], None]) -> None:
        hour, minute = self._parse_time(time_str)
        self._hour = hour
        self._minute = minute
        self._trigger_fn = trigger_fn

    @staticmethod
    def _parse_time(time_str: str) -> tuple[int, int]:
        parts = str(time_str or "").strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"无效调度时间: {time_str!r}")
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"调度时间超出范围: {time_str!r}")
        return hour, minute

    def _target(self) -> timedelta:
        return timedelta(hours=self._hour, minutes=self._minute)

    def next_run_after(self, now: datetime) -> datetime:
        today_target = now.replace(hour=self._hour, minute=self._minute, second=0, microsecond=0)
        if now < today_target:
            return today_target
        return today_target + timedelta(days=1)

    def should_fire(self, now: datetime, already_fired_today: Optional[bool]) -> str:
        """返回 'fire' | 'wait' | 'skip_today'。"""
        if now.hour == self._hour and now.minute == self._minute:
            if already_fired_today:
                return "skip_today"
            return "fire"
        return "wait"

    def trigger(self) -> None:
        self._trigger_fn()


class SchedulerThread(threading.Thread):
    def __init__(self, time_str: str, on_fire: Callable[[], None]) -> None:
        super().__init__(daemon=True)
        self._scheduler = LocalScheduler(time_str, trigger_fn=on_fire)
        self._stop = threading.Event()
        self._fired_today: Optional[datetime] = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            now = datetime.now()
            decision = self._scheduler.should_fire(now, self._fired_today is not None and self._fired_today.date() == now.date())
            if decision == "fire":
                self._fired_today = now
                try:
                    self._scheduler.trigger()
                except Exception:
                    # 本调度器失败不影响服务主循环；由触发方负责记日志
                    pass
            self._stop.wait(20)