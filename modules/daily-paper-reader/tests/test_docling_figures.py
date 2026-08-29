import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _png_bytes(size, color):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_result(png_dir, *, anchors, raw=None, aux=None):
    """构造与 figure_pipeline result.json 同构的最小 payload 并返回 dict。"""
    raw = raw or []
    aux = aux or []
    return {
        "schema_version": 1,
        "paper": {"slug": "testpaper", "title": "Test"},
        "anchors": anchors,
        "raw_figures": raw,
        "auxiliary_picture_indices": aux,
    }


class DoclingFiguresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        src_dir = root / "src"
        cls.mod = _load_module("docling_figures_mod", src_dir / "docling_figures.py")
        # 强制走自带 stub（避免依赖 paper_figures 的副作用），确保纯转换可独立测
        cls.src_dir = src_dir

    def _write_png(self, base, rel, size, color):
        p = Path(base) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_png_bytes(size, color))
        return str(p)

    def test_parse_docling_result_basic(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            # 模拟 output/<slug>/ 目录
            out_root = tmp / "out"
            png_dir = out_root / "testpaper"
            self._write_png(png_dir, "figures/fig1.png", (640, 480), (200, 30, 30))
            self._write_png(png_dir, "figures/fig2.png", (800, 600), (30, 200, 30))
            self._write_png(png_dir, "raw_figures/figure_001.png", (120, 120), (30, 30, 200))

            anchors = [
                {"key": "fig1", "caption": "Figure 1: Overview of the framework.", "page_no": 1, "image_path": "figures/fig1.png", "section": "body"},
                {"key": "fig2", "caption": "Figure 2: Main results.", "page_no": 3, "image_path": "figures/fig2.png", "section": "body"},
            ]
            raw = [{"index": 1, "image_path": "raw_figures/figure_001.png", "caption": ""}]
            payload = _make_result(png_dir, anchors=anchors, raw=[raw[0]], aux=[1])
            (png_dir / "result.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            fig_dir = tmp / "assets" / "figures" / "arxiv" / "p"
            tab_dir = tmp / "assets" / "tables" / "arxiv" / "p"
            figures, tables = self.mod.parse_docling_result(
                os.path.join(png_dir, "result.json"),
                str(fig_dir),
                "assets/figures/arxiv/p",
                str(tab_dir),
                "assets/tables/arxiv/p",
                result_root=str(out_root),
            )

            # 2 主图 + 1 辅助候选
            self.assertEqual(len(figures), 3)
            self.assertEqual(tables, [])

            # 主图 fig1：index=图号、caption 完整、page 正确
            f1 = figures[0]
            self.assertEqual(f1["index"], 1)
            self.assertEqual(f1["caption"], "Figure 1: Overview of the framework.")
            self.assertEqual(f1["page"], 1)
            self.assertEqual(f1["label"], "Figure")
            self.assertTrue(f1["url"].endswith("fig-001.webp"))
            self.assertEqual((f1["width"], f1["height"]), (640, 480))
            self.assertTrue((fig_dir / "fig-001.webp").exists())

            # 主图 fig2
            f2 = figures[1]
            self.assertEqual((f2["index"], f2["page"]), (2, 3))
            self.assertEqual((f2["width"], f2["height"]), (800, 600))

            # 辅助候选：index=1000+aux
            f_aux = figures[2]
            self.assertEqual(f_aux["index"], 1001)
            self.assertEqual(f_aux["section"], "auxiliary")

            # meta.json extractor
            meta = json.loads((fig_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["extractor"], "docling")
            self.assertEqual(len(meta["figures"]), 3)

    def test_parse_docling_result_table_separation(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            png_dir = tmp / "testpaper"
            self._write_png(png_dir, "figures/table1.png", (640, 480), (10, 10, 210))
            anchors = [
                {"key": "table1", "caption": "Table 1: Ablation study.", "page_no": 5, "image_path": "figures/table1.png", "section": "appendix"},
            ]
            payload = _make_result(png_dir, anchors=anchors)
            (png_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")

            fig_dir = tmp / "fig"
            tab_dir = tmp / "tab"
            figures, tables = self.mod.parse_docling_result(
                os.path.join(png_dir, "result.json"), str(fig_dir), "f", str(tab_dir), "t", result_root=str(tmp)
            )
            self.assertEqual(figures, [])
            self.assertEqual(len(tables), 1)
            self.assertEqual(tables[0]["label"], "Table")
            self.assertEqual(tables[0]["index"], 1)
            self.assertTrue(tables[0]["url"].endswith("table-001.webp"))
            self.assertTrue((tab_dir / "table-001.webp").exists())

    def test_parse_docling_result_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.mod.parse_docling_result(os.path.join(d, "nope.json"), "a", "f", "b", "t"), ([], []))

    def test_parse_docling_result_bad_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "result.json")
            Path(p).write_text("not json{{", encoding="utf-8")
            self.assertEqual(self.mod.parse_docling_result(p, "a", "f", "b", "t"), ([], []))

    def test_parse_docling_result_missing_anchor_image(self):
        with tempfile.TemporaryDirectory() as d:
            png_dir = Path(d) / "testpaper"
            png_dir.mkdir(parents=True)
            anchors = [{"key": "fig1", "caption": "Figure 1: x", "page_no": 1, "image_path": "figures/missing.png", "section": "body"}]
            (png_dir / "result.json").write_text(json.dumps(_make_result(png_dir, anchors=anchors)), encoding="utf-8")
            figures, tables = self.mod.parse_docling_result(
                os.path.join(png_dir, "result.json"), "a", "f", "b", "t", result_root=str(Path(d))
            )
            self.assertEqual(figures, [])
            self.assertEqual(tables, [])


if __name__ == "__main__":
    unittest.main()