"""Unit tests for _compute_institution_stats."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from citationclaw.core.dashboard_generator import DashboardGenerator


def _make_gen():
    gen = DashboardGenerator.__new__(DashboardGenerator)
    gen.log = lambda *a: None
    return gen


def test_google_matched_via_institution():
    gen = _make_gen()
    papers = [{"title": "Paper A", "institution": "Google Brain", "author_affiliation": ""}]
    result = gen._compute_institution_stats(papers)
    assert "国际科技企业" in result
    names = [name for name, _ in result["国际科技企业"]]
    assert "Google" in names


def test_huawei_matched_via_author_affiliation():
    """Matches individual author affiliations (parsed line-by-line)."""
    gen = _make_gen()
    # Huawei in first author institution → matched
    papers = [{"title": "Paper B", "institution": "Huawei Noah's Ark Lab", "author_affiliation": ""}]
    result = gen._compute_institution_stats(papers)
    assert "国内科技企业" in result
    names = [name for name, _ in result["国内科技企业"]]
    assert "华为" in names

    # Huawei in co-author affiliation (structured: name\naffil\nname\naffil)
    papers2 = [{"title": "Paper B2", "institution": "Unknown University",
                "author_affiliation": "Alice\nUnknown University\nBob\nHuawei Noah's Ark Lab"}]
    result2 = gen._compute_institution_stats(papers2)
    assert "国内科技企业" in result2

    # 未知机构 should not match anything
    papers3 = [{"title": "Paper B3", "institution": "", "author_affiliation": "Alice\n未知机构"}]
    result3 = gen._compute_institution_stats(papers3)
    assert result3 == {}


def test_deduplication_same_paper():
    gen = _make_gen()
    papers = [
        {"title": "Paper C", "institution": "OpenAI", "author_affiliation": ""},
        {"title": "Paper C", "institution": "OpenAI", "author_affiliation": ""},
    ]
    result = gen._compute_institution_stats(papers)
    entries = dict(result.get("国际科技企业", []))
    assert len(entries.get("OpenAI", [])) == 1


def test_sorted_by_paper_count():
    gen = _make_gen()
    papers = [
        {"title": "P1", "institution": "Stanford University", "author_affiliation": ""},
        {"title": "P2", "institution": "MIT",                 "author_affiliation": ""},
        {"title": "P3", "institution": "MIT",                 "author_affiliation": ""},
    ]
    result = gen._compute_institution_stats(papers)
    entries = result.get("海外顶尖高校", [])
    assert entries[0][0] == "MIT"
    assert entries[1][0] == "Stanford"


def test_empty_fields():
    gen = _make_gen()
    papers = [{"title": "Paper X", "institution": "", "author_affiliation": ""}]
    result = gen._compute_institution_stats(papers)
    for cat_entries in result.values():
        assert len(cat_entries) == 0


def test_no_match_returns_empty_dict():
    gen = _make_gen()
    papers = [{"title": "P", "institution": "Unknown University", "author_affiliation": ""}]
    result = gen._compute_institution_stats(papers)
    assert result == {}


def test_category_order():
    gen = _make_gen()
    papers = [
        {"title": "P1", "institution": "Tsinghua University", "author_affiliation": ""},
        {"title": "P2", "institution": "Google",              "author_affiliation": ""},
    ]
    result = gen._compute_institution_stats(papers)
    keys = list(result.keys())
    assert keys.index("国际科技企业") < keys.index("国内顶尖高校/机构")
