import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from citationclaw.core.pdf_mineru_parser import MinerUParser


def test_extract_references():
    text = "Some text\n\nReferences\n[1] Paper A\n[2] Paper B"
    refs = MinerUParser._extract_references(text)
    assert "[1] Paper A" in refs
    assert "[2] Paper B" in refs


def test_extract_references_not_found():
    assert MinerUParser._extract_references("No refs here") == ""


def test_md_to_first_page():
    text = "Title\nAuthor Name\nUniversity\n\nAbstract text"
    blocks = MinerUParser._md_to_first_page(text)
    assert len(blocks) >= 3
    assert blocks[0]["text"] == "Title"
    assert blocks[0]["page_idx"] == 0


def test_paper_key():
    parser = MinerUParser()
    k1 = parser.paper_key({"doi": "10.1234/test"})
    k2 = parser.paper_key({"doi": "10.1234/test"})
    k3 = parser.paper_key({"doi": "10.5678/other"})
    assert k1 == k2  # same DOI -> same key
    assert k1 != k3  # different DOI -> different key
