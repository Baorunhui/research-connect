"""综述报告落盘与侧栏注册。

- 报告写入 docs/survey/<slug>-<hash>.md（复用会议链路的 255 字节短名策略，中文主题退化为 hash 名）；
- docs/_sidebar.md 增加 * Survey Reports 分组，块标记 <!--dpr-survey:<report_id>--> 幂等替换；
- 前端契约与会议/日报论文行一致：dpr-sidebar-item-structured 链接 + data-sidebar-item payload。
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from conference_sidebar import (
    slugify,
    stable_short_hash,
    shorten_slug_bytes,
    yaml_escape_value,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DOCS_DIR = ROOT_DIR / "docs"
DEFAULT_SIDEBAR_PATH = ROOT_DIR / "docs" / "_sidebar.md"
SURVEY_HEADING = "* Survey Reports\n"
SURVEY_ROUTE_DIR = "survey"
SURVEY_FILENAME_MAX_BYTES = 255
SURVEY_BASENAME_MAX_BYTES = 240

try:  # fcntl 仅 Unix 存在；Windows 降级无锁（与 conference_sidebar 同策略）。
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore


def _utc_date_token() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def build_report_basename(query: str, date_token: str | None = None) -> tuple[str, str]:
    """返回 (basename, report_id)。中文主题 slug 后为空，退化为 survey-<hash> 保证可读且稳定。"""
    day = date_token or _utc_date_token()
    digest = stable_short_hash("survey", query, day)
    query_slug = slugify(query)
    head = f"survey-{query_slug}" if query_slug and query_slug != "paper" else "survey"
    basename = shorten_slug_bytes(head, SURVEY_BASENAME_MAX_BYTES, suffix=digest)
    return basename, digest


def build_report_route(basename: str) -> str:
    return f"{SURVEY_ROUTE_DIR}/{basename}"


def build_report_markdown(result: Dict[str, Any], *, date_label: str | None = None) -> str:
    meta = result.get("report_meta") or {}
    title_zh = str((result.get("outline") or {}).get("title_zh") or "研究综述").strip()
    generated_at = str(meta.get("generated_at") or "").strip()
    date_label = date_label or (generated_at[:10] if generated_at else _utc_date_token()[:4] + "-" + _utc_date_token()[4:6] + "-" + _utc_date_token()[6:])
    clusters = result.get("clusters") or []
    front_matter = [
        "---",
        f"title: {yaml_escape_value(title_zh)}",
        f"title_zh: {yaml_escape_value(title_zh)}",
        f"date: {yaml_escape_value(date_label)}",
        f"query: {yaml_escape_value(result.get('query') or '')}",
        f"paper_count: {int(meta.get('n_papers') or len(result.get('papers') or []))}",
        f"clusters: {yaml_escape_value(json.dumps([c.get('name_zh') for c in clusters], ensure_ascii=False))}",
        "tags:",
        "  - kind: label",
        "    label: 综述",
        "selection_source: survey_pipeline",
        "---",
        "",
    ]
    body = str(result.get("report_markdown") or "").strip()
    return "\n".join(front_matter) + body + "\n"


def write_report_docs(docs_dir: Path, result: Dict[str, Any], *, date_token: str | None = None) -> Dict[str, Any]:
    """写 docs/survey/<basename>.md，返回报告注册信息。"""
    day = date_token or _utc_date_token()
    basename, report_id = build_report_basename(str(result.get("query") or ""), day)
    survey_dir = Path(docs_dir) / SURVEY_ROUTE_DIR
    survey_dir.mkdir(parents=True, exist_ok=True)
    md_path = survey_dir / f"{basename}.md"
    title_zh = str((result.get("outline") or {}).get("title_zh") or "研究综述").strip()
    md_path.write_text(build_report_markdown(result), encoding="utf-8")
    return {
        "report_id": report_id,
        "basename": basename,
        "route": build_report_route(basename),
        "paper_id": build_report_route(basename),
        "md_path": str(md_path),
        "title_zh": title_zh,
        "date": f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else day,
    }


# --------------------------------------------------------------------------- #
# 侧栏注册
# --------------------------------------------------------------------------- #

_SURVEY_MARKER_RE = r"<!--dpr-survey:([a-z0-9]+)-->"


def _payload_json(info: Dict[str, Any]) -> str:
    payload = {
        "title": info.get("title_zh") or "研究综述",
        "link": "",
        "selection_source": "survey_pipeline",
        "tags": [{"kind": "label", "label": "综述"}],
        "published": info.get("date") or "",
    }
    return html.escape(json.dumps(payload, ensure_ascii=False), quote=True)


def build_survey_block(info: Dict[str, Any]) -> List[str]:
    label = f"{info.get('date') or ''} · {info.get('title_zh') or '研究综述'}".strip(" ·")
    href = f"#/{info['route']}"
    return [
        f"  * {label} <!--dpr-survey:{info['report_id']}-->\n",
        "    * "
        f'<a class="dpr-sidebar-item-link dpr-sidebar-item-structured" href="{html.escape(href, quote=True)}" '
        f'data-sidebar-item="{_payload_json(info)}">{html.escape(info.get("title_zh") or "研究综述")}</a>\n',
    ]


def find_survey_heading(lines: List[str]) -> int:
    for idx, line in enumerate(lines):
        if line.strip() == "* Survey Reports":
            return idx
    return -1


def ensure_survey_heading(lines: List[str]) -> int:
    """确保 * Survey Reports 标题存在：插在 * Daily Papers 之前（位于会议分组之后）。"""
    idx = find_survey_heading(lines)
    if idx >= 0:
        return idx
    insert_idx = len(lines)
    for i, line in enumerate(lines):
        if line.strip() == "* Daily Papers":
            insert_idx = i
            break
    if insert_idx > 0 and lines[insert_idx - 1].strip():
        lines.insert(insert_idx, "\n")
        insert_idx += 1
    lines.insert(insert_idx, SURVEY_HEADING)
    return insert_idx


def remove_survey_block(lines: List[str], report_id: str) -> None:
    """删除指定 report_id 的综述块（标题行 + 其下 4 空格子行）。"""
    marker = f"<!--dpr-survey:{report_id}-->"
    i = 0
    while i < len(lines):
        if marker in lines[i] and re.match(r"^\s{2}\*\s", lines[i]):
            del lines[i]
            while i < len(lines) and (not lines[i].strip() or re.match(r"^\s{4}\*", lines[i])):
                del lines[i]
            continue
        i += 1


def update_sidebar_with_survey(sidebar_path: Path, info: Dict[str, Any]) -> None:
    """注册（或按 report_id 替换）一个综述报告条目。线程/进程间用 docs/.sidebar.lock 互斥。"""
    sidebar_path = Path(sidebar_path)
    sidebar_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sidebar_path.parent / ".sidebar.lock"
    block = build_survey_block(info)
    with open(lock_path, "w") as lock_fd:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            lines = (
                sidebar_path.read_text(encoding="utf-8").splitlines(keepends=True)
                if sidebar_path.exists()
                else []
            )
            remove_survey_block(lines, info["report_id"])
            heading_idx = ensure_survey_heading(lines)
            lines[heading_idx + 1 : heading_idx + 1] = block
            sidebar_path.write_text("".join(lines), encoding="utf-8")
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)


def persist_survey_report(result: Dict[str, Any], *, docs_dir: Path | None = None, sidebar_path: Path | None = None) -> Dict[str, Any]:
    """写报告页 + 注册侧栏，返回报告信息（含 route/paper_id/md_path）。"""
    docs_dir = Path(docs_dir) if docs_dir else DEFAULT_DOCS_DIR
    sidebar_path = Path(sidebar_path) if sidebar_path else DEFAULT_SIDEBAR_PATH
    info = write_report_docs(docs_dir, result)
    update_sidebar_with_survey(sidebar_path, info)
    info["registered"] = True
    return info
