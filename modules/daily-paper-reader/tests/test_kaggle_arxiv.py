"""kaggle_arxiv 单元测试：FTS 查询清洗 / 快照行解析 / 小样本建库检索（真 SQLite）/ 认证形态。
全部本地（tmp 目录 + mock requests），不出网、不依赖真实快照。"""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location("kaggle_arxiv_mod", ROOT / "src" / "kaggle_arxiv.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["kaggle_arxiv_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def _sample_line(pid, title, abstract, categories="cs.CV", created="Sat, 02 Jan 2021 00:00:00 GMT"):
    return json.dumps(
        {
            "id": pid,
            "title": title,
            "abstract": abstract,
            "authors": "Alice et al.",
            "categories": categories,
            "versions": [{"version": 1, "created": created}],
            "update_date": "2021-01-02",
        }
    )


class QueryToFtsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_stops_and_syntax_isolated(self):
        and_q, or_q = self.mod._query_to_fts("The robot's assembly: (deep) learning-based manipulation!")
        self.assertNotIn("the", and_q.lower())
        self.assertNotIn("(", and_q)
        self.assertNotIn(")", and_q)
        self.assertNotIn("!", and_q)
        self.assertTrue(and_q)  # 仍有内容词
        for term in ("robot", "assembly", "manipulation"):
            self.assertIn(f'"{term}"', and_q)

    def test_and_or_forms(self):
        and_q, or_q = self.mod._query_to_fts("vision language navigation")
        self.assertEqual(and_q, '"vision" "language" "navigation"')
        self.assertEqual(or_q, '"vision" OR "language" OR "navigation"')

    def test_empty_query_returns_empty(self):
        self.assertEqual(self.mod._query_to_fts("the a of"), ("", ""))

    def test_long_query_truncated(self):
        words = " ".join(f"w{i}" for i in range(40))
        _, or_q = self.mod._query_to_fts(words)
        self.assertLessEqual(len(or_q.split(" OR ")), self.mod._FTS_OR_MAX_TERMS)

    def test_short_tokens_dropped(self):
        and_q, _ = self.mod._query_to_fts("3d pose a to")
        self.assertNotIn('"a"', and_q)
        self.assertNotIn('"to"', and_q)


class ParseRowsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_parse_published(self):
        self.assertEqual(self.mod._parse_published([{"created": "Sat, 02 Jan 2021 00:00:00 GMT"}]), "2021-01-02")
        self.assertEqual(self.mod._parse_published([]), "")
        self.assertEqual(self.mod._parse_published([{"created": "garbage"}]), "")
        self.assertEqual(self.mod._parse_published(None), "")

    def test_iter_snapshot_rows_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "snap.json"
            p.write_text(
                "\n".join(
                    [
                        _sample_line("2101.00001", "Paper One", "Abstract about assembly."),
                        "{ broken json",
                        json.dumps({"id": "", "title": "no id"}),
                        json.dumps({"id": "2101.00002", "title": "", "abstract": "x"}),
                        _sample_line("2101.00003", "Paper Three", "Another.", categories="cs.RO cs.AI"),
                    ]
                ),
                encoding="utf-8",
            )
            rows = list(self.mod.iter_snapshot_rows(p))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "2101.00001")
        self.assertEqual(rows[0][5], "2021-01-02")  # published
        self.assertEqual(rows[1][4], "cs.RO cs.AI")  # categories


