from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Tuple

import fitz
import requests
from PIL import Image

from docling_figures import (
    extract_media_with_docling,
    docling_enabled,
    resolve_docling,
)


MIN_FIGURE_WIDTH = 240
MIN_FIGURE_HEIGHT = 180
MIN_FIGURE_AREA = 120_000
WEBP_QUALITY = 82
FIGURE_META_VERSION = 2
PAPERCROPPER_SCRIPT_ENV = "PAPERCROPPER_SCRIPT"
PAPERCROPPER_DIR_ENV = "PAPERCROPPER_DIR"
PAPERCROPPER_MODEL_ENV = "PAPERCROPPER_MODEL"
PAPERCROPPER_PYTHON_ENV = "PAPERCROPPER_PYTHON"
PAPERCROPPER_DISABLE_ENV = "PAPERCROPPER_DISABLE"
PAPERCROPPER_MODEL_FILENAME = "doclayout_yolo_docstructbench_imgsz1280_2501.pt"
PAPERCROPPER_LOG_LIMIT = 1200
DPR_CAPTION_RENDER_ENV = "DPR_USE_CAPTION_RENDER"
CAPTION_GAP = 4.0
# Text blocks at least this tall (in PDF points) are treated as body prose,
# never as figure content, and therefore excluded from the crop region.
# Multi-line paragraphs are typically 25-70pt; figure labels are short single
# lines (~6-15pt).
PROSE_MIN_HEIGHT = 24.0
FIGURE_CAPTION_RE = re.compile(r"^(?:Figure|Fig\.?)\s*(\d+)\s*[.:]?", re.IGNORECASE)
TABLE_CAPTION_RE = re.compile(r"^Table\s*(\d+)\s*[.:]?", re.IGNORECASE)


def _safe_asset_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "paper"
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text)
    text = text.strip("-._")
    return text or "paper"


def _relative_prefix(source_key: str, asset_key: str) -> str:
    return "/".join(["assets", "figures", source_key, _safe_asset_key(asset_key)])


def _absolute_dir(docs_dir: str, source_key: str, asset_key: str) -> str:
    return os.path.join(docs_dir, "assets", "figures", source_key, _safe_asset_key(asset_key))


def _relative_tables_prefix(source_key: str, asset_key: str) -> str:
    return "/".join(["assets", "tables", source_key, _safe_asset_key(asset_key)])


def _absolute_tables_dir(docs_dir: str, source_key: str, asset_key: str) -> str:
    return os.path.join(docs_dir, "assets", "tables", source_key, _safe_asset_key(asset_key))


def _load_cached_media(meta_path: str, key: str) -> List[Dict[str, Any]]:
    if not os.path.exists(meta_path):
        return []
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            payload = json.load(f) or {}
    except Exception:
        return []
    if int(payload.get("version") or 0) != FIGURE_META_VERSION:
        return []
    items = payload.get(key)
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        out.append(
            {
                "url": url,
                "caption": str(item.get("caption") or "").strip(),
                "page": int(item.get("page") or 0),
                "index": int(item.get("index") or 0),
                "width": int(item.get("width") or 0),
                "height": int(item.get("height") or 0),
            }
        )
    return out


def _load_cached_figures(meta_path: str) -> List[Dict[str, Any]]:
    return _load_cached_media(meta_path, "figures")


def _load_cached_tables(meta_path: str) -> List[Dict[str, Any]]:
    return _load_cached_media(meta_path, "tables")


def _save_media_meta(meta_path: str, items: List[Dict[str, Any]], *, extractor: str, key: str) -> None:
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "version": FIGURE_META_VERSION,
                "extractor": extractor,
                key: items,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def _save_figures_meta(meta_path: str, figures: List[Dict[str, Any]], *, extractor: str) -> None:
    _save_media_meta(meta_path, figures, extractor=extractor, key="figures")


def _save_tables_meta(meta_path: str, tables: List[Dict[str, Any]], *, extractor: str) -> None:
    _save_media_meta(meta_path, tables, extractor=extractor, key="tables")


