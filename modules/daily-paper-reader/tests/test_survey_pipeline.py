"""survey_pipeline 单元测试：纯函数 + fake LLM 下的阶段编排。"""

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location("survey_pipeline_mod", ROOT / "src" / "survey_pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["survey_pipeline_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


class SanitizeCitationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_keeps_valid_and_drops_invalid(self):
        md, removed = self.mod.sanitize_citations("A [1] B [2,3] C [99] D [0]", 3)
        self.assertIn("[1]", md)
        self.assertIn("[2,3]", md)
        self.assertNotIn("99", md)
        self.assertNotIn("[0]", md)
        self.assertEqual(removed, 2)

    def test_range_tokens(self):
        md, removed = self.mod.sanitize_citations("[1-3] and [2-9]", 3)
        self.assertIn("[1-3]", md)
        self.assertNotIn("[2-9]", md)
        self.assertEqual(removed, 1)

    def test_non_numeric_brackets_untouched(self):
        md, removed = self.mod.sanitize_citations("[fig1] and [Table 2]", 3)
        self.assertIn("[fig1]", md)
        self.assertEqual(removed, 0)


class ClusterEmbeddingTextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_joins_extracted_fields(self):
        ext = {
            "core_problem": "P",
            "key_methodology": {"name": "M", "principle": "R"},
            "main_results": "Acc 92%",
            "contributions": ["c1", "c2"],
        }
        text = self.mod.cluster_embedding_text(ext)
        self.assertIn("Problem: P", text)
        self.assertIn("Method: M - R", text)
        self.assertIn("Results: Acc 92%", text)
        self.assertIn("Contributions: c1; c2", text)

    def test_falls_back_to_title_abstract(self):
        text = self.mod.cluster_embedding_text({"title": "T", "abstract": "A"})
        self.assertEqual(text, "T A")


class DetermineOptimalKTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_two_separated_groups(self):
        import numpy as np

        embeddings = np.array(
            [
                [0.0, 0.0],
                [0.05, 0.0],
                [10.0, 10.0],
                [10.05, 10.0],
            ],
            dtype=np.float32,
        )
        k = self.mod.determine_optimal_k(embeddings)
        self.assertIn(k, (2, 3))

    def test_tiny_input_returns_one(self):
        import numpy as np

        self.assertEqual(self.mod.determine_optimal_k(np.zeros((2, 4))), 1)


class PaperToDictTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_pdf_url_from_abs_link(self):
        class P:
            id = "2608.1"
            title = "t"
            abstract = "a"
            authors = []
            published = "2026-08-01"
            link = "https://arxiv.org/abs/2608.1"
            source = "arxiv"

        d = self.mod._paper_to_dict(P())
        self.assertEqual(d["pdf_url"], "https://arxiv.org/pdf/2608.1")
        self.assertEqual(d["paper_id"], "2608.1")


class _FakeCtx:
    def __init__(self, query="test query"):
        self.query = query
        self.warnings = []
        self.events = []
        # 对齐 _Ctx 的召回路统计 / 漏斗 / 候选画像字段（recall 与编排消费）
        self.lane_stats = {}
        self.funnel = {}
        self.candidate_profile = ""
        # 对齐 _Ctx 的种子锚定产物字段（build_outline/write_sections 消费）
        self.task_definition = ""
        self.input_boundary = ""
        self.dataset_names = []
        self.non_arxiv_refs = []

    def progress(self, stage, message, **kwargs):
        self.events.append((stage, message))

    def check_cancel(self):
        return None

    def warn(self, message):
        self.warnings.append(str(message))


class RerankPapersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _papers(self, n):
        return [{"paper_id": str(i), "title": f"T{i}", "abstract": "a"} for i in range(n)]

    def test_fallback_to_rrf_order_on_error(self):
        mod = self.mod
        ctx = _FakeCtx()

        def boom():
            raise RuntimeError("no reranker")

        mod._build_reranker = boom
        out = mod.rerank_papers(ctx, self._papers(10), max_papers=3)
        self.assertEqual([p["paper_id"] for p in out], ["0", "1", "2"])
        self.assertTrue(any("rerank" in w for w in ctx.warnings))

    def test_selects_by_index_ordered_by_score(self):
        mod = self.mod
        ctx = _FakeCtx()

        class FakeReranker:
            def rerank(self, *, query, documents, top_n, model):
                return {"results": [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.4}]}

        mod._build_reranker = lambda: (FakeReranker(), "fake-model")
        out = mod.rerank_papers(ctx, self._papers(10), max_papers=5)
        self.assertEqual([p["paper_id"] for p in out], ["2", "0"])
        self.assertEqual(out[0]["rerank_score"], 0.9)


class ExtractPapersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_merges_and_filters_by_relevance(self):
        mod = self.mod
        papers = [
            {"paper_id": "a", "title": "A", "abstract": "x"},
            {"paper_id": "b", "title": "B", "abstract": "y"},
            {"paper_id": "c", "title": "C", "abstract": "z"},
        ]

        def fake_extract_one(paper, client_factory, **kwargs):
            if paper["paper_id"] == "b":
                return {"relevance": 2.0, "core_problem": "low", "main_results": ""}
            return {
                "relevance": 9.0,
                "core_problem": "p",
                "key_methodology": {"name": "m", "principle": "r", "novelty": "n"},
                "main_results": "res",
                "limitations": "lim",
                "contributions": ["c"],
            }

        original = mod._extract_one
        mod._extract_one = fake_extract_one
        try:
            ctx = _FakeCtx()
            out = mod.extract_papers(ctx, papers, client_factory=lambda: None, concurrency=2)
        finally:
            mod._extract_one = original
        self.assertEqual([p["paper_id"] for p in out], ["a", "c"])
        self.assertEqual(out[0]["relevance"], 9.0)
        self.assertEqual(out[0]["title"], "A")  # 原始论文字段保留

    def test_paradigm_gate_drops_off_paradigm_papers(self):
        """范式门：target_paradigm 存在时，paradigm_consistency 低于门槛的跨范式论文被剔除。"""
        mod = self.mod
        papers = [
            {"paper_id": "same", "title": "Same Paradigm", "abstract": "x"},
            {"paper_id": "off", "title": "Off Paradigm", "abstract": "y"},
            {"paper_id": "degraded", "title": "Degraded", "abstract": "z"},
        ]

        def fake_extract_one(paper, client_factory, **kwargs):
            self.assertEqual(kwargs.get("target_paradigm"), "VLM grounding evaluation")
            if paper["paper_id"] == "same":
                return {"relevance": 9.0, "paradigm_consistency": 8.5,
                        "task_paradigm": "vlm grounding benchmark", "core_problem": "p", "main_results": ""}
            if paper["paper_id"] == "off":
                return {"relevance": 7.0, "paradigm_consistency": 2.0,
                        "task_paradigm": "video compression codec", "core_problem": "p", "main_results": ""}
            # 抽取失败降级：无一致性分数，不应被范式门误杀
            return {"relevance": 6.0, "core_problem": "p", "main_results": "", "_degraded": True}

        original = mod._extract_one
        mod._extract_one = fake_extract_one
        try:
            ctx = _FakeCtx()
            out = mod.extract_papers(
                ctx, papers, client_factory=lambda: None, concurrency=1,
                survey_topic="vlm grounding", target_paradigm="VLM grounding evaluation",
            )
        finally:
            mod._extract_one = original
        self.assertEqual([p["paper_id"] for p in out], ["same", "degraded"])
        self.assertTrue(any("范式" in w for w in ctx.warnings))

    def test_no_target_paradigm_skips_gate(self):
        """范式定义失败（空串）时退化为仅 relevance 过滤。"""
        mod = self.mod
        papers = [{"paper_id": "p", "title": "T", "abstract": "x"}]

        def fake_extract_one(paper, client_factory, **kwargs):
            assert kwargs.get("target_paradigm") == ""
            return {"relevance": 5.0, "paradigm_consistency": 0.0, "core_problem": "", "main_results": ""}

        original = mod._extract_one
        mod._extract_one = fake_extract_one
        try:
            ctx = _FakeCtx()
            out = mod.extract_papers(ctx, papers, client_factory=lambda: None)
        finally:
            mod._extract_one = original
        self.assertEqual(len(out), 1)


class AdaptiveRecallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_top_k_scales_with_window(self):
        mod = self.mod
        self.assertEqual(mod._adaptive_recall_top_k(9), mod.RECALL_TOP_K)
        self.assertEqual(mod._adaptive_recall_top_k(90), 350)
        self.assertEqual(mod._adaptive_recall_top_k(365), 500)

    def test_max_fetch_days_supports_three_years(self):
        self.assertEqual(self.mod.MAX_FETCH_DAYS, 1095)


class OutlineScaffoldTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_scaffold_guarantees_intro_conclusion_and_cluster_coverage(self):
        mod = self.mod
        ctx = _FakeCtx("survey topic")
        clusters = [
            {"cluster_id": 0, "paper_indices": [0], "name_zh": "方向一", "keywords": ["k"]},
            {"cluster_id": 1, "paper_indices": [1], "name_zh": "方向二", "keywords": ["k"]},
        ]

        def fake_chat_structured(client, system, user, schema_name, schema):
            return {
                "title_zh": "测试综述",
                "sections": [
                    {"heading": "方向一小节", "focus": "f", "cluster_ids": [0], "all_clusters": False},
                ],
            }

        original = mod._chat_structured
        mod._chat_structured = fake_chat_structured
        try:
            outline = mod.build_outline(ctx, global_analysis="ga", clusters=clusters, client_factory=lambda: None)
        finally:
            mod._chat_structured = original

        headings = [s["heading"] for s in outline["sections"]]
        self.assertIn("引言", headings[0])
        self.assertTrue(any("结论" in h or "展望" in h for h in headings))
        # 未覆盖的簇 1 自动补节
        self.assertIn("方向二", headings)
        self.assertEqual(outline["title_zh"], "测试综述")

    def test_default_skeleton_when_llm_fails(self):
        mod = self.mod
        ctx = _FakeCtx("topic")

        def boom(*args, **kwargs):
            raise RuntimeError("llm down")

        original = mod._chat_structured
        mod._chat_structured = boom
        try:
            clusters = [{"cluster_id": 0, "paper_indices": [0], "name_zh": "方向", "keywords": []}]
            outline = mod.build_outline(ctx, global_analysis="", clusters=clusters, client_factory=lambda: None)
        finally:
            mod._chat_structured = original
        self.assertGreaterEqual(len(outline["sections"]), 3)


class AssembleReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_reference_numbering_and_structure(self):
        mod = self.mod
        md = mod.assemble_report(
            query="q",
            title_zh="标题",
            section_markdowns=["## 第一节\n\n内容 [1]"],
            extractions=[{"title": "Paper One", "link": "https://arxiv.org/abs/1"}, {"title": "Paper Two", "link": ""}],
            clusters=[{"name_zh": "簇A", "paper_indices": [0]}],
            generated_at="2026-08-27 00:00 UTC",
        )
        self.assertIn("# 标题", md)
        self.assertIn("## 第一节", md)
        self.assertIn("## 参考文献", md)
        self.assertIn("[1] Paper One — https://arxiv.org/abs/1", md)
        self.assertIn("[2] Paper Two", md)


class WriteSectionsCitationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_invalid_citations_removed(self):
        mod = self.mod
        ctx = _FakeCtx("q")

        captured = {}

        def fake_chat_text(client, system, user, **kwargs):
            captured["user"] = user
            return "论断 [1] 以及编造 [42] 和 [2,99]。"

        original = mod._chat_text
        mod._chat_text = fake_chat_text
        try:
            extractions = [
                {"title": "A", "published": "2026-01-01", "key_methodology": {"name": "m"}, "main_results": "r", "limitations": "l"},
                {"title": "B", "published": "2026-01-02", "key_methodology": {}, "main_results": "", "limitations": ""},
            ]
            outline = {
                "title_zh": "t",
                "sections": [{"heading": "节", "focus": "f", "cluster_ids": [], "all_clusters": True}],
            }
            clusters = [{"cluster_id": 0, "paper_indices": [0, 1], "name_zh": "c", "keywords": []}]
            section_mds, sections = mod.write_sections(
                ctx,
                outline=outline,
                clusters=clusters,
                cluster_analyses=[{"cluster_id": 0, "theme": "c", "analysis": "analysis text", "keywords": []}],
                global_analysis="GA",
                extractions=extractions,
                client_factory=lambda: None,
                concurrency=1,
            )
        finally:
            mod._chat_text = original
        self.assertIn("## 节", section_mds[0])
        self.assertIn("[1]", section_mds[0])
        self.assertNotIn("42", section_mds[0])
        self.assertTrue(any("非法引用" in w for w in ctx.warnings))
        # 写作上下文必须带上论文编号资料
        self.assertIn("[1] A", captured["user"])


