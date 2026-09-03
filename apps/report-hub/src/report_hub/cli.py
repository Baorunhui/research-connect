from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta, timezone
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
    admin.add_argument("--show-install-data", metavar="INSTALL_ID")
    admin.add_argument("--clear-install-data", metavar="INSTALL_ID")
    admin.add_argument("--delete-install", metavar="INSTALL_ID")
    admin.add_argument("--issue-invite", metavar="LABEL")
    admin.add_argument("--list-invites", action="store_true")
    admin.add_argument("--revoke-invite", metavar="INVITE_ID")
    parser.add_argument("--max-uses", type=int, default=1)
    parser.add_argument("--expires-in", default="7d")
    parser.add_argument("--yes", action="store_true", help="confirm destructive operation")
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
    if any((
        args.issue_install,
        args.list_installs,
        args.revoke_install,
        args.rotate_install,
        args.show_install_data,
        args.clear_install_data,
        args.delete_install,
        args.issue_invite,
        args.list_invites,
        args.revoke_invite,
    )):
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
        elif args.rotate_install:
            token = storage.rotate_installation(args.rotate_install)
            if not token:
                raise SystemExit("Installation not found")
            print(f"install_id={args.rotate_install}")
            print(f"REPORT_HUB_API_URL={settings.public_base_url}")
            print(f"REPORT_HUB_AGENT_TOKEN={token}")
            print("The previous token is invalid. This replacement is shown once.")
        elif args.show_install_data:
            try:
                summary = storage.installation_storage_summary(args.show_install_data)
            except ValueError as exc:
                raise SystemExit("Installation not found") from exc
            print(
                f"install_id={summary['install_id']}\tlabel={summary['label']}\t"
                f"sites={summary['site_count']}\tbytes={summary['total_bytes']}"
            )
            for site in summary["sites"]:
                print(
                    f"{site['site_id']}\t{site['module_name']}\t{site['size_bytes']} bytes\t"
                    f"runs={site['run_count']}\t{site['title']}"
                )
        elif args.clear_install_data:
            _require_yes(args.yes, "--clear-install-data")
            try:
                result = storage.clear_installation_data(args.clear_install_data)
            except ValueError as exc:
                raise SystemExit("Installation not found") from exc
            print(
                f"Cleared {result['deleted_sites']} site(s) for installation "
                f"{args.clear_install_data}; its token remains valid"
            )
        elif args.delete_install:
            _require_yes(args.yes, "--delete-install")
            if not storage.delete_installation(args.delete_install):
                raise SystemExit("Installation not found")
            print(
                f"Deleted installation {args.delete_install}, all owned data, and its token"
            )
        elif args.issue_invite:
            if args.max_uses < 1:
                raise SystemExit("--max-uses must be at least 1")
            expires_at = _expiry(args.expires_in)
            invite, code = storage.issue_invite(
                args.issue_invite,
                max_uses=args.max_uses,
                expires_at=expires_at,
            )
            print(f"invite_id={invite['invite_id']}")
            print(f"label={invite['label']}")
            print(f"max_uses={invite['max_uses']}")
            print(f"expires_at={invite['expires_at']}")
            print(f"REPORT_HUB_INVITE_CODE={code}")
            print("This invite code is shown once. Share it through a private channel.")
        elif args.list_invites:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for item in storage.list_invites():
                if not item["enabled"]:
                    state = "revoked"
                elif item["expires_at"] <= now:
                    state = "expired"
                elif item["used_count"] >= item["max_uses"]:
                    state = "exhausted"
                else:
                    state = "active"
                print(
                    f"{item['invite_id']}\t{state}\t{item['used_count']}/{item['max_uses']}\t"
                    f"{item['expires_at']}\t{item['label']}"
                )
        else:
            if not storage.revoke_invite(args.revoke_invite):
                raise SystemExit("Invite not found or already revoked")
            print(f"Revoked invite {args.revoke_invite}")
        return
    from .app import create_app
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1)


def initialize_config(path: Path) -> None:
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing config: {path}")
    path.write_text(
        "REPORT_HUB_HOST=0.0.0.0\n"
        "REPORT_HUB_PORT=58787\n"
        "REPORT_HUB_PUBLIC_BASE_URL=https://report.sinksilk.com:58443\n"
        "REPORT_HUB_DATA_DIR=./data\n"
        "REPORT_HUB_MAX_UPLOAD_MB=256\n"
        "REPORT_HUB_MAX_EXPANDED_MB=1024\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print(f"Created {path}.")


def _require_yes(confirmed: bool, operation: str) -> None:
    if not confirmed:
        raise SystemExit(f"{operation} is destructive; repeat with --yes")


def _expiry(value: str) -> str:
    match = re.fullmatch(r"([1-9][0-9]*)([mhd])", value.strip().lower())
    if not match:
        raise SystemExit("--expires-in must look like 30m, 24h, or 30d")
    amount = int(match.group(1))
    unit = match.group(2)
    delta = {
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")