def _warn_papercropper(message: str) -> None:
    print(f"[WARN] PaperCropper 表格/图表提取降级：{message}", flush=True)


def _tail_log_text(text: str, limit: int = PAPERCROPPER_LOG_LIMIT) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(compact) <= limit:
        return compact
    return "..." + compact[-limit:]


def _papercropper_was_configured() -> bool:
    return any(
        str(os.getenv(name) or "").strip()
        for name in [PAPERCROPPER_SCRIPT_ENV, PAPERCROPPER_DIR_ENV, PAPERCROPPER_MODEL_ENV, PAPERCROPPER_PYTHON_ENV]
    )


def _download_pdf_bytes(pdf_url: str, timeout: int = 90) -> bytes:
    resp = requests.get(
        str(pdf_url or "").strip(),
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=max(int(timeout or 1), 1),
    )
    resp.raise_for_status()
    return resp.content


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _caption_render_enabled() -> bool:
    value = str(os.getenv(DPR_CAPTION_RENDER_ENV) or "").strip().lower()
    return not value or _truthy_env(DPR_CAPTION_RENDER_ENV)


def _first_existing(candidates: List[str]) -> str:
    for candidate in candidates:
        path = str(candidate or "").strip()
        if path and os.path.exists(path):
            return path
    return ""


def _resolve_papercropper() -> Tuple[str, str, str]:
    if _truthy_env(PAPERCROPPER_DISABLE_ENV):
        return "", "", ""

    configured_dir = str(os.getenv(PAPERCROPPER_DIR_ENV) or "").strip()
    cache_root = os.path.expanduser("~/.cache/dpr-tools/papercropper")
    script_path = _first_existing(
        [
            str(os.getenv(PAPERCROPPER_SCRIPT_ENV) or "").strip(),
            os.path.join(configured_dir, "extract.py") if configured_dir else "",
            os.path.join(cache_root, "PaperCropper", "extract.py"),
            os.path.expanduser("~/.cache/dpr-tools/PaperCropper/extract.py"),
            "/tmp/PaperCropper/extract.py",
        ]
    )
    model_path = _first_existing(
        [
            str(os.getenv(PAPERCROPPER_MODEL_ENV) or "").strip(),
            os.path.join(configured_dir, "models", PAPERCROPPER_MODEL_FILENAME) if configured_dir else "",
            os.path.join(cache_root, "models", PAPERCROPPER_MODEL_FILENAME),
            os.path.expanduser(f"~/.cache/dpr-tools/papercropper/models/{PAPERCROPPER_MODEL_FILENAME}"),
            f"/tmp/papercropper-run/models/{PAPERCROPPER_MODEL_FILENAME}",
        ]
    )
    python_path = _first_existing(
        [
            str(os.getenv(PAPERCROPPER_PYTHON_ENV) or "").strip(),
            os.path.join(cache_root, "venv", "bin", "python"),
            "/tmp/papercropper-venv/bin/python",
            sys.executable,
        ]
    )
    if not script_path or not model_path or not python_path:
        return "", "", ""
    return python_path, script_path, model_path


def _load_image_size(path: str) -> tuple[int, int]:
    with Image.open(path) as img:
        img.load()
        return img.size


def _save_webp_from_image(img: Image.Image, dst_path: str) -> tuple[int, int]:
    width, height = img.size
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        export_img = bg
    elif img.mode != "RGB":
        export_img = img.convert("RGB")
    else:
        export_img = img.copy()
    export_img.save(dst_path, format="WEBP", quality=WEBP_QUALITY, method=6)
    return width, height


def _save_webp_from_path(src_path: str, dst_path: str) -> tuple[int, int]:
    with Image.open(src_path) as img:
        img.load()
        return _save_webp_from_image(img, dst_path)


