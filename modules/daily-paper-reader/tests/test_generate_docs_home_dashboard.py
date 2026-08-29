import importlib.util
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    if "fitz" not in sys.modules:
        fitz_stub = types.ModuleType("fitz")
        fitz_stub.open = lambda *args, **kwargs: None
        sys.modules["fitz"] = fitz_stub
    if "llm" not in sys.modules:
        llm_stub = types.ModuleType("llm")

        class DummyDeepSeekClient:
            def __init__(self, *args, **kwargs):
                pass

        llm_stub.DeepSeekClient = DummyDeepSeekClient
        sys.modules["llm"] = llm_stub

    src_path = ROOT / "src" / "6.generate_docs.py"
    spec = importlib.util.spec_from_file_location("gen6_home_dashboard", src_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    module.LLM_CLIENT = None
    return module


def _sample_entries():
    deep = [
        (
            "202607/20/paper-a",
            "Paper <A>",
            [("keyword", "RL"), ("query", "agents"), ("score", "9.0")],
        ),
        (
            "202607/20/paper-b",
            "Paper B",
            [("keyword", "RL"), ("score", "8.0")],
        ),
    ]
    quick = [
        (
            "202607/20/paper-c",
            "Paper C",
            [("keyword", "systems"), ("score", "7.0")],
        )
    ]
    return deep, quick


def test_home_latest_report_renders_one_two_by_two_dashboard():
    mod = _load_module()
    deep, quick = _sample_entries()

    html = mod.build_latest_report_section(
        date_str="20260720",
        date_label=None,
        generated_at="2026-07-20 10:30:00 UTC",
        recommend_exists=True,
        deep_entries=deep,
        quick_entries=quick,
        paper_evidence_by_id={},
        run_count=2,
    )

    assert html.count('class="dpr-home-dashboard-card ') == 4
    assert 'class="dpr-home-dashboard-grid"' in html
    assert "dpr-home-report-card" in html
    assert "dpr-home-brief-card" in html
    assert "dpr-home-deep-card" in html
    assert "dpr-home-skim-card" in html
    assert "今日汇总" in html
    assert "累计更新" in html and "2 次" in html
    assert "共 3 篇" in html
    assert "精读推荐" in html and "2 篇" in html
    assert "速读推荐" in html and "1 篇" in html
    assert "Paper &lt;A&gt;" in html
    assert "Paper <A>" not in html
    assert '<span class="dpr-home-dashboard-paper-title"' in html
    assert "<a " not in html
    assert "href=" not in html


def test_same_day_home_sync_replaces_the_dashboard_instead_of_appending(tmp_path):
    mod = _load_module()
    deep, quick = _sample_entries()

    mod.sync_home_readme_from_day_report(
        docs_dir=str(tmp_path),
        date_str="20260720",
        date_label="2026-07-20",
        generated_at="2026-07-20 09:00:00 UTC",
        recommend_exists=True,
        deep_entries=deep[:1],
        quick_entries=[],
        paper_evidence_by_id={},
        run_count=1,
        summary="第一次生成。",
    )
    home_path = mod.sync_home_readme_from_day_report(
        docs_dir=str(tmp_path),
        date_str="20260720",
        date_label="2026-07-20",
        generated_at="2026-07-20 10:00:00 UTC",
        recommend_exists=True,
        deep_entries=deep,
        quick_entries=quick,
        paper_evidence_by_id={},
        run_count=2,
        summary="合并后重新生成。",
    )

    content = Path(home_path).read_text(encoding="utf-8")
    assert content.count('class="dpr-home-dashboard-grid"') == 1
    assert content.count('class="dpr-home-dashboard-card ') == 4
    assert "累计更新</dt><dd>2 次" in content
    assert "共 3 篇" in content
    assert "合并后重新生成。" in content
    assert "第一次生成。" not in content
    dashboard = content.split('<div class="dpr-home-dashboard-grid">', 1)[1]
    assert "<a " not in dashboard


def test_home_sync_reads_stable_templates_instead_of_runtime_modules(tmp_path):
    mod = _load_module()
    docs_dir = tmp_path / "docs"
    template_dir = tmp_path / "docs_init"
    docs_dir.mkdir()
    template_dir.mkdir()
    (docs_dir / "_home_notice.md").write_text("旧运行态公告", encoding="utf-8")
    (template_dir / "_home_notice.md").write_text("新版稳定公告", encoding="utf-8")
    mod.HOME_TEMPLATE_DIR = str(template_dir)

    home_path = mod.sync_home_readme_from_day_report(
        docs_dir=str(docs_dir),
        date_str="20260720",
        date_label="2026-07-20",
        generated_at="2026-07-20 10:00:00 UTC",
        recommend_exists=False,
        deep_entries=[],
        quick_entries=[],
        paper_evidence_by_id={},
    )

    content = Path(home_path).read_text(encoding="utf-8")
    assert "新版稳定公告" in content
    assert "旧运行态公告" not in content


def test_day_report_uses_cumulative_wording_after_merge():
    mod = _load_module()
    deep, quick = _sample_entries()

    report = mod.build_day_report_markdown(
        date_str="20260720",
        date_label=None,
        deep_entries=deep,
        quick_entries=quick,
        recommend_exists=True,
        run_count=3,
        generated_at="2026-07-20 11:00:00 UTC",
    )

    assert "今日累计更新：3 次" in report
    assert "今日累计推荐总数：3" in report
    assert "当次推荐总数" not in report
    assert "精读区：2" in report
    assert "速读区：1" in report


def test_build_daily_run_papers_keeps_source_id_separate_from_route(tmp_path):
    mod = _load_module()
    paper = {
        "id": "2401.00001v2",
        "title": "A Better Paper",
        "llm_score": 8.7,
        "llm_tags": ["query:agents", "keyword:rl"],
        "canonical_evidence": "值得精读。",
    }
    _, _, route = mod.prepare_paper_paths(
        str(tmp_path),
        "20260720",
        paper["title"],
        paper["id"],
    )
    entries = [(route, paper["title"], mod.extract_sidebar_tags(paper))]

    records = mod.build_daily_run_papers(
        docs_dir=str(tmp_path),
        date_str="20260720",
        deep_list=[paper],
        quick_list=[],
        deep_entries=entries,
        quick_entries=[],
        evidence_by_route={route: "值得精读。"},
    )

    assert records["quick"] == []
    assert records["deep"] == [
        {
            "paper_id": "2401.00001v2",
            "route": route,
            "title": "A Better Paper",
            "section": "deep",
            "tags": [
                {"kind": "query", "label": "agents"},
                {"kind": "query", "label": "rl"},
            ],
            "score": 8.7,
            "evidence": "值得精读。",
        }
    ]


def test_meta_index_is_rebuilt_from_cumulative_entries(tmp_path):
    mod = _load_module()
    day_dir = tmp_path / "202607" / "20"
    day_dir.mkdir(parents=True)
    for filename, title, score in (
        ("paper-a.md", "Paper A", "9.0"),
        ("paper-b.md", "Paper B", "7.0"),
    ):
        (day_dir / filename).write_text(
            "\n".join(
                [
                    "---",
                    f"title: {title}",
                    f"score: {score}",
                    "---",
                    "",
                    "## Abstract",
                    "Abstract body.",
                ]
            ),
            encoding="utf-8",
        )

    out_path = mod.write_day_meta_index_json(
        docs_dir=str(tmp_path),
        date_str="20260720",
        date_label=None,
        deep_list=[],
        quick_list=[],
        merged_deep_entries=[("202607/20/paper-a", "Paper A", [])],
        merged_quick_entries=[("202607/20/paper-b", "Paper B", [])],
    )

    payload = json.loads(Path(out_path).read_text(encoding="utf-8"))
    assert payload["count"] == 2
    assert [(item["paper_id"], item["section"]) for item in payload["papers"]] == [
        ("202607/20/paper-a", "deep"),
        ("202607/20/paper-b", "quick"),
    ]


def test_merge_daily_run_results_updates_one_persistent_day_state(tmp_path):
    mod = _load_module()
    first_paper = {
        "id": "paper-a",
        "title": "Paper A",
        "llm_score": 7.0,
        "llm_tags": ["query:rl"],
    }
    _, _, first_route = mod.prepare_paper_paths(
        str(tmp_path), "20260720", first_paper["title"], first_paper["id"]
    )
    first_entries = [
        (first_route, first_paper["title"], mod.extract_sidebar_tags(first_paper))
    ]

    first = mod.merge_daily_run_results(
        docs_dir=str(tmp_path),
        date_str="20260720",
        date_label="2026-07-20",
        generated_at="2026-07-20 09:00:00 UTC",
        recommend_exists=True,
        deep_list=[],
        quick_list=[first_paper],
        deep_entries=[],
        quick_entries=first_entries,
        evidence_by_route={},
    )

    promoted = dict(first_paper, llm_score=9.0, llm_tags=["query:agents"])
    second_paper = {
        "id": "paper-b",
        "title": "Paper B",
        "llm_score": 8.0,
        "llm_tags": ["query:systems"],
    }
    _, _, second_route = mod.prepare_paper_paths(
        str(tmp_path), "20260720", second_paper["title"], second_paper["id"]
    )
    deep_entries = [
        (first_route, promoted["title"], mod.extract_sidebar_tags(promoted)),
        (second_route, second_paper["title"], mod.extract_sidebar_tags(second_paper)),
    ]
    second = mod.merge_daily_run_results(
        docs_dir=str(tmp_path),
        date_str="20260720",
        date_label="2026-07-20",
        generated_at="2026-07-20 10:00:00 UTC",
        recommend_exists=True,
        deep_list=[promoted, second_paper],
        quick_list=[],
        deep_entries=deep_entries,
        quick_entries=[],
        evidence_by_route={},
    )

    state, merged_deep, merged_quick, _evidence, state_path = second
    assert first[0]["run_count"] == 1
    assert state["run_count"] == 2
    assert [item["paper_id"] for item in state["papers"]] == ["paper-a", "paper-b"]
    assert [route for route, _title, _tags in merged_deep] == [first_route, second_route]
    assert merged_quick == []
    assert Path(state_path).name == "_daily_state.json"
