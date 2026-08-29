#!/usr/bin/env python
"""从 CCFDDL 开源数据集（github.com/ccfddl/ccf-deadlines, MIT）同步会议截止日期。

替代硬编码的 scripts/refresh_conference_schedule.py：
  - 通过 GitHub API 拉取仓库文件树，逐个下载 conference/**/*.yml
  - 把每个 deadline 从源时区换算到北京时间（AoE / UTC±N / PT 含夏令时）
  - 合并 scripts/ccfddl_overrides.json（CCFDDL 没有的 review/notification/camera_ready 等中间里程碑）
  - 输出 app/conference-schedule.json（schema v3）

用法：
  python scripts/sync_ccfddl_deadlines.py                      # 联网拉取
  python scripts/sync_ccfddl_deadlines.py --local-dir <dir>    # 离线 / 测试（本地 checkout）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT_DIR / "app" / "conference-schedule.json"
DEFAULT_OVERRIDES = ROOT_DIR / "scripts" / "ccfddl_overrides.json"

CCFDDL_REPO = "ccfddl/ccf-deadlines"
CCFDDL_BRANCH = "main"
CCFDDL_TREE_URL = (
    f"https://api.github.com/repos/{CCFDDL_REPO}/git/trees/{CCFDDL_BRANCH}?recursive=1"
)
CCFDDL_RAW_BASE = (
    f"https://raw.githubusercontent.com/{CCFDDL_REPO}/{CCFDDL_BRANCH}/conference"
)

SCHEMA_VERSION = 3
SUB_ORDER = ["DS", "NW", "SC", "SE", "DB", "CT", "CG", "AI", "HI", "MX"]

SOURCE_CCFDDL = "ccfddl"
SOURCE_DATETEXT = "ccfddl:date_text"
SOURCE_OVERRIDE = "override"

LABELS = {
    "abstract_deadline": "摘要投稿截止",
    "full_paper": "全文投稿截止",
    "review": "审稿阶段",
    "notification": "录取通知",
    "camera_ready": "Camera Ready",
    "conference": "会议召开",
}

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTH_ALT = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))

UTC_RE = re.compile(r"^UTC([+-])(\d{1,2})$")
DEADLINE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$")
DATETEXT_RE = re.compile(rf"(?i)\b({_MONTH_ALT})\s+(\d{{1,2}})")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
OVERRIDE_KEY_RE = re.compile(r"^(.*?)(\d{2}|\d{4})$")

BEIJING_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# 时区换算
# ---------------------------------------------------------------------------

def _second_sunday_of_march(year: int) -> date:
    first = date(year, 3, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(days=7)


def _first_sunday_of_november(year: int) -> date:
    first = date(year, 11, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7)


def resolve_offset(tz_label: str, when: date) -> Tuple[timedelta, bool]:
    """返回 (源时区相对 UTC 的偏移, 是否未知时区)。未知时区按 UTC 处理。"""
    label = (tz_label or "").strip()
    if label == "AoE":
        return timedelta(hours=-12), False
    if label == "UTC":
        return timedelta(0), False
    m = UTC_RE.match(label)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return timedelta(hours=sign * int(m.group(2))), False
    if label == "PT":
        start = _second_sunday_of_march(when.year)
        end = _first_sunday_of_november(when.year)
        if start <= when < end:
            return timedelta(hours=-7), False
        return timedelta(hours=-8), False
    return timedelta(0), True


def parse_deadline(text: str) -> Optional[datetime]:
    m = DEADLINE_RE.match((text or "").strip())
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def deadline_to_beijing(
    deadline_text: str, tz_label: str
) -> Tuple[Optional[datetime], Optional[datetime], bool]:
    """返回 (utc_dt, beijing_dt, unknown_tz)。无法解析时返回 (None, None, False)。"""
    src = parse_deadline(deadline_text)
    if src is None:
        return None, None, False
    offset, unknown = resolve_offset(tz_label, src.date())
    utc = src - offset
    beijing = utc + timedelta(hours=8)
    return utc, beijing, unknown


# ---------------------------------------------------------------------------
# 里程碑构建
# ---------------------------------------------------------------------------

def build_deadline_milestone(
    mtype: str, deadline_text: str, tz_label: str, round_num: Optional[int] = None
) -> Dict[str, Any]:
    utc, beijing, unknown = deadline_to_beijing(deadline_text, tz_label)
    src = parse_deadline(deadline_text)
    if utc is None or src is None:
        return {
            "type": mtype,
            "date": "",
            "ts": None,
            "time_text": str(deadline_text or "").strip() or "TBD",
            "label": LABELS[mtype],
            "is_tbd": True,
            "round": None,
            "source": SOURCE_CCFDDL,
        }
    tz_display = (tz_label or "").strip() or "UTC"
    hint = (
        f"{src:%H:%M:%S} {tz_display} · 北京 {beijing.month}/{beijing.day} "
        f"{beijing.hour:02d}:{beijing.minute:02d}"
    )
    if unknown:
        hint += "（时区未知，按UTC处理）"
        logging.warning("unknown timezone %r for %s, treated as UTC", tz_label, deadline_text)
    return {
        "type": mtype,
        "date": f"{src:%Y-%m-%d}",
        "ts": int(utc.replace(tzinfo=timezone.utc).timestamp() * 1000),
        "time_text": hint,
        "label": LABELS[mtype],
        "is_tbd": False,
        "round": round_num,
        "source": SOURCE_CCFDDL,
    }


def _tbd_milestone(mtype: str) -> Dict[str, Any]:
    return {
        "type": mtype,
        "date": "",
        "ts": None,
        "time_text": "TBD",
        "label": LABELS[mtype],
        "is_tbd": True,
        "round": None,
        "source": SOURCE_CCFDDL,
    }


def _pick_round(
    non_tbd_entries: List[Tuple[int, str]], tz_label: str
) -> Optional[Tuple[int, str]]:
    """从非 TBD 的 deadline 条目中选出最早的一轮（优先未来轮次）。

    返回 (round_num, deadline_text)；round_num 为在非 TBD 条目中的 1-based 序号。
    """
    parsed: List[Tuple[datetime, int, str]] = []
    for round_num, (_, dl) in enumerate(non_tbd_entries, start=1):
        utc, _, _ = deadline_to_beijing(dl, tz_label)
        if utc is not None:
            parsed.append((utc, round_num, dl))
    if not parsed:
        return None
    now = datetime.now(timezone.utc)
    future = [p for p in parsed if p[0].replace(tzinfo=timezone.utc) >= now]
    pool = future or parsed
    chosen = min(pool, key=lambda p: p[0])
    # round 只在多轮（多个非 TBD 条目）时保留；单轮时间线 round 为 null
    round_num = chosen[1] if len(non_tbd_entries) > 1 else None
    return round_num, chosen[2]


def parse_date_text(text: str) -> Optional[date]:
    raw = (text or "").strip()
    if not raw:
        return None
    m = DATETEXT_RE.search(raw)
    if not m:
        return None
    month = MONTH_NAMES[m.group(1).lower()]
    day = int(m.group(2))
    ym = YEAR_RE.search(raw)
    if not ym:
        return None
    try:
        return date(int(ym.group(0)), month, day)
    except ValueError:
        return None


def build_conference_from_datetext(date_text: str) -> Dict[str, Any]:
    parsed = parse_date_text(date_text)
    if parsed is None:
        return {
            "type": "conference",
            "date": "",
            "ts": None,
            "time_text": str(date_text or "").strip(),
            "label": LABELS["conference"],
            "is_tbd": False,
            "round": None,
            "source": SOURCE_DATETEXT,
        }
    midnight_bj = datetime(parsed.year, parsed.month, parsed.day, tzinfo=BEIJING_TZ)
    return {
        "type": "conference",
        "date": parsed.isoformat(),
        "ts": int(midnight_bj.timestamp() * 1000),
        "time_text": str(date_text or "").strip(),
        "label": LABELS["conference"],
        "is_tbd": False,
        "round": None,
        "source": SOURCE_DATETEXT,
    }


def build_override_milestone(entry: Dict[str, Any]) -> Dict[str, Any]:
    mtype = str(entry.get("type") or "").strip()
    date_str = str(entry.get("date") or "").strip()
    label = str(entry.get("label") or "").strip() or LABELS.get(mtype, mtype)
    parsed = None
    try:
        parsed = date.fromisoformat(date_str) if date_str else None
    except ValueError:
        parsed = None
    ts = None
    if parsed is not None:
        midnight_bj = datetime(parsed.year, parsed.month, parsed.day, tzinfo=BEIJING_TZ)
        ts = int(midnight_bj.timestamp() * 1000)
    return {
        "type": mtype,
        "date": date_str,
        "ts": ts,
        "time_text": label,
        "label": label,
        "is_tbd": False,
        "round": None,
        "source": SOURCE_OVERRIDE,
    }


# ---------------------------------------------------------------------------
# 会议 / 年份 / 里程碑组装
# ---------------------------------------------------------------------------

def build_year_entry(
    conf_year: Dict[str, Any], tz_label: str, overrides: List[Dict[str, Any]]
) -> Dict[str, Any]:
    timeline = conf_year.get("timeline") or []
    milestones: List[Dict[str, Any]] = []

    dl_entries = [
        (i, e.get("deadline")) for i, e in enumerate(timeline) if "deadline" in e
    ]
    non_tbd = [
        (i, dl) for i, dl in dl_entries if str(dl).strip().upper() != "TBD"
    ]
    if non_tbd:
        chosen = _pick_round(non_tbd, tz_label)
        if chosen:
            milestones.append(
                build_deadline_milestone("full_paper", chosen[1], tz_label, chosen[0])
            )
        else:
            milestones.append(_tbd_milestone("full_paper"))
    elif dl_entries:
        milestones.append(_tbd_milestone("full_paper"))

    ab_entries = [
        (i, e.get("abstract_deadline"))
        for i, e in enumerate(timeline)
        if "abstract_deadline" in e
    ]
    ab_non_tbd = [
        (i, ab) for i, ab in ab_entries if str(ab).strip().upper() != "TBD"
    ]
    if ab_non_tbd:
        chosen = _pick_round(ab_non_tbd, tz_label)
        if chosen:
            milestones.append(
                build_deadline_milestone("abstract_deadline", chosen[1], tz_label, chosen[0])
            )
        else:
            milestones.append(_tbd_milestone("abstract_deadline"))
    elif ab_entries:
        milestones.append(_tbd_milestone("abstract_deadline"))

    for ov in overrides:
        mtype = str(ov.get("type") or "").strip()
        if mtype in ("review", "notification", "camera_ready", "conference"):
            milestones.append(build_override_milestone(ov))

    if not any(m["type"] == "conference" for m in milestones):
        milestones.append(
            build_conference_from_datetext(str(conf_year.get("date") or ""))
        )

    milestones.sort(key=lambda m: (m["ts"] is None, m["ts"] if m["ts"] is not None else 0))

    is_tbd = bool(dl_entries) and all(
        str(dl).strip().upper() == "TBD" for _, dl in dl_entries
    )
    if not dl_entries:
        is_tbd = True

    return {
        "year": int(conf_year.get("year")),
        "id": str(conf_year.get("id") or ""),
        "link": str(conf_year.get("link") or ""),
        "place": str(conf_year.get("place") or ""),
        "date_text": str(conf_year.get("date") or ""),
        "timezone": tz_label,
        "is_tbd": is_tbd,
        "milestones": milestones,
    }


def _parse_override_key(key: str) -> Tuple[Optional[str], Optional[int]]:
    m = OVERRIDE_KEY_RE.match((key or "").strip())
    if not m:
        return None, None
    title_part = m.group(1).strip().lower()
    year_str = m.group(2)
    if len(year_str) == 2:
        yy = int(year_str)
        year = 1900 + yy if yy >= 70 else 2000 + yy
    else:
        year = int(year_str)
    return title_part or None, year


def _match_overrides(
    conf_year: Dict[str, Any],
    title: str,
    year: int,
    overrides_by_id: Dict[str, List[Dict[str, Any]]],
    overrides_by_title_year: Dict[Tuple[str, int], List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    cid = str(conf_year.get("id") or "")
    if cid in overrides_by_id:
        return overrides_by_id[cid]
    return overrides_by_title_year.get((title.lower(), year), [])


def build_conference(
    conf: Dict[str, Any],
    overrides_by_id: Dict[str, List[Dict[str, Any]]],
    overrides_by_title_year: Dict[Tuple[str, int], List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    title = str(conf.get("title") or "").strip()
    if not title:
        return None
    key = title.lower()
    rank_raw = conf.get("rank") or {}
    rank = {
        "ccf": str(rank_raw.get("ccf") or "N"),
        "core": rank_raw.get("core") or None,
        "thcpl": rank_raw.get("thcpl") or None,
    }
    years = []
    for cy in conf.get("confs") or []:
        try:
            year = int(cy.get("year"))
        except (TypeError, ValueError):
            continue
        tz_label = str(cy.get("timezone") or "").strip() or "UTC"
        overrides = _match_overrides(cy, title, year, overrides_by_id, overrides_by_title_year)
        years.append(build_year_entry(cy, tz_label, overrides))
    years.sort(key=lambda y: -y["year"])
    return {
        "key": key,
        "label": title,
        "description": str(conf.get("description") or "").strip(),
        "sub": str(conf.get("sub") or "").strip(),
        "rank": rank,
        "dblp": str(conf.get("dblp") or "").strip() or None,
        "years": years,
    }


def build_schedule(
    conferences_data: List[Dict[str, Any]],
    types_data: List[Dict[str, Any]],
    overrides: Dict[str, List[Dict[str, Any]]],
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    types_by_sub = {str(t.get("sub")): t for t in (types_data or [])}
    fields = []
    for sub in SUB_ORDER:
        t = types_by_sub.get(sub)
        fields.append({
            "sub": sub,
            "name": str(t.get("name") or "") if t else "",
            "name_en": str(t.get("name_en") or "") if t else "",
        })

    overrides_by_id = {str(k): list(v or []) for k, v in (overrides or {}).items()}
    overrides_by_title_year: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    for k, v in (overrides or {}).items():
        tp, yr = _parse_override_key(str(k))
        if tp is not None and yr is not None:
            overrides_by_title_year.setdefault((tp, yr), []).extend(v or [])

    conferences = []
    for conf in conferences_data or []:
        if not isinstance(conf, dict):
            continue
        built = build_conference(conf, overrides_by_id, overrides_by_title_year)
        if built and built["years"]:
            conferences.append(built)
    conferences.sort(key=lambda c: c["label"].lower())

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "ccfddl/ccf-deadlines",
        "license": "MIT",
        "fields": fields,
        "conferences": conferences,
    }


# ---------------------------------------------------------------------------
# 数据获取（GitHub API / 本地目录）
# ---------------------------------------------------------------------------

def _get_with_retry(session: requests.Session, url: str, headers: Dict[str, str],
                    attempts: int = 4, base_delay: float = 1.0) -> requests.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            resp = session.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (403, 429):
                last_exc = RuntimeError(f"HTTP {resp.status_code} for {url}")
            else:
                resp.raise_for_status()
        except requests.RequestException as exc:
            last_exc = exc
        delay = base_delay * (2 ** attempt)
        logging.warning("fetch %s failed (%s); retrying in %.1fs", url, last_exc, delay)
        time.sleep(delay)
    raise RuntimeError(f"failed to fetch {url}: {last_exc}")


def _auth_headers(token: str) -> Dict[str, str]:
    headers = {"User-Agent": "dpr-ccfddl-sync"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_tree(session: requests.Session, token: str = "") -> List[str]:
    resp = _get_with_retry(session, CCFDDL_TREE_URL, _auth_headers(token))
    data = resp.json()
    if data.get("truncated"):
        logging.warning("CCFDDL tree response truncated")
    paths = [e.get("path") for e in data.get("tree", []) if e.get("type") == "blob"]
    return [
        p for p in paths
        if p.startswith("conference/") and p.endswith(".yml") and not p.endswith("types.yml")
    ]


def fetch_yaml(session: requests.Session, rel_path: str, token: str = "") -> Any:
    url = f"{CCFDDL_RAW_BASE}/{rel_path[len('conference/'):]}"
    resp = _get_with_retry(session, url, _auth_headers(token))
    return yaml.safe_load(resp.text)


def load_local_dir(local_dir: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    base = Path(local_dir)
    conf_base = base / "conference" if (base / "conference").is_dir() else base
    types_data: List[Dict[str, Any]] = []
    types_path = conf_base / "types.yml"
    if types_path.exists():
        try:
            types_data = yaml.safe_load(types_path.read_text(encoding="utf-8")) or []
        except Exception as exc:
            logging.warning("skip bad types.yml: %s", exc)
    conferences: List[Dict[str, Any]] = []
    for p in sorted(conf_base.rglob("*.yml")):
        if p.name == "types.yml":
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                conferences.extend(data)
        except Exception as exc:
            logging.warning("skip bad YAML %s: %s", p, exc)
    return conferences, types_data


def write_schedule(path: Path, snapshot: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="从 CCFDDL 数据集同步会议日程到 app/conference-schedule.json"
    )
    parser.add_argument("--local-dir", default="", help="本地 CCFDDL checkout 目录（离线/测试用）")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出 JSON 路径")
    parser.add_argument("--overrides", default=str(DEFAULT_OVERRIDES), help="override 文件路径")
    parser.add_argument("--generated-at", default="", help="generated_at 覆盖（测试用）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="[ccfddl] %(levelname)s %(message)s")

    token = os.environ.get("GITHUB_TOKEN") or ""
    if args.local_dir:
        conferences, types_data = load_local_dir(args.local_dir)
    else:
        session = requests.Session()
        try:
            paths = fetch_tree(session, token)
        except Exception as exc:
            logging.error("无法获取 CCFDDL 文件树: %s", exc)
            return 1
        try:
            types_data = fetch_yaml(session, "conference/types.yml", token) or []
        except Exception as exc:
            logging.warning("无法获取 types.yml: %s", exc)
            types_data = []
        conferences = []
        for rel in paths:
            try:
                data = fetch_yaml(session, rel, token)
                if isinstance(data, list):
                    conferences.extend(data)
            except Exception as exc:
                logging.warning("跳过 %s: %s", rel, exc)

    overrides: Dict[str, Any] = {}
    ov_path = Path(args.overrides)
    if ov_path.exists():
        try:
            overrides = json.loads(ov_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            logging.warning("override 文件解析失败: %s", exc)
    else:
        logging.warning("override 文件不存在: %s", ov_path)

    snapshot = build_schedule(
        conferences, types_data, overrides, generated_at=args.generated_at or None
    )
    write_schedule(Path(args.output), snapshot)
    n_years = sum(len(c["years"]) for c in snapshot["conferences"])
    print(
        f"[ccfddl] wrote {args.output}: {len(snapshot['conferences'])} conferences, "
        f"{n_years} years",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
