#!/usr/bin/env python3
"""One-shot Docling figure extractor used by Daily Paper.

The process intentionally exits after one PDF. This keeps Docling and its
layout model out of the always-on Connect Hub process.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def _safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-") or "paper"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf")
    parser.add_argument("--output", required=True)
    parser.add_argument("--scale", type=float, default=2.0)
    args = parser.parse_args()

    from docling.datamodel.document import PictureItem
    from docling.document_converter import DocumentConverter

    pdf = Path(args.pdf).resolve()
    slug = _safe_slug(pdf.stem)
    root = Path(args.output).resolve() / slug
    figures = root / "figures"
    figures.mkdir(parents=True, exist_ok=True)

    converter = DocumentConverter()
    converted = converter.convert(pdf)
    anchors = []
    raw_figures = []
    index = 0
    for item, _level in converted.document.iterate_items():
        if not isinstance(item, PictureItem):
            continue
        try:
            image = item.get_image(converted.document)
        except Exception:
            continue
        if image is None:
            continue
        index += 1
        name = f"fig{index}.png"
        image.save(figures / name)
        caption = ""
        try:
            caption = str(item.caption_text(converted.document) or "").strip()
        except Exception:
            pass
        page_no = 0
        if getattr(item, "prov", None):
            page_no = int(getattr(item.prov[0], "page_no", 0) or 0)
        record = {
            "index": index,
            "image_path": f"figures/{name}",
            "caption": caption,
            "page_no": page_no,
        }
        raw_figures.append(record)
        anchors.append({"key": f"fig{index}", **record})

    payload = {
        "schema_version": 1,
        "paper": {"slug": slug, "source": str(pdf)},
        "anchors": anchors,
        "raw_figures": raw_figures,
        "auxiliary_picture_indices": [],
    }
    (root / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(str(root / "result.json"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
