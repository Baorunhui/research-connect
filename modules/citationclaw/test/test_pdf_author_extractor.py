import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from citationclaw.core.pdf_author_extractor import PDFAuthorExtractor


def test_parse_response_valid():
    ext = PDFAuthorExtractor()
    text = '[{"name": "Alice Smith", "affiliation": "MIT", "email": "alice@mit.edu"}]'
    result = ext._parse_response(text)
    assert len(result) == 1
    assert result[0]["name"] == "Alice Smith"
    assert result[0]["affiliation"] == "MIT"


def test_parse_response_with_markdown():
    ext = PDFAuthorExtractor()
    text = '```json\n[{"name": "Bob", "affiliation": "Stanford"}]\n```'
    result = ext._parse_response(text)
    assert len(result) == 1
    assert result[0]["name"] == "Bob"


def test_parse_response_empty():
    ext = PDFAuthorExtractor()
    assert ext._parse_response("[]") == []
    assert ext._parse_response("invalid") == []


def test_extract_no_api_key():
    import asyncio
    ext = PDFAuthorExtractor()  # no api_key
    result = asyncio.get_event_loop().run_until_complete(
        ext.extract([{"text": "Alice Smith"}])
    )
    assert result == []
