from datetime import datetime
from src.local_scheduler import LocalScheduler


def test_next_run_after_same_day_future():
    s = LocalScheduler("18:30", trigger_fn=lambda: None)
    now = datetime(2026, 8, 6, 10, 0)
    nxt = s.next_run_after(now)
    assert nxt == datetime(2026, 8, 6, 18, 30)


def test_next_run_after_past_rolls_to_next_day():
    s = LocalScheduler("18:30", trigger_fn=lambda: None)
    now = datetime(2026, 8, 6, 20, 0)
    nxt = s.next_run_after(now)
    assert nxt == datetime(2026, 8, 7, 18, 30)


def test_should_fire_returns_fire_when_matching_and_not_fired():
    s = LocalScheduler("18:30", trigger_fn=lambda: None)
    now = datetime(2026, 8, 6, 18, 30)
    assert s.should_fire(now, already_fired_today=None) == "fire"


def test_should_fire_skip_when_already_fired_today():
    s = LocalScheduler("18:30", trigger_fn=lambda: None)
    now = datetime(2026, 8, 6, 18, 30)
    assert s.should_fire(now, already_fired_today=True) == "skip_today"