import html
import json
from pathlib import Path
from unittest import mock

import pytest

from src.daily_report_state import (
    bootstrap_daily_state_from_sidebar,
    daily_state_path,
    entries_from_state,
    load_daily_state,
    merge_daily_state,
    save_daily_state,
)


def _paper(
    paper_id,
    route,
    title,
    section,
    score,
    *,
    tags=None,
    evidence="",
):
    return {
        "paper_id": paper_id,
        "route": route,
        "title": title,
        "section": section,
        "tags": tags or [],
        "score": score,
        "evidence": evidence,
    }


def test_merge_daily_state_dedupes_two_runs_and_promotes_to_deep():
    state = merge_daily_state(
        existing={},
        date_str="20260719",
        run_papers={
            "quick": [
                _paper(
                    "paper-a",
                    "202607/19/paper-a-v1",
                    "Old Title",
                    "quick",
                    8.1,
                    tags=[{"kind": "query", "label": "rl"}],
                    evidence="old evidence",
                )
            ],
            "deep": [
                _paper(
                    "paper-b",
                    "202607/19/paper-b",
                    "Paper B",
                    "deep",
                    7.0,
                    tags=[{"kind": "query", "label": "vision"}],
                )
            ],
        },
        generated_at="2026-07-19T09:00:00Z",
        recommend_exists=True,
        date_label="2026-07-19",
    )

    merged = merge_daily_state(
        existing=state,
        date_str="20260719",
        run_papers={
            "deep": [
                _paper(
                    "paper-a",
                    "202607/19/paper-a-v2",
                    "New Title",
                    "deep",
                    9.3,
                    tags=[{"kind": "query", "label": "agents"}],
                    evidence="new evidence",
                )
            ]
        },
        generated_at="2026-07-19T10:00:00Z",
        recommend_exists=True,
        date_label="2026-07-19",
    )

    assert merged["run_count"] == 2
    assert [item["paper_id"] for item in merged["papers"]] == ["paper-a", "paper-b"]
    paper_a = merged["papers"][0]
    assert paper_a["section"] == "deep"
    assert paper_a["route"] == "202607/19/paper-a-v2"
    assert paper_a["title"] == "New Title"
    assert paper_a["score"] == 9.3
    assert paper_a["evidence"] == "new evidence"
    assert paper_a["first_seen_at"] == "2026-07-19T09:00:00Z"
    assert paper_a["updated_at"] == "2026-07-19T10:00:00Z"

    deep_entries, quick_entries, evidence_by_route = entries_from_state(merged)
    assert [route for route, _title, _tags in deep_entries] == [
        "202607/19/paper-a-v2",
        "202607/19/paper-b",
    ]
    assert quick_entries == []
    assert evidence_by_route["202607/19/paper-a-v2"] == "new evidence"


def test_merge_daily_state_unions_tags_and_keeps_highest_score():
    merged = merge_daily_state(
        existing={},
        date_str="20260719",
        run_papers={
            "deep": [
                _paper(
                    "paper-a",
                    "202607/19/paper-a",
                    "Paper A",
                    "deep",
                    7.5,
                    tags=[
                        {"kind": "query", "label": "rl"},
                        {"kind": "query", "label": "agents"},
                    ],
                )
            ]
        },
        generated_at="2026-07-19T09:00:00Z",
        recommend_exists=True,
    )
    merged = merge_daily_state(
        existing=merged,
        date_str="20260719",
        run_papers={
            "quick": [
                _paper(
                    "paper-a",
                    "202607/19/paper-a",
                    "",
                    "quick",
                    9.1,
                    tags=[
                        {"kind": "query", "label": "rl"},
                        {"kind": "keyword", "label": "robotics"},
                    ],
                )
            ]
        },
        generated_at="2026-07-19T10:00:00Z",
        recommend_exists=True,
    )

    paper = merged["papers"][0]
    assert paper["section"] == "deep"
    assert paper["score"] == 9.1
    assert paper["title"] == "Paper A"
    assert paper["tags"] == [
        {"kind": "keyword", "label": "robotics"},
        {"kind": "query", "label": "agents"},
        {"kind": "query", "label": "rl"},
    ]

    deep_entries, _quick_entries, _evidence_by_route = entries_from_state(merged)
    assert deep_entries[0][2] == [
        ("score", "9.1"),
        ("keyword", "robotics"),
        ("query", "agents"),
        ("query", "rl"),
    ]


def test_merge_daily_state_empty_run_keeps_existing_entries():
    existing = merge_daily_state(
        existing={},
        date_str="20260719",
        run_papers={
            "deep": [
                _paper("paper-a", "202607/19/paper-a", "Paper A", "deep", 8.0)
            ]
        },
        generated_at="2026-07-19T09:00:00Z",
        recommend_exists=True,
    )

    merged = merge_daily_state(
        existing=existing,
        date_str="20260719",
        run_papers={"deep": [], "quick": []},
        generated_at="2026-07-19T11:00:00Z",
        recommend_exists=False,
    )

    assert merged["run_count"] == 2
    assert [item["paper_id"] for item in merged["papers"]] == ["paper-a"]
    assert merged["papers"][0]["updated_at"] == "2026-07-19T09:00:00Z"
    assert merged["recommend_exists"] is True


def test_daily_state_path_supports_single_day_and_ranges(tmp_path):
    assert daily_state_path(str(tmp_path), "20260719") == str(
        tmp_path / "202607" / "19" / "_daily_state.json"
    )
    assert daily_state_path(str(tmp_path), "20260701-20260719") == str(
        tmp_path / "20260701-20260719" / "_daily_state.json"
    )


