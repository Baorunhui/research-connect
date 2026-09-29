import json
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from citationclaw.core.honor_list import (
    HonorList, _Builder, affiliation_match, email_matches_affiliation, name_key,
)
from citationclaw.core.scholar_fast_path import LocalCitingIndex, build_fast_report


def _honors(tmp_path):
    db = tmp_path / "honors.sqlite3"
    b = _Builder(db)
    b.add(name="Wei Wang", honor="ACM Fellow", source="wikidata",
          affiliation="University of California, Los Angeles")
    b.add(name="", name_zh="王伟", honor="长江学者", source="changjiang", affiliation_zh="华中科技大学")
    b.add(name="Jane Roe", honor="IEEE Fellow", source="test", email="jane@example.edu")
    b.finish({})
    return HonorList(db)


def test_name_key_is_order_and_initial_insensitive():
    assert name_key("Kai-Ming He") == name_key("He, Kaiming") == "he kaiming"
    assert name_key("A. Smith") == ""


def test_affiliation_rules():
    assert affiliation_match("Dept. of CS, Tsinghua Univ., Beijing", "Tsinghua University")
    assert affiliation_match("清华大学计算机系", "Tsinghua University")
    assert affiliation_match("USTC", "University of Science and Technology of China")
    assert not affiliation_match("Peking University", "Peking Union Medical College")
    assert not affiliation_match("Beijing Institute of Technology", "Tsinghua University, Beijing")
    assert email_matches_affiliation("x@cs.ucla.edu", "University of California, Los Angeles")
    assert not email_matches_affiliation("x@gmail.com", "University of California, Los Angeles")


def test_same_name_different_affiliation_is_not_a_hit(tmp_path):
    h = _honors(tmp_path)
    assert h.match("Wei Wang", "UCLA")["hits"][0]["basis"] == "单位"
    assert h.match("Wei Wang", "", "w@cs.ucla.edu")["hits"][0]["basis"] == "邮箱域名"
    miss = h.match("Wei Wang", "Harbin Engineering University")
    assert miss["hits"] == [] and miss["name_only"] == 2
    zh = h.match("Wei Wang", "Huazhong University of Science and Technology")
    assert [x["honor"] for x in zh["hits"]] == ["长江学者"]
    assert h.match("Jane Roe", "", "jane@example.edu")["hits"][0]["basis"] == "邮箱"
    assert h.match("Wei Wang", "")["hits"] == []


def test_fast_report_uses_only_local_records(tmp_path):
    h = _honors(tmp_path)
    phase1 = tmp_path / "data" / "cache" / "openalex_phase1"
    phase1.mkdir(parents=True)
    (phase1 / "a.json").write_text(json.dumps({
        "title": "Target Paper On Graphs",
        "works": [
            {"title": "Citing One", "authors": [{"name": "Wei Wang", "affiliation": "UCLA"}]},
            {"title": "Citing Two", "authors": [{"name": "Wei Wang", "affiliation": "Harbin Engineering University"}]},
            {"title": "Self Cite", "authors": [{"name": "Jane Doe", "affiliation": "X"}]},
        ],
    }), encoding="utf-8")
    index = LocalCitingIndex([tmp_path / "data"]).load()
    report = build_fast_report(
        [{"title": "Target paper on graphs", "citations": 12000}, {"title": "Unknown", "citations": 5}],
        "Jane Doe", h, index, kaggle=None, log=lambda _m: None)
    cov = report["coverage"]
    assert report["mode"] == "fast_metadata_honor_match"
    assert cov["targets_with_local_citing_data"] == 1
    assert cov["local_citing_papers"] == 2 and cov["self_citations_skipped"] == 1
    assert [c["name"] for c in report["honor_citers"]] == ["Wei Wang"]
    assert report["honor_citers"][0]["citing_papers"] == ["Citing One"]
