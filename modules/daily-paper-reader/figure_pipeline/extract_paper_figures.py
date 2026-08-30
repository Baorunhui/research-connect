#!/usr/bin/env python3
"""Docling whole-figure extraction for daily-paper-reader.

Contract (consumed by src/docling_figures.py):
  CLI:  extract_paper_figures.py <pdf> --output <dir> --scale <n>
  Out:  <dir>/<slug>/result.json  (schema_version 1)
        paper: {slug}
        anchors: [{key, caption, page_no, section, image_path}]
        auxiliary_picture_indices: [int]
        raw_figures: [{index, image_path, caption}]
        image_path is relative to <dir>/<slug>/ (e.g. "figures/fig1.png")
  Plus the PNG files under <dir>/<slug>/.

Strategy: numbered captions ("Figure N:" / "Table N:") are anchors. All
PictureItems on the same page (same column for two-column layouts) adjacent
to the caption are unioned into one crop, so composite figures are not
split. Unnumbered pictures are emitted as auxiliary raw figures.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

try:
    import resource
except ImportError:  # Windows; docling_figures injects a stub before running us
    resource = None  # type: ignore

FIG_CAPTION_RE = re.compile(
    r"^\s*(?:Figure|Fig\.?)\s+([A-Za-z]?)(?:\.)?\s*(\d+(?:\.\d+)*)\s*[:.\-]?\s*(.*)$"
)
TABLE_CAPTION_RE = re.compile(
    r"^\s*(?:Table|Tbl\.?)\s+([A-Za-z]?)(?:\.)?\s*(\d+(?:\.\d+)*)\s*[:.\-]?\s*(.*)$"
)
APPENDIX_HEADER_RE = re.compile(r"\bAppendix\s+([A-Z])\b", re.IGNORECASE)

FIG_ABOVE_GAP = 0.16
FIG_BELOW_GAP = 0.10
MERGE_GAP = 0.06
MIN_PIC_AREA = 0.0015


@dataclass
class Box:
    page_no: int
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    def union(self, other: "Box") -> "Box":
        return Box(
            self.page_no,
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )


@dataclass
class Caption:
    key: str
    kind: str  # "figure" | "table"
    text: str
    box: Box
    section: str = "body"


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    return " ".join(text.split())


def _sanitize_slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return (slug or "paper")[:60]


def _item_box(item, doc) -> Optional[Box]:
    provs = getattr(item, "prov", None) or []
    if not provs:
        return None
    p = provs[0]
    bbox = getattr(p, "bbox", None)
    if bbox is None:
        return None
    page_no = int(getattr(p, "page_no", 0) or 0)
    l, t, r, b = float(bbox.l), float(bbox.t), float(bbox.r), float(bbox.b)
    origin = str(getattr(bbox, "coord_origin", "topleft") or "topleft").lower()
    page = doc.pages.get(page_no)
    size = getattr(page, "size", None) if page is not None else None
    w = float(getattr(size, "width", 0) or 0) if size is not None else 0.0
    h = float(getattr(size, "height", 0) or 0) if size is not None else 0.0
    absolute = w > 10 and h > 10 and max(r, b) > 1.5
    if origin != "topleft":
        if absolute:
            t, b = h - t, h - b
        else:
            t, b = 1.0 - t, 1.0 - b
    if absolute:
        l, t, r, b = l / w, t / h, r / w, b / h
    return Box(page_no, l, t, r, b)


def _label(item) -> str:
    return str(getattr(item, "label", "") or "").lower()


def _text(item) -> str:
    return _norm(getattr(item, "text", "") or "")


def _collect_captions(doc) -> List[Caption]:
    captions: List[Caption] = []
    section = "body"
    appendix_letter = ""
    for item in doc.texts:
        label = _label(item)
        text = _text(item)
        if not text:
            continue
        box = _item_box(item, doc)
        if box is None:
            continue
        if label in ("section_header", "title"):
            m = APPENDIX_HEADER_RE.search(text)
            if m:
                appendix_letter = m.group(1).upper()
            if label == "section_header":
                section = text[:80]
        if label not in ("caption", "text"):
            continue
        for regex, kind in ((FIG_CAPTION_RE, "figure"), (TABLE_CAPTION_RE, "table")):
            m = regex.match(text)
            if not m:
                continue
            letter, number, _rest = m.group(1), m.group(2), m.group(3)
            if letter:
                letter = letter.upper()
            elif kind == "figure" and appendix_letter:
                letter = appendix_letter
            prefix = "fig" if kind == "figure" else "table"
            key = f"{prefix}{letter.lower() if letter else ''}{number.replace('.', '')}"
            captions.append(
                Caption(key=key, kind=kind, text=text, box=box, section=section)
            )
            break
    return _merge_captions(captions)


def _merge_captions(captions: List[Caption]) -> List[Caption]:
    groups: Dict[tuple, List[Caption]] = {}
    for cap in captions:
        groups.setdefault((cap.box.page_no, cap.kind, cap.key), []).append(cap)
    merged: List[Caption] = []
    for caps in groups.values():
        caps.sort(key=lambda c: (c.box.y0, c.box.x0))
        head = caps[0]
        for other in caps[1:]:
            if (
                other.box.page_no == head.box.page_no
                and (other.box.y0 - head.box.y1) < MERGE_GAP
            ):
                head.text = (head.text + " " + other.text).strip()
                head.box = head.box.union(other.box)
            else:
                merged.append(head)
                head = other
        merged.append(head)
    merged.sort(key=lambda c: (c.box.page_no, c.box.y0))
    seen = set()
    out: List[Caption] = []
    for cap in merged:
        if cap.key in seen:
            continue
        seen.add(cap.key)
        out.append(cap)
    return out


def _same_column(a: Box, b: Box) -> bool:
    if a.width > 0.45 or b.width > 0.45:
        return True
    return (a.cx < 0.5) == (b.cx < 0.5)


def _overlaps_x(a: Box, b: Box) -> bool:
    return min(a.x1, b.x1) - max(a.x0, b.x0) > 0


def _associate(
    items: List[Box], cap: Box, above_gap: float, below_gap: float
) -> List[int]:
    hits = []
    for i, box in enumerate(items):
        if box.page_no != cap.page_no:
            continue
        if not _same_column(box, cap) or not _overlaps_x(box, cap):
            continue
        d_above = cap.y0 - box.y1
        d_below = box.y0 - cap.y1
        if 0 <= d_above <= above_gap or 0 <= d_below <= below_gap:
            hits.append(i)
    return hits


def extract(pdf_path: Path, out_root: Path, scale: float) -> int:
    import pypdfium2 as pdfium
    from docling.document_converter import DocumentConverter

    slug = _sanitize_slug(pdf_path.stem)
    slug_dir = out_root / slug
    fig_dir = slug_dir / "figures"
    raw_dir = slug_dir / "raw_figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    print(f"[figure_pipeline] converting {pdf_path.name} with Docling ...", flush=True)
    converter = DocumentConverter()
    result = converter.convert(str(pdf_path))
    doc = result.document

    pdf = pdfium.PdfDocument(str(pdf_path))
    page_cache: Dict[int, object] = {}

    def page_image(page_no: int):
        if page_no not in page_cache:
            page = pdf[page_no - 1]
            page_cache[page_no] = page.render(scale=scale).to_pil()
        return page_cache[page_no]

    def save_crop(box: Box, out_path: Path) -> None:
        img = page_image(box.page_no)
        w, h = img.size
        pad_x = int(0.005 * w)
        pad_y = int(0.005 * h)
        x0 = max(0, int(box.x0 * w) - pad_x)
        y0 = max(0, int(box.y0 * h) - pad_y)
        x1 = min(w, int(box.x1 * w) + pad_x)
        y1 = min(h, int(box.y1 * h) + pad_y)
        if x1 - x0 < 8 or y1 - y0 < 8:
            raise ValueError("crop too small")
        img.crop((x0, y0, x1, y1)).save(out_path, format="PNG")

    pictures = []
    for item in doc.pictures:
        box = _item_box(item, doc)
        if box is None or box.width * box.height < MIN_PIC_AREA:
            continue
        pictures.append((item, box))

    table_boxes: List[Box] = []
    for item in doc.tables:
        box = _item_box(item, doc)
        if box is not None:
            table_boxes.append(box)

    captions = _collect_captions(doc)

    anchors = []
    consumed = set()
    for cap in captions:
        if cap.kind == "figure":
            hits = _associate(
                [b for _, b in pictures], cap.box, FIG_ABOVE_GAP, FIG_BELOW_GAP
            )
            if not hits:
                continue
            region = cap.box
            for i in hits:
                consumed.add(i)
                region = region.union(pictures[i][1])
        else:
            hits = _associate(table_boxes, cap.box, FIG_ABOVE_GAP, FIG_BELOW_GAP)
            if not hits:
                continue
            region = cap.box
            for i in hits:
                region = region.union(table_boxes[i])
        image_rel = f"figures/{cap.key}.png"
        out_path = slug_dir / image_rel
        try:
            save_crop(region, out_path)
        except Exception as e:
            print(f"[figure_pipeline] crop failed for {cap.key}: {e}", flush=True)
            continue
        anchors.append(
            {
                "key": cap.key,
                "caption": cap.text,
                "page_no": cap.box.page_no,
                "section": cap.section,
                "image_path": image_rel,
            }
        )

    raw_figures = []
    aux_indices = []
    for i, (item, box) in enumerate(pictures):
        if i in consumed:
            continue
        out_path = raw_dir / f"figure_{i:03d}.png"
        try:
            img = getattr(item, "image", None)
            if img is not None:
                img.save(out_path, format="PNG")
            else:
                save_crop(box, out_path)
        except Exception as e:
            print(f"[figure_pipeline] raw figure {i} failed: {e}", flush=True)
            continue
        raw_figures.append(
            {
                "index": i,
                "image_path": f"raw_figures/figure_{i:03d}.png",
                "caption": "",
            }
        )
        aux_indices.append(i)

    payload = {
        "schema_version": 1,
        "paper": {"slug": slug},
        "anchors": anchors,
        "auxiliary_picture_indices": aux_indices,
        "raw_figures": raw_figures,
    }
    with open(slug_dir / "result.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(
        f"[figure_pipeline] {slug}: {len(anchors)} anchors, "
        f"{len(aux_indices)} auxiliary figures",
        flush=True,
    )
    if resource is not None:
        try:
            ru = resource.getrusage(resource.RUSAGE_SELF)
            print(f"[figure_pipeline] peak_rss={ru.ru_maxrss}", flush=True)
        except Exception:
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Docling whole-figure extraction")
    parser.add_argument("pdf")
    parser.add_argument("--output", required=True)
    parser.add_argument("--scale", type=float, default=2.0)
    args = parser.parse_args()
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"[figure_pipeline] PDF not found: {pdf_path}", flush=True)
        return 2
    out_root = Path(args.output)
    out_root.mkdir(parents=True, exist_ok=True)
    return extract(pdf_path, out_root, float(args.scale))


if __name__ == "__main__":
    sys.exit(main())
