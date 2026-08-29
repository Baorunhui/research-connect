import importlib.util
import io
import json
import os
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class PaperFiguresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        src_dir = root / "src"
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
        cls.mod = _load_module("paper_figures_mod", src_dir / "paper_figures.py")

    def _make_png_bytes(self, size, color):
        img = Image.new("RGB", size, color)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_extract_figures_from_pdf(self):
        with tempfile.TemporaryDirectory() as d:
            pdf_path = Path(d) / "sample.pdf"
            out_dir = Path(d) / "assets"

            big_img = self._make_png_bytes((640, 480), (220, 80, 80))
            small_img = self._make_png_bytes((80, 80), (80, 80, 220))

            doc = fitz.open()
            page = doc.new_page()
            page.insert_image(fitz.Rect(40, 40, 400, 320), stream=big_img)
            page.insert_image(fitz.Rect(420, 40, 500, 120), stream=small_img)
            doc.save(pdf_path)
            doc.close()

            figures = self.mod.extract_figures_from_pdf(
                str(pdf_path),
                str(out_dir),
                "assets/figures/arxiv/test-paper",
            )

            self.assertEqual(len(figures), 1)
            self.assertTrue(figures[0]["url"].endswith("fig-001.webp"))
            self.assertTrue((out_dir / "fig-001.webp").exists())

            meta_path = out_dir / "meta.json"
            self.assertTrue(meta_path.exists())
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(len(meta["figures"]), 1)
            self.assertEqual(meta["version"], 2)

    def test_extract_figures_from_pdf_bytes(self):
        # 内存 PDF 字节入口：复用日报抽图逻辑，产出 url 为本地绝对路径（供论文总结等一次性场景）
        with tempfile.TemporaryDirectory() as d:
            pdf_path = Path(d) / "sample.pdf"
            out_dir = Path(d) / "tmpout"

            big_img = self._make_png_bytes((640, 480), (30, 120, 200))
            doc = fitz.open()
            page = doc.new_page()
            page.insert_image(fitz.Rect(40, 40, 400, 320), stream=big_img)
            small_img = self._make_png_bytes((60, 60), (90, 200, 60))
            page.insert_image(fitz.Rect(420, 40, 480, 100), stream=small_img)
            doc.save(pdf_path)
            doc.close()

            pdf_bytes = pdf_path.read_bytes()
            original_docling = self.mod.docling_enabled
            try:
                self.mod.docling_enabled = lambda: False
                figures, tables = self.mod.extract_figures_from_pdf_bytes(pdf_bytes, str(out_dir))
            finally:
                self.mod.docling_enabled = original_docling

            self.assertEqual(len(figures), 1)
            # url 归一化为本地绝对路径且文件真实存在
            url = figures[0]["url"]
            self.assertTrue(os.path.isabs(url), url)
            self.assertTrue(os.path.exists(url), url)
            self.assertTrue(url.endswith("fig-001.webp"), url)
            # 小图因尺寸阈值被过滤
            self.assertEqual(len(tables), 0)

    def test_caption_render_keeps_composite_figure_whole(self):
        # 复合大图（a/b 两个子图并排、共用一个 caption）必须被整块渲染为 1 张图
        with tempfile.TemporaryDirectory() as d:
            pdf_path = Path(d) / "composite.pdf"
            fig_dir = Path(d) / "figures"
            tbl_dir = Path(d) / "tables"

            sub_a = self._make_png_bytes((300, 200), (220, 80, 80))
            sub_b = self._make_png_bytes((300, 200), (80, 120, 220))

            doc = fitz.open()
            page = doc.new_page()
            page.insert_textbox(fitz.Rect(40, 30, 550, 60), "Body text above the figure.", fontsize=11)
            page.insert_image(fitz.Rect(40, 80, 300, 280), stream=sub_a)
            page.insert_image(fitz.Rect(320, 80, 580, 280), stream=sub_b)
            page.insert_textbox(fitz.Rect(40, 300, 580, 330), "Figure 1: Overview", fontsize=12)
            doc.save(pdf_path)
            doc.close()

            figures, tables = self.mod._extract_media_with_caption_render(
                str(pdf_path),
                str(fig_dir),
                "assets/figures/arxiv/test-paper",
                str(tbl_dir),
                "assets/tables/arxiv/test-paper",
            )

            self.assertEqual(len(figures), 1)
            self.assertEqual(len(tables), 0)
            self.assertEqual(figures[0]["index"], 1)
            self.assertEqual(figures[0]["page"], 1)
            self.assertEqual(figures[0]["label"], "Figure")
            self.assertTrue(figures[0]["url"].endswith("fig-001.webp"))
            out_file = fig_dir / "fig-001.webp"
            self.assertTrue(out_file.exists())
            with Image.open(out_file) as img:
                self.assertEqual(img.format, "WEBP")
                # 整页宽渲染：两个并排子图在同一张图里，宽度远大于单个子图
                self.assertGreater(img.width, 1000)
            meta_path = fig_dir / "meta.json"
            self.assertTrue(meta_path.exists())
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(len(meta["figures"]), 1)
            self.assertEqual(meta["version"], 2)

    def test_caption_render_excludes_body_prose(self):
        # Regression: caption-render regions must start below overlying body
        # prose (no prose captured) and trim horizontally to the figure extent
        # (no full-width capture of surrounding prose).
        with tempfile.TemporaryDirectory() as d:
            pdf_path = Path(d) / "prose.pdf"
            fig_dir = Path(d) / "figures"
            tbl_dir = Path(d) / "tables"

            sub_a = self._make_png_bytes((300, 200), (220, 80, 80))
            sub_b = self._make_png_bytes((300, 200), (80, 120, 220))

            doc = fitz.open()
            page = doc.new_page()
            # Tall multi-line body paragraph near the top of the page.
            page.insert_textbox(fitz.Rect(60, 60, 540, 140), "A " + "long body paragraph. " * 20)
            # Composite figure in the lower-right ONLY, not full width.
            page.insert_image(fitz.Rect(300, 180, 400, 300), stream=sub_a)
            page.insert_image(fitz.Rect(420, 180, 520, 300), stream=sub_b)
            page.insert_textbox(fitz.Rect(60, 320, 540, 340), "Figure 1: Overview")
            doc.save(pdf_path)
            doc.close()

            figures, tables = self.mod._extract_media_with_caption_render(
                str(pdf_path),
                str(fig_dir),
                "assets/figures/arxiv/test-paper",
                str(tbl_dir),
                "assets/tables/arxiv/test-paper",
            )

            self.assertEqual(len(figures), 1)
            self.assertEqual(len(tables), 0)
            self.assertEqual(figures[0]["index"], 1)
            out_file = fig_dir / "fig-001.webp"
            self.assertTrue(out_file.exists())

            # Directly verify the computed region avoids the prose band and the
            # left margin (prose is full-width-ish; the figure is right-side).
            doc = fitz.open(pdf_path)
            try:
                page = doc[0]
                captions = self.mod._find_caption_blocks(page)
                self.assertEqual(len(captions), 1)
                regions = self.mod._figure_regions_for_page(page, captions, page.rect)
                self.assertEqual(len(regions), 1)
                cap, region = regions[0]
                self.assertEqual(cap["num"], 1)

                prose_y1 = None
                for block in page.get_text("dict").get("blocks") or []:
                    if block.get("type") != 0:
                        continue
                    text = "".join(
                        span.get("text") or ""
                        for line in block.get("lines") or []
                        for span in line.get("spans") or []
                    )
                    if "long body paragraph" in text:
                        prose_y1 = block["bbox"][3]
                self.assertIsNotNone(prose_y1)
                # Region must start below the body prose (prose excluded).
                self.assertGreaterEqual(region.y0, prose_y1)
                # Horizontal extent trimmed to the right-side figure, not the
                # full page (no left prose captured).
                self.assertGreaterEqual(region.x0, 280)
                self.assertLess(region.x1, page.rect.x1)
            finally:
                doc.close()

    def test_extract_figures_from_pdf_bytes_empty(self):
        self.assertEqual(self.mod.extract_figures_from_pdf_bytes(b"", "/tmp/x"), ([], []))

    def test_papercropper_failure_is_reported_before_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            tmp_dir = Path(d)
            pdf_path = tmp_dir / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")

            original_resolve = self.mod._resolve_papercropper
            original_run = self.mod.subprocess.run

            class DummyResult:
                returncode = 1
                stdout = "starting"
                stderr = "ModuleNotFoundError: No module named 'scipy'"

            def fake_run(*args, **kwargs):
                return DummyResult()

            self.mod._resolve_papercropper = lambda: (sys.executable, "/tmp/extract.py", "/tmp/model.pt")
            self.mod.subprocess.run = fake_run
            output = io.StringIO()
            try:
                with contextlib.redirect_stdout(output):
                    figures, tables = self.mod._extract_media_with_papercropper(
                        str(pdf_path),
                        str(tmp_dir / "figures"),
                        "assets/figures/arxiv/sample",
                        str(tmp_dir / "tables"),
                        "assets/tables/arxiv/sample",
                    )
            finally:
                self.mod._resolve_papercropper = original_resolve
                self.mod.subprocess.run = original_run

            self.assertEqual(figures, [])
            self.assertEqual(tables, [])
            self.assertIn("PaperCropper 表格/图表提取降级", output.getvalue())
            self.assertIn("No module named 'scipy'", output.getvalue())

    def test_papercropper_empty_output_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            tmp_dir = Path(d)
            pdf_path = tmp_dir / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")

            original_resolve = self.mod._resolve_papercropper
            original_run = self.mod.subprocess.run

            class DummyResult:
                returncode = 0
                stdout = "done"
                stderr = ""

            def fake_run(*args, **kwargs):
                return DummyResult()

            self.mod._resolve_papercropper = lambda: (sys.executable, "/tmp/extract.py", "/tmp/model.pt")
            self.mod.subprocess.run = fake_run
            output = io.StringIO()
            try:
                with contextlib.redirect_stdout(output):
                    figures, tables = self.mod._extract_media_with_papercropper(
                        str(pdf_path),
                        str(tmp_dir / "figures"),
                        "assets/figures/arxiv/sample",
                        str(tmp_dir / "tables"),
                        "assets/tables/arxiv/sample",
                    )
            finally:
                self.mod._resolve_papercropper = original_resolve
                self.mod.subprocess.run = original_run

            self.assertEqual(figures, [])
            self.assertEqual(tables, [])
            self.assertIn("执行完成但未产出 figure/table", output.getvalue())

    def test_ensure_paper_media_pymupdf_fallback(self):
        # Regression: ensure_paper_media must write the PDF to a temp file that
        # PyMuPDF can open by path. Windows NamedTemporaryFile(delete=True)
        # keeps the handle locked, which broke fitz.open -> Permission denied.
        with tempfile.TemporaryDirectory() as d:
            docs_dir = Path(d)
            out_dir = docs_dir / "assets" / "figures" / "arxiv" / "test-paper"

            big_img = self._make_png_bytes((640, 480), (220, 80, 80))
            pdf_path = Path(d) / "src.pdf"
            doc = fitz.open()
            page = doc.new_page()
            page.insert_image(fitz.Rect(40, 40, 400, 320), stream=big_img)
            doc.save(pdf_path)
            doc.close()
            pdf_bytes = pdf_path.read_bytes()

            original_download = self.mod._download_pdf_bytes
            original_resolve = self.mod._resolve_papercropper
            original_docling = self.mod.docling_enabled
            try:
                self.mod._download_pdf_bytes = lambda url: pdf_bytes
                # force the pymupdf fallback branch (no docling, no papercropper available)
                self.mod.docling_enabled = lambda: False
                self.mod._resolve_papercropper = lambda: ("", "", "")
                figures, tables = self.mod.ensure_paper_media(
                    pdf_url="https://example.com/x.pdf",
                    docs_dir=str(docs_dir),
                    source_key="arxiv",
                    asset_key="test-paper",
                    force=True,
                )
            finally:
                self.mod._download_pdf_bytes = original_download
                self.mod._resolve_papercropper = original_resolve
                self.mod.docling_enabled = original_docling

            self.assertEqual(len(figures), 1)
            self.assertTrue(out_dir.joinpath("fig-001.webp").exists())
            self.assertTrue(out_dir.joinpath("meta.json").exists())
            self.assertEqual(tables, [])


if __name__ == "__main__":
    unittest.main()
