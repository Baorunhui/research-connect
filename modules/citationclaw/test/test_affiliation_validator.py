import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from citationclaw.core.affiliation_validator import AffiliationValidator


def test_pdf_overrides_api():
    v = AffiliationValidator()
    api = [{"name": "Alice Smith", "affiliation": "Google", "country": "US"}]
    pdf = [{"name": "Alice Smith", "affiliation": "MIT", "email": "alice@mit.edu"}]
    result = v.validate(api, pdf)
    assert len(result) == 1
    assert result[0]["affiliation"] == "MIT"  # PDF wins
    assert result[0]["affiliation_source"] == "pdf"
    assert result[0]["email"] == "alice@mit.edu"


def test_api_kept_when_no_pdf_match():
    v = AffiliationValidator()
    api = [{"name": "Alice", "affiliation": "Google", "country": "US"}]
    pdf = [{"name": "Bob", "affiliation": "Stanford"}]
    result = v.validate(api, pdf)
    assert result[0]["name"] == "Alice"
    assert result[0]["affiliation"] == "Google"  # API kept
    assert result[1]["name"] == "Bob"  # PDF-only appended


def test_chinese_english_name_match():
    v = AffiliationValidator()
    api = [{"name": "Deren Li", "affiliation": "", "country": "CN"}]
    pdf = [{"name": "李德仁 (Deren Li)", "affiliation": "武汉大学"}]
    result = v.validate(api, pdf)
    assert result[0]["affiliation"] == "武汉大学"  # PDF matched via English name


def test_empty_pdf():
    v = AffiliationValidator()
    api = [{"name": "Alice", "affiliation": "MIT"}]
    result = v.validate(api, [])
    assert result == api  # No change


def test_empty_api():
    v = AffiliationValidator()
    pdf = [{"name": "Alice", "affiliation": "MIT"}]
    result = v.validate([], pdf)
    assert len(result) == 1
    assert result[0]["affiliation_source"] == "pdf"
