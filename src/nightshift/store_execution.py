"""Leasing, completion, recovery, and query operations for the dispatcher store."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any

from .errors import InvalidTransitionError, LeaseError, SafetyViolation
from .models import (
    EventType,
    ExecutionResult,
    TaskRecord,
    TaskState,
    isoformat,
    state_from_value,
    utc_now,
)
from .store_schema import ALLOWED_TRANSITIONS
from .task_states import DEPENDENCY_SATISFIED_STATES


class StoreExecutionMixin:
    """Atomic queue operations used by worker processes."""

    def claim_next(
        self,
        builder_id: str,
        *,
        worker_id: str,
        lease_seconds: int,
        max_concurrency: int = 1,
        now: datetime | None = None,
    ) -> TaskRecord | None:
        current = now or utc_now()
        timestamp = isoformat(current)
        expiry = isoformat(current + timedelta(seconds=lease_seconds))
        with self._write() as conn:
            active_count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE builder_id = ? AND state IN ('CLAIMED', 'RUNNING', 'VERIFYING')",
                (builder_id,),
            ).fetchone()[0]
            if active_count >= max_concurrency:
                return None
            rows = conn.execute(
                """SELECT * FROM tasks
                   WHERE builder_id = ? AND state IN ('READY', 'WAITING_DEPENDENCY') AND available_at <= ?
                   ORDER BY priority DESC, created_at ASC, task_id ASC""",
                (builder_id, timestamp),
            ).fetchall()
            active_locks = [
                set(json.loads(item[0]))
                for item in conn.execute(
                    """SELECT resource_locks_json FROM tasks
                       WHERE state IN ('CLAIMED', 'RUNNING', 'VERIFYING')
                       AND resource_locks_json != '[]'"""
                ).fetchall()
            ]
            for row in rows:
                candidate_locks = set(json.loads(row["resource_locks_json"]))
                if candidate_locks and any(
                    candidate_locks & held for held in active_locks
                ):
                    self._append_event(
                        conn,
                        row["task_id"],
                        EventType.RESOURCE_LOCK_WAIT,
                        worker_id,
                        timestamp,
                        row["state"],
                        row["state"],
                        {"resource_locks": sorted(candidate_locks)},
                    )
                    continue
                dependencies = tuple(json.loads(row["dependency_ids_json"]))
                if dependencies:
                    dep_rows = conn.execute(
                        f"SELECT task_id, state FROM tasks WHERE task_id IN ({','.join('?' for _ in dependencies)})",
                        dependencies,
                    ).fetchall()
                    dep_states = {
                        dep["task_id"]: state_from_value(dep["state"])
                        for dep in dep_rows
                    }
                    if any(dep_id not in dep_states for dep_id in dependencies):
                        continue
                    if any(
                        dep_states[dep_id]
                        in {
                            TaskState.FAILED_SAFE,
                            TaskState.CANCELLED,
                            TaskState.BLOCKED,
                        }
                        for dep_id in dependencies
                    ):
                        conn.execute(
                            "UPDATE tasks SET state = 'BLOCKED', updated_at = ?, last_error = ? WHERE task_id = ?",
                            (timestamp, "dependency did not succeed", row["task_id"]),
                        )
                        self._append_event(
                            conn,
                            row["task_id"],
                            EventType.BLOCKED,
                            worker_id,
                            timestamp,
                            row["state"],
                            TaskState.BLOCKED.value,
                            {"dependencies": list(dependencies)},
                        )
                        continue
                    if any(
                        dep_states[dep_id] not in DEPENDENCY_SATISFIED_STATES
                        for dep_id in dependencies
                    ):
                        if row["state"] == TaskState.READY.value:
                            conn.execute(
                                "UPDATE tasks SET state = 'WAITING_DEPENDENCY', updated_at = ? WHERE task_id = ?",
                                (timestamp, row["task_id"]),
                            )
                            self._append_event(
                                conn,
                                row["task_id"],
                                EventType.WAITING_DEPENDENCY,
                                worker_id,
                                timestamp,
                                TaskState.READY.value,
                                TaskState.WAITING_DEPENDENCY.value,
                                {"dependencies": list(dependencies)},
                            )
                        continue
                generation = row["lease_generation"] + 1
                new_attempt = row["attempt_count"] + 1
                updated = conn.execute(
                    """UPDATE tasks SET state = 'CLAIMED', attempt_count = ?, lease_generation = ?,
                       updated_at = ?, lease_owner = ?, lease_expires_at = ?, process_id = NULL,
                       last_error = NULL, failure_class = NULL
                       WHERE task_id = ? AND state IN ('READY', 'WAITING_DEPENDENCY')""",
                    (
                        new_attempt,
                        generation,
                        timestamp,
                        worker_id,
                        expiry,
                        row["task_id"],
                    ),
                )
                if (
                    updated.rowcount != 1
                ):  # pragma: no cover - BEGIN IMMEDIATE fences races
                    continue
                active_locks.append(candidate_locks)
                self._append_event(
                    conn,
                    row["task_id"],
                    EventType.CLAIMED,
                    worker_id,
                    timestamp,
                    row["state"],
                    TaskState.CLAIMED.value,
                    {
                        "attempt": new_attempt,
                        "lease_expires_at": expiry,
                        "lease_generation": generation,
                        "resource_locks": sorted(candidate_locks),
                    },
                )
                return self._record(self._get_row(conn, row["task_id"]))
        return None

    def heartbeat(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: int,
        lease_generation: int | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        current = now or utc_now()
        timestamp = isoformat(current)
        expiry = isoformat(current + timedelta(seconds=lease_seconds))
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            self._assert_active_lease(
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                now=current,
            )
            conn.execute(
                "UPDATE tasks SET updated_at = ?, lease_expires_at = ? WHERE task_id = ?",
                (timestamp, expiry, task_id),
            )
            self._append_event(
                conn,
                task_id,
                EventType.HEARTBEAT,
                worker_id,
                timestamp,
                row["state"],
                row["state"],
                {
                    "lease_expires_at": expiry,
                    "lease_generation": row["lease_generation"],
                },
            )
            return self._record(self._get_row(conn, task_id))

    def complete(
        self,
        task_id: str,
        *,
        worker_id: str,
        execution: ExecutionResult,
        lease_generation: int | None = None,
        retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        current = now or utc_now()
        timestamp = isoformat(current)
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            previous = state_from_value(row["state"])
            self._assert_active_lease(
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                now=current,
            )
            result_json = self._json(execution.as_dict())
            signature: str | None = None
            repeat_count = row["failure_repeat_count"]
            debug_attempt = row["debug_attempt_count"]
            park_debug = False
            delivery_recovery = self._delivery_recovery(row, execution)
            delivery_recovered = delivery_recovery is not None
            if delivery_recovery is not None:
                new_state = (
                    TaskState.PR_READY
                    if execution.success
                    and delivery_recovery["pr_number"]
                    and delivery_recovery["pr_url"]
                    else TaskState.BLOCKED
                )
                event = (
                    EventType.PR_READY
                    if new_state is TaskState.PR_READY
                    else EventType.DELIVERY_BLOCKED
                )
                details = {
                    "summary": execution.summary,
                    "delivery_blocked": True,
                    "implementation_preserved": True,
                    "commit_sha": delivery_recovery["commit_sha"],
                    "remote_sha": delivery_recovery["remote_sha"],
                    "pr_number": delivery_recovery["pr_number"],
                }
                updates = {
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "process_id": None,
                    "result_json": result_json,
                    "last_error": f"DELIVERY_BLOCKED: {execution.summary or 'delivery requires operator reconciliation'}"[
                        :4000
                    ],
                    "failure_class": "DELIVERY_FAILED",
                    "commit_sha": delivery_recovery["commit_sha"],
                    "remote_sha": delivery_recovery["remote_sha"],
                    "pr_number": delivery_recovery["pr_number"],
                    "pr_url": delivery_recovery["pr_url"],
                    "verification_json": self._json(delivery_recovery["verification"]),
                    "delivery_json": self._json(delivery_recovery["delivery"]),
                }
            elif execution.success:
                new_state = execution.terminal_state or TaskState.COMPLETED
                if row["requires_pr"] and new_state is not TaskState.PR_READY:
                    raise SafetyViolation(
                        "code-changing task cannot complete without PR_READY evidence"
                    )
                if new_state is TaskState.PR_READY and not (
                    row["pr_number"] and row["pr_url"]
                ):
                    raise SafetyViolation(
                        "PR_READY requires recorded GitHub pull-request evidence"
                    )
                event = (
                    EventType.PR_READY
                    if new_state is TaskState.PR_READY
                    else EventType.COMPLETED
                )
                details = {"summary": execution.summary}
                updates = {
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "process_id": None,
                    "result_json": result_json,
                    "last_error": None,
                    "failure_class": None,
                }
            elif execution.retryable:
                signature = self._failure_signature(execution)
                repeat_count = (
                    row["failure_repeat_count"] + 1
                    if row["last_failure_signature"] == signature
                    else 1
                )
                debug_attempt = row["debug_attempt_count"] + 1
                debug_budget = row["debug_budget"] or 0
                repeat_limit = row["repeated_failure_limit"] or 2
                park_debug = (
                    execution.retryable
                    and debug_budget > 0
                    and (debug_attempt >= debug_budget or repeat_count >= repeat_limit)
                )
            repeated_timeout = (
                not delivery_recovered
                and not execution.success
                and execution.retryable
                and self._is_timeout(execution)
                and signature is not None
                and row["last_failure_signature"] == signature
            )
            if repeated_timeout:
                new_state, event = TaskState.FAILED_SAFE, EventType.DEAD_LETTERED
                reason = "REPEATED_TIMEOUT"
                details = {
                    "summary": execution.summary,
                    "attempt": row["attempt_count"],
                    "failure_repeat_count": repeat_count,
                    "timeout_signature": execution.timeout_signature
                    or execution.data.get("timeout_signature"),
                    "reason": reason,
                }
                updates = {
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "process_id": None,
                    "result_json": result_json,
                    "last_error": f"{reason}: {execution.summary or 'identical timeout repeated'}"[
                        :4000
                    ],
                    "failure_class": reason,
                    "debug_attempt_count": debug_attempt,
                    "last_failure_signature": signature,
                    "failure_repeat_count": repeat_count,
                }
            elif (
                not delivery_recovered
                and not execution.success
                and execution.retryable
                and row["attempt_count"] < row["max_attempts"]
                and not park_debug
            ):
                new_state, event = TaskState.READY, EventType.RETRY_SCHEDULED
                next_at = isoformat(retry_at or current)
                details = {
                    "summary": execution.summary,
                    "attempt": row["attempt_count"],
                    "available_at": next_at,
                }
                updates = {
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "process_id": None,
                    "result_json": result_json,
                    "last_error": execution.summary or "worker failed",
                    "available_at": next_at,
                    "failure_class": execution.failure_class
                    or execution.data.get("failure_class")
                    or "RETRYABLE_FAILURE",
                    "debug_attempt_count": debug_attempt,
                    "last_failure_signature": signature,
                    "failure_repeat_count": repeat_count,
                }
            elif (
                not delivery_recovered
                and not execution.success
                and execution.retryable
                and park_debug
            ):
                new_state, event = TaskState.BLOCKED, EventType.BLOCKED
                requested_retry = retry_at or current
                next_at = isoformat(
                    max(requested_retry, current + timedelta(seconds=60))
                )
                reason = (
                    "AUTONOMOUS_DEBUG_BUDGET_EXHAUSTED"
                    if debug_attempt >= debug_budget
                    else "AUTONOMOUS_DEBUG_REPEATED_FAILURE"
                )
                details = {
                    "summary": execution.summary,
                    "attempt": row["attempt_count"],
                    "debug_attempt": debug_attempt,
                    "debug_budget": debug_budget,
                    "failure_repeat_count": repeat_count,
                    "reason": reason,
                    "available_at": next_at,
                }
                updates = {
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "process_id": None,
                    "result_json": result_json,
                    "last_error": f"{reason}: {execution.summary or 'worker failed'}"[
                        :4000
                    ],
                    "available_at": next_at,
                    "failure_class": reason,
                    "debug_attempt_count": debug_attempt,
                    "last_failure_signature": signature,
                    "failure_repeat_count": repeat_count,
                }
            elif not delivery_recovered and not execution.success:
                new_state, event = TaskState.FAILED_SAFE, EventType.FAILED_SAFE
                classified = execution.failure_class or execution.data.get(
                    "failure_class"
                )
                failure_class = (
                    classified
                    if isinstance(classified, str) and classified.strip()
                    else "DEAD_LETTER"
                    if execution.retryable
                    else "FAILED_SAFE"
                )
                details = {
                    "summary": execution.summary,
                    "attempt": row["attempt_count"],
                    "retryable": execution.retryable,
                }
                updates = {
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "process_id": None,
                    "result_json": result_json,
                    "last_error": execution.summary or "worker failed safely",
                    "failure_class": failure_class,
                    "debug_attempt_count": row["debug_attempt_count"] + 1
                    if not execution.success and row["debug_budget"]
                    else row["debug_attempt_count"],
                }
            if new_state not in ALLOWED_TRANSITIONS[previous]:
                raise InvalidTransitionError(
                    f"{previous.value} -> {new_state.value} is not permitted"
                )
            assignments = ["state = ?", "updated_at = ?"]
            values: list[Any] = [new_state.value, timestamp]
            for column, value in updates.items():
                assignments.append(f"{column} = ?")
                values.append(value)
            values.append(task_id)
            conn.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE task_id = ?", values
            )
            self._append_event(
                conn,
                task_id,
                event,
                worker_id,
                timestamp,
                previous.value,
                new_state.value,
                details,
            )
            return self._record(self._get_row(conn, task_id))

    @staticmethod
    def _failure_signature(execution: ExecutionResult) -> str:
        classified = execution.failure_class or execution.data.get("failure_class")
        failure_class = (
            classified.strip() if isinstance(classified, str) else "worker_failure"
        )
        timeout_signature = execution.timeout_signature or execution.data.get(
            "timeout_signature"
        )
        if isinstance(timeout_signature, str) and timeout_signature.strip():
            return sha256(
                f"timeout:{timeout_signature.strip()[:256]}".encode()
            ).hexdigest()
        summary = " ".join(execution.summary.split())[:1000]
        return sha256(f"{failure_class}:{summary}".encode()).hexdigest()

    @staticmethod
    def _is_timeout(execution: ExecutionResult) -> bool:
        failure_class = execution.failure_class or execution.data.get("failure_class")
        return (
            isinstance(failure_class, str)
            and failure_class.strip().upper() in {"TIMEOUT", "DEAD_LETTER", "EXECUTOR_TIMEOUT"}
        ) or bool(
            isinstance(execution.timeout_signature, str)
            or isinstance(execution.data.get("timeout_signature"), str)
            or "timed out" in execution.summary.lower()
        )

    @staticmethod
    def _delivery_recovery(
        row: Any, execution: ExecutionResult
    ) -> dict[str, Any] | None:
        """Extract only already-produced evidence from a delivery failure."""

        data = execution.data
        status = str(data.get("delivery_status", "")).strip().lower()
        if status not in {"blocked", "failed", "delivery_failed"}:
            return None
        implementation_success = data.get("implementation_success") is True or bool(
            data.get("commit_sha")
            or data.get("remote_sha")
            or data.get("verification_json")
        )
        existing_verification = (
            json.loads(row["verification_json"]) if row["verification_json"] else {}
        )
        verification = data.get("verification_json")
        if not isinstance(verification, dict) or not verification:
            verification = existing_verification
        if not isinstance(verification, dict) or not verification:
            return None
        if not implementation_success:
            implementation_success = bool(
                row["commit_sha"] or row["remote_sha"] or existing_verification
            )
        commit_sha = data.get("commit_sha") or row["commit_sha"]
        remote_sha = data.get("remote_sha") or row["remote_sha"]
        pr_number = data.get("pr_number") or row["pr_number"]
        pr_url = data.get("pr_url") or row["pr_url"]
        has_commit_and_remote = bool(
            isinstance(commit_sha, str)
            and commit_sha.strip()
            and isinstance(remote_sha, str)
            and remote_sha.strip()
        )
        has_pr = isinstance(pr_number, int) and not isinstance(pr_number, bool) and isinstance(pr_url, str) and bool(pr_url.strip())
        if not implementation_success or not (has_commit_and_remote or has_pr):
            return None
        existing_delivery = (
            json.loads(row["delivery_json"]) if row["delivery_json"] else {}
        )
        if not isinstance(existing_delivery, dict):
            existing_delivery = {}
        existing_delivery.update(
            {
                "delivery_blocked": True,
                "delivery_reason": data.get("delivery_reason") or execution.summary,
                "commit_sha": commit_sha,
                "remote_sha": remote_sha,
                "pr_number": pr_number,
                "pr_url": pr_url,
                "verification": verification,
            }
        )
        return {
            "commit_sha": commit_sha,
            "remote_sha": remote_sha,
            "pr_number": pr_number if has_pr else None,
            "pr_url": pr_url if has_pr else None,
            "verification": verification,
            "delivery": existing_delivery,
        }

    def recover_expired(
        self, *, now: datetime | None = None, actor: str = "reaper"
    ) -> list[TaskRecord]:
        current = now or utc_now()
        timestamp = isoformat(current)
        recovered: list[TaskRecord] = []
        with self._write() as conn:
            rows = conn.execute(
                """SELECT * FROM tasks WHERE state IN ('CLAIMED', 'RUNNING', 'VERIFYING')
                   AND lease_expires_at IS NOT NULL AND lease_expires_at <= ? ORDER BY task_id""",
                (timestamp,),
            ).fetchall()
            for row in rows:
                process_alive = self._process_alive(row["process_id"])
                if process_alive:
                    new_state = TaskState.FAILED_SAFE
                    event = EventType.FAILED_SAFE
                    available = row["available_at"]
                    failure = "STALE_PROCESS_RUNNING"
                elif row["attempt_count"] < row["max_attempts"]:
                    new_state = TaskState.READY
                    event = EventType.LEASE_EXPIRED
                    available = timestamp
                    failure = "STALE_WORKER"
                else:
                    new_state = TaskState.FAILED_SAFE
                    event = EventType.DEAD_LETTERED
                    available = row["available_at"]
                    failure = "DEAD_LETTER"
                fenced_generation = row["lease_generation"] + 1
                conn.execute(
                    """UPDATE tasks SET state = ?, updated_at = ?, available_at = ?,
                       lease_owner = NULL, lease_expires_at = NULL, process_id = NULL,
                       lease_generation = ?, last_error = ?, failure_class = ?
                       WHERE task_id = ?""",
                    (
                        new_state.value,
                        timestamp,
                        available,
                        fenced_generation,
                        "lease expired before completion",
                        failure,
                        row["task_id"],
                    ),
                )
                self._append_event(
                    conn,
                    row["task_id"],
                    event,
                    actor,
                    timestamp,
                    row["state"],
                    new_state.value,
                    {
                        "previous_owner": row["lease_owner"],
                        "attempt": row["attempt_count"],
                        "lease_generation": row["lease_generation"],
                        "fenced_generation": fenced_generation,
                        "process_alive": process_alive,
                    },
                )
                recovered.append(self._record(self._get_row(conn, row["task_id"])))
        return recovered

    @staticmethod
    def _process_alive(process_id: int | None) -> bool:
        """Conservatively prevent reclaiming a worktree held by a live PID."""

        if not isinstance(process_id, int) or process_id <= 0:
            return False
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def cancel(
        self, task_id: str, *, actor: str, reason: str = "", now: datetime | None = None
    ) -> TaskRecord:
        current = now or utc_now()
        record = self.get(task_id)
        if record.lease_active:
            raise LeaseError("leased work must finish or expire before cancellation")
        return self.transition(
            task_id,
            expected=record.state,
            new_state=TaskState.CANCELLED,
            actor=actor,
            event_type=EventType.CANCELLED,
            details={"reason": reason},
            now=current,
            updates={"last_error": reason or "cancelled"},
        )

    def list_tasks(
        self,
        *,
        state: TaskState | None = None,
        builder_id: str | None = None,
        limit: int = 100,
    ) -> list[TaskRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value)
        if builder_id is not None:
            clauses.append("builder_id = ?")
            params.append(builder_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at DESC, task_id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._record(row) for row in rows]
