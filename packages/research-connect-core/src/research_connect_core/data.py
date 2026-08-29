from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def resolve_data_root() -> Path:
    """Resolve the one machine-local data root used by every module.

    RESEARCH_CONNECT_DATA_DIR is the stable public setting. The legacy
    CITATIONCLAW_DATA_DIR name is accepted during migration, but now denotes
    the common root rather than only the two CitationClaw database files.
    """

    configured = str(
        os.getenv("RESEARCH_CONNECT_DATA_DIR")
        or os.getenv("CITATIONCLAW_DATA_DIR")
        or ""
    ).strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".research-connect" / "data"
    return root.resolve()


@dataclass(frozen=True)
class DataPaths:
    module_name: str
    root: Path
    cache: Path
    artifacts: Path
    state: Path

    @classmethod
    def for_module(cls, module_name: str, *, create: bool = True) -> "DataPaths":
        clean = module_name.strip().lower().replace("_", "-")
        if not clean or any(part in clean for part in ("/", "\\", "..")):
            raise ValueError("module_name must be a simple identifier")
        root = resolve_data_root() / "modules" / clean
        value = cls(
            module_name=clean,
            root=root,
            cache=root / "cache",
            artifacts=root / "artifacts",
            state=root / "state",
        )
        if create:
            for path in (value.root, value.cache, value.artifacts, value.state):
                path.mkdir(parents=True, exist_ok=True)
        return value
