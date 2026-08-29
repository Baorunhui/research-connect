import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from citationclaw.core.self_citation import SelfCitationDetector


def test_exact_match():
    detector = SelfCitationDetector()
    target = [{"name": "Alice Smith", "affiliation": "MIT"}]
    citing = [{"name": "Alice Smith"}, {"name": "Bob Jones"}]
    result = detector.check(target, citing)
    assert result["is_self_citation"] is True
    assert result["method"] == "exact"


def test_no_match():
    detector = SelfCitationDetector()
    target = [{"name": "Alice Smith"}]
    citing = [{"name": "Bob Jones"}]
    result = detector.check(target, citing)
    assert result["is_self_citation"] is False


def test_abbreviation_match():
    """X. Wang matches Xiao-Ming Wang (same surname + same initial)."""
    detector = SelfCitationDetector()
    target = [{"name": "Xiao-Ming Wang"}]
    citing = [{"name": "X. Wang"}]
    result = detector.check(target, citing)
    assert result["is_self_citation"] is True
    assert result["method"] == "surname_initial"


def test_same_surname_different_initial():
    """Michael Yu should NOT match Yi Yu (different initial)."""
    detector = SelfCitationDetector()
    target = [{"name": "Yi Yu"}]
    citing = [{"name": "Michael Yu"}]
    result = detector.check(target, citing)
    assert result["is_self_citation"] is False


def test_name_reversal():
    """Yang Xue = Xue Yang (word order reversed)."""
    detector = SelfCitationDetector()
    target = [{"name": "Xue Yang"}]
    citing = [{"name": "Yang Xue"}]
    result = detector.check(target, citing)
    assert result["is_self_citation"] is True


def test_chinese_exact():
    """Chinese exact name match."""
    detector = SelfCitationDetector()
    target = [{"name": "杨雪"}]
    citing = [{"name": "杨雪"}]
    result = detector.check(target, citing)
    assert result["is_self_citation"] is True


def test_chinese_same_surname_different_name():
    """王晓明 vs 王小红: same surname 王 but different given name initial."""
    detector = SelfCitationDetector()
    target = [{"name": "王晓明"}]
    citing = [{"name": "王小红"}]
    result = detector.check(target, citing)
    # 晓 ≠ 小 (different chars), should NOT match
    assert result["is_self_citation"] is False


def test_chinese_same_surname_same_initial():
    """王晓明 vs 王晓: same surname + same given initial."""
    detector = SelfCitationDetector()
    target = [{"name": "王晓明"}]
    citing = [{"name": "王晓"}]
    result = detector.check(target, citing)
    assert result["is_self_citation"] is True


def test_different_surname_same_affiliation():
    detector = SelfCitationDetector()
    target = [{"name": "Alice Smith"}]
    citing = [{"name": "Bob Jones"}]
    result = detector.check(target, citing)
    assert result["is_self_citation"] is False


def test_empty_lists():
    detector = SelfCitationDetector()
    result = detector.check([], [])
    assert result["is_self_citation"] is False