class RunSurveyWiringTest(unittest.TestCase):
    """编排接线：各阶段被依序调用，结果 dict 契约完整（全部阶段打桩，不出网）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_full_wiring(self):
        mod = self.mod
        calls = []

        papers = [{"paper_id": "a", "title": "Test Topic Research Survey", "abstract": "on test topic methods", "pdf_url": "", "link": ""}]
        extractions = [dict(papers[0], relevance=9.0, core_problem="p", main_results="r")]
        clusters = [{"cluster_id": 0, "paper_indices": [0], "name_zh": "方向", "keywords": ["k"]}]

        mod.plan_recall_queries = lambda ctx, factory: calls.append("plan") or ["test topic research"]

        mod.recall_papers = (
            lambda ctx, *, fetch_days, queries=None, seed_citations=None, use_deepxiv=True,
            use_kaggle=True, coarse_top_k=None: (
                calls.append("recall")
                or (self.assertEqual(queries, ["test topic research"]) if queries is not None else None)
                or papers
            )
        )
        mod.rerank_papers = lambda ctx, ps, *, max_papers: calls.append("rerank") or ps
        mod.define_task_paradigm = lambda ctx, factory, *, seed_analysis=None: calls.append("paradigm") or "target paradigm"
        mod.extract_papers = (
            lambda ctx, ps, *, client_factory, concurrency=4, survey_topic="", target_paradigm="": (
                calls.append("extract")
                or (self.assertEqual(survey_topic, "测试主题")
                    or self.assertEqual(target_paradigm, "target paradigm"))
                or extractions
            )
        )
        mod.cluster_papers = lambda ctx, es, *, client_factory: calls.append("cluster") or clusters
        mod.deep_read_core_papers = lambda ctx, cs, es, *, enabled=True, deepxiv=None: calls.append("deepread")
        mod.analyse_clusters = (
            lambda ctx, cs, es, *, client_factory, concurrency=2: calls.append("analyse")
            or ([{"cluster_id": 0, "theme": "方向", "keywords": [], "paper_count": 1, "analysis": "a"}], "GA")
        )
        mod.build_outline = (
            lambda ctx, *, global_analysis, clusters, client_factory: calls.append("outline")
            or {"title_zh": "T", "sections": [{"heading": "引言", "focus": "", "cluster_ids": [], "all_clusters": True}]}
        )
        mod.write_sections = (
            lambda ctx, **kwargs: calls.append("write")
            or (["## 引言\n\n正文 [1]"], [{"heading": "引言", "focus": "", "cluster_ids": [], "all_clusters": True}])
        )
        mod.review_draft = lambda ctx, draft, *, client_factory: calls.append("review") or (draft, [])

        progress = []
        result = mod.run_survey(
            "测试主题",
            on_progress=lambda stage, message, **kw: progress.append(stage),
            client_factory=lambda: None,
        )
        self.assertEqual(
            calls,
            ["plan", "recall", "rerank", "paradigm", "extract", "cluster", "deepread", "analyse", "outline", "write", "review"],
        )
        self.assertIn("recall", progress)
        self.assertTrue(result["report_markdown"].startswith("# T"))
        self.assertEqual(result["report_meta"]["n_papers"], 1)
        self.assertEqual(result["clusters"][0]["name_zh"], "方向")
        self.assertIn("测试主题", result["report_markdown"])

    def test_empty_query_raises(self):
        mod = self.mod
        with self.assertRaises(ValueError):
            mod.run_survey("   ")


if __name__ == "__main__":
    unittest.main()


class NormalizeArxivIdTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_strips_version(self):
        f = self.mod._normalize_arxiv_id
        self.assertEqual(f("2608.19567v4"), "2608.19567")
        self.assertEqual(f("2608.19567"), "2608.19567")
        self.assertEqual(f("openreview-icml-2025-abc"), "openreview-icml-2025-abc")
        self.assertEqual(f(""), "")


class FuseRecallPoolsTest(unittest.TestCase):
    """三路融合 + 归一化 id 去重（修 Block3D v1-v4 占多个引用位的 bug）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _paper(self, pid, title, source="local", **extra):
        return dict({"paper_id": pid, "title": title, "abstract": "a", "source": source}, **extra)

    def test_same_paper_versions_merge_to_single_entry(self):
        mod = self.mod
        local = [self._paper("2608.19567v1", "Block3D"), self._paper("2608.19567", "Block3D")]
        deepxiv = [self._paper("2608.19567v4", "Block3D", source="deepxiv", citation_count=99)]
        citations = [self._paper("2302.01881", "IKEA-Manual", source="seed_citation")]
        fused = mod.fuse_recall_pools([local, deepxiv, citations], pool_cap=50)
        ids = [p["paper_id"] for p in fused]
        self.assertEqual(len(ids), len(set(ids)), "同一论文去版本号后不得重复")
        self.assertIn("2608.19567", ids)
        self.assertIn("2302.01881", ids)
        block = next(p for p in fused if p["paper_id"] == "2608.19567")
        self.assertEqual(block["citation_count"], 99, "DeepXiv 富化字段应补进融合记录")
        self.assertIn("deepxiv", block["recall_sources"])

    def test_seed_citation_lane_rank_beats_late_local_rank(self):
        mod = self.mod
        citations = [self._paper("2411.18011", "Manual-PA", source="seed_citation")]
        local = [self._paper(f"2701.{i:05d}", f"P{i}") for i in range(1, 60)]
        fused = mod.fuse_recall_pools([local, citations], pool_cap=10)
        # 引文 lane rank=1（1/61）远高于本地第 10 名（1/70），Manual-PA 应在池前列
        self.assertIn("2411.18011", [p["paper_id"] for p in fused[:5]])

    def test_pool_cap_respected_and_empty_lanes_ok(self):
        mod = self.mod
        lane = [self._paper(f"2701.{i:05d}", f"P{i}") for i in range(30)]
        fused = mod.fuse_recall_pools([[], lane, []], pool_cap=5)
        self.assertEqual(len(fused), 5)
        self.assertEqual(mod.fuse_recall_pools([[], []], 10), [])


class SeedAnchoredParadigmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_seed_analysis_short_circuits_llm(self):
        mod = self.mod
        ctx = _FakeCtx("主题")

        def boom(*args, **kwargs):
            raise AssertionError("有种子时不应调用 LLM 归纳范式")

        original = mod._chat_text
        mod._chat_text = boom
        try:
            paradigm = mod.define_task_paradigm(
                ctx,
                lambda: None,
                seed_analysis={
                    "target_paradigm": "instruction-diagram driven 3D part assembly",
                    "task_definition": "输入多页示意图输出位姿序列",
                    "input_boundary": "不含 CAD 图纸",
                    "dataset_names": ["IKEA-Manual"],
                    "non_arxiv_refs": [{"title": "Classic", "venue": "SIGGRAPH", "year": "2015"}],
                },
            )
        finally:
            mod._chat_text = original
        self.assertEqual(paradigm, "instruction-diagram driven 3D part assembly")
        self.assertEqual(ctx.task_definition, "输入多页示意图输出位姿序列")
        self.assertEqual(ctx.dataset_names, ["IKEA-Manual"])
        self.assertEqual(ctx.non_arxiv_refs[0]["title"], "Classic")


class OutlineTableBackfillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_tables_and_task_definition_backfilled(self):
        mod = self.mod
        ctx = _FakeCtx("3D 装配")
        ctx.task_definition = "输入分步示意图，输出零件 6D 位姿序列"
        clusters = [{"cluster_id": 0, "paper_indices": [0], "name_zh": "方向", "keywords": []}]

        # LLM 只给了一个主题节：脚手架必须补齐任务定义节 + 两张表 + 引言/结论
        def fake_chat_structured(client, system, user, schema_name, schema):
            return {
                "title_zh": "T",
                "sections": [{"heading": "方向", "focus": "f", "cluster_ids": [0], "all_clusters": False}],
            }

        original = mod._chat_structured
        mod._chat_structured = fake_chat_structured
        try:
            outline = mod.build_outline(ctx, global_analysis="", clusters=clusters, client_factory=lambda: None)
        finally:
            mod._chat_structured = original
        headings = [s["heading"] for s in outline["sections"]]
        tables = {s.get("required_table") for s in outline["sections"]}
        self.assertIn("datasets", tables)
        self.assertIn("methods", tables)
        self.assertTrue(any("任务定义" in h for h in headings))
        self.assertIn("引言", headings[0])
        self.assertTrue(any("结论" in h or "展望" in h for h in headings))
        # 任务定义节的 focus 携带种子锚定的定义
        task_section = next(s for s in outline["sections"] if "任务定义" in s["heading"])
        self.assertIn("零件 6D 位姿", task_section["focus"])


class AssembleReportEnrichmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_citation_count_and_extended_reading(self):
        mod = self.mod
        md = mod.assemble_report(
            query="q",
            title_zh="标题",
            section_markdowns=["## 引言\n\n正文"],
            extractions=[
                {"title": "Paper One", "link": "https://arxiv.org/abs/1", "citation_count": 217,
                 "recall_sources": ["seed_citation"]},
                {"title": "Paper Two", "link": ""},
            ],
            clusters=[{"name_zh": "簇", "paper_indices": [0]}],
            generated_at="2026-08-28 00:00 UTC",
            non_arxiv_refs=[{"title": "Classic CAD", "venue": "SIGGRAPH", "year": "2015"}],
        )
        self.assertIn("[1] ★ Paper One（被引 217） — https://arxiv.org/abs/1", md)
        self.assertIn("[2] Paper Two", md)
        self.assertIn("## 延伸阅读（非 arXiv 文献）", md)
        self.assertIn("Classic CAD（SIGGRAPH，2015）", md)

    def test_no_extended_reading_when_empty(self):
        md = self.mod.assemble_report(
            query="q",
            title_zh="t",
            section_markdowns=["## 引言"],
            extractions=[{"title": "P", "link": ""}],
            clusters=[],
            generated_at="x",
        )
        self.assertNotIn("延伸阅读", md)


