#!/usr/bin/env python
"""重新生成会议日程快照 app/conference-schedule.json。

默认按日历推算（无实时数据库），保证给定相同输入（--ref-date）时输出字节完全一致，
以便 CI 的 commit 步骤在无变更时为 no-op。日期随时间更新，可维护。

用法示例：
  python scripts/refresh_conference_schedule.py --ref-date 2026-08-17
  python scripts/refresh_conference_schedule.py --ref-date $(date -u +%F)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT_DIR / "app" / "conference-schedule.json"

SCHEMA_VERSION = 2

# 每个会议的可重复时间线模板（相对年份节点的月份/日近似值，仅作生成缺省结构使用）。
# 关键：模板不含绝对年份，避免跨周期位移；下面用 BASE_SCHEDULE 存真实占位日期。
CONFERENCE_SPECS: Tuple[Dict[str, str], ...] = (
    {"key": "iclr", "label": "ICLR"},
    {"key": "neurips", "label": "NeurIPS"},
    {"key": "icml", "label": "ICML"},
    {"key": "aaai", "label": "AAAI"},
    {"key": "cvpr", "label": "CVPR"},
    {"key": "eccv", "label": "ECCV"},
    {"key": "ijcai", "label": "IJCAI"},
    {"key": "acl", "label": "ACL"},
    {"key": "emnlp", "label": "EMNLP"},
    {"key": "osdi", "label": "OSDI"},
    {"key": "sosp", "label": "SOSP"},
    {"key": "ieee_sp", "label": "IEEE S&P"},
    {"key": "ndss", "label": "NDSS"},
)

# 会议通用时间线模板（type 与 label 固定，date 留空由脚本用 offset 生成或直接覆盖）。
DEFAULT_TEMPLATE: List[Dict[str, Any]] = [
    {"type": "abstract_deadline", "label": "摘要投稿截止"},
    {"type": "full_paper", "label": "全文投稿截止"},
    {"type": "review", "label": "REVIEW 阶段"},
    {"type": "notification", "label": "录取通知"},
    {"type": "camera_ready", "label": "Camera Ready"},
    {"type": "conference", "label": "会议召开"},
]

# Covers a conference label for output, without depending on the (optionally offline) database.
CONFERENCE_ALIASES: Dict[str, str] = {
    "iclr": "ICLR",
    "neuralips": "NeurIPS",
    "nips": "NeurIPS",
    "neurips": "NeurIPS",
    "icml": "ICML",
    "aaai": "AAAI",
    "cvpr": "CVPR",
    "eccv": "ECCV",
    "ijcai": "IJCAI",
    "acl": "ACL",
    "emnlp": "EMNLP",
    "osdi": "OSDI",
    "sosp": "SOSP",
    "ieee_sp": "IEEE S&P",
    "ieee-sp": "IEEE S&P",
    "sp": "IEEE S&P",
    "ndss": "NDSS",
}


def _norm(value: object) -> str:
    return str(value or "").strip()


def default_years(ref_today: date) -> List[int]:
    # 覆盖进行中的周期：当前年、次年、前一年
    return [ref_today.year - 1, ref_today.year, ref_today.year + 1]


def parse_years(value: str, ref_today: date) -> List[int]:
    raw = _norm(value)
    if not raw:
        return default_years(ref_today)
    years: List[int] = []
    seen = set()
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        text = _norm(part)
        if not text or not text.isdigit():
            continue
        y = int(text)
        if y not in seen:
            seen.add(y)
            years.append(y)
    return years or default_years(ref_today)


def parse_date(value: str) -> date | None:
    text = _norm(value)
    try:
        return date.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def apply_offset(base: date, offset_days: int) -> date:
    return base + timedelta(days=offset_days)


# 每个会议每个年份的里程碑时间线（真实占位日期，随时间校准）。
# 结构: BASE_SCHEDULE[key][year] = [ {type,date,label}, ... ]
BASE_SCHEDULE: Dict[str, Dict[int, List[Dict[str, Any]]]] = {
    "iclr": {
        2027: [
            {"type": "abstract_deadline", "date": "2026-09-18", "label": "摘要投稿截止"},
            {"type": "full_paper", "date": "2026-09-25", "label": "全文投稿截止"},
            {"type": "review", "date": "2026-10-15", "label": "REVIEW 阶段"},
            {"type": "notification", "date": "2027-01-15", "label": "录取通知"},
            {"type": "camera_ready", "date": "2027-02-05", "label": "Camera Ready"},
            {"type": "conference", "date": "2027-04-24", "label": "会议召开 4/24-4/29"},
        ]
    },
    "neurips": {
        2026: [
            {"type": "full_paper", "date": "2026-05-27", "label": "全文投稿截止"},
            {"type": "review", "date": "2026-07-15", "label": "REVIEW 阶段"},
            {"type": "notification", "date": "2026-09-23", "label": "录取通知"},
            {"type": "camera_ready", "date": "2026-10-30", "label": "Camera Ready"},
            {"type": "conference", "date": "2026-12-06", "label": "会议召开 12/6-12/12"},
        ]
    },
    "icml": {
        2027: [
            {"type": "abstract_deadline", "date": "2027-01-25", "label": "摘要投稿截止"},
            {"type": "full_paper", "date": "2027-02-01", "label": "全文投稿截止"},
            {"type": "review", "date": "2027-03-01", "label": "REVIEW 阶段"},
            {"type": "notification", "date": "2027-05-10", "label": "录取通知"},
            {"type": "camera_ready", "date": "2027-06-01", "label": "Camera Ready"},
            {"type": "conference", "date": "2027-07-17", "label": "会议召开 7/17-7/23"},
        ]
    },
    "aaai": {
        2027: [
            {"type": "abstract_deadline", "date": "2026-08-01", "label": "摘要投稿截止"},
            {"type": "full_paper", "date": "2026-08-08", "label": "全文投稿截止"},
            {"type": "review", "date": "2026-09-15", "label": "REVIEW 阶段"},
            {"type": "notification", "date": "2026-11-15", "label": "录取通知"},
            {"type": "camera_ready", "date": "2027-01-05", "label": "Camera Ready"},
            {"type": "conference", "date": "2027-02-08", "label": "会议召开 2/8-2/14"},
        ]
    },
    "cvpr": {
        2027: [
            {"type": "abstract_deadline", "date": "2026-11-10", "label": "摘要投稿截止"},
            {"type": "full_paper", "date": "2026-11-17", "label": "全文投稿截止"},
            {"type": "review", "date": "2027-01-05", "label": "REVIEW 阶段"},
            {"type": "notification", "date": "2027-03-01", "label": "录取通知"},
            {"type": "camera_ready", "date": "2027-04-20", "label": "Camera Ready"},
            {"type": "conference", "date": "2027-06-12", "label": "会议召开 6/12-6/18"},
        ]
    },
    "eccv": {
        2026: [
            {"type": "conference", "date": "2026-09-27", "label": "会议召开 9/27-10/3"},
        ]
    },
    "ijcai": {
        2027: [
            {"type": "abstract_deadline", "date": "2027-01-15", "label": "摘要投稿截止"},
            {"type": "full_paper", "date": "2027-01-22", "label": "全文投稿截止"},
            {"type": "review", "date": "2027-02-15", "label": "REVIEW 阶段"},
            {"type": "notification", "date": "2027-04-20", "label": "录取通知"},
            {"type": "camera_ready", "date": "2027-05-30", "label": "Camera Ready"},
            {"type": "conference", "date": "2027-08-21", "label": "会议召开 8/21-8/27"},
        ]
    },
    "acl": {
        2027: [
            {"type": "abstract_deadline", "date": "2026-12-01", "label": "摘要投稿截止"},
            {"type": "full_paper", "date": "2026-12-08", "label": "全文投稿截止"},
            {"type": "review", "date": "2027-01-15", "label": "审稿阶段"},
            {"type": "notification", "date": "2027-03-28", "label": "录取通知"},
            {"type": "camera_ready", "date": "2027-05-10", "label": "Camera Ready"},
            {"type": "conference", "date": "2027-07-01", "label": "会议召开 7/1-7/7"},
        ]
    },
    "emnlp": {
        2027: [
            {"type": "abstract_deadline", "date": "2027-06-01", "label": "摘要投稿截止"},
            {"type": "full_paper", "date": "2027-06-08", "label": "全文投稿"},
            {"type": "review", "date": "2027-07-01", "label": "REVIEW 阶段"},
            {"type": "notification", "date": "2027-09-01", "label": "录取通知"},
            {"type": "conference", "date": "2027-11-01", "label": "会议召开"},
        ]
    },
    "osdi": {
        2027: [
            {"type": "full_paper", "date": "2026-09-10", "label": "全文投稿截止"},
            {"type": "review", "date": "2026-10-01", "label": "REVIEW 阶段"},
            {"type": "notification", "date": "2026-12-01", "label": "录取通知"},
            {"type": "camera_ready", "date": "2027-02-01", "label": "Camera Ready"},
            {"type": "conference", "date": "2027-07-12", "label": "会议召开 7/12-7/14"},
        ]
    },
}

CANONICAL: Dict[str, Dict[str, str]] = {
    "iclr": {"submission": "abstract", "notification": "january", "conference": "april"},
    "neurips": {"submission": "abstract", "notification": "september", "conference": "december"},
    "icml": {"submission": "abstract", "notification": "may", "conference": "july"},
    "aaai": {"submission": "abstract", "notification": "november", "conference": "february"},
    "cvpr": {"submission": "abstract", "notification": "march", "conference": "june"},
    "eccv": {"submission": "abstract", "notification": "may", "conference": "september"},
    "ijcai": {"submission": "abstract", "notification": "april", "conference": "august"},
    "acl": {"submission": "abstract", "notification": "march", "conference": "july"},
    "emnlp": {"submission": "abstract", "notification": "september", "conference": "november"},
    "osdi": {"submission": "full", "notification": "december", "conference": "july"},
    "sosp": {"submission": "full", "notification": "march", "conference": "october"},
    "ieee_sp": {"submission": "abstract", "notification": "september", "conference": "may"},
    "ndss": {"submission": "abstract", "notification": "october", "conference": "february"},
}


def build_schedule(
    ref_today: date,
    years: Iterable[int] | None = None,
    generated_at: str | None = None,
) -> Dict[str, Any]:
    """根据参考日期确定性生成会议日程快照。

    ref_today 只用于标注 generated_at 与挑选进行中的年份；里程碑日期全部来自
    内嵌的真实时间线模板（无 now() 依赖，保证同输入字节一致）。
    """
    selected_years = sorted(set(int(y) for y in (years or default_years(ref_today))))
    generated = generated_at or f"{ref_today:%Y-%m-%dT00:00:00Z}"

    # 每会议只产出有真实时间线的那些年份（此处保持 3 年窗口）
    conferences = []
    for spec in CONFERENCE_SPECS:
        key = spec["key"]
        timeline = BASE_SCHEDULE.get(key)
        if not timeline:
            continue
        label = spec["label"]
        entry_years = []
        for y in selected_years:
            milestones = timeline.get(y)
            if not milestones:
                continue
            entry_years.append({"year": y, "milestones": milestones})
        if not entry_years:
            continue
        entry_years.sort(key=lambda item: -item["year"])
        conferences.append(
            {"key": key, "label": label, "canonical": CANONICAL.get(key), "years": entry_years}
        )
    conferences.sort(key=lambda item: item["label"].lower())
    return {"schema_version": SCHEMA_VERSION, "generated_at": generated, "conferences": conferences}


def write_schedule(path: Path, snapshot: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[schedule] wrote {path}", flush=True)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重新生成会议日程快照。")
    parser.add_argument("--ref-date", default="", help="参考日期（YYYY-MM-DD），用于 generated_at 与年份窗口；默认今天。")
    parser.add_argument("--years", default="", help="年份列表，默认当前年前后一年。")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="静态 JSON 输出路径。")
    parser.add_argument("--generated-at", default="", help="generated_at 覆盖（通常由 --ref-date 派生，测试用）。")
    args = parser.parse_args(argv)

    ref_today = parse_date(args.ref_date) or date.today()
    snapshot = build_schedule(
        ref_today,
        years=parse_years(args.years, ref_today),
        generated_at=args.generated_at or None,
    )
    write_schedule(Path(args.output), snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
