from __future__ import annotations

import argparse
import secrets
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser(description="Research Connect Report Hub")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--init-config", action="store_true")
    args = parser.parse_args()
    env_path = Path(args.env_file)
    if args.init_config:
        initialize_config(env_path)
        return
    load_dotenv(env_path)
    from .app import create_app
    from .config import Settings

    settings = Settings.from_env()
    settings.validate()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1)


def initialize_config(path: Path) -> None:
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing config: {path}")
    path.write_text(
        "REPORT_HUB_HOST=0.0.0.0\n"
        "REPORT_HUB_PORT=8787\n"
        "REPORT_HUB_PUBLIC_BASE_URL=http://211.86.155.100:8787\n"
        f"REPORT_HUB_AGENT_TOKEN={secrets.token_urlsafe(48)}\n"
        "REPORT_HUB_DATA_DIR=./data\n"
        "REPORT_HUB_MAX_UPLOAD_MB=256\n"
        "REPORT_HUB_MAX_EXPANDED_MB=1024\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print(f"Created {path}. The generated agent token was not printed.")
