from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 58787
    public_base_url: str = "http://127.0.0.1:58787"
    data_dir: Path = Path("data")
    # Daily Paper sites contain historical figures. A normal site can exceed
    # 50 MiB after only a few runs, so the standalone defaults must accommodate
    # the product's actual whole-site publishing mode.
    max_upload_mb: int = 256
    max_expanded_mb: int = 1024

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("REPORT_HUB_HOST", "0.0.0.0"),
            port=int(os.getenv("REPORT_HUB_PORT", "58787")),
            public_base_url=os.getenv(
                "REPORT_HUB_PUBLIC_BASE_URL", "http://127.0.0.1:58787"
            ).rstrip("/"),
            data_dir=Path(os.getenv("REPORT_HUB_DATA_DIR", "data")).expanduser().resolve(),
            max_upload_mb=int(os.getenv("REPORT_HUB_MAX_UPLOAD_MB", "256")),
            max_expanded_mb=int(os.getenv("REPORT_HUB_MAX_EXPANDED_MB", "1024")),
        )

    def validate(self) -> None:
        if self.max_upload_mb <= 0 or self.max_expanded_mb < self.max_upload_mb:
            raise ValueError("invalid upload size limits")