def _natural_sort_key(path: str) -> List[Any]:
    name = os.path.basename(path)
    parts = re.split(r"(\d+)", name)
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def _collect_papercropper_pngs(
    src_dir: str,
    output_dir: str,
    relative_prefix: str,
    *,
    file_prefix: str,
    label: str,
) -> List[Dict[str, Any]]:
    if not os.path.isdir(src_dir):
        return []

    os.makedirs(output_dir, exist_ok=True)
    items: List[Dict[str, Any]] = []
    seen_hash: set[str] = set()
    paths = [
        os.path.join(src_dir, name)
        for name in os.listdir(src_dir)
        if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    ]
    for index, src_path in enumerate(sorted(paths, key=_natural_sort_key), start=1):
        try:
            with open(src_path, "rb") as f:
                sha = hashlib.sha256(f.read()).hexdigest()
        except Exception:
            continue
        if sha in seen_hash:
            continue
        seen_hash.add(sha)
        file_name = f"{file_prefix}-{len(items) + 1:03d}.webp"
        abs_path = os.path.join(output_dir, file_name)
        try:
            width, height = _save_webp_from_path(src_path, abs_path)
        except Exception:
            continue
        items.append(
            {
                "url": "/".join([relative_prefix.strip("/"), file_name]),
                "caption": "",
                "page": 0,
                "index": len(items) + 1,
                "width": width,
                "height": height,
                "label": label,
            }
        )
    return items


def _extract_media_with_papercropper(
    pdf_path: str,
    figure_output_dir: str,
    figure_relative_prefix: str,
    table_output_dir: str,
    table_relative_prefix: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    python_path, script_path, model_path = _resolve_papercropper()
    if not python_path or not script_path or not model_path:
        if not _truthy_env(PAPERCROPPER_DISABLE_ENV) and _papercropper_was_configured():
            _warn_papercropper("未找到可用的 PaperCropper 脚本或模型，改用备用图片提取器。")
        return [], []

    timeout = int(os.getenv("PAPERCROPPER_TIMEOUT_SECONDS") or "360")
    conf = str(os.getenv("PAPERCROPPER_CONF") or "0.4")
    imgsz = str(os.getenv("PAPERCROPPER_IMGSZ") or "1024")
    dpi = str(os.getenv("PAPERCROPPER_DPI") or "200")
    png_dpi = str(os.getenv("PAPERCROPPER_PNG_DPI") or "260")
    batch_size = str(os.getenv("PAPERCROPPER_BATCH_SIZE") or "4")
    padding = str(os.getenv("PAPERCROPPER_PADDING") or "2.0")

    with tempfile.TemporaryDirectory(prefix="papercropper_") as tmp_root:
        cmd = [
            python_path,
            script_path,
            "--pdf",
            pdf_path,
            "--model",
            model_path,
            "--output",
            tmp_root,
            "--formats",
            "png",
            "--targets",
            "figure,table",
            "--conf",
            conf,
            "--imgsz",
            imgsz,
            "--dpi",
            dpi,
            "--png-dpi",
            png_dpi,
            "--batch-size",
            batch_size,
            "--padding",
            padding,
        ]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(timeout, 30),
                check=False,
            )
        except subprocess.TimeoutExpired:
            _warn_papercropper(f"执行超时（>{max(timeout, 30)}s），改用备用图片提取器。")
            return [], []
        if proc.returncode != 0:
            detail = _tail_log_text("\n".join([proc.stdout or "", proc.stderr or ""]))
            suffix = f"；输出：{detail}" if detail else ""
            _warn_papercropper(f"执行失败 returncode={proc.returncode}{suffix}")
            return [], []

        doc_output = os.path.join(tmp_root, os.path.splitext(os.path.basename(pdf_path))[0])
        figures = _collect_papercropper_pngs(
            os.path.join(doc_output, "Figures_png"),
            figure_output_dir,
            figure_relative_prefix,
            file_prefix="fig",
            label="Figure",
        )
        tables = _collect_papercropper_pngs(
            os.path.join(doc_output, "Tables_png"),
            table_output_dir,
            table_relative_prefix,
            file_prefix="table",
            label="Table",
        )
        if figures:
            _save_figures_meta(os.path.join(figure_output_dir, "meta.json"), figures, extractor="papercropper")
        if tables:
            _save_tables_meta(os.path.join(table_output_dir, "meta.json"), tables, extractor="papercropper")
        if not figures and not tables:
            detail = _tail_log_text("\n".join([proc.stdout or "", proc.stderr or ""]))
            suffix = f"；输出：{detail}" if detail else ""
            _warn_papercropper(f"执行完成但未产出 figure/table{suffix}")
        else:
            print(f"[INFO] PaperCropper 提取完成：figures={len(figures)} tables={len(tables)}", flush=True)
        return figures, tables


