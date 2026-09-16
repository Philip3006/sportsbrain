"""Durable SQLite queue and append-only audit store for Night Shift."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit import AuditIntegrityError, AuditMixin
from .control import QueueControlMixin
from .errors import IdempotencyConflictError, InvalidTransitionError, TaskNotFoundError
from .models import (
    EventType,
    RiskClass,
    TaskRecord,
    TaskSpec,
    TaskState,
    isoformat,
    state_from_value,
    utc_now,
)
from .store_execution import StoreExecutionMixin
from .store_lifecycle import StoreLifecycleMixin
from .store_schema import ALLOWED_TRANSITIONS, DDL

__all__ = ["ALLOWED_TRANSITIONS", "AuditIntegrityError", "DispatcherStore"]


class DispatcherStore(
    StoreLifecycleMixin, StoreExecutionMixin, QueueControlMixin, AuditMixin
):
    """SQLite store whose state changes and audit events commit atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connection()
        try:
            conn.executescript(DDL)
            self._migrate(conn)
            conn.execute(
                "INSERT OR IGNORE INTO dispatcher_meta(key, value) VALUES ('paused', '0')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO dispatcher_meta(key, value) VALUES ('last_event_hash', ?)",
                ("0" * 64,),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        requires_pr_added = "requires_pr" not in columns
        additions = {
            "allowed_paths_json": "TEXT NOT NULL DEFAULT '[]'",
            "prohibited_paths_json": "TEXT NOT NULL DEFAULT '[]'",
            "resource_locks_json": "TEXT NOT NULL DEFAULT '[]'",
            "worktree_path": "TEXT",
            "diagnostic_path": "TEXT",
            "failure_class": "TEXT",
            "expected_base_sha": "TEXT",
            "base_branch": "TEXT NOT NULL DEFAULT 'main'",
            "base_sha": "TEXT",
            "origin_sha": "TEXT",
            "required_tests_json": "TEXT NOT NULL DEFAULT '[]'",
            "verification_commands_json": "TEXT NOT NULL DEFAULT '[]'",
            "max_runtime_seconds": "INTEGER NOT NULL DEFAULT 900",
            "requires_pr": "INTEGER NOT NULL DEFAULT 0",
            "lease_generation": "INTEGER NOT NULL DEFAULT 0",
            "process_id": "INTEGER",
            "commit_sha": "TEXT",
            "remote_sha": "TEXT",
            "pr_number": "INTEGER",
            "pr_url": "TEXT",
            "verification_json": "TEXT",
            "delivery_json": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        conn.execute(
            """UPDATE tasks SET state = CASE state
                WHEN 'pending_approval' THEN 'BACKLOG'
                WHEN 'queued' THEN 'READY'
                WHEN 'retry_wait' THEN 'READY'
                WHEN 'leased' THEN 'CLAIMED'
                WHEN 'running' THEN 'RUNNING'
                WHEN 'verifying' THEN 'VERIFYING'
                WHEN 'succeeded' THEN 'COMPLETED'
                WHEN 'pr_ready' THEN 'PR_READY'
                WHEN 'ceo_review' THEN 'CEO_REVIEW'
                WHEN 'failed' THEN 'FAILED_SAFE'
                WHEN 'failed_safe' THEN 'FAILED_SAFE'
                WHEN 'dead_letter' THEN 'FAILED_SAFE'
                WHEN 'blocked' THEN 'BLOCKED'
                WHEN 'cancelled' THEN 'CANCELLED'
                ELSE state END"""
        )
        if requires_pr_added:
            conn.execute(
                "UPDATE tasks SET requires_pr = 1 WHERE risk_class = 'code_change'"
            )

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), timeout=30, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connection()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            builder_id=row["builder_id"],
            objective=row["objective"],
            branch=row["branch"],
            repo=row["repo"],
            task_type=row["task_type"],
            template_id=row["template_id"],
            payload=json.loads(row["payload_json"]),
            risk_class=RiskClass(row["risk_class"]),
            requires_approval=bool(row["requires_approval"]),
            priority=row["priority"],
            max_attempts=row["max_attempts"],
            attempt_count=row["attempt_count"],
            state=state_from_value(row["state"]),
            requested_by=row["requested_by"],
            parent_task_id=row["parent_task_id"],
            dependency_ids=tuple(json.loads(row["dependency_ids_json"])),
            allowed_paths=tuple(json.loads(row["allowed_paths_json"])),
            prohibited_paths=tuple(json.loads(row["prohibited_paths_json"])),
            resource_locks=tuple(json.loads(row["resource_locks_json"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            available_at=row["available_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            last_error=row["last_error"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            worktree_path=row["worktree_path"],
            expected_base_sha=row["expected_base_sha"],
            base_branch=row["base_branch"] or "main",
            base_sha=row["base_sha"],
            origin_sha=row["origin_sha"],
            required_tests=tuple(json.loads(row["required_tests_json"])),
            verification_commands=tuple(
                tuple(command)
                for command in json.loads(row["verification_commands_json"])
            ),
            max_runtime_seconds=row["max_runtime_seconds"] or 900,
            requires_pr=bool(row["requires_pr"]),
            lease_generation=row["lease_generation"] or 0,
            process_id=row["process_id"],
            commit_sha=row["commit_sha"],
            remote_sha=row["remote_sha"],
            pr_number=row["pr_number"],
            pr_url=row["pr_url"],
            verification=json.loads(row["verification_json"])
            if row["verification_json"]
            else None,
            delivery=json.loads(row["delivery_json"]) if row["delivery_json"] else None,
            diagnostic_path=row["diagnostic_path"],
            failure_class=row["failure_class"],
        )

    @staticmethod
    def _get_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"task {task_id!r} does not exist")
        return row

    def get(self, task_id: str) -> TaskRecord:
        with self._read() as conn:
            return self._record(self._get_row(conn, task_id))

    def get_by_idempotency(self, key: str) -> TaskRecord | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?", (key,)
            ).fetchone()
            return self._record(row) if row is not None else None

    def create(
        self,
        task: TaskSpec,
        *,
        initial_state: TaskState,
        initial_error: str | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            try:
                conn.execute(
                    """INSERT INTO tasks (
                        task_id, idempotency_key, builder_id, objective, branch, repo,
                        task_type, template_id, payload_json, risk_class, requires_approval,
                        priority, max_attempts, state, requested_by, parent_task_id,
                        dependency_ids_json, allowed_paths_json, prohibited_paths_json, resource_locks_json,
                        created_at, updated_at, available_at, last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task.task_id,
                        task.idempotency_key,
                        task.builder_id,
                        task.objective,
                        task.branch,
                        task.repo,
                        task.task_type,
                        task.template_id,
                        self._json(task.payload),
                        task.risk_class.value,
                        int(task.approval_required),
                        task.priority,
                        task.max_attempts,
                        initial_state.value,
                        task.requested_by,
                        task.parent_task_id,
                        self._json(list(task.dependency_ids)),
                        self._json(list(task.allowed_paths)),
                        self._json(list(task.prohibited_paths)),
                        self._json(list(task.resource_locks)),
                        timestamp,
                        timestamp,
                        timestamp,
                        initial_error,
                    ),
                )
                conn.execute(
                    """UPDATE tasks SET expected_base_sha = ?, base_branch = ?,
                       required_tests_json = ?, verification_commands_json = ?,
                       max_runtime_seconds = ?, requires_pr = ? WHERE task_id = ?""",
                    (
                        task.expected_base_sha,
                        task.base_branch,
                        self._json(list(task.required_tests)),
                        self._json(
                            [list(command) for command in task.verification_commands]
                        ),
                        task.max_runtime_seconds,
                        int(task.pr_required),
                        task.task_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if task.idempotency_key:
                    row = conn.execute(
                        "SELECT * FROM tasks WHERE idempotency_key = ?",
                        (task.idempotency_key,),
                    ).fetchone()
                    existing = self._record(row) if row is not None else None
                    if existing is not None:
                        if existing.canonical() == task.canonical():
                            return existing
                        raise IdempotencyConflictError(
                            "idempotency key already belongs to another task"
                        ) from exc
                raise
            self._append_event(
                conn,
                task.task_id,
                EventType.CREATED,
                task.requested_by,
                timestamp,
                None,
                initial_state.value,
                {"builder_id": task.builder_id, "reason": initial_error}
                if initial_error
                else {"builder_id": task.builder_id},
            )
            return self._record(self._get_row(conn, task.task_id))

    def transition(
        self,
        task_id: str,
        *,
        expected: TaskState,
        new_state: TaskState,
        actor: str,
        event_type: EventType | str,
        details: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> TaskRecord:
        if new_state not in ALLOWED_TRANSITIONS[expected]:
            raise InvalidTransitionError(
                f"{expected.value} -> {new_state.value} is not permitted"
            )
        timestamp = isoformat(now or utc_now())
        updates = dict(updates or {})
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            actual = TaskState(row["state"])
            if actual is not expected:
                raise InvalidTransitionError(
                    f"task {task_id} is {actual.value}, expected {expected.value}"
                )
            assignments = ["state = ?", "updated_at = ?"]
            values: list[Any] = [new_state.value, timestamp]
            for column, value in updates.items():
                if column not in {
                    "available_at",
                    "lease_owner",
                    "lease_expires_at",
                    "last_error",
                    "result_json",
                    "attempt_count",
                    "worktree_path",
                    "diagnostic_path",
                    "failure_class",
                    "expected_base_sha",
                    "base_branch",
                    "base_sha",
                    "origin_sha",
                    "required_tests_json",
                    "verification_commands_json",
                    "max_runtime_seconds",
                    "requires_pr",
                    "lease_generation",
                    "process_id",
                    "commit_sha",
                    "remote_sha",
                    "pr_number",
                    "pr_url",
                    "verification_json",
                    "delivery_json",
                }:
                    raise InvalidTransitionError(f"unsupported task update {column}")
                assignments.append(f"{column} = ?")
                values.append(value)
            values.append(task_id)
            conn.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id = ?", values
            )
            self._append_event(
                conn,
                task_id,
                event_type,
                actor,
                timestamp,
                expected.value,
                new_state.value,
                details or {},
            )
            return self._record(self._get_row(conn, task_id))
