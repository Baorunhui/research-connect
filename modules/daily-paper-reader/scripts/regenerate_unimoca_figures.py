#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
One-off regeneration driver for UniMoCa (arXiv 2608.01944v1) figures + captions.

The paper's on-disk figure assets were produced by the OLD bitmap extractor
(`pymupdf-images`): figures were split into fragments / bare 640x360 frames and
all captions were empty. This driver:

  1. Reconstructs the paper dict from the markdown YAML front matter
     (title / method) + the English `## Abstract` body section.
  2. FORCE re-extracts figures+tables with the NEW caption-render path
     (whole-page-region per figure caption) via
     `src.paper_figures.ensure_paper_media(..., force=True)`.
  3. Runs the text-based interpretation
     (`src.figure_interpretation.interpret_paper_figures`) — captions +
     body references + abstract/method context, batched text-LLM summaries,
     importance-first reorder.
  4. Writes the interpreted captions back to the markdown front matter
     `figures_json` (and `tables_json` if tables exist), preserving the rest
     of the file byte-for-byte (CRLF-aware).

Usage (from repo root):
    python scripts/regenerate_unimoca_figures.py

Consumes real text-LLM API calls (user approved the spend).
"""

import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Load .env (repo root) so LLM_API_KEY / LLM_BASE_URL are available.
# Without this the text client resolves an empty key and the interpretation
# is silently skipped.
from local_env import load_local_env  # noqa: E402

load_local_env()

MD_PATH = (
    REPO_ROOT
    / "docs"
    / "20260803-20260812"
    / "2608.01944v1-unimoca-unifying-motion-and-camera-controls-as-visual-proxies-for-faithful-human-video-generation.md"
)
DOCS_DIR = str(REPO_ROOT / "docs")
PDF_URL = "https://arxiv.org/pdf/2608.01944v1"
SOURCE_KEY = "arxiv"
ASSET_KEY = "2608.01944v1"

# Force the NEW caption-render extractor (default-on, but be explicit).
os.environ["DPR_USE_CAPTION_RENDER"] = "1"


# ---------------------------------------------------------------------------
# Front matter / abstract helpers
# ---------------------------------------------------------------------------

def _parse_front_matter(md_text: str) -> dict:
    """Parse the YAML front matter block (between leading `---` and closing `---`)."""
    text = md_text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[4:end]
    try:
        import yaml

        data = yaml.safe_load(block) or {}
        return data if isinstance(data, dict) else {}
    except Exception as e:  # pragma: no cover - fallback path
        print(f"[WARN] PyYAML parse failed ({e}); falling back to key:value line parsing")
        out: dict = {}
        for line in block.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
        return out


def _extract_abstract(body: str) -> str:
    """Extract the English `## Abstract` paragraph, stripping LaTeX/markdown markup."""
    m = re.search(r"^##\s+Abstract\s*$", body, re.M)
    if not m:
        return ""
    start = m.end()
    nxt = re.search(r"^##\s", body[start:], re.M)
    end = start + nxt.start() if nxt else len(body)
    para = body[start:end].strip()
    # \textbf{...} -> inner text
    para = re.sub(r"\\textbf\{([^{}]*)\}", r"\1", para)
    # drop remaining backslash commands (\emph, \cite, ...)
    para = re.sub(r"\\[a-zA-Z]+\*?", "", para)
    # remove stray braces
    para = para.replace("{", "").replace("}", "")
    para = re.sub(r"\s+", " ", para).strip()
    return para


# ---------------------------------------------------------------------------
# Front matter field upsert (CRLF-preserving, mirrors 6.generate_docs helpers)
# ---------------------------------------------------------------------------

def _yaml_escape_value(s: str) -> str:
    """Same escaping as src/6.generate_docs.yaml_escape_value (quoted JSON string style)."""
    if not s:
        return '""'
    if any(c in s for c in [":", "#", '"', "'", "\n", "[", "]", "{", "}", ",", "&", "*", "!", "|", ">", "%", "@", "`"]):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return s


def _line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    return "\n"


def _upsert_front_matter_field(md_text: str, key: str, value: str):
    """
    Minimal idempotent front-matter field upsert that preserves the rest of the
    file byte-for-byte (including CRLF line endings). Mirrors the semantics of
    src/6.generate_docs.upsert_front_matter_field without importing that heavy
    module (which pulls daily_report_state + module-level LLM resolution).
    """
    text = str(md_text or "")
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        return text, False
    eol = _line_ending(text)
    lines = text.splitlines(keepends=True)
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            end_idx = i
            break
    if end_idx is None:
        return text, False
    block = lines[1:end_idx]
    replaced = False
    new_block = []
    for line in block:
        if line.rstrip("\r\n").startswith(f"{key}:"):
            new_block.append(f"{key}: {value}{eol}")
            replaced = True
        else:
            new_block.append(line)
    if not replaced:
        new_block.append(f"{key}: {value}{eol}")
    updated = "".join([lines[0]] + new_block + [lines[end_idx]] + lines[end_idx + 1 :])
    return updated, updated != text


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

def _page_distribution(figures: list) -> str:
    pages: dict = {}
    for f in figures:
        pages[int(f.get("page") or 0)] = pages.get(int(f.get("page") or 0), 0) + 1
    return ", ".join(f"p{p}:{n}" for p, n in sorted(pages.items()))


def _clean_stale_media(figure_dir: str, table_dir: str, figures: list, tables: list) -> list:
    """Remove webp files in the media dirs that are no longer referenced by meta.json."""
    removed: list = []
    for d, items in ((figure_dir, figures), (table_dir, tables)):
        if not os.path.isdir(d):
            continue
        referenced = {os.path.basename(str(it.get("url") or "")) for it in items}
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith(".webp"):
                continue
            if name not in referenced:
                p = os.path.join(d, name)
                try:
                    os.remove(p)
                    removed.append(p)
                except OSError as e:
                    print(f"[WARN] 无法删除陈旧文件 {p}: {e}")
    return removed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 78)
    print("UniMoCa (2608.01944v1) figure regeneration driver")
    print("=" * 78, flush=True)

    # ---- 1. Reconstruct paper dict from markdown -------------------------
    if not MD_PATH.exists():
        print(f"[ERROR] markdown 不存在: {MD_PATH}")
        return 1
    md_text = MD_PATH.read_text(encoding="utf-8")
    fm = _parse_front_matter(md_text)
    title = str(fm.get("title") or "").strip()
    method = str(fm.get("method") or "").strip()
    pdf_url = str(fm.get("pdf") or "").strip() or PDF_URL
    body = md_text.split("\n---", 1)[1] if "\n---" in md_text else md_text
    abstract = _extract_abstract(body)
    if not abstract:
        print("[WARN] 未能从正文提取英文 Abstract，回退为仅用 title 作为上下文")
        abstract = title
    paper = {"title": title, "abstract": abstract, "method": method}
    print(f"[1] paper dict: title={title[:60]!r}... method={method[:40]!r}... abstract_len={len(abstract)}")
    print(f"    pdf_url={pdf_url}", flush=True)

    # ---- 2. Force re-extract figures+tables (new caption-render path) ----
    from paper_figures import ensure_paper_media

    print("[2] 强制重新提取图表（caption-render 路径，重新下载 PDF）...", flush=True)
    try:
        figures, tables = ensure_paper_media(
            pdf_url=pdf_url,
            docs_dir=DOCS_DIR,
            source_key=SOURCE_KEY,
            asset_key=ASSET_KEY,
            force=True,
        )
    except Exception as e:
        print(f"[ERROR] PDF 下载/提取失败，停止（不对陈旧图表做解读）: {e}")
        return 1
    if not figures:
        print("[ERROR] 重新提取后未得到任何 figure，停止")
        return 1

    figure_dir = os.path.join(DOCS_DIR, "assets", "figures", SOURCE_KEY, ASSET_KEY)
    table_dir = os.path.join(DOCS_DIR, "assets", "tables", SOURCE_KEY, ASSET_KEY)
    meta_path = os.path.join(figure_dir, "meta.json")
    extractor = ""
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            extractor = str((json.load(f) or {}).get("extractor") or "")
    except Exception as e:
        print(f"[WARN] 读取 meta.json 失败: {e}")

    removed = _clean_stale_media(figure_dir, table_dir, figures, tables)
    if removed:
        print(f"    清理 {len(removed)} 个陈旧 webp 文件（不再被 meta.json 引用）")
        for p in removed:
            print(f"      - {os.path.basename(p)}")

    print(f"[2] 结果: figures={len(figures)} tables={len(tables)} extractor={extractor!r}")
    print(f"    figure 页面分布: {_page_distribution(figures)}")
    if tables:
        print(f"    table 页面分布: {_page_distribution(tables)}")
    print(flush=True)

    # ---- 3. Enhanced vision interpretation -------------------------------
    from figure_interpretation import interpret_paper_figures

    from llm import resolve_llm_api_key

    vision_key = (resolve_llm_api_key() or "").strip()
    if not vision_key:
        print("[ERROR] 未找到视觉模型 API Key（.env 中 LLM_API_KEY 缺失），停止，不伪造 caption")
        return 3
    print(f"[3] 视觉模型 API Key 已就绪（长度 {len(vision_key)}）")

    print("[3] 运行纯文本解读（图注 + 正文引用段 → 批量文本模型，重要性重排）...", flush=True)
    new_figures, new_tables = figures, tables
    try:
        new_figures, new_tables = interpret_paper_figures(figures, tables, paper, DOCS_DIR, full_text=body, client=None)
    except Exception as e:
        print(f"[WARN] 图表解读整体失败（best-effort 继续，不伪造 caption）: {e}")

    captioned = [f for f in new_figures if str(f.get("caption") or "").strip()]
    print(f"[3] 解读完成: figures={len(new_figures)}（其中 {len(captioned)} 个有非空 caption）")
    for f in new_figures:
        cap = str(f.get("caption") or "").strip()
        print(f"    fig idx={f.get('index')} page={f.get('page')} cat={f.get('category')!r} caption={cap[:80]!r}")
    if new_tables:
        for t in new_tables:
            cap = str(t.get("caption") or "").strip()
            print(f"    table idx={t.get('index')} page={t.get('page')} cat={t.get('category')!r} caption={cap[:80]!r}")
    print(flush=True)

    # ---- 4. Write captions back to front matter --------------------------
    if not captioned:
        print("[4] 所有 caption 仍为空（解读未产出有效内容），跳过 front matter 更新，避免伪造")
        return 2

    updated_text = md_text
    changed = False
    figures_json = _yaml_escape_value(json.dumps(new_figures, ensure_ascii=False))
    updated_text, c1 = _upsert_front_matter_field(updated_text, "figures_json", figures_json)
    changed = changed or c1
    if new_tables:
        tables_json = _yaml_escape_value(json.dumps(new_tables, ensure_ascii=False))
        updated_text, c2 = _upsert_front_matter_field(updated_text, "tables_json", tables_json)
        changed = changed or c2

    if changed:
        MD_PATH.write_text(updated_text, encoding="utf-8")
        print(f"[4] 已更新 front matter: {MD_PATH}")
        print(f"    figures_json 长度: {len(figures_json)} 字符")
        if new_tables:
            print(f"    tables_json 长度: {len(json.dumps(new_tables, ensure_ascii=False))} 字符")
    else:
        print("[4] front matter 未发生变化（内容与现有值一致）")
    print(flush=True)

    # ---- 5. Verification -------------------------------------------------
    print("=" * 78)
    print("VERIFICATION")
    print("=" * 78)
    print(f"figure 数量: {len(new_figures)}")
    print(f"extractor (meta.json): {extractor!r}  (期望 'caption-render')")
    print(f"figure 页面分布: {_page_distribution(new_figures)}")
    print(f"tables 数量: {len(new_tables)}")
    print(f"非空 caption 数量: {len(captioned)} / {len(new_figures)}")
    print(f"markdown: {MD_PATH}")
    print(f"figures_json 已更新: {changed}")
    if changed:
        # before/after excerpt of the figures_json line
        old_line = next((ln for ln in md_text.splitlines() if ln.startswith("figures_json:")), "")
        new_line = next((ln for ln in updated_text.splitlines() if ln.startswith("figures_json:")), "")
        print(f"  before: {old_line[:120]}...")
        print(f"  after : {new_line[:120]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())