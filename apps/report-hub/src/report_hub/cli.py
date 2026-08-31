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
    admin = parser.add_mutually_exclusive_group()
    admin.add_argument("--issue-install", metavar="LABEL")
    admin.add_argument("--list-installs", action="store_true")
    admin.add_argument("--revoke-install", metavar="INSTALL_ID")
    admin.add_argument("--rotate-install", metavar="INSTALL_ID")
    args = parser.parse_args()
    env_path = Path(args.env_file)
    if args.init_config:
        initialize_config(env_path)
        return
    load_dotenv(env_path)
    from .config import Settings
    from .storage import Storage

    settings = Settings.from_env()
    settings.validate()
    if args.issue_install or args.list_installs or args.revoke_install or args.rotate_install:
        storage = Storage(settings.data_dir)
        storage.initialize()
        if args.issue_install:
            installation, token = storage.issue_installation(args.issue_install)
            print(f"install_id={installation['install_id']}")
            print(f"label={installation['label']}")
            print(f"REPORT_HUB_API_URL={settings.public_base_url}")
            print(f"REPORT_HUB_AGENT_TOKEN={token}")
            print("This token is shown once. Send it to that user through a private channel.")
        elif args.list_installs:
            for item in storage.list_installations():
                state = "enabled" if item["enabled"] else "revoked"
                print(f"{item['install_id']}\t{state}\t{item['label']}\t{item['created_at']}")
        elif args.revoke_install:
            if not storage.revoke_installation(args.revoke_install):
                raise SystemExit("Installation not found or already revoked")
            print(f"Revoked installation {args.revoke_install}")
        else:
            token = storage.rotate_installation(args.rotate_install)
            if not token:
                raise SystemExit("Installation not found")
            print(f"install_id={args.rotate_install}")
            print(f"REPORT_HUB_API_URL={settings.public_base_url}")
            print(f"REPORT_HUB_AGENT_TOKEN={token}")
            print("The previous token is invalid. This replacement is shown once.")
        return
    from .app import create_app
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
