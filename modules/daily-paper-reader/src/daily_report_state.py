import html
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urlparse


def daily_state_path(docs_dir: str, date_str: str) -> str:
    token = str(date_str or "").strip()
    if re.fullmatch(r"\d{8}", token):
        return os.path.join(docs_dir, token[:6], token[6:8], "_daily_state.json")
    return os.path.join(docs_dir, token, "_daily_state.json")


def load_daily_state(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("daily state must be a JSON object")
    return _normalize_state(payload, str(payload.get("date") or "").strip())


def save_daily_state(path: str, state: Dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    normalized = _normalize_state(state, str(state.get("date") or "").strip())
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=".daily_state.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = handle.name
            json.dump(normalized, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def merge_daily_state(
    existing: Dict[str, Any] | None,
    date_str: str,
    run_papers: Any,
    generated_at: str,
    recommend_exists: bool,
    date_label: str = "",
) -> Dict[str, Any]:
    token = str(date_str or "").strip()
    current_generated_at = _clean_text(generated_at)
    state = _normalize_state(existing or {}, token)
    records = [_normalize_record(item) for item in state.get("papers", [])]

    for incoming in _iter_run_records(run_papers):
        match_index = _find_match_index(records, incoming["paper_id"], incoming["route"])
        if match_index < 0:
            incoming["first_seen_at"] = current_generated_at or incoming["first_seen_at"]
            incoming["updated_at"] = current_generated_at or incoming["updated_at"]
            records.append(incoming)
            continue
        records[match_index] = _merge_record(records[match_index], incoming, current_generated_at)

    return _normalize_state(
        {
            "date": token,
            "date_label": _clean_text(date_label) or state.get("date_label", ""),
            "generated_at": current_generated_at or state.get("generated_at", ""),
            "recommend_exists": bool(recommend_exists) or bool(state.get("recommend_exists")),
            "run_count": int(state.get("run_count", 0) or 0) + 1,
            "papers": records,
        },
        token,
    )


def entries_from_state(
    state: Dict[str, Any] | None,
) -> Tuple[
    List[Tuple[str, str, List[Tuple[str, str]]]],
    List[Tuple[str, str, List[Tuple[str, str]]]],
    Dict[str, str],
]:
    normalized = _normalize_state(state or {}, str((state or {}).get("date") or "").strip())
    deep_entries: List[Tuple[str, str, List[Tuple[str, str]]]] = []
    quick_entries: List[Tuple[str, str, List[Tuple[str, str]]]] = []
    evidence_by_route: Dict[str, str] = {}

    for paper in normalized["papers"]:
        route = _clean_text(paper.get("route")) or _clean_text(paper.get("paper_id"))
        if not route:
            continue
        title = _clean_text(paper.get("title")) or route
        tags = _entry_tags(paper)
        evidence = _clean_text(paper.get("evidence"))
        if evidence:
            evidence_by_route[route] = evidence
        if paper.get("section") == "deep":
            deep_entries.append((route, title, tags))
        else:
            quick_entries.append((route, title, tags))
    return deep_entries, quick_entries, evidence_by_route


def bootstrap_daily_state_from_sidebar(
    sidebar_path: str,
    date_str: str,
    date_label: str = "",
    generated_at: str = "",
) -> Dict[str, Any]:
    token = str(date_str or "").strip()
    if not sidebar_path or not os.path.exists(sidebar_path):
        return _normalize_state(
            {
                "date": token,
                "date_label": _clean_text(date_label),
                "generated_at": _clean_text(generated_at),
                "recommend_exists": False,
                "run_count": 0,
                "papers": [],
            },
            token,
        )

    with open(sidebar_path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()

    block_lines, inferred_label = _extract_sidebar_day_block(lines, token)
    section = "deep"
    papers: List[Dict[str, Any]] = []
    for line in block_lines:
        stripped = line.strip()
        if "精读区" in stripped:
            section = "deep"
            continue
        if "速读区" in stripped:
            section = "quick"
            continue
        if "data-sidebar-item=" not in line or "href=" not in line:
            continue
        payload = _parse_sidebar_payload(line)
        route = _extract_sidebar_route(line)
        title = _clean_text(payload.get("title")) or _extract_anchor_text(line) or route
        evidence = _clean_text(payload.get("evidence"))
        papers.append(
            _normalize_record(
                {
                    "paper_id": _infer_sidebar_paper_id(payload, route),
                    "route": route,
                    "title": title,
                    "section": section,
                    "tags": payload.get("tags"),
                    "score": payload.get("score"),
                    "evidence": evidence,
                    "first_seen_at": _clean_text(generated_at),
                    "updated_at": _clean_text(generated_at),
                }
            )
        )

    return _normalize_state(
        {
            "date": token,
            "date_label": _clean_text(date_label) or inferred_label,
            "generated_at": _clean_text(generated_at),
            "recommend_exists": bool(papers),
            "run_count": 0,
            "papers": papers,
        },
        token,
    )


def _normalize_state(state: Dict[str, Any], date_str: str) -> Dict[str, Any]:
    papers = [_normalize_record(item) for item in (state.get("papers") or []) if isinstance(item, dict)]
    papers = sorted(papers, key=_paper_sort_key)
    token = str(date_str or state.get("date") or "").strip()
    return {
        "date": token,
        "date_label": _clean_text(state.get("date_label")),
        "generated_at": _clean_text(state.get("generated_at")),
        "recommend_exists": bool(state.get("recommend_exists", False)),
        "run_count": int(state.get("run_count", 0) or 0),
        "papers": papers,
    }


def _normalize_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    paper_id = _clean_text(raw.get("paper_id"))
    route = _clean_text(raw.get("route")) or paper_id
    if not paper_id:
        paper_id = route
    first_seen_at = _clean_text(raw.get("first_seen_at"))
    updated_at = _clean_text(raw.get("updated_at"))
    return {
        "paper_id": paper_id,
        "route": route,
        "title": _clean_text(raw.get("title")),
        "section": _normalize_section(raw.get("section")),
        "tags": _normalize_tags(raw.get("tags")),
        "score": _normalize_score(raw.get("score")),
        "evidence": _clean_text(raw.get("evidence")),
        "first_seen_at": first_seen_at,
        "updated_at": updated_at,
    }


def _normalize_section(section: Any) -> str:
    return "deep" if _clean_text(section).lower() == "deep" else "quick"


def _normalize_tags(raw_tags: Any) -> List[Dict[str, str]]:
    tags: List[Dict[str, str]] = []
    seen = set()
    if not isinstance(raw_tags, list):
        return tags
    for tag in raw_tags:
        if not isinstance(tag, dict):
            continue
        kind = _clean_text(tag.get("kind")) or "query"
        label = _clean_text(tag.get("label"))
        if not label:
            continue
        dedup_key = (kind, label)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        tags.append({"kind": kind, "label": label})
    tags.sort(key=lambda item: (item["kind"], item["label"]))
    return tags


def _normalize_score(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _iter_run_records(run_papers: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(run_papers, dict):
        items: List[Dict[str, Any]] = []
        for section in ("deep", "quick"):
            papers = run_papers.get(section) or []
            if not isinstance(papers, list):
                continue
            for paper in papers:
                if not isinstance(paper, dict):
                    continue
                merged = dict(paper)
                merged["section"] = section
                items.append(_normalize_record(merged))
        return items
    if isinstance(run_papers, list):
        return [_normalize_record(paper) for paper in run_papers if isinstance(paper, dict)]
    return []


def _find_match_index(records: List[Dict[str, Any]], paper_id: str, route: str) -> int:
    clean_paper_id = _clean_text(paper_id)
    clean_route = _clean_text(route)
    for index, record in enumerate(records):
        if clean_paper_id and clean_paper_id == record["paper_id"]:
            return index
    for index, record in enumerate(records):
        if clean_route and clean_route == record["paper_id"]:
            return index
    for index, record in enumerate(records):
        if clean_paper_id and clean_paper_id == record["route"]:
            return index
    for index, record in enumerate(records):
        if clean_route and clean_route == record["route"]:
            return index
    return -1


def _merge_record(
    existing: Dict[str, Any],
    incoming: Dict[str, Any],
    generated_at: str,
) -> Dict[str, Any]:
    score_existing = existing.get("score")
    score_incoming = incoming.get("score")
    if score_existing is None:
        merged_score = score_incoming
    elif score_incoming is None:
        merged_score = score_existing
    else:
        merged_score = max(score_existing, score_incoming)
    return _normalize_record(
        {
            "paper_id": _clean_text(incoming.get("paper_id")) or existing.get("paper_id"),
            "route": _clean_text(incoming.get("route")) or existing.get("route"),
            "title": _clean_text(incoming.get("title")) or existing.get("title"),
            "section": "deep"
            if existing.get("section") == "deep" or incoming.get("section") == "deep"
            else "quick",
            "tags": _merge_tags(existing.get("tags"), incoming.get("tags")),
            "score": merged_score,
            "evidence": _clean_text(incoming.get("evidence")) or existing.get("evidence"),
            "first_seen_at": _clean_text(existing.get("first_seen_at"))
            or _clean_text(incoming.get("first_seen_at"))
            or generated_at,
            "updated_at": generated_at
            or _clean_text(incoming.get("updated_at"))
            or _clean_text(existing.get("updated_at")),
        }
    )


def _merge_tags(existing_tags: Any, incoming_tags: Any) -> List[Dict[str, str]]:
    tags = _normalize_tags(existing_tags) + _normalize_tags(incoming_tags)
    return _normalize_tags(tags)


def _paper_sort_key(paper: Dict[str, Any]) -> Tuple[float, float, str]:
    score = paper.get("score")
    score_key = -(float(score) if score is not None else float("-inf"))
    updated_key = -_timestamp_key(_clean_text(paper.get("updated_at")))
    return score_key, updated_key, _clean_text(paper.get("paper_id"))


def _timestamp_key(value: str) -> float:
    if not value:
        return 0.0
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _entry_tags(paper: Dict[str, Any]) -> List[Tuple[str, str]]:
    tags: List[Tuple[str, str]] = []
    score = paper.get("score")
    if score is not None:
        tags.append(("score", _format_score(score)))
    for tag in _normalize_tags(paper.get("tags")):
        tags.append((tag["kind"], tag["label"]))
    return tags


def _format_score(value: Any) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return _clean_text(value)


def _extract_sidebar_day_block(lines: List[str], date_str: str) -> Tuple[List[str], str]:
    marker = f"<!--dpr-date:{date_str}-->"
    legacy_label = _format_date_label(date_str)
    day_index = -1
    date_label = ""
    for index, line in enumerate(lines):
        if marker in line:
            day_index = index
            date_label = _clean_text(line.split(marker, 1)[0].replace("*", "", 1))
            break
        if (
            legacy_label
            and line.startswith("  * ")
            and not line.startswith("    * ")
            and _clean_text(line[4:]) == legacy_label
        ):
            day_index = index
            date_label = legacy_label
            break
    if day_index < 0:
        return [], ""
    end = day_index + 1
    while end < len(lines):
        if lines[end].startswith("  * ") and not lines[end].startswith("    * "):
            break
        if lines[end].startswith("* "):
            break
        end += 1
    return lines[day_index + 1 : end], date_label


def _format_date_label(date_str: str) -> str:
    token = _clean_text(date_str)
    if re.fullmatch(r"\d{8}", token):
        return f"{token[:4]}-{token[4:6]}-{token[6:]}"
    match = re.fullmatch(r"(\d{8})-(\d{8})", token)
    if match:
        start, end = match.groups()
        return (
            f"{start[:4]}-{start[4:6]}-{start[6:]} ~ "
            f"{end[:4]}-{end[4:6]}-{end[6:]}"
        )
    return token


def _parse_sidebar_payload(line: str) -> Dict[str, Any]:
    match = re.search(r'data-sidebar-item="([^"]*)"', line)
    if not match:
        return {}
    try:
        payload = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_sidebar_route(line: str) -> str:
    match = re.search(r'href="([^"]+)"', line)
    href = match.group(1) if match else ""
    href = href.lstrip("#")
    return href[1:] if href.startswith("/") else href


def _extract_anchor_text(line: str) -> str:
    match = re.search(r">([^<]*)</a>", line)
    return html.unescape(match.group(1)).strip() if match else ""


def _infer_sidebar_paper_id(payload: Dict[str, Any], route: str) -> str:
    link = _clean_text(payload.get("link"))
    if link:
        parsed = urlparse(link)
        path = parsed.path.rstrip("/")
        if path:
            tail = path.split("/")[-1]
            if tail:
                return tail
    route_tail = _clean_text(route).rstrip("/").split("/")[-1]
    return route_tail or _clean_text(route)
