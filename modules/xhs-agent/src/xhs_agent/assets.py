from __future__ import annotations

import os
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image

from .schemas import ImageAsset, SocialContentRequest


def prepare_render_assets(
    request: SocialContentRequest,
    assets_dir: Path,
    html_dir: Path,
) -> dict[str, dict[str, Any]]:
    assets_dir.mkdir(parents=True, exist_ok=True)
    resolved: dict[str, dict[str, Any]] = {}
    for asset in request.source.assets:
        if asset.type != "image":
            continue
        try:
            dest = materialize_image_asset(asset, assets_dir)
            width, height = image_size(dest)
        except OSError:
            continue
        resolved[asset.id] = {
            "id": asset.id,
            "src": os.path.relpath(dest, html_dir),
            "path": str(dest),
            "label": asset.label or "",
            "caption": asset.caption or asset.label or "",
            "kind": asset.kind,
            "fit": asset.fit,
            "object_position": asset.object_position,
            "source_url": asset.source_url or "",
            "width": width,
            "height": height,
        }
    return resolved


def materialize_image_asset(asset: ImageAsset, assets_dir: Path) -> Path:
    ext = image_extension(asset.uri)
    dest = unique_path(assets_dir / f"{safe_name(asset.id)}{ext}")
    parsed = urllib.parse.urlparse(asset.uri)
    if parsed.scheme in {"http", "https"}:
        request = urllib.request.Request(asset.uri, headers={"User-Agent": "xhs_agent/0.1"})
        with urllib.request.urlopen(request, timeout=20) as response, dest.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    else:
        src = Path(urllib.request.url2pathname(parsed.path)) if parsed.scheme == "file" else Path(asset.uri)
        if not src.is_absolute():
            src = Path.cwd() / src
        shutil.copy2(src, dest)
    with Image.open(dest) as image:
        image.verify()
    return dest


def image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def image_extension(uri: str) -> str:
    parsed = urllib.parse.urlparse(uri)
    path = urllib.parse.unquote(parsed.path)
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    for idx in range(2, 100):
        candidate = path.with_name(f"{stem}-{idx}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}-{os.getpid()}{path.suffix}")


def safe_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-").lower()
    return clean[:64] or "asset"
