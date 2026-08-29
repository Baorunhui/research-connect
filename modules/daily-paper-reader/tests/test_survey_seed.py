"""survey_seed 单元测试：id 提取、Atom 解析、引文交叉、LLM 分析（mock）。"""

import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load_module():
    spec = importlib.util.spec_from_file_location("survey_seed_mod", SRC / "survey_seed.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["survey_seed_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2302.01881v2</id>
    <title>IKEA-Manual: Seeing Shape Assembly Step by Step</title>
    <summary> We introduce a real-world dataset... </summary>
    <published>2023-02-04T18:57:40Z</published>
    <author><name>Ruocheng Wang</name></author>
    <author><name>Yun-Chun Chen</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2411.18011v1</id>
    <title>Manual-PA</title>
    <summary>Learning 3D Part Assembly from Instruction Diagrams.</summary>
    <published>2024-11-27T00:00:00Z</published>
    <author><name>Someone</name></author>
  </entry>
</feed>
"""


class ExtractArxivIdTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_various_forms(self):
        f = self.mod.extract_arxiv_id
        self.assertEqual(f("https://arxiv.org/abs/2302.01881"), "2302.01881")
        self.assertEqual(f("https://arxiv.org/pdf/2411.18011v2"), "2411.18011")
        self.assertEqual(f("arXiv:2302.01881v1"), "2302.01881")
        self.assertEqual(f("2302.01881"), "2302.01881")
        self.assertIsNone(f("https://example.com/paper"))
        self.assertIsNone(f(""))


class AtomParsingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_parse_entries(self):
        entries = self.mod._parse_atom_entries(_ATOM_XML)
        self.assertEqual(len(entries), 2)
        first = entries[0]
        self.assertEqual(first["arxiv_id"], "2302.01881")  # 版本号剥离
        self.assertEqual(first["title"], "IKEA-Manual: Seeing Shape Assembly Step by Step")
        self.assertEqual(first["published"], "2023-02-04")
        self.assertIn("Ruocheng Wang", first["authors"])

    def test_fetch_entries_batches_and_dedups(self):
        mod = self.mod
        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured.update(params=params)
            return MagicMock(status_code=200, text=_ATOM_XML)

        with patch.object(mod._ATOM_SESSION, "get", side_effect=fake_get):
            entries = mod._fetch_arxiv_atom_entries(["2302.01881", "2411.18011"])
        self.assertEqual(len(entries), 2)
        self.assertEqual(captured["params"]["id_list"], "2302.01881,2411.18011")


class CitationMergeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_regex_ids_from_text(self):
        text = "Built on [12] (arXiv:2302.01881) and 2411.18011v3, plus noise 9999.999999."
        ids = self.mod._extract_cited_ids_from_text(text)
        self.assertIn("2302.01881", ids)
        self.assertIn("2411.18011", ids)

    def test_analyze_seed_merges_llm_and_regex_ids(self):
        mod = self.mod

        def fake_chat_structured(client, system, user, schema_name, schema):
            return {
                "task_definition": "输入多页分步示意图，输出零件位姿序列",
                "input_boundary": "聚焦分步示意图，不含 CAD 图纸",
                "target_paradigm": "instruction-diagram driven 3D part assembly",
                "queries": ["part assembly from instruction diagrams", "  ", "assembly pose estimation"],
                "cited_arxiv_ids": ["2302.01881v2", "garbage"],
                "non_arxiv_refs": [{"title": "Classic CAD Paper", "venue": "SIGGRAPH", "year": "2015"}],
                "dataset_names": ["IKEA-Manual"],
            }

        seed_text = {"text": "see also 2411.18011 and 2302.01881 again", "title": "T", "arxiv_id": "2411.18011"}
        original = sys.modules.get("survey_pipeline")
        fake_pipeline = MagicMock()
        fake_pipeline._chat_structured = staticmethod(fake_chat_structured)
        sys.modules["survey_pipeline"] = fake_pipeline
        try:
            result = mod.analyze_seed(seed_text, lambda: None)
        finally:
            if original is not None:
                sys.modules["survey_pipeline"] = original
        self.assertIsNotNone(result)
        self.assertEqual(result["queries"], ["part assembly from instruction diagrams", "assembly pose estimation"])
        # LLM 列举 ∪ 正则：v 后缀剥离、garbage 剔除、去重
        self.assertIn("2302.01881", result["cited_arxiv_ids"])
        self.assertIn("2411.18011", result["cited_arxiv_ids"])
        self.assertNotIn("garbage", result["cited_arxiv_ids"])
        self.assertEqual(len([i for i in result["cited_arxiv_ids"] if i == "2302.01881"]), 1)


class FetchCitationPapersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_maps_entries_to_paper_dicts(self):
        mod = self.mod
        with patch.object(mod._ATOM_SESSION, "get", return_value=MagicMock(status_code=200, text=_ATOM_XML)):
            papers = mod.fetch_citation_papers(["2302.01881", "2411.18011"])
        self.assertEqual(len(papers), 2)
        p = papers[0]
        self.assertEqual(p["source"], "seed_citation")
        self.assertEqual(p["paper_id"], "2302.01881")
        self.assertEqual(p["pdf_url"], "https://arxiv.org/pdf/2302.01881")
        self.assertIn("IKEA", p["title"])


class FetchSeedTextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_pdf_text_passthrough(self):
        mod = self.mod
        result = mod.fetch_seed_text({"text": "全文内容" * 100, "title": "My PDF"})
        self.assertEqual(result["title"], "My PDF")
        self.assertTrue(result["text"].startswith("全文内容"))

    def test_no_id_raises(self):
        with self.assertRaises(ValueError):
            self.mod.fetch_seed_text({"url": "https://example.com/x"})

    def test_atom_fallback_when_deepxiv_fails(self):
        mod = self.mod

        class BrokenDeepxiv:
            def get_paper_markdown(self, arxiv_id):
                raise mod.DeepXivError("boom", 500)  # type: ignore[attr-defined]

            def get_paper_meta(self, arxiv_id):
                raise RuntimeError("unreachable")

        # DeepXivError 在 deepxiv_client 模块中定义，survey_seed 导入了它
        from deepxiv_client import DeepXivError  # noqa: E402

        class Broken:
            def get_paper_markdown(self, arxiv_id):
                raise DeepXivError("boom", 500)

            def get_paper_meta(self, arxiv_id):
                raise RuntimeError("unreachable")

        with patch.object(mod.requests, "get", return_value=MagicMock(status_code=200, text=_ATOM_XML)) as _:
            result = mod.fetch_seed_text({"arxiv_id": "2302.01881"}, deepxiv=Broken())
        self.assertEqual(result["arxiv_id"], "2302.01881")
        self.assertIn("IKEA", result["text"])
        _ = BrokenDeepxiv  # noqa: F841


if __name__ == "__main__":
    unittest.main()


class YearCoercionTest(unittest.TestCase):
    """回归：LLM 回传整数年份曾导致整份种子分析被 schema 校验判废。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_int_year_coerced_to_string(self):
        mod = self.mod

        def fake_chat_structured(client, system, user, schema_name, schema):
            return {
                "task_definition": "d",
                "target_paradigm": "p",
                "queries": ["q1"],
                "cited_arxiv_ids": ["2302.01881"],
                "non_arxiv_refs": [{"title": "T", "venue": "SIGGRAPH", "year": 2015}],
                "dataset_names": ["D"],
            }

        original = sys.modules.get("survey_pipeline")
        fake_pipeline = MagicMock()
        fake_pipeline._chat_structured = staticmethod(fake_chat_structured)
        sys.modules["survey_pipeline"] = fake_pipeline
        try:
            result = mod.analyze_seed({"text": "x", "title": "t", "arxiv_id": ""}, lambda: None)
        finally:
            if original is not None:
                sys.modules["survey_pipeline"] = original
        self.assertEqual(result["non_arxiv_refs"][0]["year"], "2015")
