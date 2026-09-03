from __future__ import annotations

import json
import hashlib
import secrets
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.db_path = data_dir / "report-hub.sqlite3"
        self.site_dir = data_dir / "sites"

    def initialize(self) -> None:
        self.site_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sites (
                    site_id TEXT PRIMARY KEY,
                    public_token TEXT NOT NULL UNIQUE,
                    module_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    command_policy_json TEXT NOT NULL DEFAULT '[]',
                    report_ready INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    owner_install_id TEXT
                );
                CREATE TABLE IF NOT EXISTS site_runs (
                    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL,
                    run_json TEXT NOT NULL,
                    log_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(site_id, run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_site_runs_updated
                    ON site_runs(site_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS site_commands (
                    command_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    request_headers_json TEXT NOT NULL,
                    request_body_b64 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_status INTEGER,
                    response_headers_json TEXT,
                    response_body_b64 TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    claimed_at TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_site_commands_queue
                    ON site_commands(site_id, status, created_at);
                CREATE TABLE IF NOT EXISTS installations (
                    install_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    feishu_app_id_hash TEXT UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS installation_invites (
                    invite_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    code_hash TEXT NOT NULL UNIQUE,
                    max_uses INTEGER NOT NULL,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_installation_invites_active
                    ON installation_invites(enabled, expires_at);
                """
            )
            self._ensure_column(db, "sites", "owner_install_id", "TEXT")
            self._ensure_column(
                db, "sites", "command_policy_json", "TEXT NOT NULL DEFAULT '[]'"
            )
            self._ensure_column(db, "installations", "feishu_app_id_hash", "TEXT")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def issue_installation(self, label: str) -> tuple[dict[str, Any], str]:
        install_id = secrets.token_hex(8)
        token = f"rhi_{install_id}_{secrets.token_urlsafe(36)}"
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO installations(install_id, label, token_hash, created_at) VALUES (?, ?, ?, ?)",
                (install_id, label.strip()[:120] or install_id, hashlib.sha256(token.encode()).hexdigest(), now),
            )
        return self.get_installation(install_id) or {}, token

    def issue_invite(
        self, label: str, *, max_uses: int, expires_at: str
    ) -> tuple[dict[str, Any], str]:
        invite_id = secrets.token_hex(8)
        code = f"rhi_inv_{invite_id}_{secrets.token_urlsafe(24)}"
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO installation_invites("
                "invite_id, label, code_hash, max_uses, expires_at, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    invite_id,
                    label.strip()[:120] or invite_id,
                    hashlib.sha256(code.encode()).hexdigest(),
                    max_uses,
                    expires_at,
                    now,
                ),
            )
        return self.get_invite(invite_id) or {}, code

    def get_invite(self, invite_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT invite_id, label, max_uses, used_count, expires_at, "
                "enabled, created_at, revoked_at FROM installation_invites "
                "WHERE invite_id = ?",
                (invite_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_invites(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT invite_id, label, max_uses, used_count, expires_at, "
                "enabled, created_at, revoked_at FROM installation_invites "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_invite(self, invite_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE installation_invites SET enabled = 0, revoked_at = ? "
                "WHERE invite_id = ? AND enabled = 1",
                (utc_now(), invite_id),
            )
        return cursor.rowcount > 0

    def register_installation(
        self, *, invite_code: str, feishu_app_id: str, label: str
    ) -> tuple[dict[str, Any], str]:
        now = utc_now()
        invite_hash = hashlib.sha256(invite_code.encode()).hexdigest()
        app_hash = hashlib.sha256(feishu_app_id.strip().lower().encode()).hexdigest()
        install_id = secrets.token_hex(8)
        token = f"rhi_{install_id}_{secrets.token_urlsafe(36)}"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            invite = db.execute(
                "SELECT * FROM installation_invites WHERE code_hash = ?",
                (invite_hash,),
            ).fetchone()
            if invite is None:
                raise ValueError("invalid_invite")
            if not bool(invite["enabled"]):
                raise ValueError("invite_revoked")
            if str(invite["expires_at"]) <= now:
                raise ValueError("invite_expired")
            if int(invite["used_count"]) >= int(invite["max_uses"]):
                raise ValueError("invite_exhausted")
            if db.execute(
                "SELECT 1 FROM installations WHERE feishu_app_id_hash = ?",
                (app_hash,),
            ).fetchone():
                raise ValueError("feishu_app_already_registered")
            db.execute(
                "INSERT INTO installations(install_id, label, token_hash, "
                "feishu_app_id_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    install_id,
                    label.strip()[:120] or feishu_app_id.strip()[:120],
                    token_hash,
                    app_hash,
                    now,
                ),
            )
            db.execute(
                "UPDATE installation_invites SET used_count = used_count + 1 "
                "WHERE invite_id = ?",
                (invite["invite_id"],),
            )
        return self.get_installation(install_id) or {}, token

    def authenticate_installation(self, token: str) -> dict[str, Any] | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute(
                "SELECT install_id, label, enabled, created_at, revoked_at FROM installations "
                "WHERE token_hash = ? AND enabled = 1", (digest,),
            ).fetchone()
        return dict(row) if row else None

    def get_installation(self, install_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT install_id, label, enabled, created_at, revoked_at FROM installations WHERE install_id = ?",
                (install_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_installations(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT install_id, label, enabled, created_at, revoked_at FROM installations ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_installation(self, install_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE installations SET enabled = 0, revoked_at = ? WHERE install_id = ? AND enabled = 1",
                (utc_now(), install_id),
            )
        return cursor.rowcount > 0

    def rotate_installation(self, install_id: str) -> str | None:
        if not self.get_installation(install_id):
            return None
        token = f"rhi_{install_id}_{secrets.token_urlsafe(36)}"
        with self.connect() as db:
            db.execute(
                "UPDATE installations SET token_hash = ?, enabled = 1, revoked_at = NULL WHERE install_id = ?",
                (hashlib.sha256(token.encode()).hexdigest(), install_id),
            )
        return token

    def installation_storage_summary(self, install_id: str) -> dict[str, Any]:
        installation = self.get_installation(install_id)
        if not installation:
            raise ValueError("installation_not_found")
        with self.connect() as db:
            rows = db.execute(
                "SELECT s.*, "
                "(SELECT COUNT(*) FROM site_runs r WHERE r.site_id = s.site_id) AS run_count, "
                "(SELECT COUNT(*) FROM site_commands c WHERE c.site_id = s.site_id) AS command_count "
                "FROM sites s WHERE s.owner_install_id = ? ORDER BY s.created_at",
                (install_id,),
            ).fetchall()
        sites: list[dict[str, Any]] = []
        total_bytes = 0
        for row in rows:
            item = dict(row)
            site_id = str(item["site_id"])
            size_bytes = self._tree_size(self.site_dir / site_id)
            upload_bytes = self._tree_size(self.data_dir / "site_uploads" / site_id)
            total_bytes += size_bytes + upload_bytes
            sites.append(
                {
                    "site_id": site_id,
                    "module_name": item["module_name"],
                    "title": item["title"],
                    "report_ready": bool(item["report_ready"]),
                    "size_bytes": size_bytes,
                    "upload_bytes": upload_bytes,
                    "run_count": int(item["run_count"]),
                    "command_count": int(item["command_count"]),
                    "created_at": item["created_at"],
                    "updated_at": item["updated_at"],
                }
            )
        return {
            "install_id": installation["install_id"],
            "label": installation["label"],
            "enabled": bool(installation["enabled"]),
            "site_count": len(sites),
            "total_bytes": total_bytes,
            "sites": sites,
        }

    def delete_site(self, site_id: str, *, owner_install_id: str | None = None) -> bool:
        site = self.get_site(site_id=site_id)
        if not site:
            return False
        if owner_install_id is not None and site.get("owner_install_id") != owner_install_id:
            return False
        self._remove_tree(self.site_dir / site_id, parent=self.site_dir)
        uploads_dir = self.data_dir / "site_uploads"
        self._remove_tree(uploads_dir / site_id, parent=uploads_dir)
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM sites WHERE site_id = ?"
                + (" AND owner_install_id = ?" if owner_install_id is not None else ""),
                (site_id, owner_install_id) if owner_install_id is not None else (site_id,),
            )
        return cursor.rowcount > 0

    def clear_installation_data(self, install_id: str) -> dict[str, Any]:
        if not self.get_installation(install_id):
            raise ValueError("installation_not_found")
        with self.connect() as db:
            site_ids = [
                str(row["site_id"])
                for row in db.execute(
                    "SELECT site_id FROM sites WHERE owner_install_id = ?", (install_id,)
                ).fetchall()
            ]
        deleted = sum(
            1 for site_id in site_ids
            if self.delete_site(site_id, owner_install_id=install_id)
        )
        return {"install_id": install_id, "deleted_sites": deleted}

    def delete_installation(self, install_id: str) -> bool:
        if not self.get_installation(install_id):
            return False
        self.clear_installation_data(install_id)
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM installations WHERE install_id = ?", (install_id,)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _tree_size(path: Path) -> int:
        if not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        return sum(
            item.stat().st_size
            for item in path.rglob("*")
            if item.is_file() and not item.is_symlink()
        )

    @staticmethod
    def _remove_tree(path: Path, *, parent: Path) -> None:
        resolved_parent = parent.resolve()
        resolved_path = path.resolve()
        if resolved_path.parent != resolved_parent:
            raise ValueError("unsafe_storage_path")
        if resolved_path.is_symlink() or resolved_path.is_file():
            resolved_path.unlink(missing_ok=True)
        elif resolved_path.is_dir():
            shutil.rmtree(resolved_path)

    def create_site(
        self, *, site_id: str, public_token: str, module_name: str, title: str,
        owner_install_id: str | None = None,
        command_policy: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO sites(site_id, public_token, module_name, title, command_policy_json, "
                "created_at, updated_at, owner_install_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    site_id,
                    public_token,
                    module_name,
                    title,
                    json.dumps(command_policy or [], ensure_ascii=False),
                    now,
                    now,
                    owner_install_id,
                ),
            )
        return self.get_site(site_id=site_id)

    def update_site_command_policy(
        self, site_id: str, command_policy: list[dict[str, str]]
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE sites SET command_policy_json = ?, updated_at = ? WHERE site_id = ?",
                (json.dumps(command_policy, ensure_ascii=False), utc_now(), site_id),
            )

    def get_site(
        self, *, site_id: str | None = None, public_token: str | None = None
    ) -> dict[str, Any] | None:
        field, value = ("site_id", site_id) if site_id else ("public_token", public_token)
        with self.connect() as db:
            row = db.execute(f"SELECT * FROM sites WHERE {field} = ?", (value,)).fetchone()
        return dict(row) if row else None

    def mark_site_ready(self, site_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE sites SET report_ready = 1, updated_at = ? WHERE site_id = ?",
                (utc_now(), site_id),
            )

    def enqueue_site_command(
        self, *, command_id: str, site_id: str, method: str, path: str,
        headers: dict[str, str], body_b64: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "DELETE FROM site_commands WHERE "
                "(status IN ('completed', 'failed') AND julianday(completed_at) < julianday('now', '-1 hour')) "
                "OR (status = 'queued' AND julianday(created_at) < julianday('now', '-1 day'))"
            )
            db.execute(
                "INSERT INTO site_commands(command_id, site_id, method, path, "
                "request_headers_json, request_body_b64, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                (command_id, site_id, method, path, json.dumps(headers), body_b64, now),
            )
        return self.get_site_command(command_id) or {}

    def claim_site_command(self, site_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT command_id FROM site_commands WHERE site_id = ? AND status = 'queued' "
                "ORDER BY created_at LIMIT 1", (site_id,),
            ).fetchone()
            if not row:
                return None
            now = utc_now()
            db.execute(
                "UPDATE site_commands SET status = 'claimed', claimed_at = ? "
                "WHERE command_id = ? AND status = 'queued'", (now, row["command_id"]),
            )
        return self.get_site_command(str(row["command_id"]))

    def get_site_command(self, command_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM site_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        return dict(row) if row else None

    def complete_site_command(
        self, command_id: str, *, status_code: int, headers: dict[str, str],
        body_b64: str, error_message: str = "",
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE site_commands SET status = ?, response_status = ?, "
                "response_headers_json = ?, response_body_b64 = ?, error_message = ?, "
                "completed_at = ? WHERE command_id = ?",
                ("failed" if error_message else "completed", status_code,
                 json.dumps(headers), body_b64, error_message, utc_now(), command_id),
            )

    def delete_site_command(self, command_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM site_commands WHERE command_id = ?", (command_id,))

    def delete_queued_site_command(self, command_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM site_commands WHERE command_id = ? AND status = 'queued'",
                (command_id,),
            )

    def upsert_site_run(
        self, *, site_id: str, run_id: str, run: dict[str, Any], log_text: str
    ) -> None:
        now = utc_now()
        created_at = str(run.get("created_at") or now)
        with self.connect() as db:
            db.execute(
                "INSERT INTO site_runs(site_id, run_id, run_json, log_text, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(site_id, run_id) DO UPDATE SET "
                "run_json = excluded.run_json, log_text = excluded.log_text, "
                "updated_at = excluded.updated_at",
                (
                    site_id,
                    run_id,
                    json.dumps(run, ensure_ascii=False),
                    log_text,
                    created_at,
                    now,
                ),
            )

    def list_site_runs(self, site_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT run_json FROM site_runs WHERE site_id = ? "
                "ORDER BY created_at DESC, updated_at DESC",
                (site_id,),
            ).fetchall()
        return [json.loads(row["run_json"]) for row in rows]

    def get_site_run(self, site_id: str, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT run_json, log_text FROM site_runs WHERE site_id = ? AND run_id = ?",
                (site_id, run_id),
            ).fetchone()
        if not row:
            return None
        return {"run": json.loads(row["run_json"]), "log": row["log_text"]}