def _find_caption_blocks(page: fitz.Page) -> List[Dict[str, Any]]:
    captions: List[Dict[str, Any]] = []
    try:
        raw_data = page.get_text("dict")
        data = raw_data if isinstance(raw_data, dict) else {}
    except Exception:
        return captions
    for block in data.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") != 0:
            continue
        spans = [span for line in block.get("lines") or [] for span in line.get("spans") or []]
        block_text = "".join(span.get("text") or "" for span in spans).strip()
        bbox_vals = block.get("bbox") or (0, 0, 0, 0)
        bbox = (float(bbox_vals[0]), float(bbox_vals[1]), float(bbox_vals[2]), float(bbox_vals[3]))
        candidates: List[Tuple[str, Tuple[float, float, float, float]]] = []
        if block_text:
            candidates.append((block_text, bbox))
        for span in spans:
            text = (span.get("text") or "").strip()
            if text:
                span_vals = span.get("bbox") or bbox_vals
                candidates.append(
                    (
                        text,
                        (
                            float(span_vals[0]),
                            float(span_vals[1]),
                            float(span_vals[2]),
                            float(span_vals[3]),
                        ),
                    )
                )
        for text, span_bbox in candidates:
            m = FIGURE_CAPTION_RE.match(text)
            if m:
                kind = "figure"
            else:
                m = TABLE_CAPTION_RE.match(text)
                kind = "table" if m else ""
            if not m or not kind:
                continue
            x0, y0, x1, y1 = span_bbox
            captions.append(
                {
                    "kind": kind,
                    "num": int(m.group(1)),
                    "caption": text,
                    "y0": y0,
                    "y1": y1,
                }
            )
            break
    return captions


