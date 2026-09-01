import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from citationclaw.core.s2_client import S2Client

def test_build_search_url():
    client = S2Client()
    url = client._build_search_url("Attention is All You Need")
    assert "semanticscholar.org" in url
    assert "query" in url or "search" in url

def test_parse_paper_response():
    client = S2Client()
    mock_response = {
        "paperId": "P123",
        "title": "Attention is All You Need",
        "year": 2017,
        "citationCount": 100000,
        "influentialCitationCount": 5000,
        "authors": [
            {
                "authorId": "A1",
                "name": "Ashish Vaswani",
            }
        ],
        "externalIds": {"DOI": "10.xxxx"},
        "isOpenAccess": True,
        "openAccessPdf": {"url": "https://arxiv.org/pdf/1706.03762"},
    }
    result = client._parse_paper(mock_response)
    assert result["title"] == "Attention is All You Need"
    assert result["authors"][0]["name"] == "Ashish Vaswani"
    assert result["influential_citation_count"] == 5000
    assert result["source"] == "s2"

def test_parse_author_response():
    client = S2Client()
    mock_author = {
        "authorId": "A1",
        "name": "Ashish Vaswani",
        "hIndex": 30,
        "citationCount": 200000,
        "affiliations": ["Google Brain"],
    }
    result = client._parse_author(mock_author)
    assert result["name"] == "Ashish Vaswani"
    assert result["h_index"] == 30
    assert result["citation_count"] == 200000
    assert result["affiliation"] == "Google Brain"


def test_get_author_papers_preserves_s2_identity_and_authors():
    client = S2Client(api_key="test-key")
    client._rate_delay = 0
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "data": [{
            "paperId": "P123",
            "title": "Known Paper",
            "year": 2025,
            "citationCount": 12,
            "authors": [{"authorId": "A1", "name": "Alice"}],
            "externalIds": {"ArXiv": "2501.00001"},
        }]
    }
    client._client.get = AsyncMock(return_value=response)

    papers = asyncio.get_event_loop().run_until_complete(
        client.get_author_papers("A1", max_papers=100)
    )

    assert papers == [{
        "title": "Known Paper",
        "year": 2025,
        "citations": 12,
        "s2_id": "P123",
        "authors": [{"name": "Alice", "s2_id": "A1", "affiliation": ""}],
        "arxiv_id": "2501.00001",
    }]