class KaggleRecallLaneTest(unittest.TestCase):
    """Kaggle 快照粗筛路：多查询合并去重 + 无索引降级 + 融合池抬升。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()
        import kaggle_arxiv  # noqa: F401

        cls.ka = sys.modules["kaggle_arxiv"]

    def _paper(self, pid, title):
        return {"paper_id": pid, "title": title, "abstract": "a", "source": "kaggle"}

    def test_merges_queries_dedups_and_normalizes_ids(self):
        mod = self.mod
        q1, q2 = "assembly planning", "robot manipulation"
        shared = self._paper("2101.00001", "Shared")
        only2 = self._paper("2101.99999v3", "Only2")

        class FakeIndex:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def search(self, query, *, top_k, date_start, date_end):
                self.calls.append(query)
                return [shared] if query == q1 else [dict(shared), only2]

        fake = FakeIndex()
        original = self.ka.KaggleArxivIndex
        self.ka.KaggleArxivIndex = lambda *a, **k: fake
        try:
            ctx = _FakeCtx()
            lane = mod._kaggle_recall_lane(ctx, [q1, q2], fetch_days=365, coarse_top_k=100)
        finally:
            self.ka.KaggleArxivIndex = original
        self.assertEqual([p["paper_id"] for p in lane], ["2101.00001", "2101.99999"])
        self.assertEqual(fake.calls, [q1, q2])
        self.assertEqual(lane[1]["paper_id"], "2101.99999", "版本号应被归一化")

    def _patch_recall_deps(self):
        mod = self.mod
        mod._load_repo_config = lambda: {}
        mod.get_supabase_read_config = lambda config: {}

    def test_recall_papers_kaggle_widens_pool_and_records_stats(self):
        mod = self.mod
        self._patch_recall_deps()
        kaggle_papers = [self._paper(f"21{i:02d}.0000{i}", f"K{i}") for i in range(1, 6)]
        original_ready = self.ka.is_kaggle_ready
        original_lane = mod._kaggle_recall_lane
        self.ka.is_kaggle_ready = lambda: (True, "")
        mod._kaggle_recall_lane = lambda ctx, queries, *, fetch_days, coarse_top_k: list(kaggle_papers)
        try:
            ctx = _FakeCtx()
            papers = mod.recall_papers(
                ctx, fetch_days=365, use_deepxiv=False, use_kaggle=True, coarse_top_k=5000
            )
        finally:
            self.ka.is_kaggle_ready = original_ready
            mod._kaggle_recall_lane = original_lane
        self.assertEqual(len(papers), 5)
        self.assertEqual(ctx.funnel.get("fts_candidates"), 5)
        self.assertEqual(ctx.lane_stats["kaggle"]["hits"], 5)
        self.assertGreaterEqual(ctx.lane_stats["kaggle"]["latency_s"], 0.0)

    def test_use_kaggle_false_never_probes_index(self):
        mod = self.mod
        self._patch_recall_deps()

        def boom(*args, **kwargs):
            raise AssertionError("use_kaggle=False 不应探测/调用 Kaggle 路")

        original_ready = self.ka.is_kaggle_ready
        original_lane = mod._kaggle_recall_lane
        self.ka.is_kaggle_ready = boom
        mod._kaggle_recall_lane = boom
        try:
            ctx = _FakeCtx()
            papers = mod.recall_papers(
                ctx,
                fetch_days=365,
                use_deepxiv=False,
                use_kaggle=False,
                seed_citations=[{"paper_id": "2411.18011", "title": "S", "abstract": "a", "source": "seed_citation"}],
            )
        finally:
            self.ka.is_kaggle_ready = original_ready
            mod._kaggle_recall_lane = original_lane
        self.assertEqual(len(papers), 1)
        self.assertNotIn("kaggle", ctx.lane_stats)

    def test_pure_chinese_queries_warn_and_skip(self):
        """纯中文查询组（无任何可提取英文词）：显式 warn 并返回空（不再静默 0 命中）。
        注意「6D位姿估计」能提出孤词 6d，会走单词条阶梯命中——真库已验证；
        此用例用完全无英文词的查询，并 mock 索引确保不连真库。"""
        mod = self.mod

        def boom(*args, **kwargs):
            raise AssertionError("无可提取英文词时不应打开索引")

        original = self.ka.KaggleArxivIndex
        self.ka.KaggleArxivIndex = boom
        try:
            ctx = _FakeCtx()
            lane = mod._kaggle_recall_lane(ctx, ["位姿估计方法综述"], fetch_days=365, coarse_top_k=100)
        finally:
            self.ka.KaggleArxivIndex = original
        self.assertEqual(lane, [])
        self.assertTrue(any("只支持英文检索词" in w for w in ctx.warnings))

    def test_missing_index_warns_and_continues(self):
        mod = self.mod
        self._patch_recall_deps()
        reason = "未检测到 Kaggle arXiv 快照索引，构建方式：python scripts/build_kaggle_arxiv_index.py --download"
        original_ready = self.ka.is_kaggle_ready
        self.ka.is_kaggle_ready = lambda: (False, reason)
        try:
            ctx = _FakeCtx()
            papers = mod.recall_papers(
                ctx,
                fetch_days=365,
                use_deepxiv=False,
                use_kaggle=True,
                seed_citations=[{"paper_id": "2411.18011", "title": "S", "abstract": "a", "source": "seed_citation"}],
            )
        finally:
            self.ka.is_kaggle_ready = original_ready
        self.assertEqual(len(papers), 1, "索引缺失只降级不失败")
        self.assertTrue(any(reason in w for w in ctx.warnings))

    def test_disable_local_lane_env_skips_supabase(self):
        """DPR_SURVEY_DISABLE_LOCAL_LANE=1：Supabase 配置齐全也不走本地路（A/B 隔离变量）。"""
        mod = self.mod

        def boom():
            raise AssertionError("本地路应被禁用，不应发起 Supabase 召回")

        mod._load_repo_config = lambda: {"supabase": {"url": "https://x", "anon_key": "k"}}
        mod.get_supabase_read_config = lambda config: {"url": "https://x", "anon_key": "k"}
        original_lane = mod._local_recall_lane
        mod._local_recall_lane = boom
        try:
            mod.os.environ["DPR_SURVEY_DISABLE_LOCAL_LANE"] = "1"
            ctx = _FakeCtx()
            papers = mod.recall_papers(
                ctx,
                fetch_days=365,
                use_deepxiv=False,
                use_kaggle=False,
                seed_citations=[{"paper_id": "2411.18011", "title": "S", "abstract": "a", "source": "seed_citation"}],
            )
        finally:
            mod._local_recall_lane = original_lane
            mod.os.environ.pop("DPR_SURVEY_DISABLE_LOCAL_LANE", None)
        self.assertEqual(len(papers), 1)
        self.assertNotIn("local", ctx.lane_stats)
        self.assertTrue(any("本地库召回路已按需跳过" in w for w in ctx.warnings))


class CoarseRankTest(unittest.TestCase):
    """本地语义粗排：小池直通 / 最大查询余弦排序截断 / 失败降级。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _papers(self, n, good_titles=("GOOD1",)):
        out = []
        for i in range(n):
            title = good_titles[i] if i < len(good_titles) else f"BAD{i}"
            out.append({"paper_id": str(i), "title": title, "abstract": "irrelevant body"})
        return out

    def test_small_pool_passthrough_without_model(self):
        mod = self.mod

        def boom():
            raise AssertionError("池不大时不应加载 embedding 模型")

        original = mod._load_coarse_embedding_model
        mod._load_coarse_embedding_model = boom
        try:
            out = mod.coarse_rank_papers(_FakeCtx(), self._papers(2), ["q"], embed_pool=300)
        finally:
            mod._load_coarse_embedding_model = original
        self.assertEqual(len(out), 2)

    def test_ranks_by_max_query_cosine_and_truncates(self):
        import numpy as np

        mod = self.mod

        class FakeModel:
            device = "cpu"

            def encode(self, texts, **kwargs):
                vecs = [[1.0, 0.0] if str(t).startswith("query:") else
                        ([0.9, 0.1] if "GOOD" in str(t) else [0.0, 1.0]) for t in texts]
                return np.asarray(vecs)

        papers = self._papers(8, good_titles=("GOOD1", "GOOD2"))
        original = mod._load_coarse_embedding_model
        mod._load_coarse_embedding_model = lambda: FakeModel()
        try:
            ctx = _FakeCtx()
            out = mod.coarse_rank_papers(ctx, papers, ["assembly query"], embed_pool=3)
        finally:
            mod._load_coarse_embedding_model = original
        titles = {p["title"] for p in out}
        self.assertEqual(len(out), 3)
        self.assertIn("GOOD1", titles)
        self.assertIn("GOOD2", titles, "与查询语义最近的论文应排前")

    def test_failure_degrades_to_truncation(self):
        mod = self.mod

        def boom():
            raise RuntimeError("no local model")

        original = mod._load_coarse_embedding_model
        mod._load_coarse_embedding_model = boom
        try:
            ctx = _FakeCtx()
            out = mod.coarse_rank_papers(ctx, self._papers(5), ["q"], embed_pool=2)
        finally:
            mod._load_coarse_embedding_model = original
        self.assertEqual([p["paper_id"] for p in out], ["0", "1"], "降级为 RRF 序截断")
        self.assertTrue(any("粗排失败" in w for w in ctx.warnings))


