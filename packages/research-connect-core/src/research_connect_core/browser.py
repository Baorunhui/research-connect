from __future__ import annotations

import os
from pathlib import Path

from .data import resolve_data_root


def configure_playwright_browsers(path: str | Path | None = None) -> Path:
    """Make every module use the same Playwright browser installation."""

    target = Path(path).expanduser() if path else resolve_data_root() / "browsers"
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(target))
    return Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