class BuildAndSearchTest(unittest.TestCase):
    """小样本真建库 → 双查询/日期/类别过滤/top_k 截断（验证 SQL 与输出契约）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.json_path = pathlib.Path(cls.tmp.name) / "snap.json"
        cls.db_path = pathlib.Path(cls.tmp.name) / "index.sqlite3"
        lines = [
            _sample_line("2101.00001", "Furniture assembly planning", "We study assembly of chairs with robots."),
            _sample_line("2102.00002", "Language navigation for robots", "Vision language navigation in houses."),
            _sample_line(
                "1901.00003",
                "Old assembly paper",
                "Assembly sequencing from the nineties.",
                created="Mon, 01 Jan 2001 00:00:00 GMT",
            ),
            _sample_line("2103.00004", "Diffusion image generation", "Unrelated topic on diffusion models.", categories="cs.LG"),
        ]
        cls.json_path.write_text("\n".join(lines), encoding="utf-8")
        cls.mod.build_index(cls.json_path, cls.db_path, log=lambda *_: None)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_single_term_ladder_hits(self):
        """中英混排主题只提出孤词（如 6D位姿估计→'6d'）时，末级单词条阶梯兜底命中。"""
        with tempfile.TemporaryDirectory() as tmp:
            json_path = pathlib.Path(tmp) / "snap.json"
            db_path = pathlib.Path(tmp) / "index.sqlite3"
            lines = [
                _sample_line("2101.00001", "6D Pose Estimation Revisited", "We estimate 6d object pose."),
                _sample_line("2101.00002", "Unrelated NLP work", "Document retrieval embeddings."),
            ]
            json_path.write_text("\n".join(lines), encoding="utf-8")
            self.mod.build_index(json_path, db_path, log=lambda *_: None)
            with self.mod.KaggleArxivIndex(db_path) as index:
                hits = index.search("6D位姿估计", top_k=5)
        self.assertEqual([h["paper_id"] for h in hits], ["2101.00001"])

    def test_pure_chinese_returns_empty(self):
        with self.mod.KaggleArxivIndex(self.db_path) as index:
            self.assertEqual(index.search("位姿估计方法", top_k=5), [])

    def test_meta_recorded(self):
        with self.mod.KaggleArxivIndex(self.db_path) as index:
            meta = index.meta()
            self.assertEqual(meta.get("row_count"), "4")
            self.assertEqual(index.count(), 4)

    def test_and_search_hits(self):
        with self.mod.KaggleArxivIndex(self.db_path) as index:
            hits = index.search("furniture assembly planning", top_k=10)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["paper_id"], "2101.00001")
        paper = hits[0]
        self.assertEqual(paper["source"], "kaggle")
        self.assertEqual(paper["citation_count"], 0)
        self.assertEqual(paper["link"], "https://arxiv.org/abs/2101.00001")
        self.assertEqual(paper["pdf_url"], "https://arxiv.org/pdf/2101.00001")
        self.assertEqual(paper["published"], "2021-01-02")
        self.assertEqual(paper["authors"], ["Alice et al."])
        self.assertIn("bm25_score", paper)

    def test_widening_ladder_finds_sparse_hits(self):
        # 一串不共现于任何单篇的长查询：12 词 AND 命中 0 → 逐级放宽到更少词命中
        with self.mod.KaggleArxivIndex(self.db_path) as index:
            hits = index.search("furniture navigation diffusion assembly", top_k=10)
        self.assertTrue(hits)

    def test_date_filter(self):
        with self.mod.KaggleArxivIndex(self.db_path) as index:
            hits = index.search("assembly sequencing", top_k=10, date_start="2020-01-01")
        ids = {h["paper_id"] for h in hits}
        self.assertNotIn("1901.00003", ids)
        with self.mod.KaggleArxivIndex(self.db_path) as index:
            hits_all = index.search("assembly sequencing", top_k=10)
        self.assertIn("1901.00003", {h["paper_id"] for h in hits_all})

    def test_category_filter(self):
        with self.mod.KaggleArxivIndex(self.db_path) as index:
            hits = index.search("diffusion models generation", top_k=10, categories=["cs.CV"])
        self.assertEqual({h["paper_id"] for h in hits}, set())

    def test_top_k_truncates(self):
        with self.mod.KaggleArxivIndex(self.db_path) as index:
            hits = index.search("robot navigation language vision", top_k=1)
        self.assertEqual(len(hits), 1)

    def test_is_ready(self):
        with patch.dict(self.mod.os.environ, {"DPR_SURVEY_KAGGLE_INDEX": str(pathlib.Path(self.tmp.name) / "none.sqlite3")}):
            ready, reason = self.mod.is_kaggle_ready()
            self.assertFalse(ready)
            self.assertIn("build_kaggle_arxiv_index", reason)
        with patch.dict(self.mod.os.environ, {"DPR_SURVEY_KAGGLE_INDEX": str(self.db_path)}):
            self.assertEqual(self.mod.is_kaggle_ready()[0], True)


class AuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_bearer_token_preferred(self):
        with patch.dict(self.mod.os.environ, {"KAGGLE_API_TOKEN": "KGAT_x", "KAGGLE_USERNAME": "u", "KAGGLE_KEY": "k"}):
            headers, basic = self.mod._kaggle_auth()
            self.assertEqual(headers, {"Authorization": "Bearer KGAT_x"})
            self.assertIsNone(basic)

    def test_basic_fallback(self):
        env = {k: "" for k in ("KAGGLE_API_TOKEN",)}
        env.update({"KAGGLE_USERNAME": "u", "KAGGLE_KEY": "k"})
        with patch.dict(self.mod.os.environ, env):
            self.mod.os.environ.pop("KAGGLE_API_TOKEN", None)
            headers, basic = self.mod._kaggle_auth()
            self.assertEqual(headers, {})
            self.assertEqual(basic, ("u", "k"))

    def test_missing_credentials_raises(self):
        with patch.dict(self.mod.os.environ, {}):
            for key in ("KAGGLE_API_TOKEN", "KAGGLE_USERNAME", "KAGGLE_KEY"):
                self.mod.os.environ.pop(key, None)
            with self.assertRaises(self.mod.KaggleArxivError) as cm:
                self.mod._kaggle_auth()
            self.assertIn("KAGGLE_API_TOKEN", str(cm.exception))


class DownloadSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_unauthorized_raises(self):
        mod = self.mod
        resp = MagicMock()
        resp.status_code = 401
        session = MagicMock()
        session.get.return_value = resp
        with patch.dict(mod.os.environ, {"KAGGLE_API_TOKEN": "KGAT_bad"}), \
                patch.object(mod.requests, "Session", return_value=session):
            with self.assertRaises(mod.KaggleArxivError) as cm:
                mod.download_snapshot(pathlib.Path(tempfile.mkdtemp()))
            self.assertIn("401", str(cm.exception))

    def test_existing_snapshot_reused(self):
        mod = self.mod
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / mod.SNAPSHOT_JSON_NAME
            p.write_bytes(b"x" * (2 * 1024 * 1024))
            result = mod.download_snapshot(pathlib.Path(tmp), log=lambda *_: None)
        self.assertEqual(result, p)


if __name__ == "__main__":
    unittest.main()