class LaneStatsAndProfileTest(unittest.TestCase):
    """lane 对比统计聚合 + 候选池画像（A/B 对比核心）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_aggregate_lane_stats(self):
        mod = self.mod
        extractions = [
            {"recall_sources": ["kaggle", "deepxiv"], "relevance": 8.0, "paradigm_consistency": 7.0},
            {"recall_sources": ["kaggle"], "relevance": 6.0, "paradigm_consistency": 5.0},
            {"recall_sources": ["seed_citation"], "relevance": 9.0},
        ]
        lane_stats = {
            "kaggle": {"latency_s": 3.2, "hits": 8000},
            "deepxiv": {"latency_s": 12.0, "hits": 120},
            "orphan": {"latency_s": 1.0, "hits": 5},
        }
        out = mod._aggregate_lane_stats(extractions, lane_stats)
        self.assertEqual(out["kaggle"]["papers_in_final"], 2)
        self.assertEqual(out["kaggle"]["avg_relevance"], 7.0)
        self.assertEqual(out["kaggle"]["avg_paradigm_consistency"], 6.0)
        self.assertEqual(out["kaggle"]["hits"], 8000)
        self.assertEqual(out["deepxiv"]["papers_in_final"], 1)
        self.assertEqual(out["seed_citation"]["papers_in_final"], 1)
        self.assertNotIn("avg_paradigm_consistency", out["seed_citation"], "无一致性分数时省略键")
        self.assertEqual(out["orphan"], {"latency_s": 1.0, "hits": 5}, "未进终稿的路保留召回侧统计")

    def test_candidate_profile_years_and_categories(self):
        mod = self.mod
        papers = [
            {"published": "2021-05-01", "categories": "cs.CV cs.RO"},
            {"published": "2021-09-01", "categories": "cs.CV"},
            {"published": "2023-01-01", "categories": "cs.RO"},
        ]
        profile = mod.summarize_candidate_profile(papers)
        self.assertIn("2021 年 2 篇", profile)
        self.assertIn("2023 年 1 篇", profile)
        self.assertIn("cs.CV", profile)
        self.assertEqual(mod.summarize_candidate_profile([{"published": ""}]), "")


class PlanRecallQueriesTest(unittest.TestCase):
    """无种子查询规划：任意语言主题 → 英文检索查询组（中文主题防漂移的根因修复）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_plan_returns_english_queries(self):
        mod = self.mod
        captured = {}

        def fake_chat(client, system, user, tag, schema):
            captured["user"] = user
            return {"queries": ["6D pose estimation from RGB images", "category-level pose estimation", "x"] * 3}

        original = mod._chat_structured
        mod._chat_structured = fake_chat
        try:
            ctx = _FakeCtx("6D位姿估计")
            queries = mod.plan_recall_queries(ctx, lambda: None)
        finally:
            mod._chat_structured = original
        self.assertEqual(len(queries), 8, "查询组 cap 8")
        self.assertTrue(all(q == q.strip() for q in queries))
        self.assertIn("6D位姿估计", captured["user"])

    def test_plan_failure_degrades_to_empty(self):
        mod = self.mod

        def boom(*args, **kwargs):
            raise RuntimeError("llm down")

        original = mod._chat_structured
        mod._chat_structured = boom
        try:
            ctx = _FakeCtx("topic")
            queries = mod.plan_recall_queries(ctx, lambda: None)
        finally:
            mod._chat_structured = original
        self.assertEqual(queries, [])
        self.assertTrue(any("查询规划失败" in w for w in ctx.warnings))


