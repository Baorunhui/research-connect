"""survey_docs 单元测试：文件名策略、报告渲染、侧栏注册幂等。"""

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location("survey_docs_mod", ROOT / "src" / "survey_docs.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["survey_docs_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def _sample_result():
    return {
        "query": "多模态大模型的安全与对齐",
        "papers": [
            {"paper_id": "2608.1", "title": "Paper One", "link": "https://arxiv.org/abs/2608.1"},
            {"paper_id": "2608.2", "title": "Paper Two", "link": ""},
        ],
        "clusters": [{"cluster_id": 0, "name_zh": "安全对齐", "paper_ids": ["2608.1"]}],
        "outline": {"title_zh": "多模态大模型安全研究综述", "sections": ["引言"]},
        "report_markdown": "# 多模态大模型安全研究综述\n\n## 引言\n\n内容 [1]\n\n## 参考文献\n\n[1] Paper One",
        "report_meta": {"generated_at": "2026-08-27 08:00 UTC", "n_papers": 2, "n_clusters": 1},
    }


class BasenameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_chinese_query_falls_back_to_hash_name(self):
        base, rid = self.mod.build_report_basename("多模态大模型的安全与对齐", "20260827")
        self.assertTrue(base.startswith("survey-"))
        self.assertEqual(rid, base.rsplit("-", 1)[-1])
        self.assertLessEqual(len((base + ".md").encode("utf-8")), 255)

    def test_english_query_keeps_readable_slug(self):
        base, _ = self.mod.build_report_basename("diffusion image generation", "20260827")
        self.assertIn("diffusion", base)
        self.assertLessEqual(len((base + ".md").encode("utf-8")), 255)

    def test_stable_for_same_input(self):
        self.assertEqual(
            self.mod.build_report_basename("same query", "20260827"),
            self.mod.build_report_basename("same query", "20260827"),
        )

    def test_long_enriched_query_fits_typical_windows_install_path(self):
        query = (
            "3D visual grounding 3DVG recent methods for localizing objects in "
            "3D point cloud scenes from natural language descriptions covering "
            "LLM based approaches reasoning pipelines zero shot open world "
            "generalization and spatial pruning"
        )
        base, report_id = self.mod.build_report_basename(query, "20260901")
        install_dir = (
            r"C:\Users\13955\Desktop\research-connect\modules\daily-paper-reader"
            r"\docs\survey"
        )

        self.assertLessEqual(len(base.encode("utf-8")), 120)
        self.assertLess(len(install_dir + "\\" + base + ".md"), 240)
        self.assertTrue(base.endswith(report_id))


class ReportMarkdownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_front_matter_and_body(self):
        md = self.mod.build_report_markdown(_sample_result())
        self.assertTrue(md.startswith("---\n"))
        self.assertIn("title: 多模态大模型安全研究综述", md)
        self.assertIn("date:", md)
        self.assertIn("query:", md)
        self.assertIn("paper_count: 2", md)
        self.assertIn("label: 综述", md)
        self.assertIn("selection_source: survey_pipeline", md)
        self.assertIn("## 参考文献", md)


class WriteReportDocsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_writes_under_survey_dir(self):
        with tempfile.TemporaryDirectory() as td:
            docs = pathlib.Path(td)
            info = self.mod.write_report_docs(docs, _sample_result(), date_token="20260827")
            md_path = pathlib.Path(info["md_path"])
            self.assertTrue(md_path.exists())
            self.assertEqual(md_path.parent.name, "survey")
            self.assertEqual(info["route"], f"survey/{info['basename']}")
            self.assertEqual(info["date"], "2026-08-27")
            content = md_path.read_text(encoding="utf-8")
            self.assertIn("多模态大模型安全研究综述", content)

    def test_sidebar_failure_keeps_generated_report(self):
        with tempfile.TemporaryDirectory() as td:
            docs = pathlib.Path(td) / "docs"
            sidebar = docs / "_sidebar.md"
            with mock.patch.object(
                self.mod,
                "update_sidebar_with_survey",
                side_effect=PermissionError("sidebar is busy"),
            ):
                info = self.mod.persist_survey_report(
                    _sample_result(), docs_dir=docs, sidebar_path=sidebar
                )

            self.assertFalse(info["registered"])
            self.assertIn("PermissionError: sidebar is busy", info["registration_error"])
            self.assertTrue(pathlib.Path(info["md_path"]).exists())


class SidebarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _sidebar_path(self, td):
        path = pathlib.Path(td) / "_sidebar.md"
        path.write_text(
            "* [首页](#/)\n\n* Conference Papers\n\n* Daily Papers\n  * 2026-08-26  <!--dpr-date:20260826-->\n",
            encoding="utf-8",
        )
        return path

    def test_registers_block_and_heading_before_daily(self):
        mod = self.mod
        with tempfile.TemporaryDirectory() as td:
            path = self._sidebar_path(td)
            info = mod.write_report_docs(pathlib.Path(td), _sample_result(), date_token="20260827")
            mod.update_sidebar_with_survey(path, info)
            text = path.read_text(encoding="utf-8")
            self.assertIn("* Survey Reports", text)
            self.assertIn(f"<!--dpr-survey:{info['report_id']}-->", text)
            self.assertIn(f'href="#/{info["route"]}"', text)
            # 标题插在 Daily Papers 之前、会议分组之后
            self.assertLess(text.index("* Survey Reports"), text.index("* Daily Papers"))
            self.assertGreater(text.index("* Survey Reports"), text.index("* Conference Papers"))

    def test_idempotent_same_report(self):
        mod = self.mod
        with tempfile.TemporaryDirectory() as td:
            path = self._sidebar_path(td)
            info = mod.write_report_docs(pathlib.Path(td), _sample_result(), date_token="20260827")
            mod.update_sidebar_with_survey(path, info)
            mod.update_sidebar_with_survey(path, info)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count(f"<!--dpr-survey:{info['report_id']}-->"), 1)

    def test_multiple_reports_newest_after_heading(self):
        mod = self.mod
        with tempfile.TemporaryDirectory() as td:
            path = self._sidebar_path(td)
            first = mod.write_report_docs(pathlib.Path(td), _sample_result(), date_token="20260826")
            second = mod.write_report_docs(pathlib.Path(td), {**_sample_result(), "query": "另一个主题"}, date_token="20260827")
            mod.update_sidebar_with_survey(path, first)
            mod.update_sidebar_with_survey(path, second)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("<!--dpr-survey:"), 2)
            # 新报告紧跟标题（最新在前）
            heading_idx = text.index("* Survey Reports")
            self.assertLess(text.index(f"<!--dpr-survey:{second['report_id']}-->"), text.index(f"<!--dpr-survey:{first['report_id']}-->"))
            self.assertLess(heading_idx, text.index(f"<!--dpr-survey:{second['report_id']}-->"))

    def test_remove_survey_block_isolated(self):
        mod = self.mod
        with tempfile.TemporaryDirectory() as td:
            path = self._sidebar_path(td)
            info = mod.write_report_docs(pathlib.Path(td), _sample_result(), date_token="20260827")
            mod.update_sidebar_with_survey(path, info)
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            mod.remove_survey_block(lines, info["report_id"])
            text = "".join(lines)
            self.assertNotIn(f"<!--dpr-survey:{info['report_id']}-->", text)
            self.assertIn("* Survey Reports", text)  # 标题保留
            self.assertIn("* Daily Papers", text)


if __name__ == "__main__":
    unittest.main()
