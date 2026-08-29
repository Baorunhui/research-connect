from __future__ import annotations

import json
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
        self.report_dir = data_dir / "reports"

    def initialize(self) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    public_token TEXT NOT NULL UNIQUE,
                    module_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    report_ready INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    error_message TEXT
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT,
                    message TEXT NOT NULL,
                    current_value REAL,
                    total_value REAL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    artifact_type TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_job_seq ON job_events(job_id, seq);
                """
            )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def create_job(
        self, *, job_id: str, public_token: str, module_name: str, title: str
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(job_id, public_token, module_name, title, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (job_id, public_token, module_name, title, now, now),
            )
        return self.get_job(job_id=job_id)

    def get_job(
        self, *, job_id: str | None = None, public_token: str | None = None
    ) -> dict[str, Any] | None:
        field, value = ("job_id", job_id) if job_id else ("public_token", public_token)
        with self.connect() as db:
            row = db.execute(f"SELECT * FROM jobs WHERE {field} = ?", (value,)).fetchone()
        return dict(row) if row else None

    def append_event(self, job_id: str, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        now = utc_now()
        terminal = {"job.completed": "completed", "job.failed": "failed", "job.cancelled": "cancelled"}
        current_job = self.get_job(job_id=job_id)
        current_status = current_job["status"] if current_job else "queued"
        status = terminal.get(event["event_type"])
        if status is None:
            status = current_status if current_status in {"completed", "failed", "cancelled"} else "running"
        try:
            with self.connect() as db:
                cur = db.execute(
                    "INSERT INTO job_events(job_id, event_id, event_type, stage, message, "
                    "current_value, total_value, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        event["event_id"],
                        event["event_type"],
                        event.get("stage"),
                        event["message"],
                        event.get("current"),
                        event.get("total"),
                        json.dumps(event.get("payload") or {}, ensure_ascii=False),
                        now,
                    ),
                )
                db.execute(
                    "UPDATE jobs SET status = ?, updated_at = ?, error_code = ?, error_message = ? WHERE job_id = ?",
                    (
                        status,
                        now,
                        (event.get("payload") or {}).get("error_code") if status == "failed" else None,
                        event["message"] if status == "failed" else None,
                        job_id,
                    ),
                )
                seq = cur.lastrowid
        except sqlite3.IntegrityError:
            with self.connect() as db:
                row = db.execute(
                    "SELECT * FROM job_events WHERE job_id = ? AND event_id = ?",
                    (job_id, event["event_id"]),
                ).fetchone()
            return self._event_dict(row), False
        with self.connect() as db:
            row = db.execute("SELECT * FROM job_events WHERE seq = ?", (seq,)).fetchone()
        return self._event_dict(row), True

    def mark_report_ready(self, job_id: str, size_bytes: int) -> None:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "DELETE FROM artifacts WHERE job_id = ? AND artifact_type = 'html_report'", (job_id,)
            )
            db.execute(
                "INSERT INTO artifacts(job_id, artifact_type, relative_path, size_bytes, created_at) "
                "VALUES (?, 'html_report', 'index.html', ?, ?)",
                (job_id, size_bytes, now),
            )
            db.execute(
                "UPDATE jobs SET report_ready = 1, status = 'completed', updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )

    def snapshot(self, public_token: str) -> dict[str, Any] | None:
        job = self.get_job(public_token=public_token)
        if not job:
            return None
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY seq", (job["job_id"],)
            ).fetchall()
        return {"job": job, "events": [self._event_dict(row) for row in rows]}

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item