def test_merge_daily_state_uses_route_as_migration_alias():
    existing = {
        "date": "20260719",
        "run_count": 3,
        "papers": [
            {
                "paper_id": "legacy-route-only",
                "route": "202607/19/paper-a",
                "title": "Legacy Title",
                "section": "quick",
                "tags": [{"kind": "query", "label": "legacy"}],
                "score": 6.0,
                "evidence": "",
                "first_seen_at": "2026-07-19T08:00:00Z",
                "updated_at": "2026-07-19T08:00:00Z",
            }
        ],
    }

    merged = merge_daily_state(
        existing=existing,
        date_str="20260719",
        run_papers={
            "deep": [
                _paper(
                    "paper-a",
                    "202607/19/paper-a",
                    "Canonical Title",
                    "deep",
                    9.0,
                    evidence="canonical evidence",
                )
            ]
        },
        generated_at="2026-07-19T12:00:00Z",
        recommend_exists=True,
    )

    assert len(merged["papers"]) == 1
    paper = merged["papers"][0]
    assert paper["paper_id"] == "paper-a"
    assert paper["section"] == "deep"
    assert paper["title"] == "Canonical Title"
    assert paper["evidence"] == "canonical evidence"
    assert merged["run_count"] == 4


def test_daily_state_save_and_load_is_atomic(tmp_path):
    path = daily_state_path(str(tmp_path), "20260719")
    state = merge_daily_state(
        existing={},
        date_str="20260719",
        run_papers={
            "quick": [
                _paper("paper-a", "202607/19/paper-a", "Paper A", "quick", 6.2)
            ]
        },
        generated_at="2026-07-19T09:00:00Z",
        recommend_exists=True,
    )

    real_replace = __import__("os").replace
    with mock.patch("src.daily_report_state.os.replace", side_effect=real_replace) as mocked_replace:
        save_daily_state(path, state)

    assert mocked_replace.call_count == 1
    assert Path(path).exists()
    assert not list(Path(path).parent.glob("*.tmp"))
    assert load_daily_state(path) == state


def test_bootstrap_daily_state_from_sidebar_reads_same_day_block(tmp_path):
    payload_deep = html.escape(
        json.dumps(
            {
                "title": "Paper A",
                "link": "https://arxiv.org/abs/2401.00001",
                "score": "9.2",
                "evidence": "中文解释 A",
                "tags": [{"kind": "query", "label": "rl"}],
            },
            ensure_ascii=False,
        ),
        quote=True,
    )
    payload_quick = html.escape(
        json.dumps(
            {
                "title": "Paper B",
                "link": "https://arxiv.org/abs/2401.00002",
                "score": "7.4",
                "tags": [{"kind": "keyword", "label": "robot"}],
            },
            ensure_ascii=False,
        ),
        quote=True,
    )
    sidebar = tmp_path / "_sidebar.md"
    sidebar.write_text(
        "\n".join(
            [
                "* [首页](/)",
                "* Daily Papers",
                "  * 2026-07-18 <!--dpr-date:20260718-->",
                "    * 精读区",
                f'      * <a class="dpr-sidebar-item-link dpr-sidebar-item-structured" href="#/202607/18/old" data-sidebar-item="{payload_quick}">Old</a>',
                "  * 2026-07-19 <!--dpr-date:20260719-->",
                "    * 精读区",
                f'      * <a class="dpr-sidebar-item-link dpr-sidebar-item-structured" href="#/202607/19/paper-a" data-sidebar-item="{payload_deep}">Fallback A</a>',
                "    * 速读区",
                f'      * <a class="dpr-sidebar-item-link dpr-sidebar-item-structured" href="#/202607/19/paper-b" data-sidebar-item="{payload_quick}">Fallback B</a>',
                "",
            ]
        ),
        encoding="utf-8",
    )

    state = bootstrap_daily_state_from_sidebar(
        str(sidebar),
        "20260719",
        generated_at="2026-07-19T10:00:00Z",
    )

    assert state["date"] == "20260719"
    assert state["date_label"] == "2026-07-19"
    assert state["run_count"] == 0
    assert state["recommend_exists"] is True
    assert [item["paper_id"] for item in state["papers"]] == [
        "2401.00001",
        "2401.00002",
    ]
    assert state["papers"][0]["section"] == "deep"
    assert state["papers"][1]["section"] == "quick"

    deep_entries, quick_entries, evidence_by_route = entries_from_state(state)
    assert deep_entries == [
        (
            "202607/19/paper-a",
            "Paper A",
            [("score", "9.2"), ("query", "rl")],
        )
    ]
    assert quick_entries == [
        (
            "202607/19/paper-b",
            "Paper B",
            [("score", "7.4"), ("keyword", "robot")],
        )
    ]
    assert evidence_by_route == {"202607/19/paper-a": "中文解释 A"}


def test_bootstrap_daily_state_from_legacy_sidebar_without_marker(tmp_path):
    payload = html.escape(
        json.dumps(
            {
                "title": "Legacy Paper",
                "link": "https://arxiv.org/abs/2401.00003",
                "score": "8.0",
                "tags": [{"kind": "query", "label": "legacy"}],
            },
            ensure_ascii=False,
        ),
        quote=True,
    )
    sidebar = tmp_path / "_sidebar.md"
    sidebar.write_text(
        "\n".join(
            [
                "* Daily Papers",
                "  * 2026-07-19",
                "    * 精读区",
                f'      * <a href="#/202607/19/legacy-paper" data-sidebar-item="{payload}">Legacy Paper</a>',
                "",
            ]
        ),
        encoding="utf-8",
    )

    state = bootstrap_daily_state_from_sidebar(str(sidebar), "20260719")

    assert state["date_label"] == "2026-07-19"
    assert [paper["paper_id"] for paper in state["papers"]] == ["2401.00003"]
