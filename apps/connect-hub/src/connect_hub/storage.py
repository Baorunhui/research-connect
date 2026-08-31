from __future__ import annotations

import sqlite3
import threading
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from connect_hub.contracts import ACTIVE_JOB_STATUSES, SCHEMA_VERSION


def _feishu_open_id(session_key: str) -> str:
    if not session_key.startswith("feishu:"):
        return ""
    parts = session_key.split(":")
    return parts[-1].strip() if len(parts) >= 3 else ""


def _feishu_user_settings_key(open_id: str) -> str:
    return f"feishu-user:{open_id}"


class ConversationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session_id "
                "ON messages(session_key, id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_runs_session_id "
                "ON tool_runs(session_key, id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    module_name TEXT NOT NULL,
                    module_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    pid INTEGER,
                    process_executable TEXT NOT NULL DEFAULT '',
                    error_code TEXT,
                    error_message TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_session_created "
                "ON jobs(session_key, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_created "
                "ON jobs(status, created_at DESC)"
            )
            # Existing installations may already have the P0 jobs table. Keep
            # migrations additive so the same SQLite file works after upgrade.
            job_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "process_executable" not in job_columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN "
                    "process_executable TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    current INTEGER,
                    total INTEGER,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_events_job_id "
                "ON job_events(job_id, id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    size_bytes INTEGER,
                    sha256 TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_job_id "
                "ON artifacts(job_id, id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_index (
                    cache_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size_bytes INTEGER,
                    sha256 TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    last_accessed_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_kind_accessed "
                "ON cache_index(kind, last_accessed_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    api_calls INTEGER NOT NULL DEFAULT 1,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    status_code INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_job_id "
                "ON usage_records(job_id, id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_settings (
                    session_key TEXT PRIMARY KEY,
                    web_mode TEXT NOT NULL DEFAULT 'auto'
                        CHECK(web_mode IN ('auto', 'on', 'off')),
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Legacy schema compatibility only. The generative agent no longer
            # reads or writes task drafts; keep the table so existing databases
            # remain an additive migration and can be downgraded safely.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_drafts (
                    session_key TEXT PRIMARY KEY,
                    intent TEXT NOT NULL,
                    status TEXT NOT NULL,
                    slots_json TEXT NOT NULL DEFAULT '{}',
                    missing_json TEXT NOT NULL DEFAULT '[]',
                    clarification_round INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_drafts_expires "
                "ON task_drafts(expires_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model_calls INTEGER NOT NULL DEFAULT 0,
                    search_calls INTEGER NOT NULL DEFAULT 0,
                    url_fetch_calls INTEGER NOT NULL DEFAULT 0,
                    business_tool_calls INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_runs_session_created "
                "ON agent_runs(session_key, created_at DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    step_type TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    tool_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_steps_run_id "
                "ON agent_steps(run_id, id)"
            )

    def append(self, session_key: str, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("role must be user or assistant")
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO messages(session_key, role, content) VALUES (?, ?, ?)",
                (session_key, role, content),
            )

    def history(self, session_key: str, limit: int = 20) -> list[dict[str, str]]:
        if limit <= 0:
            return []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content FROM messages
                    WHERE session_key = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC
                """,
                (session_key, limit),
            ).fetchall()
        return [{"role": str(row["role"]), "content": str(row["content"])} for row in rows]

    def clear(self, session_key: str) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM messages WHERE session_key = ?", (session_key,)
            )
            return int(cursor.rowcount)

    def start_agent_run(self, run_id: str, session_key: str, user_message: str) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, session_key, user_message, status, created_at
                ) VALUES (?, ?, ?, 'running', ?)
                """,
                (run_id, session_key, user_message, now),
            )

    def append_agent_step(
        self,
        run_id: str,
        *,
        step_index: int,
        step_type: str,
        status: str,
        provider: str = "",
        model: str = "",
        tool_name: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        duration_ms: int = 0,
        payload: Mapping[str, Any] | None = None,
    ) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_steps(
                    run_id, step_index, step_type, provider, model, tool_name,
                    status, input_tokens, output_tokens, duration_ms,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    max(0, step_index),
                    step_type,
                    provider,
                    model,
                    tool_name,
                    status,
                    max(0, input_tokens),
                    max(0, output_tokens),
                    max(0, duration_ms),
                    _json(payload or {}),
                    _utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def finish_agent_run(
        self,
        run_id: str,
        *,
        status: str,
        model_calls: int,
        search_calls: int,
        url_fetch_calls: int,
        business_tool_calls: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_message: str = "",
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs SET
                    status = ?, model_calls = ?, search_calls = ?,
                    url_fetch_calls = ?, business_tool_calls = ?,
                    input_tokens = ?, output_tokens = ?, error_message = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    max(0, model_calls),
                    max(0, search_calls),
                    max(0, url_fetch_calls),
                    max(0, business_tool_calls),
                    max(0, input_tokens),
                    max(0, output_tokens),
                    error_message,
                    _utc_now(),
                    run_id,
                ),
            )

    def get_agent_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def recent_agent_runs(self, session_key: str, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_runs
                WHERE session_key = ? ORDER BY created_at DESC LIMIT ?
                """,
                (session_key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def agent_steps(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json"), {})
            result.append(item)
        return result

    def get_web_mode(self, session_key: str) -> str:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT web_mode FROM conversation_settings WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if row is None:
                open_id = _feishu_open_id(session_key)
                if open_id:
                    row = connection.execute(
                        "SELECT web_mode FROM conversation_settings WHERE session_key = ?",
                        (_feishu_user_settings_key(open_id),),
                    ).fetchone()
        return str(row["web_mode"]) if row is not None else "on"

    def set_web_mode(self, session_key: str, mode: str) -> None:
        normalized = mode.strip().lower()
        if normalized not in {"auto", "on", "off"}:
            raise ValueError("web mode must be auto, on or off")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_settings(session_key, web_mode, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    web_mode = excluded.web_mode,
                    updated_at = excluded.updated_at
                """,
                (session_key, normalized, _utc_now()),
            )

    def set_web_mode_for_feishu_user(self, open_id: str, mode: str) -> None:
        """Set the bot-menu preference for all known sessions of one user.

        Feishu's ``application.bot.menu_v6`` event contains an operator open_id
        but no chat_id.  A user-level default covers future chats, while updating
        known session rows prevents an older per-session value from shadowing a
        newly selected menu value.
        """

        user_open_id = open_id.strip()
        if not user_open_id:
            raise ValueError("Feishu open_id is required")
        normalized = mode.strip().lower()
        if normalized not in {"auto", "on", "off"}:
            raise ValueError("web mode must be auto, on or off")
        now = _utc_now()
        suffix = f":{user_open_id}"
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_settings(session_key, web_mode, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    web_mode = excluded.web_mode,
                    updated_at = excluded.updated_at
                """,
                (_feishu_user_settings_key(user_open_id), normalized, now),
            )
            rows = connection.execute(
                "SELECT session_key FROM conversation_settings"
            ).fetchall()
            known_sessions = [
                str(row["session_key"])
                for row in rows
                if str(row["session_key"]).startswith("feishu:")
                and not str(row["session_key"]).startswith("feishu-user:")
                and str(row["session_key"]).endswith(suffix)
            ]
            connection.executemany(
                """
                UPDATE conversation_settings
                SET web_mode = ?, updated_at = ?
                WHERE session_key = ?
                """,
                ((normalized, now, key) for key in known_sessions),
            )

    def save_task_draft(
        self,
        session_key: str,
        *,
        intent: str,
        status: str,
        slots: Mapping[str, Any],
        missing: list[str],
        clarification_round: int,
        ttl_hours: int = 24,
    ) -> None:
        """Deprecated compatibility API; the active chat path never calls it."""
        now = datetime.now(timezone.utc)
        expires = now.timestamp() + max(1, ttl_hours) * 3600
        expires_at = datetime.fromtimestamp(expires, timezone.utc).isoformat(
            timespec="seconds"
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO task_drafts(
                    session_key, intent, status, slots_json, missing_json,
                    clarification_round, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    intent = excluded.intent,
                    status = excluded.status,
                    slots_json = excluded.slots_json,
                    missing_json = excluded.missing_json,
                    clarification_round = excluded.clarification_round,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (
                    session_key,
                    intent,
                    status,
                    _json(slots),
                    _json(missing),
                    max(0, clarification_round),
                    now.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                    expires_at,
                ),
            )

    def get_task_draft(self, session_key: str) -> dict[str, Any] | None:
        """Deprecated compatibility API; retained for old SQLite installations."""
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM task_drafts WHERE expires_at <= ?", (now,))
            row = connection.execute(
                "SELECT * FROM task_drafts WHERE session_key = ?", (session_key,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["slots"] = _loads(item.pop("slots_json"), {})
        item["missing"] = _loads(item.pop("missing_json"), [])
        return item

    def delete_task_draft(self, session_key: str) -> bool:
        """Deprecated compatibility API; retained for downgrade safety."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM task_drafts WHERE session_key = ?", (session_key,)
            )
            return cursor.rowcount > 0

    def start_tool_run(
        self,
        session_key: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tool_runs(
                    session_key, tool_call_id, tool_name, arguments_json,
                    status, started_at
                ) VALUES (?, ?, ?, ?, 'in_progress', ?)
                """,
                (
                    session_key,
                    tool_call_id,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def finish_tool_run(
        self,
        run_id: int,
        *,
        status: str,
        result: object | None = None,
        error: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        result_json = None if result is None else json.dumps(result, ensure_ascii=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE tool_runs
                SET status = ?, result_json = ?, error = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, result_json, error, now, run_id),
            )

    def recent_tool_runs(self, session_key: str, limit: int = 20) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, tool_call_id, tool_name, arguments_json, status,
                       result_json, error, started_at, finished_at
                FROM tool_runs WHERE session_key = ?
                ORDER BY id DESC LIMIT ?
                """,
                (session_key, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_job(
        self,
        job_id: str,
        *,
        session_key: str,
        job_type: str,
        module_name: str,
        module_version: str,
        input_data: Mapping[str, Any],
        options: Mapping[str, Any] | None = None,
        status: str = "queued",
    ) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    id, schema_version, session_key, job_type, module_name,
                    module_version, status, input_json, options_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    SCHEMA_VERSION,
                    session_key,
                    job_type,
                    module_name,
                    module_version,
                    status,
                    _json(input_data),
                    _json(options or {}),
                    now,
                    now,
                ),
            )

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        pid: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        result: Any = None,
        set_result: bool = False,
    ) -> None:
        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [_utc_now()]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
            if status == "running":
                fields.append("started_at = COALESCE(started_at, ?)")
                values.append(_utc_now())
            if status not in ACTIVE_JOB_STATUSES:
                fields.append("finished_at = COALESCE(finished_at, ?)")
                values.append(_utc_now())
        if pid is not None:
            fields.append("pid = ?")
            values.append(pid)
        if error_code is not None:
            fields.append("error_code = ?")
            values.append(error_code)
        if error_message is not None:
            fields.append("error_message = ?")
            values.append(error_message)
        if set_result:
            fields.append("result_json = ?")
            values.append(_json(result))
        values.append(job_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values
            )

    def set_job_process(self, job_id: str, pid: int, executable: str) -> None:
        if pid <= 0:
            raise ValueError("pid must be positive")
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET pid = ?, process_executable = ?, updated_at = ? "
                "WHERE id = ?",
                (pid, executable, _utc_now(), job_id),
            )

    def clear_job_process(self, job_id: str, pid: int | None = None) -> None:
        with self._lock, self._connect() as connection:
            if pid is None:
                connection.execute(
                    "UPDATE jobs SET pid = NULL, process_executable = '', updated_at = ? "
                    "WHERE id = ?",
                    (_utc_now(), job_id),
                )
            else:
                connection.execute(
                    "UPDATE jobs SET pid = NULL, process_executable = '', updated_at = ? "
                    "WHERE id = ? AND pid = ?",
                    (_utc_now(), job_id, pid),
                )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _decode_job(row) if row is not None else None

    def recent_jobs(self, session_key: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE session_key = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (session_key, max(1, limit)),
            ).fetchall()
        return [_decode_job(row) for row in rows]

    def active_jobs(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) "
                "ORDER BY created_at",
                sorted(ACTIVE_JOB_STATUSES),
            ).fetchall()
        return [_decode_job(row) for row in rows]

    def find_job(self, session_key: str, job_reference: str) -> dict[str, Any] | None:
        reference = job_reference.strip()
        if not reference:
            return None
        exact = reference if reference.startswith("job-") else f"job-{reference}"
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE session_key = ? "
                "AND (id = ? OR id LIKE ?) "
                "ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, created_at DESC LIMIT 1",
                (session_key, exact, exact + "%", exact),
            ).fetchone()
        return _decode_job(row) if row is not None else None

    def latest_active_job(self, session_key: str) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        params: list[Any] = [session_key, *sorted(ACTIVE_JOB_STATUSES)]
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM jobs WHERE session_key = ? "
                f"AND status IN ({placeholders}) ORDER BY created_at DESC LIMIT 1",
                params,
            ).fetchone()
        return _decode_job(row) if row is not None else None

    def interrupt_active_jobs(self, message: str = "service restarted") -> int:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        now = _utc_now()
        params: list[Any] = [
            "interrupted",
            "SERVICE_RESTARTED",
            message,
            now,
            now,
            *sorted(ACTIVE_JOB_STATUSES),
        ]
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET status = ?, error_code = ?, error_message = ?, "
                f"finished_at = ?, updated_at = ? WHERE status IN ({placeholders})",
                params,
            )
            return int(cursor.rowcount)

    def append_job_event(
        self,
        *,
        event_id: str,
        job_id: str,
        event_type: str,
        stage: str = "",
        message: str = "",
        current: int | None = None,
        total: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO job_events(
                    event_id, job_id, event_type, stage, message,
                    current, total, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    job_id,
                    event_type,
                    stage,
                    message,
                    current,
                    total,
                    _json(payload or {}),
                    _utc_now(),
                ),
            )
            return cursor.rowcount > 0

    def job_events(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json"), {})
            result.append(item)
        return result

    def add_artifact(
        self,
        job_id: str,
        *,
        kind: str,
        path: str = "",
        url: str = "",
        name: str = "",
        size_bytes: int | None = None,
        sha256: str = "",
    ) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO artifacts(
                    job_id, kind, path, url, name, size_bytes, sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, kind, path, url, name, size_bytes, sha256, _utc_now()),
            )
            return int(cursor.lastrowid)

    def artifacts(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_cache(
        self,
        cache_key: str,
        *,
        kind: str,
        path: str,
        size_bytes: int | None = None,
        sha256: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cache_index(
                    cache_key, kind, path, size_bytes, sha256,
                    metadata_json, created_at, last_accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    kind = excluded.kind,
                    path = excluded.path,
                    size_bytes = excluded.size_bytes,
                    sha256 = excluded.sha256,
                    metadata_json = excluded.metadata_json,
                    last_accessed_at = excluded.last_accessed_at
                """,
                (
                    cache_key,
                    kind,
                    path,
                    size_bytes,
                    sha256,
                    _json(metadata or {}),
                    now,
                    now,
                ),
            )

    def get_cache(self, cache_key: str, *, touch: bool = True) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cache_index WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if row is not None and touch:
                connection.execute(
                    "UPDATE cache_index SET last_accessed_at = ? WHERE cache_key = ?",
                    (_utc_now(), cache_key),
                )
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = _loads(item.pop("metadata_json"), {})
        return item

    def record_usage(
        self,
        job_id: str,
        *,
        provider: str,
        operation: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        api_calls: int = 1,
        duration_ms: int = 0,
        status_code: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO usage_records(
                    job_id, provider, operation, input_tokens, output_tokens,
                    api_calls, duration_ms, status_code, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    provider,
                    operation,
                    max(0, input_tokens),
                    max(0, output_tokens),
                    max(0, api_calls),
                    max(0, duration_ms),
                    status_code,
                    _json(metadata or {}),
                    _utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def usage_summary(self, job_id: str) -> dict[str, int]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(api_calls), 0) AS api_calls,
                       COALESCE(SUM(duration_ms), 0) AS duration_ms
                FROM usage_records WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        return {key: int(row[key]) for key in row.keys()}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["input"] = _loads(item.pop("input_json"), {})
    item["options"] = _loads(item.pop("options_json"), {})
    item["result"] = _loads(item.pop("result_json"), None)
    return item