def _figure_regions_for_page(
    page: fitz.Page,
    captions: List[Dict[str, Any]],
    page_rect: fitz.Rect,
) -> List[Tuple[Dict[str, Any], fitz.Rect]]:
    """Compute a crop region per caption, derived from the page's content blocks.

    Each region:
    - starts below the lowest overlying body paragraph (or below the previous
      figure's region bottom), excluding running page headers and prose;
    - ends just above the caption;
    - is trimmed horizontally to the figure's images/labels, falling back to
      full page width when the horizontal band has no image/label blocks.

    Returns (caption, region) pairs in caption y-order. Degenerate regions
    (top >= bottom) are skipped, mirroring the caller's old guard.
    """
    ordered = sorted(captions, key=lambda item: item["y0"])
    if not ordered:
        return []

    # ---- Gather content blocks: text (flagged when it carries a caption) + images ----
    text_blocks: List[Tuple[fitz.Rect, bool]] = []
    try:
        raw_data = page.get_text("dict")
        data = raw_data if isinstance(raw_data, dict) else {}
    except Exception:
        data = {}
    caption_ranges = [(cap["y0"], cap["y1"]) for cap in ordered]
    for block in data.get("blocks") or []:
        if not isinstance(block, dict) or block.get("type") != 0:
            continue
        bbox_vals = block.get("bbox") or (0, 0, 0, 0)
        bbox = fitz.Rect(
            float(bbox_vals[0]),
            float(bbox_vals[1]),
            float(bbox_vals[2]),
            float(bbox_vals[3]),
        )
        is_caption = any(cap_y0 <= bbox.y1 and bbox.y0 <= cap_y1 for cap_y0, cap_y1 in caption_ranges)
        text_blocks.append((bbox, is_caption))

    image_bboxes: List[fitz.Rect] = []
    try:
        image_bboxes = [
            fitz.Rect(*(float(v) for v in info["bbox"]))
            for info in page.get_image_info()
            if info.get("bbox") is not None and len(info["bbox"]) == 4
        ]
    except Exception:
        image_bboxes = []

    all_bboxes = [bbox for bbox, _is_caption in text_blocks] + image_bboxes
    content_top = page_rect.y0
    if all_bboxes:
        content_top = max(page_rect.y0, min(b.y0 for b in all_bboxes))

    # Body prose = non-caption text blocks tall enough to be multi-line paragraphs.
    prose_blocks = [
        bbox
        for bbox, is_caption in text_blocks
        if not is_caption and (bbox.y1 - bbox.y0) >= PROSE_MIN_HEIGHT
    ]

    regions: List[Tuple[Dict[str, Any], fitz.Rect]] = []
    prev_region_bottom = content_top
    for cap in ordered:
        bottom = min(page_rect.y1, cap["y0"] - CAPTION_GAP)

        # Top = highest of the candidate lower edges (all strictly above bottom):
        # the previous figure's bottom, or the bottom of any body paragraph
        # above this caption.
        candidates = [prev_region_bottom + CAPTION_GAP]
        candidates.extend(prose.y1 + CAPTION_GAP for prose in prose_blocks if prose.y1 < bottom)
        valid_tops = [candidate for candidate in candidates if candidate < bottom]
        if not valid_tops:
            prev_region_bottom = bottom
            continue
        top = max(page_rect.y0, max(valid_tops))

        if top >= bottom:
            prev_region_bottom = bottom
            continue

        # ---- Horizontal extent: images + short non-prose labels overlapping the band ----
        x_sources = [
            bbox
            for bbox in image_bboxes
            if bbox.y1 >= top and bbox.y0 <= bottom
        ]
        x_sources.extend(
            bbox
            for bbox, is_caption in text_blocks
            if not is_caption
            and (bbox.y1 - bbox.y0) < PROSE_MIN_HEIGHT
            and bbox.y1 >= top
            and bbox.y0 <= bottom
        )
        if x_sources:
            left = max(page_rect.x0, min(b.x0 for b in x_sources) - CAPTION_GAP)
            right = min(page_rect.x1, max(b.x1 for b in x_sources) + CAPTION_GAP)
        else:
            left, right = page_rect.x0, page_rect.x1

        if left >= right:
            left, right = page_rect.x0, page_rect.x1

        regions.append((cap, fitz.Rect(left, top, right, bottom)))
        prev_region_bottom = bottom

    return regions


