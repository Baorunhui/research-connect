import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class _FakeClient:
    def __init__(self, payload=""):
        self.kwargs = {}
        self.calls = []
        self._payload = payload

    def chat(self, messages=None, response_format=None):
        self.calls.append(messages)
        if isinstance(self._payload, Exception):
            raise self._payload
        return {"content": self._payload}


# 各引用句之间用 >400 字符的填充段隔开，保证默认 radius=400 的窗口不会跨图泄漏
_FILLER = (
    "The encoder processes each frame independently and produces a compact motion representation. "
    "These tokens are then aligned with the camera trajectory before decoding. "
) * 4
FULL_TEXT = (
    "We propose a new framework for video generation. "
    "As shown in Figure 1, our framework consists of a motion encoder and a camera decoder. "
    "The motion encoder extracts per-frame motion tokens. "
    + _FILLER
    + "In Figure 2 we compare our method against three baselines on two datasets. "
    "Our method achieves the best accuracy on both benchmarks. "
    + _FILLER
    + "Figure 10 shows an ablation study in the appendix. "
    + _FILLER
    + "Table 1 summarizes the hyperparameters used in all experiments."
)


class FigureInterpretationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        src_dir = root / "src"
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
        cls.mod = _load_module("figure_interpretation_mod", src_dir / "figure_interpretation.py")

    # ---- 开关 / 纯函数 ----

    def test_figures_enabled_default_on(self):
        # 缺省开启；显式 0/off/false 关闭
        self.assertTrue(self.mod.figures_enabled())
        with mock.patch.dict(os.environ, {"DPR_INTERPRET_FIGURES": "0"}, clear=False):
            self.assertFalse(self.mod.figures_enabled())
        with mock.patch.dict(os.environ, {"DPR_INTERPRET_FIGURES": "off"}, clear=False):
            self.assertFalse(self.mod.figures_enabled())
        with mock.patch.dict(os.environ, {"DPR_INTERPRET_FIGURES": "1"}, clear=False):
            self.assertTrue(self.mod.figures_enabled())

    def test_batch_size_default_and_env(self):
        m = self.mod
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DPR_FIGURE_BATCH_SIZE", None)
            self.assertEqual(m.batch_size(), 8)
        with mock.patch.dict(os.environ, {"DPR_FIGURE_BATCH_SIZE": "3"}, clear=False):
            self.assertEqual(m.batch_size(), 3)
        with mock.patch.dict(os.environ, {"DPR_FIGURE_BATCH_SIZE": "abc"}, clear=False):
            self.assertEqual(m.batch_size(), 8)

    def test_select_key_figures_priority_and_cap(self):
        m = self.mod
        classified = [
            {"category": "result", "caption": "r1"},   # idx0 = Figure 1
            {"category": "method", "caption": "m1"},    # idx1 = Figure 2
            {"category": "other", "caption": "o1"},     # idx2 附录，丢弃
            {"category": "table", "caption": "t1"},     # idx3
            {"category": "architecture", "caption": "a1"},  # idx4
        ]
        # 图号优先：自然序前2张(Fig1 result、Fig2 method)强制保留；其余按类别补足
        sel = m.select_key_figures(classified, max_n=10)
        self.assertEqual(sel, [0, 1, 3, 4])
        # 兜底保持非 other 更靠前的结果也纳入序号0
        sel2 = m.select_key_figures(classified, max_n=2)
        self.assertEqual(sel2, [0, 1])

    def test_select_key_figures_keeps_first_figure_even_if_other(self):
        m = self.mod
        classified = [
            {"category": "other", "caption": ""},     # idx0 = Figure 1 被判 other
            {"category": "method", "caption": "m1"},   # idx1
            {"category": "result", "caption": "r1"},   # idx2
        ]
        sel = m.select_key_figures(classified, max_n=10)
        self.assertIn(0, sel)
        self.assertEqual(sel, [0, 1, 2])

    def test_select_key_figures_empty(self):
        m = self.mod
        self.assertEqual(m.select_key_figures([], max_n=4), [])

    # ---- 启发式分类 ----

    def test_heuristic_category_table(self):
        m = self.mod
        self.assertEqual(m.heuristic_category({"label": "Table", "caption": ""}), "table")
        self.assertEqual(
            m.heuristic_category({"label": "Figure", "caption": "Table 2: Comparison of methods."}),
            "table",
        )

    def test_heuristic_category_result_before_method(self):
        m = self.mod
        # result 关键词优先于 method（“comparison of our method” 是结果图）
        self.assertEqual(
            m.heuristic_category({"label": "Figure", "caption": "Figure 3: Comparison of our method with baselines."}),
            "result",
        )
        self.assertEqual(m.heuristic_category({"label": "Figure", "caption": "Figure 4: Ablation study."}), "result")
        # 引用段也能触发
        self.assertEqual(
            m.heuristic_category({"label": "Figure", "caption": ""}, refs_text="performance on the benchmark"),
            "result",
        )

    def test_heuristic_category_method(self):
        m = self.mod
        self.assertEqual(
            m.heuristic_category({"label": "Figure", "caption": "Figure 1: Overview of our framework."}),
            "method",
        )
        self.assertEqual(
            m.heuristic_category({"label": "Figure", "caption": "Figure 2: Architecture of the system."}),
            "method",
        )

    def test_heuristic_category_figure1_default_method(self):
        m = self.mod
        self.assertEqual(m.heuristic_category({"label": "Figure", "caption": "Some picture.", "index": 1}), "method")
        self.assertEqual(m.heuristic_category({"label": "Figure", "caption": "Some picture.", "index": 5}), "other")

    # ---- 正文引用收集 ----

    def test_collect_figure_references_basic(self):
        m = self.mod
        refs = m.collect_figure_references(FULL_TEXT, "Figure", 1)
        self.assertIn("As shown in Figure 1", refs)
        self.assertIn("motion encoder", refs)
        # 不串到 Figure 2 的引用段
        self.assertNotIn("compare our method against three baselines", refs)

    def test_collect_figure_references_no_match(self):
        m = self.mod
        self.assertEqual(m.collect_figure_references("no references here", "Figure", 7), "")
        self.assertEqual(m.collect_figure_references("", "Figure", 1), "")
        self.assertEqual(m.collect_figure_references(FULL_TEXT, "Figure", 0), "")

    def test_collect_figure_references_number_boundary(self):
        m = self.mod
        # Figure 1 不得匹配 Figure 10
        refs = m.collect_figure_references(FULL_TEXT, "Figure", 1)
        self.assertNotIn("ablation study in the appendix", refs)
        refs10 = m.collect_figure_references(FULL_TEXT, "Figure", 10)
        self.assertIn("ablation study in the appendix", refs10)

    def test_collect_figure_references_table_label(self):
        m = self.mod
        refs = m.collect_figure_references(FULL_TEXT, "Table", 1)
        self.assertIn("hyperparameters", refs)
        # Table 正则不匹配 Figure
        self.assertNotIn("As shown in Figure 1", refs)

    def test_collect_figure_references_dedupes_inline_caption(self):
        m = self.mod
        caption = "Figure 1: Our framework consists of a motion encoder and a camera decoder."
        text = "Intro. " + caption + " As shown in Figure 1, the encoder extracts tokens."
        refs = m.collect_figure_references(text, "Figure", 1, caption=caption)
        # 图注被内联进正文时应去重
        self.assertNotIn(caption, refs)
        self.assertIn("the encoder extracts tokens", refs)

    def test_collect_figure_references_max_chars(self):
        m = self.mod
        text = "x " * 50 + "Figure 1 is here. " + "y " * 50
        refs = m.collect_figure_references(text, "Figure", 1, max_chars=80)
        self.assertLessEqual(len(refs), 80)

    # ---- 清洗 / 解析工具 ----

    def test_clean_caption_json_caption_stays_clean(self):
        m = self.mod
        raw = '{"caption": "该图展示了编码器-解码器结构，含残差连接。第二句说明。第三句是结论。"}'
        cap = m._clean_caption(m._field_from_text(raw, "caption"))
        self.assertEqual(cap, "该图展示了编码器-解码器结构，含残差连接。第二句说明。第三句是结论。")
        for token in ("**", "用户希望", "{"):
            self.assertNotIn(token, cap)
        pre = m._clean_caption("用户希望解读这张图。 **该图** 呈现上升趋势。")
        self.assertNotIn("用户希望解读", pre)
        self.assertNotIn("**", pre)

    def test_field_from_text_extracts_caption_json(self):
        m = self.mod
        raw = '{"caption": "该图展示了编码器-解码器结构，含残差连接。"}'
        self.assertEqual(m._field_from_text(raw, "caption"), "该图展示了编码器-解码器结构，含残差连接。")
        raw2 = '"caption": "第二句。"'
        self.assertEqual(m._field_from_text(raw2, "caption"), "第二句。")
        raw3 = '```json\n{"caption": "第三句。"}\n```'
        self.assertEqual(m._field_from_text(raw3, "caption"), "第三句。")

    def test_clean_caption_truncates_at_sentence_boundary(self):
        m = self.mod
        text = (
            "第一句话说明这张图的内容。"
            "第二句话介绍关键信息，这部分会很长故意写得用来测试是否能在句子中间被拦腰切断掉。"
            "第三句话是结论之后还会继续写很多内容以触发超出长度上限。" * 3
        )
        out = m._clean_caption(text, max_chars=60)
        self.assertLessEqual(len(out), 60)
        self.assertTrue(out.endswith("。") or out.endswith("."), out)

    def test_clean_caption_short_unchanged(self):
        m = self.mod
        s = "该图展示了编码器-解码器结构，含残差连接。"
        self.assertEqual(m._clean_caption(s), s)

    def test_parse_interpretation_array(self):
        m = self.mod
        out = m._parse_interpretation_array('```json\n[{"id": 1, "interpretation": "解读一。"}, {"id": 2, "interpretation": "解读二。"}]\n```')
        self.assertEqual(out, {1: "解读一。", 2: "解读二。"})
        # 外层包一层对象
        out2 = m._parse_interpretation_array('{"items": [{"id": 3, "interpretation": "解读三。"}]}')
        self.assertEqual(out2, {3: "解读三。"})
        # 解析失败 → 空 dict
        self.assertEqual(m._parse_interpretation_array("完全不是 JSON"), {})

    def test_reorder_figures_by_importance(self):
        m = self.mod
        figs = [
            {"index": 1, "category": "result"},
            {"index": 2, "category": "other"},
            {"index": 3, "category": "method"},
            {"index": 4, "category": "architecture"},
            {"index": 5, "category": "table"},
        ]
        out = sorted(figs, key=lambda it: m._importance_sort_key(it.get("category")))
        self.assertEqual([f["category"] for f in out], ["method", "architecture", "result", "table", "other"])
        figs2 = [
            {"index": 1, "category": "method"},
            {"index": 2, "category": "method"},
            {"index": 3, "category": "result"},
            {"index": 4, "category": "result"},
        ]
        out2 = sorted(figs2, key=lambda it: m._importance_sort_key(it.get("category")))
        self.assertEqual([f["index"] for f in out2], [1, 2, 3, 4])
        figs3 = [{"index": i, "category": "other"} for i in range(3, 0, -1)]
        out3 = sorted(figs3, key=lambda it: m._importance_sort_key(it.get("category")))
        self.assertEqual([f["index"] for f in out3], [3, 2, 1])

    # ---- 总入口 interpret_paper_figures（纯文本）----

    def test_interpret_paper_figures_empty(self):
        m = self.mod
        nf, nt = m.interpret_paper_figures([], [], {"title": "T"}, "/tmp", client=object())
        self.assertEqual(nf, [])
        self.assertEqual(nt, [])

    def test_interpret_paper_figures_writes_interpretation_and_keeps_caption(self):
        # docling 英文图注(caption)保留；文本解读写入独立 interpretation 字段
        m = self.mod
        figures = [
            {"url": "assets/figures/arxiv/k/f-1.webp", "caption": "Figure 1: English caption.", "index": 1},
        ]
        client = _FakeClient('```json\n[{"id": 1, "interpretation": "结合正文的中文解读。"}]\n```')
        nf, nt = m.interpret_paper_figures(
            figures, [], {"title": "T", "abstract": "A"}, "/tmp",
            full_text=FULL_TEXT, client=client,
        )
        self.assertEqual(nf[0]["caption"], "Figure 1: English caption.")
        self.assertEqual(nf[0]["interpretation"], "结合正文的中文解读。")
        self.assertEqual(nf[0]["category"], "method")  # Figure 1 + framework 相关引用
        self.assertEqual(nt, [])
        self.assertEqual(len(client.calls), 1)

    def test_interpret_paper_figures_prompt_contains_caption_and_refs(self):
        m = self.mod
        figures = [
            {"url": "a.webp", "caption": "Figure 2: Comparison of methods.", "index": 2},
        ]
        client = _FakeClient('[{"id": 1, "interpretation": "解读。"}]')
        m.interpret_paper_figures(
            figures, [], {"title": "T", "abstract": "A"}, "/tmp",
            full_text=FULL_TEXT, client=client,
        )
        prompt = client.calls[0][1]["content"]
        self.assertIn("Figure 2: Comparison of methods.", prompt)
        self.assertIn("compare our method against three baselines", prompt)
        self.assertIn("论文标题：T", prompt)
        self.assertIn("JSON 数组", prompt)

    def test_interpret_paper_figures_batches_by_batch_size(self):
        m = self.mod
        figures = [
            {"url": f"f{i}.webp", "caption": f"Figure {i}.", "index": i} for i in range(1, 11)
        ]
        client = _FakeClient('[]')
        with mock.patch.dict(os.environ, {"DPR_FIGURE_BATCH_SIZE": "4"}, clear=False):
            m.interpret_paper_figures(figures, [], {"title": "T"}, "/tmp", full_text="", client=client)
        # 10 个图 → ceil(10/4) = 3 批
        self.assertEqual(len(client.calls), 3)

    def test_interpret_paper_figures_skips_entries_without_context(self):
        # 无图注且无正文引用 → 不进 LLM，标记 interpretation_skipped，避免编造
        m = self.mod
        figures = [
            {"url": "a.webp", "caption": "Figure 1.", "index": 1},
            {"url": "b.webp", "caption": "", "index": 9},
        ]
        client = _FakeClient('[]')
        nf, _ = m.interpret_paper_figures(
            figures, [], {"title": "T"}, "/tmp", full_text=FULL_TEXT, client=client
        )
        by_index = {f["index"]: f for f in nf}
        prompt = client.calls[0][1]["content"]
        self.assertIn("Figure 1.", prompt)
        self.assertNotIn("Figure 9", prompt)
        self.assertNotIn("interpretation", by_index[9])
        self.assertTrue(by_index[9].get("interpretation_skipped"))

    def test_interpret_paper_figures_reorders_by_importance(self):
        m = self.mod
        figures = [
            {"url": "f1.webp", "caption": "Figure 1: Comparison of results.", "index": 1},   # result
            {"url": "f2.webp", "caption": "Figure 2: Some picture.", "index": 2},            # other
            {"url": "f3.webp", "caption": "Figure 3: Overview of the framework.", "index": 3},  # method
        ]
        client = _FakeClient('[]')
        nf, _ = m.interpret_paper_figures(figures, [], {"title": "T"}, "/tmp", full_text="", client=client)
        self.assertEqual([f["index"] for f in nf], [3, 1, 2])
        self.assertEqual([f["category"] for f in nf], ["method", "result", "other"])

    def test_interpret_paper_figures_best_effort_failing_client(self):
        # LLM 调用抛错时 best-effort：不抛错、无 interpretation、category 仍写回
        m = self.mod
        figures = [{"url": "a.webp", "caption": "Figure 1: Overview.", "index": 1}]
        client = _FakeClient(RuntimeError("boom"))
        nf, _ = m.interpret_paper_figures(
            figures, [], {"title": "T"}, "/tmp", full_text=FULL_TEXT, client=client
        )
        self.assertEqual(nf[0]["index"], 1)
        self.assertNotIn("interpretation", nf[0])
        self.assertEqual(nf[0]["category"], "method")

    def test_interpret_paper_figures_no_client_no_key_returns_as_is(self):
        # 无 client 且无 API Key → 原样返回（category 写回、无网络）
        m = self.mod
        figures = [{"url": "a.webp", "caption": "Figure 1: Overview.", "index": 1}]
        with mock.patch.object(m, "create_text_client", return_value=None):
            nf, _ = m.interpret_paper_figures(figures, [], {"title": "T"}, "/tmp", full_text=FULL_TEXT, client=None)
        self.assertEqual(nf[0]["index"], 1)
        self.assertNotIn("interpretation", nf[0])
        self.assertEqual(nf[0]["category"], "method")

    def test_interpret_paper_figures_mixed_figures_and_tables(self):
        m = self.mod
        figures = [{"url": "f1.webp", "caption": "Figure 1: Overview of the framework.", "index": 1}]
        tables = [{"url": "t1.webp", "caption": "Table 1: Hyperparameters.", "index": 1}]
        client = _FakeClient(
            '[{"id": 1, "interpretation": "图解读。"}, {"id": 2, "interpretation": "表解读。"}]'
        )
        nf, nt = m.interpret_paper_figures(
            figures, tables, {"title": "T"}, "/tmp", full_text=FULL_TEXT, client=client
        )
        self.assertEqual(nf[0]["interpretation"], "图解读。")
        self.assertEqual(nt[0]["interpretation"], "表解读。")
        self.assertEqual(nt[0]["category"], "table")


if __name__ == "__main__":
    unittest.main()
