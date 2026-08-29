# -*- coding: utf-8 -*-
"""Step 1L「今日已抓取跳过」判定：arXiv 每天一批公布，同一 UTC 日内重跑
（用户换方向重出日报的典型场景）不应重复请求 arXiv API。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
  sys.path.insert(0, str(SRC_DIR))

from fetch_arxiv_window import should_skip_fetch
from local_paper_store import LocalPaperStore

COVERED_START = datetime(2026, 8, 20, 0, 0, 0, tzinfo=timezone.utc)
COVERED_END = datetime(2026, 8, 29, 2, 0, 0, tzinfo=timezone.utc)
FETCHED_AT = "2026-08-29T02:05:00+00:00"
NOW_SAME_DAY = datetime(2026, 8, 29, 8, 0, 0, tzinfo=timezone.utc)


def _make_store(tmp_path, *, fetched_at=FETCHED_AT, window=None):
  store = LocalPaperStore(tmp_path / "index.sqlite3")
  store.set_meta("last_fetch_at", fetched_at)
  store.set_meta("last_fetch_window", window or f"{COVERED_START.isoformat()}~{COVERED_END.isoformat()}")
  return store


def test_same_day_covered_window_skips(tmp_path):
  store = _make_store(tmp_path)
  skip, reason = should_skip_fetch(store, COVERED_START, COVERED_END, now=NOW_SAME_DAY)
  assert skip, reason
  assert "覆盖抓取" in reason


def test_window_end_within_24h_tolerance_skips(tmp_path):
  """当日晚些时候重跑：请求末端比已覆盖末端晚几个小时（≤24h 容差）仍跳过。"""
  store = _make_store(tmp_path)
  skip, _ = should_skip_fetch(
    store, COVERED_START, COVERED_END + timedelta(hours=6), now=NOW_SAME_DAY
  )
  assert skip


def test_next_day_fetches_again(tmp_path):
  """arXiv 隔天有新公布批次，第二天必须重抓。"""
  now_next_day = NOW_SAME_DAY + timedelta(days=1)
  store = _make_store(tmp_path)
  skip, reason = should_skip_fetch(store, COVERED_START, COVERED_END, now=now_next_day)
  assert not skip
  assert "隔天" in reason


def test_widened_backward_window_fetches(tmp_path):
  """向前扩窗（起始早于已覆盖范围）必须重抓。"""
  store = _make_store(tmp_path)
  skip, reason = should_skip_fetch(
    store, COVERED_START - timedelta(days=3), COVERED_END, now=NOW_SAME_DAY
  )
  assert not skip
  assert "扩窗" in reason


def test_end_beyond_24h_tolerance_fetches(tmp_path):
  """窗口末端超出已覆盖范围 24h 以上必须重抓。"""
  store = _make_store(tmp_path)
  skip, reason = should_skip_fetch(
    store, COVERED_START, COVERED_END + timedelta(hours=30), now=NOW_SAME_DAY
  )
  assert not skip
  assert "末端" in reason


def test_force_overrides_freshness(tmp_path):
  store = _make_store(tmp_path)
  skip, reason = should_skip_fetch(
    store, COVERED_START, COVERED_END, now=NOW_SAME_DAY, force=True
  )
  assert not skip
  assert "强制" in reason


def test_empty_store_fetches(tmp_path):
  store = LocalPaperStore(tmp_path / "index.sqlite3")
  skip, reason = should_skip_fetch(store, COVERED_START, COVERED_END, now=NOW_SAME_DAY)
  assert not skip
  assert "还没有抓取记录" in reason
