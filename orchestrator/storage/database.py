"""Durable SQLite task state with write-ahead logging.

Design rules enforced here:

* Every mutation is committed synchronously (``synchronous=FULL``) so a crash
  can never lose a dispatch decision.
* ``tasks.idempotency_key`` is UNIQUE: duplicate task creation is impossible.
* Status changes append an immutable row to ``task_events`` (audit trail).
* Approvals are stored with a status and are never implied by a model output.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import (
    ApprovalStatus,
    Task,
    TaskStatus,
    make_idempotency_key,
    new_id,
    utcnow_iso,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL,
    branch TEXT NOT NULL,
    objective TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    dispatch_at TEXT,
    arena_ref TEXT,
    status TEXT NOT NULL,
    changed_files TEXT NOT NULL DEFAULT '[]',
    test_results TEXT NOT NULL DEFAULT '{}',
    verification TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    final_result TEXT,
    parent_goal TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);

CREATE TABLE IF NOT EXISTS task_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, seq);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    operation TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    resolved_at TEXT,
    resolver TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS idempotency_log (
    key TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    response TEXT
);
"""


class DatabaseError(Exception):
    pass


class Database:
    """Thread-safe SQLite store. One instance per process is recommended."""

    def __init__(self, path: str | Path, *, verbose: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure(verbose=verbose)
        self._migrate()

    def _configure(self, verbose: bool = False) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=FULL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        self._conn.commit()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            cur = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            row = cur.fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                self._conn.commit()
            elif int(row["value"]) != SCHEMA_VERSION:
                raise DatabaseError(
                    f"unsupported schema version {row['value']} (expected {SCHEMA_VERSION})"
                )

    # -- tasks -------------------------------------------------------------

    def create_task(self, task: Task) -> Task:
        """Insert a task. Raises DatabaseError on duplicate idempotency key."""
        with self._lock:
            task.updated_at = utcnow_iso()  # type: ignore[attr-defined]
            row = task.to_row()
            row["updated_at"] = task.updated_at
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            try:
                self._conn.execute(
                    f"INSERT INTO tasks ({cols}) VALUES ({placeholders})",
                    tuple(row.values()),
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseError(
                    f"task with idempotency_key {task.idempotency_key} already exists"
                ) from exc
            self._conn.commit()
            self._record_event_locked(task.id, None, task.status, "task created")
            return task

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return Task.from_row(dict(row)) if row else None

    def get_task_by_idempotency(self, key: str) -> Optional[Task]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE idempotency_key=?", (key,)
            ).fetchone()
            return Task.from_row(dict(row)) if row else None

    def list_tasks(
        self,
        status: Optional[str] = None,
        project: Optional[str] = None,
        limit: int = 200,
    ) -> List[Task]:
        with self._lock:
            sql = "SELECT * FROM tasks"
            clauses: List[str] = []
            params: List[Any] = []
            if status:
                clauses.append("status=?")
                params.append(status)
            if project:
                clauses.append("project=?")
                params.append(project)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(int(limit))
            rows = self._conn.execute(sql, tuple(params)).fetchall()
            return [Task.from_row(dict(r)) for r in rows]

    def count_by_status(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
            return {r["status"]: int(r["n"]) for r in rows}

    def update_task(
        self,
        task_id: str,
        *,
        status: Optional[str] = None,
        arena_ref: Optional[str] = None,
        changed_files: Optional[List[str]] = None,
        test_results: Optional[Dict[str, Any]] = None,
        verification: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        retry_count: Optional[int] = None,
        final_result: Optional[str] = None,
        dispatch_at: Optional[str] = None,
        clear_error: bool = False,
        message: str = "",
    ) -> Optional[Task]:
        """Update a task and append an audit event when status changes."""
        with self._lock:
            existing = self.get_task(task_id)
            if existing is None:
                return None
            sets: List[str] = []
            params: List[Any] = []
            new_status = existing.status
            if status is not None and status != existing.status:
                sets.append("status=?")
                params.append(status)
                new_status = status
            if arena_ref is not None:
                sets.append("arena_ref=?")
                params.append(arena_ref)
            if changed_files is not None:
                sets.append("changed_files=?")
                params.append(json.dumps(changed_files))
            if test_results is not None:
                sets.append("test_results=?")
                params.append(json.dumps(test_results))
            if verification is not None:
                sets.append("verification=?")
                params.append(json.dumps(verification))
            if error is not None:
                sets.append("error=?")
                params.append(error)
            if clear_error and existing.error is not None:
                sets.append("error=?")
                params.append(None)
            if retry_count is not None:
                sets.append("retry_count=?")
                params.append(int(retry_count))
            if final_result is not None:
                sets.append("final_result=?")
                params.append(final_result)
            if dispatch_at is not None:
                sets.append("dispatch_at=?")
                params.append(dispatch_at)
            sets.append("updated_at=?")
            params.append(utcnow_iso())
            params.append(task_id)
            self._conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", tuple(params)
            )
            self._conn.commit()
            if new_status != existing.status:
                self._record_event_locked(
                    task_id, existing.status, new_status, message or "status change"
                )
            return self.get_task(task_id)

    def _record_event_locked(
        self, task_id: str, from_status: Optional[str], to_status: str, message: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO task_events(task_id, from_status, to_status, message, created_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, from_status, to_status, message, utcnow_iso()),
        )
        self._conn.commit()

    def events(self, task_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY seq", (task_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # -- idempotency -------------------------------------------------------

    def record_idempotency(self, key: str, scope: str, response: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO idempotency_log(key, scope, created_at, response) "
                "VALUES (?,?,?,?)",
                (key, scope, utcnow_iso(), response),
            )
            self._conn.commit()

    def idempotency_response(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT response FROM idempotency_log WHERE key=?", (key,)
            ).fetchone()
            return row["response"] if row else None

    # -- approvals ---------------------------------------------------------

    def request_approval(
        self, operation: str, reason: str, task_id: Optional[str] = None
    ) -> str:
        approval_id = new_id("appr")
        with self._lock:
            self._conn.execute(
                "INSERT INTO approvals(id, task_id, operation, reason, status, requested_at, detail) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    approval_id,
                    task_id,
                    operation,
                    reason,
                    ApprovalStatus.PENDING.value,
                    utcnow_iso(),
                    "",
                ),
            )
            self._conn.commit()
        return approval_id

    def resolve_approval(
        self, approval_id: str, granted: bool, resolver: str = "human"
    ) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] != ApprovalStatus.PENDING.value:
                return row["status"]
            new_status = (
                ApprovalStatus.GRANTED.value if granted else ApprovalStatus.DENIED.value
            )
            self._conn.execute(
                "UPDATE approvals SET status=?, resolved_at=?, resolver=? WHERE id=?",
                (new_status, utcnow_iso(), resolver, approval_id),
            )
            self._conn.commit()
            return new_status

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
            return dict(row) if row else None

    def pending_approvals(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE status=? ORDER BY requested_at",
                (ApprovalStatus.PENDING.value,),
            ).fetchall()
            return [dict(r) for r in rows]

    # -- maintenance -------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def checkpoint(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
