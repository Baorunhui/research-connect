from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import urllib.error
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


_IMAGE_MAX_BYTES = 8 * 1024 * 1024


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_public_host(host: str) -> bool:
    name = str(host or "").strip().lower().rstrip(".")
    if not name or name == "localhost" or name.endswith(".localhost") or name.endswith(".local") or name.endswith(".internal"):
        return False
    try:
        return _is_public_ip(ipaddress.ip_address(name))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(name, None)
    except socket.gaierror:
        return False
    addresses = [ipaddress.ip_address(item[4][0]) for item in infos]
    return bool(addresses) and all(_is_public_ip(ip) for ip in addresses)


def _asset_roots() -> list[Path]:
    configured = Path(os.getenv("XHS_AGENT_ASSET_ROOT") or Path.cwd()).resolve()
    bundled = Path(__file__).resolve().parents[2]
    return [configured, bundled]


def _local_asset_path(uri: str) -> Path:
    parsed = urllib.parse.urlparse(uri)
    raw = urllib.request.url2pathname(parsed.path) if parsed.scheme == "file" else uri
    candidate = Path(raw)
    roots = _asset_roots()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if any(_is_inside(resolved, root) for root in roots):
            return resolved
        raise OSError("image is outside the asset folder")
    for root in roots:
        resolved = (root / candidate).resolve()
        if _is_inside(resolved, root) and resolved.is_file():
            return resolved
    raise OSError("image is outside the asset folder")


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _read_public_image(url: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not _is_public_host(parsed.hostname or ""):
        raise OSError("image url is not public")

    class _StopRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    current = url
    opener = urllib.request.build_opener(_StopRedirect)
    for _hop in range(4):
        request = urllib.request.Request(current, headers={"User-Agent": "xhs_agent/0.1"})
        try:
            with opener.open(request, timeout=20) as response:
                data = response.read(_IMAGE_MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise OSError("image download failed") from exc
            location = exc.headers.get("Location") if exc.headers else ""
            try:
                exc.close()
            except Exception:
                pass
            current = urllib.parse.urljoin(current, str(location or ""))
            parsed = urllib.parse.urlparse(current)
            if parsed.scheme not in {"http", "https"} or not _is_public_host(parsed.hostname or ""):
                raise OSError("image url is not public")
            continue
        if len(data) > _IMAGE_MAX_BYTES:
            raise OSError("image is too large")
        return data
    raise OSError("image url is not public")


def materialize_image_asset(asset: ImageAsset, assets_dir: Path) -> Path:
    ext = image_extension(asset.uri)
    dest = unique_path(assets_dir / f"{safe_name(asset.id)}{ext}")
    parsed = urllib.parse.urlparse(asset.uri)
    if parsed.scheme in {"http", "https"}:
        dest.write_bytes(_read_public_image(asset.uri))
    else:
        src = _local_asset_path(asset.uri)
        if src.stat().st_size > _IMAGE_MAX_BYTES:
            raise OSError("image is too large")
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
