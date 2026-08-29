# -*- coding: utf-8 -*-
"""Step 1L（本地召回模式）：按 submittedDate 时间窗从 arXiv API 增量抓取论文元数据，
幂等 upsert 进本地 SQLite 论文库（local_paper_store.LocalPaperStore）。

与被跳过的 Step 1（fetch_arxiv.py，面向 Supabase 同步 + seen 状态机）不同，
本脚本面向本地召回：每次运行重复抓同一窗口也安全（按 id upsert 去重），
不需要 crawl_state/seen.json。

窗口解析与 2.1 的 resolve_supabase_recall_window 完全一致：
- DPR_RUN_DATE 为 YYYYMMDD-YYYYMMDD 区间 token → 精确窗口
- 否则按 arxiv_paper_setting.days_window（默认 9）回溯到当前 UTC

用法（通常由 main.py 编排，不手动跑）：
  python src/fetch_arxiv_window.py [--db PATH] [--max-results N]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]

from local_paper_store import LocalPaperStore  # noqa: E402

CONFIG_PATH = ROOT_DIR / "config.yaml"
DATE_RE_RANGE = re.compile(r"^\d{8}-\d{8}$")


def log(message: str) -> None:
  ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
  print(f"[{ts}] {message}", flush=True)


def load_config() -> Dict[str, Any]:
  if CONFIG_PATH.exists():
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}
  return {}


def resolve_window(config: Dict[str, Any]) -> tuple[datetime, datetime]:
  """与 2.1.resolve_supabase_recall_window 相同的窗口语义，返回 [start, end)。"""
  paper_setting = (config or {}).get("arxiv_paper_setting") or {}
  try:
    days = int(paper_setting.get("days_window") or 9)
  except Exception:
    days = 9
  days = max(days, 1)
  anchor = datetime.now(timezone.utc)
  token = str(os.getenv("DPR_RUN_DATE") or "").strip()
  if DATE_RE_RANGE.fullmatch(token):
    start_text, end_text = token.split("-", 1)
    try:
      start_dt = datetime.strptime(start_text, "%Y%m%d").replace(tzinfo=timezone.utc)
      end_day = datetime.strptime(end_text, "%Y%m%d").replace(tzinfo=timezone.utc)
      if end_day >= start_dt:
        return start_dt, end_day + timedelta(days=1)
    except Exception:
      pass
  return anchor - timedelta(days=days), anchor


def normalize_result(result: Any) -> Dict[str, Any]:
  """arxiv.Result → 本地库 paper 行。id 剥版本号（与全链路去重口径一致）。"""
  raw_id = str(getattr(result, "get_short_id", lambda: "")() or result.entry_id or "")
  pid = raw_id.split("v")[0] if raw_id and raw_id.count("v") and raw_id.split("v")[-1].isdigit() else raw_id
  if not pid:
    # entry_id 形如 http://arxiv.org/abs/2601.01234v1
    tail = str(result.entry_id or "").rstrip("/").split("/")[-1]
    parts = tail.split("v")
    pid = parts[0] if len(parts) > 1 and parts[-1].isdigit() else tail
  authors = [str(a) for a in (getattr(result, "authors", None) or [])]
  categories = [str(c) for c in (getattr(result, "categories", None) or [])]
  published = getattr(result, "published", None)
  updated = getattr(result, "updated", None)
  return {
    "id": pid,
    "source": "arxiv",
    "title": " ".join(str(getattr(result, "title", "") or "").split()),
    "abstract": " ".join(str(getattr(result, "summary", "") or "").split()),
    "authors": authors,
    "primary_category": str(getattr(result, "primary_category", "") or "") or (categories[0] if categories else ""),
    "categories": categories,
    "published": published.astimezone(timezone.utc).isoformat() if published else "",
    "updated": updated.astimezone(timezone.utc).isoformat() if updated else "",
    "link": str(getattr(result, "entry_id", "") or ""),
    "pdf_url": str(getattr(result, "pdf_url", "") or ""),
  }


def _system_proxy_url() -> str:
  """env 未显式配置代理时，读 Windows 系统代理（注册表）；已配置则返回空串。"""
  for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    if os.getenv(key):
      return ""
  try:
    import urllib.request

    proxies = urllib.request.getproxies()
  except Exception:  # noqa: BLE001
    return ""
  return proxies.get("https") or proxies.get("http") or ""


def fetch_window_into_store(
  store: LocalPaperStore,
  start_dt: datetime,
  end_dt: datetime,
  *,
  max_results: int = 0,
) -> int:
  """多路线尝试抓取（当前 env → 系统代理），任一路线成功即返回。

  arXiv API（export.arxiv.org）在本机网络下经常直连 SSL 重置；Clash 类系统代理
  又不会自动进入 Python 进程 env，所以按路线依次尝试。全部失败抛最后一个异常，
  由 main() 决定是否降级用本地库已有数据。
  """
  import arxiv  # 延迟导入：仅本地召回模式需要

  start_text = start_dt.strftime("%Y%m%d%H%M")
  end_text = end_dt.strftime("%Y%m%d%H%M")
  query = f"submittedDate:[{start_text} TO {end_text}]"
  search = arxiv.Search(
    query=query,
    max_results=max_results if max_results and max_results > 0 else None,
    sort_by=arxiv.SortCriterion.SubmittedDate,
    sort_order=arxiv.SortOrder.Descending,
  )

  attempts: List[Tuple[str, str | None]] = [("当前网络配置", None)]
  proxy_url = _system_proxy_url()
  if proxy_url:
    attempts.append((f"系统代理 {proxy_url}", proxy_url))

  # 预检快失败：每条路线先用 8 秒小请求探活，全死就不进完整抓取循环——
  # 否则 arXiv 不可达时每条路线要磨几分钟的 SSL 超时重试，白等十几分钟。
  alive: List[Tuple[str, str | None]] = []
  import requests as _requests

  for label, proxy in attempts:
    proxies = {"https": proxy, "http": proxy} if proxy else None
    try:
      _requests.get(
        "https://export.arxiv.org/api/query?search_query=all:electron&max_results=1",
        timeout=8,
        proxies=proxies,
      )
      alive.append((label, proxy))
    except Exception as exc:  # noqa: BLE001
      log(f"[WARN] 路线「{label}」预检失败（{type(exc).__name__}），跳过该路线")
  if not alive:
    raise RuntimeError("arXiv API 所有网络路线预检失败（直连/系统代理均不可达）")
  attempts = alive

  last_exc: Exception | None = None
  for label, proxy in attempts:
    if proxy:
      os.environ["HTTPS_PROXY"] = proxy
      os.environ["HTTP_PROXY"] = proxy
    else:
      for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        os.environ.pop(key, None)
    log(f"arXiv API 窗口抓取（路线：{label}）：submittedDate {start_text} ~ {end_text}")
    client = arxiv.Client(page_size=100, delay_seconds=3.0, num_retries=2)
    try:
      rows = [normalize_result(r) for r in client.results(search)]
    except Exception as exc:  # noqa: BLE001
      log(f"[WARN] 路线「{label}」失败：{type(exc).__name__}: {str(exc)[:120]}")
      last_exc = exc
      continue
    total = 0
    for i in range(0, len(rows), 200):
      total += store.upsert_papers(rows[i : i + 200])
      log(f"已入库 {total}/{len(rows)} 篇（分批 upsert）")
    store.set_meta("last_fetch_window", f"{start_dt.isoformat()}~{end_dt.isoformat()}")
    store.set_meta("last_fetch_at", datetime.now(timezone.utc).isoformat())
    return total
  assert last_exc is not None
  raise last_exc


def should_skip_fetch(
  store: LocalPaperStore,
  start_dt: datetime,
  end_dt: datetime,
  *,
  now: datetime | None = None,
  force: bool = False,
) -> tuple[bool, str]:
  """同一天已成功抓取且请求窗口被覆盖 → 跳过本次抓取。

  arXiv 每天一批公布（约 20:00 ET ≈ 次日 00:00-01:00 UTC），同一 UTC 日内
  重复抓取拿不到新论文——日报一天多次换方向重跑时，只有第一次需要抓。
  请求窗口末端允许比已覆盖窗口晚最多 24 小时（发布节奏对齐），起始端必须
  被完全覆盖（向前扩窗必须重抓）。--force / DPR_LOCAL_FETCH_FORCE=1 强制抓取。
  """
  if force:
    return False, "强制抓取（--force）"
  now = now or datetime.now(timezone.utc)
  last_at = store.get_meta("last_fetch_at")
  window_text = store.get_meta("last_fetch_window")
  if not last_at or not window_text:
    return False, "本地库还没有抓取记录"
  try:
    last_dt = datetime.fromisoformat(last_at).astimezone(timezone.utc)
    covered_text_start, covered_text_end = window_text.split("~", 1)
    covered_start = datetime.fromisoformat(covered_text_start).astimezone(timezone.utc)
    covered_end = datetime.fromisoformat(covered_text_end).astimezone(timezone.utc)
  except (ValueError, TypeError):
    return False, "上次抓取记录无法解析，重新抓取"
  if last_dt.date() != now.astimezone(timezone.utc).date():
    return False, f"上次抓取在 {last_dt.date()}（隔天需重抓当日新公布批次）"
  if start_dt < covered_start:
    return False, "请求窗口起始早于已覆盖范围（向前扩窗）"
  if end_dt > covered_end + timedelta(hours=24):
    return False, "请求窗口末端超出已覆盖范围一天以上"
  return True, f"今天 {last_dt.strftime('%H:%M')}UTC 已完成覆盖抓取（{window_text}），同日重跑无新数据"


def main() -> None:
  parser = argparse.ArgumentParser(description="Step 1L：arXiv 窗口增量抓取进本地论文库")
  parser.add_argument("--db", type=str, default="", help="本地库路径；默认 data/local_recall/index.sqlite3")
  parser.add_argument("--window-start", type=str, default="", help="ISO 起始时间；缺省按 config/DPR_RUN_DATE 解析")
  parser.add_argument("--window-end", type=str, default="", help="ISO 结束时间（不含）")
  parser.add_argument("--max-results", type=int, default=0, help="抓取上限，0 = 不限（调试用）")
  parser.add_argument("--force", action="store_true", help="跳过「今日已抓取」检查，强制重新抓取")
  args = parser.parse_args()

  config = load_config()
  if args.window_start and args.window_end:
    start_dt = datetime.fromisoformat(args.window_start).replace(tzinfo=timezone.utc) if "T" in args.window_start else datetime.strptime(args.window_start, "%Y%m%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(args.window_end).replace(tzinfo=timezone.utc) if "T" in args.window_end else datetime.strptime(args.window_end, "%Y%m%d").replace(tzinfo=timezone.utc)
  else:
    start_dt, end_dt = resolve_window(config)

  force = bool(args.force) or str(os.getenv("DPR_LOCAL_FETCH_FORCE") or "").strip().lower() in {"1", "true", "yes", "on"}
  with LocalPaperStore(args.db or None) as store:
    skip, reason = should_skip_fetch(store, start_dt, end_dt, force=force)
    if skip:
      log(f"[INFO] 跳过抓取：{reason}")
      total = 0
    else:
      if not force:
        log(f"[INFO] 需要抓取：{reason}")
      try:
        total = fetch_window_into_store(store, start_dt, end_dt, max_results=int(args.max_results or 0))
      except Exception as exc:  # noqa: BLE001
        # 尽力而为：所有网络路线都失败时，若本地库已有该窗口数据，降级继续
        # （日报流水线不因 arXiv 临时不可达而中断）；本地库也是空的才判失败。
        date_start = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        date_end = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
        existing = store.count_window(date_start, date_end)
        if existing > 0:
          log(
            f"[WARN] arXiv 抓取失败（{type(exc).__name__}: {str(exc)[:120]}），"
            f"但本地库已有窗口数据 {existing} 篇——降级用现有数据继续。"
          )
          total = 0
        else:
          log("[ERROR] arXiv 抓取失败且本地库为空，本地召回无法继续。")
          raise
    window_count = store.count_window(start_dt.strftime("%Y-%m-%dT%H:%M:%S"), end_dt.strftime("%Y-%m-%dT%H:%M:%S"))
  log(f"[INFO] Step 1L 完成：本次入库/更新 {total} 篇，窗口内现有 {window_count} 篇")


if __name__ == "__main__":
  main()