def _extract_media_with_caption_render(
    pdf_path: str,
    figure_output_dir: str,
    figure_relative_prefix: str,
    table_output_dir: str,
    table_relative_prefix: str,
    *,
    dpi: int = 200,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    figures: List[Dict[str, Any]] = []
    tables: List[Dict[str, Any]] = []
    try:
        with fitz.open(pdf_path) as doc:
            zoom = max(int(dpi or 200), 1) / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            seen: set[tuple[str, int]] = set()
            for page_idx in range(len(doc)):
                page = doc[page_idx]
                captions = _find_caption_blocks(page)
                if not captions:
                    continue
                page_rect = page.rect
                for cap, region in _figure_regions_for_page(page, captions, page_rect):
                    key = (cap["kind"], cap["num"])
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        pix = page.get_pixmap(matrix=matrix, clip=region, alpha=True)
                        if (
                            pix.width < MIN_FIGURE_WIDTH
                            or pix.height < MIN_FIGURE_HEIGHT
                            or pix.width * pix.height < MIN_FIGURE_AREA
                        ):
                            continue
                        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGBA")
                    except Exception:
                        continue
                    is_figure = cap["kind"] == "figure"
                    out_dir = figure_output_dir if is_figure else table_output_dir
                    relative_prefix = figure_relative_prefix if is_figure else table_relative_prefix
                    os.makedirs(out_dir, exist_ok=True)
                    file_name = f"{'fig' if is_figure else 'table'}-{cap['num']:03d}.webp"
                    abs_path = os.path.join(out_dir, file_name)
                    try:
                        width, height = _save_webp_from_image(img, abs_path)
                    except Exception:
                        continue
                    (figures if is_figure else tables).append(
                        {
                            "url": "/".join([relative_prefix.strip("/"), file_name]),
                            "caption": cap["caption"],
                            "page": page_idx + 1,
                            "index": cap["num"],
                            "width": width,
                            "height": height,
                            "label": "Figure" if is_figure else "Table",
                        }
                    )
        if figures:
            _save_figures_meta(os.path.join(figure_output_dir, "meta.json"), figures, extractor="caption-render")
        if tables:
            _save_tables_meta(os.path.join(table_output_dir, "meta.json"), tables, extractor="caption-render")
        if figures or tables:
            print(f"[INFO] 标题渲染提取完成：figures={len(figures)} tables={len(tables)}", flush=True)
    except Exception as e:
        print(f"[WARN] 标题渲染提取失败，回退备用提取器：{e}", flush=True)
        return [], []
    return figures, tables


def extract_figures_from_pdf(
    pdf_path: str,
    output_dir: str,
    relative_prefix: str,
    *,
    min_width: int = MIN_FIGURE_WIDTH,
    min_height: int = MIN_FIGURE_HEIGHT,
    min_area: int = MIN_FIGURE_AREA,
) -> List[Dict[str, Any]]:
    os.makedirs(output_dir, exist_ok=True)
    figures: List[Dict[str, Any]] = []
    seen_xref: set[int] = set()
    seen_sha: set[str] = set()
    fig_index = 1

    with fitz.open(pdf_path) as doc:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            for image_info in page.get_images(full=True):
                xref = int(image_info[0] or 0)
                if xref <= 0 or xref in seen_xref:
                    continue
                seen_xref.add(xref)
                try:
                    raw = doc.extract_image(xref)
                except Exception:
                    continue
                image_bytes = raw.get("image") if isinstance(raw, dict) else None
                if not image_bytes:
                    continue
                sha = hashlib.sha256(image_bytes).hexdigest()
                if sha in seen_sha:
                    continue
                seen_sha.add(sha)

                try:
                    with Image.open(io.BytesIO(image_bytes)) as img:
                        img.load()
                        width, height = img.size
                        if width < min_width or height < min_height or width * height < min_area:
                            continue
                        if img.mode == "RGBA":
                            bg = Image.new("RGB", img.size, (255, 255, 255))
                            bg.paste(img, mask=img.split()[-1])
                            export_img = bg
                        elif img.mode != "RGB":
                            export_img = img.convert("RGB")
                        else:
                            export_img = img.copy()
                except Exception:
                    continue

                file_name = f"fig-{fig_index:03d}.webp"
                abs_path = os.path.join(output_dir, file_name)
                export_img.save(abs_path, format="WEBP", quality=WEBP_QUALITY, method=6)

                figures.append(
                    {
                        "url": "/".join([relative_prefix.strip("/"), file_name]),
                        "caption": "",
                        "page": page_idx + 1,
                        "index": fig_index,
                        "width": width,
                        "height": height,
                    }
                )
                fig_index += 1

    _save_figures_meta(os.path.join(output_dir, "meta.json"), figures, extractor="pymupdf-images")
    return figures


def ensure_paper_figures(
    *,
    pdf_url: str,
    docs_dir: str,
    source_key: str,
    asset_key: str,
    force: bool = False,
) -> List[Dict[str, Any]]:
    figures, _tables = ensure_paper_media(
        pdf_url=pdf_url,
        docs_dir=docs_dir,
        source_key=source_key,
        asset_key=asset_key,
        force=force,
    )
    return figures


def ensure_paper_media(
    *,
    pdf_url: str,
    docs_dir: str,
    source_key: str,
    asset_key: str,
    force: bool = False,
    pdf_bytes: bytes | None = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not str(pdf_url or "").strip() and not pdf_bytes:
        return [], []

    figure_dir = _absolute_dir(docs_dir, source_key, asset_key)
    table_dir = _absolute_tables_dir(docs_dir, source_key, asset_key)
    figure_relative_prefix = _relative_prefix(source_key, asset_key)
    table_relative_prefix = _relative_tables_prefix(source_key, asset_key)
    figure_meta_path = os.path.join(figure_dir, "meta.json")
    table_meta_path = os.path.join(table_dir, "meta.json")
    if not force:
        cached_figures = _load_cached_figures(figure_meta_path)
        cached_tables = _load_cached_tables(table_meta_path)
        if cached_figures and cached_tables:
            return cached_figures, cached_tables
        if (cached_figures or os.path.exists(figure_meta_path)) and os.path.exists(table_meta_path):
            return cached_figures, cached_tables

    if pdf_bytes is None:
        pdf_bytes = _download_pdf_bytes(pdf_url)
    tmp_pdf_path = ""
    try:
        # delete=False: close the file handle before PyMuPDF opens the path.
        # Windows keeps NamedTemporaryFile(delete=True) locked open, so
        # fitz.open() on the same path fails with Permission denied.
        with tempfile.NamedTemporaryFile(
            suffix=".pdf", prefix="dpr_media_", delete=False
        ) as tmp_pdf:
            tmp_pdf.write(pdf_bytes)
            tmp_pdf.flush()
            tmp_pdf_path = tmp_pdf.name

        if docling_enabled():
            figures, tables = extract_media_with_docling(
                tmp_pdf_path,
                figure_dir,
                figure_relative_prefix,
                table_dir,
                table_relative_prefix,
            )
            if figures or tables:
                return figures, tables

        # Lightweight fallback only. PaperCropper/DocLayout-YOLO are no longer
        # part of the supported runtime path.
        return extract_figures_from_pdf(tmp_pdf_path, figure_dir, figure_relative_prefix), []
    finally:
        if tmp_pdf_path and os.path.exists(tmp_pdf_path):
            try:
                os.remove(tmp_pdf_path)
            except OSError:
                pass


def _absolutize_asset_urls(items: List[Dict[str, Any]], base_dir: str) -> List[Dict[str, Any]]:
    """把抽取结果里的相对资源 url 改写为本地绝对路径（供一次性/内存场景使用）。"""
    out: List[Dict[str, Any]] = []
    for item in items:
        d = dict(item)
        url = str(d.get("url") or "").strip()
        if url and not os.path.isabs(url):
            d["url"] = os.path.abspath(os.path.join(base_dir, os.path.basename(url)))
        out.append(d)
    return out


def extract_figures_from_pdf_bytes(
    pdf_bytes: bytes,
    output_dir: str,
    *,
    use_papercropper: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    从内存中的 PDF 字节抽取 figure/table，返回 dict 列表（`url` 为本地绝对路径）。
    供一次性 / 运行时场景（如论文总结）复用日报的抽图逻辑，无需持久化 docs_dir。
    全程 best-effort：失败返回空列表，不抛错。
    """
    if not pdf_bytes:
        return [], []
    os.makedirs(output_dir, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp_pdf.write(pdf_bytes)
        tmp_pdf.flush()
        tmp_pdf.close()

        figures_dir = os.path.join(output_dir, "figures")
        tables_dir = os.path.join(output_dir, "tables")
        figures: List[Dict[str, Any]] = []
        tables: List[Dict[str, Any]] = []
        if docling_enabled():
            figures, tables = extract_media_with_docling(
                tmp_pdf.name, figures_dir, "figures", tables_dir, "tables"
            )
            if figures:
                return _absolutize_asset_urls(figures, figures_dir), _absolutize_asset_urls(tables, tables_dir)
        if not figures:
            try:
                figures = extract_figures_from_pdf(tmp_pdf.name, figures_dir, "figures")
            except Exception:
                figures = []
        return _absolutize_asset_urls(figures, figures_dir), _absolutize_asset_urls(tables, tables_dir)
    except Exception as e:
        print(f"[WARN] PDF 抽图失败: {e}", flush=True)
        return [], []
    finally:
        try:
            os.remove(tmp_pdf.name)
        except OSError:
            pass