class LexicalCoverageGuardTest(unittest.TestCase):
    """召回-主题词面覆盖率熔断：候选池与主题错位时直接终止（防幻觉综述）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_mismatched_pool_scores_zero(self):
        mod = self.mod
        nlp_pool = [
            {"title": "Multi-vector retrieval with late interaction", "abstract": "document ranking embeddings"},
            {"title": "Interpretable document alignment", "abstract": "scientific literature understanding"},
        ]
        self.assertEqual(mod.recall_pool_lexical_coverage(nlp_pool, ["6D pose estimation from RGB images"]), 0.0)

    def test_matched_pool_scores_high(self):
        mod = self.mod
        pool = [
            {"title": "6D Pose Estimation via Dense Correspondence", "abstract": "object pose regression"},
            {"title": "Category-level pose with NOCS maps", "abstract": "pose estimation benchmark"},
        ]
        self.assertGreaterEqual(
            mod.recall_pool_lexical_coverage(pool, ["6D pose estimation from RGB images"]), 0.5
        )

    def test_pure_chinese_query_scores_zero(self):
        mod = self.mod
        pool = [{"title": "Any English Title", "abstract": "anything"}]
        self.assertEqual(mod.recall_pool_lexical_coverage(pool, ["6D位姿估计"]), 0.0)

    def test_run_survey_aborts_on_mismatched_pool(self):
        """错位池子触发熔断：宁可不生成，不可生成幻觉综述。"""
        mod = self.mod
        calls = []
        papers = [{"paper_id": "a", "title": "Multi-vector Retrieval", "abstract": "document ranking", "pdf_url": "", "link": ""}]
        mod.plan_recall_queries = lambda ctx, factory: calls.append("plan") or ["6D pose estimation methods"]
        mod.recall_papers = (
            lambda ctx, *, fetch_days, queries=None, seed_citations=None, use_deepxiv=False,
            use_kaggle=False, coarse_top_k=None: papers
        )
        original_rerank = mod.rerank_papers
        mod.rerank_papers = lambda *a, **k: (_ for _ in ()).throw(AssertionError("熔断前不得进入 rerank"))
        try:
            with self.assertRaises(RuntimeError) as cm:
                mod.run_survey("6D位姿估计", client_factory=lambda: None)
            self.assertIn("覆盖率过低", str(cm.exception))
        finally:
            mod.rerank_papers = original_rerank

    def test_quality_warning_on_low_relevance(self):
        """终稿 avg relevance 偏低时 report_meta 记 quality_warnings。"""
        mod = self.mod
        papers = [{"paper_id": "a", "title": "Topic Method Paper", "abstract": "topic methods", "pdf_url": "", "link": ""}]
        extractions = [dict(papers[0], relevance=4.2, core_problem="p", main_results="r")]
        clusters = [{"cluster_id": 0, "paper_indices": [0], "name_zh": "簇", "keywords": ["k"]}]
        mod.plan_recall_queries = lambda ctx, factory: ["topic method"]
        mod.recall_papers = (
            lambda ctx, *, fetch_days, queries=None, seed_citations=None, use_deepxiv=False,
            use_kaggle=False, coarse_top_k=None: papers
        )
        mod.rerank_papers = lambda ctx, ps, *, max_papers: ps
        mod.define_task_paradigm = lambda ctx, factory, *, seed_analysis=None: "paradigm"
        mod.extract_papers = (
            lambda ctx, ps, *, client_factory, concurrency=4, survey_topic="", target_paradigm="": extractions
        )
        mod.cluster_papers = lambda ctx, es, *, client_factory: clusters
        mod.deep_read_core_papers = lambda ctx, cs, es, *, enabled=True, deepxiv=None: None
        mod.analyse_clusters = lambda ctx, cs, es, *, client_factory, concurrency=2: (
            [{"cluster_id": 0, "theme": "t", "keywords": [], "paper_count": 1, "analysis": "a"}], "GA"
        )
        mod.build_outline = (
            lambda ctx, *, global_analysis, clusters, client_factory: {"title_zh": "T", "sections": [{"heading": "引言", "focus": "", "cluster_ids": [], "all_clusters": True}]}
        )
        mod.write_sections = lambda ctx, **kwargs: (["## 引言\n\n正文"], [{"heading": "引言", "focus": "", "cluster_ids": [], "all_clusters": True}])
        mod.review_draft = lambda ctx, draft, *, client_factory: (draft, [])
        try:
            result = mod.run_survey("topic", client_factory=lambda: None)
        finally:
            pass
        self.assertEqual(result["report_meta"]["avg_relevance"], 4.2)
        self.assertTrue(result["report_meta"]["quality_warnings"])
        self.assertIn("lexical_coverage", result["report_meta"]["funnel"])
        self.assertGreaterEqual(result["report_meta"]["funnel"]["lexical_coverage"], 0.5)
