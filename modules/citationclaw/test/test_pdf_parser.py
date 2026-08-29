import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from citationclaw.core.pdf_parser import PDFCitationParser


def test_find_reference_id():
    parser = PDFCitationParser()
    ref_text = """
References
[1] Smith et al. Some other paper. 2020.
[2] Alice Wang, Bob Jones. Attention is All You Need. NeurIPS 2017.
[3] Another reference here.
"""
    ref_id = parser._find_reference_id(ref_text, "Attention is All You Need",
                                        [{"name": "Alice Wang"}])
    assert ref_id == "[2]"


def test_find_reference_id_not_found():
    parser = PDFCitationParser()
    ref_id = parser._find_reference_id("No references section here.",
                                        "Nonexistent Paper", [{"name": "Nobody"}])
    assert ref_id is None


def test_extract_from_text_bracket_format():
    """Test [N] citation format extraction with context."""
    parser = PDFCitationParser()
    text = """
1. Introduction

Transformers have revolutionized NLP. The seminal work [2] proposed the
attention mechanism that forms the backbone of modern language models.

2. Related Work

Several approaches build on [2] including BERT and GPT.

3. Method

Our method uses a standard transformer [1] architecture.

References

[1] Some other paper.
[2] Attention is All You Need.
"""
    contexts = parser.extract_from_text(
        text, "Attention is All You Need",
        [{"name": "Alice Wang"}], target_year=2017, context_window=0,
    )
    assert len(contexts) >= 2
    assert any("revolutionized" in c["text"] for c in contexts)
    # [1] paragraph should NOT be included
    assert not any("standard transformer" in c["text"] and c["match_type"] == "direct"
                    for c in contexts)


def test_extract_from_text_author_year_format():
    """Test (Author, Year) citation format."""
    parser = PDFCitationParser()
    text = """
1. Introduction

Vision transformers have emerged (Dosovitskiy et al., 2021).
Wang et al. (2020) pioneered momentum contrast for self-supervised learning.

References

Dosovitskiy, A. An image is worth 16x16 words. ICLR, 2021.
Wang, X. Momentum contrast for visual representation learning. CVPR, 2020.
"""
    contexts = parser.extract_from_text(
        text, "Momentum contrast for visual representation learning",
        [{"name": "Xiaolong Wang"}], target_year=2020, context_window=0,
    )
    assert len(contexts) >= 1
    assert any("momentum contrast" in c["text"].lower() for c in contexts)


def test_year_disambiguation():
    """Test that 2020a and 2020b are correctly distinguished."""
    parser = PDFCitationParser()
    text = """
1. Introduction

Wang et al. (2020a) introduced momentum contrast. Wang et al. (2020b) used distillation.

2. Related Work

The distillation approach (Wang et al., 2020b) is orthogonal to momentum contrast.

References

Wang, X. Momentum contrast for visual learning. CVPR, 2020a.
Wang, X. Knowledge distillation for efficient learning. NeurIPS, 2020b.
"""
    contexts = parser.extract_from_text(
        text, "Momentum contrast for visual learning",
        [{"name": "Xiaolong Wang"}], target_year=2020, context_window=0,
    )
    # Should match Introduction (has 2020a), NOT Related Work (only 2020b)
    assert len(contexts) >= 1
    assert any("momentum contrast" in c["text"].lower() for c in contexts)
    # Related Work paragraph with ONLY 2020b should NOT be a direct hit
    assert not any(
        c["section"] == "Related Work" and c["match_type"] == "direct"
        and "2020a" not in c["text"]
        for c in contexts
    )


def test_section_detection():
    """Test section tagging of paragraphs."""
    parser = PDFCitationParser()
    text = """
1. Introduction

This paper introduces our approach.

2. Related Work

Several prior works exist.

3. Method

We propose a new method [1].

References

[1] Target paper title here.
"""
    contexts = parser.extract_from_text(
        text, "Target paper title here",
        [{"name": "Author"}], context_window=0,
    )
    assert any(c["section"] == "Method" for c in contexts)


def test_detect_section_legacy():
    """Test legacy _detect_section method still works."""
    parser = PDFCitationParser()
    assert parser._detect_section("1. Introduction\nThis paper...") == "Introduction"
    assert parser._detect_section("Related Work\nSeveral...") == "Related Work"
