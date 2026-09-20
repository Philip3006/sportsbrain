"""Lifecycle operations for leased and approval-gated dispatcher tasks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .errors import InvalidTransitionError, LeaseError, SafetyViolation
from .models import (
    EventType,
    ExecutionResult,
    TaskState,
    isoformat,
    parse_timestamp,
    utc_now,
)


class StoreLifecycleMixin:
    """State transitions that attach worktree and review lifecycle evidence."""

    def assign_worktree(
        self,
        task_id: str,
        *,
        worker_id: str,
        worktree_path: str,
        diagnostic_path: str,
        base_branch: str = "main",
        base_sha: str | None = None,
        origin_sha: str | None = None,
        lease_generation: int,
        now: datetime | None = None,
    ):
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            self._assert_active_lease(
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                states={TaskState.CLAIMED.value},
                now=now,
            )
            conn.execute(
                """UPDATE tasks SET worktree_path = ?, diagnostic_path = ?,
                   base_branch = ?, base_sha = ?, origin_sha = ?, updated_at = ?
                   WHERE task_id = ?""",
                (
                    worktree_path,
                    diagnostic_path,
                    base_branch,
                    base_sha,
                    origin_sha,
                    timestamp,
                    task_id,
                ),
            )
            self._append_event(
                conn,
                task_id,
                EventType.WORKTREE_ALLOCATED,
                worker_id,
                timestamp,
                TaskState.CLAIMED.value,
                TaskState.CLAIMED.value,
                {
                    "worktree_path": worktree_path,
                    "base_branch": base_branch,
                    "base_sha": base_sha,
                    "origin_sha": origin_sha,
                },
            )
            return self._record(self._get_row(conn, task_id))

    def _assert_active_lease(
        self,
        row: Any,
        *,
        worker_id: str,
        lease_generation: int | None,
        states: set[str] | None = None,
        now: datetime | None = None,
    ) -> None:
        active_states = states or {
            TaskState.CLAIMED.value,
            TaskState.RUNNING.value,
            TaskState.VERIFYING.value,
            TaskState.DELIVERY_RECONCILING.value,
        }
        if lease_generation is None:
            raise LeaseError("lease generation is required for worker mutation")
        current = now or utc_now()
        if (
            row["state"] not in active_states
            or row["lease_owner"] != worker_id
            or row["lease_generation"] != lease_generation
        ):
            raise LeaseError("worker lease is stale or fenced")
        try:
            expires_at = parse_timestamp(row["lease_expires_at"])
        except (TypeError, ValueError) as exc:
            raise LeaseError("worker lease is missing or invalid") from exc
        if expires_at <= current:
            raise LeaseError("worker lease is expired")

    def assert_active_lease(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        now: datetime | None = None,
    ):
        """Read-only fence used immediately before external worker mutations."""

        with self._read() as conn:
            row = self._get_row(conn, task_id)
            self._assert_active_lease(
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                now=now,
            )
            return self._record(row)

    def start_running(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        now: datetime | None = None,
    ):
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            self._assert_active_lease(
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                states={TaskState.CLAIMED.value},
                now=now,
            )
            conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?",
                (TaskState.RUNNING.value, timestamp, task_id),
            )
            self._append_event(
                conn,
                task_id,
                EventType.RUNNING,
                worker_id,
                timestamp,
                TaskState.CLAIMED.value,
                TaskState.RUNNING.value,
                {"lease_generation": lease_generation},
            )
            return self._record(self._get_row(conn, task_id))

    def begin_verifying(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        now: datetime | None = None,
    ):
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            self._assert_active_lease(
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                states={TaskState.RUNNING.value},
                now=now,
            )
            conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?",
                (TaskState.VERIFYING.value, timestamp, task_id),
            )
            self._append_event(
                conn,
                task_id,
                EventType.VERIFYING,
                worker_id,
                timestamp,
                TaskState.RUNNING.value,
                TaskState.VERIFYING.value,
                {"lease_generation": lease_generation},
            )
            return self._record(self._get_row(conn, task_id))

    def record_process(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        process_id: int,
        now: datetime | None = None,
    ):
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            self._assert_active_lease(
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                now=now,
            )
            conn.execute(
                "UPDATE tasks SET process_id = ?, updated_at = ? WHERE task_id = ?",
                (process_id, timestamp, task_id),
            )
            return self._record(self._get_row(conn, task_id))

    def record_verification(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        evidence: Mapping[str, Any],
        now: datetime | None = None,
    ):
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            self._assert_active_lease(
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                states={TaskState.VERIFYING.value},
                now=now,
            )
            conn.execute(
                "UPDATE tasks SET verification_json = ?, updated_at = ? WHERE task_id = ?",
                (self._json(dict(evidence)), timestamp, task_id),
            )
            self._append_event(
                conn,
                task_id,
                EventType.VERIFYING,
                worker_id,
                timestamp,
                TaskState.VERIFYING.value,
                TaskState.VERIFYING.value,
                {"verification_recorded": True},
            )
            return self._record(self._get_row(conn, task_id))

    def record_commit(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        commit_sha: str,
        now: datetime | None = None,
    ):
        return self._record_delivery_value(
            task_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            column="commit_sha",
            value=commit_sha,
            event=EventType.COMMITTED,
            now=now,
        )

    def record_push(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        remote_sha: str,
        now: datetime | None = None,
    ):
        return self._record_delivery_value(
            task_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            column="remote_sha",
            value=remote_sha,
            event=EventType.PUSHED,
            now=now,
        )

    def record_pr(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        number: int,
        url: str,
        evidence: Mapping[str, Any],
        reused: bool,
        now: datetime | None = None,
    ):
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            self._assert_active_lease(
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                states={TaskState.VERIFYING.value},
                now=now,
            )
            conn.execute(
                """UPDATE tasks SET pr_number = ?, pr_url = ?, delivery_json = ?, updated_at = ?
                   WHERE task_id = ?""",
                (number, url, self._json(dict(evidence)), timestamp, task_id),
            )
            self._append_event(
                conn,
                task_id,
                EventType.PR_REUSED if reused else EventType.PR_CREATED,
                worker_id,
                timestamp,
                TaskState.VERIFYING.value,
                TaskState.VERIFYING.value,
                {"pr_number": number, "pr_url": url},
            )
            return self._record(self._get_row(conn, task_id))

    def _record_delivery_value(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        column: str,
        value: Any,
        event: EventType,
        now: datetime | None,
    ):
        if column not in {"commit_sha", "remote_sha"}:
            raise InvalidTransitionError("unsupported delivery evidence column")
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            self._assert_active_lease(
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                states={TaskState.VERIFYING.value},
                now=now,
            )
            conn.execute(
                f"UPDATE tasks SET {column} = ?, updated_at = ? WHERE task_id = ?",
                (value, timestamp, task_id),
            )
            self._append_event(
                conn,
                task_id,
                event,
                worker_id,
                timestamp,
                TaskState.VERIFYING.value,
                TaskState.VERIFYING.value,
                {column: value},
            )
            return self._record(self._get_row(conn, task_id))

    def block_delivery(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        reason: str,
        failure_class: str,
        evidence: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ):
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            self._assert_active_lease(
                row,
                worker_id=worker_id,
                lease_generation=lease_generation,
                states={TaskState.VERIFYING.value},
                now=now,
            )
            existing_delivery = (
                self._json(dict(evidence or {}))
                if evidence
                else row["delivery_json"] or self._json({})
            )
            has_pr = bool(row["pr_number"] and row["pr_url"])
            new_state = TaskState.PR_READY if has_pr else TaskState.BLOCKED
            event = EventType.PR_READY if has_pr else EventType.DELIVERY_BLOCKED
            conn.execute(
                """UPDATE tasks SET state = ?, last_error = ?, failure_class = ?,
                   delivery_json = ?, lease_owner = NULL, lease_expires_at = NULL,
                   process_id = NULL, updated_at = ? WHERE task_id = ?""",
                (
                    new_state.value,
                    f"DELIVERY_BLOCKED: {reason}"[:4000],
                    failure_class,
                    existing_delivery,
                    timestamp,
                    task_id,
                ),
            )
            self._append_event(
                conn,
                task_id,
                event,
                worker_id,
                timestamp,
                TaskState.VERIFYING.value,
                new_state.value,
                {
                    "failure_class": failure_class,
                    "delivery_blocked": True,
                    "preserved": bool(row["commit_sha"] or row["remote_sha"] or has_pr),
                },
            )
            return self._record(self._get_row(conn, task_id))

    def start_delivery_reconciliation(
        self,
        task_id: str,
        *,
        actor: str,
        evidence: Mapping[str, Any],
        lease_generation: int | None = None,
        max_attempts: int = 2,
        now: datetime | None = None,
    ):
        """Enter the bounded, durable base-drift recovery state.

        A task already in ``DELIVERY_RECONCILING`` is returned unchanged so a
        restart or duplicate dispatcher invocation cannot create a second
        recovery branch or consume another reconciliation attempt.
        """

        if not isinstance(actor, str) or not actor.strip():
            raise SafetyViolation("reconciliation actor is required")
        if not 1 <= max_attempts <= 2:
            raise SafetyViolation("delivery reconciliation is bounded to two attempts")
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            current = TaskState(row["state"])
            if current is TaskState.DELIVERY_RECONCILING:
                return self._record(row)
            if current not in {
                TaskState.VERIFYING,
                TaskState.BLOCKED,
                TaskState.FAILED_SAFE,
            }:
                raise InvalidTransitionError(
                    f"{current.value} cannot enter delivery reconciliation"
                )
            if current is TaskState.FAILED_SAFE and not (
                str(row["failure_class"] or "").upper().startswith("DELIVERY")
            ):
                raise SafetyViolation(
                    "non-delivery FAILED_SAFE work cannot enter reconciliation"
                )
            if current is TaskState.VERIFYING:
                self._assert_active_lease(
                    row,
                    worker_id=actor,
                    lease_generation=lease_generation,
                    states={TaskState.VERIFYING.value},
                    now=now,
                )
            elif row["lease_owner"] is not None:
                self._assert_active_lease(
                    row,
                    worker_id=actor,
                    lease_generation=lease_generation,
                    states={current.value},
                    now=now,
                )
            prior = {}
            if row["reconciliation_json"]:
                try:
                    loaded = json.loads(row["reconciliation_json"])
                    if isinstance(loaded, dict):
                        prior = loaded
                except (TypeError, ValueError, json.JSONDecodeError):
                    prior = {}
            attempts = int(prior.get("attempts", 0) or 0) + 1
            if attempts > max_attempts:
                raise SafetyViolation("delivery reconciliation attempt limit exhausted")
            details = dict(prior)
            details.update(dict(evidence))
            details.update(
                {
                    "attempts": attempts,
                    "max_attempts": max_attempts,
                    "source_state": current.value,
                    "original_branch": details.get("original_branch", row["branch"]),
                    "started_at": details.get("started_at", timestamp),
                }
            )
            conn.execute(
                """UPDATE tasks SET state = ?, reconciliation_json = ?,
                   last_error = ?, updated_at = ? WHERE task_id = ?""",
                (
                    TaskState.DELIVERY_RECONCILING.value,
                    self._json(details),
                    "DELIVERY_RECONCILING: authoritative base drift is being recovered",
                    timestamp,
                    task_id,
                ),
            )
            self._append_event(
                conn,
                task_id,
                EventType.DELIVERY_BASE_DRIFT_DETECTED,
                actor,
                timestamp,
                current.value,
                current.value,
                {
                    "original_base_sha": details.get("original_base_sha"),
                    "authoritative_base_sha": details.get("authoritative_base_sha"),
                    "classification": details.get("classification"),
                    "attempt": attempts,
                },
            )
            self._append_event(
                conn,
                task_id,
                EventType.DELIVERY_RECONCILIATION_STARTED,
                actor,
                timestamp,
                current.value,
                TaskState.DELIVERY_RECONCILING.value,
                {
                    "attempt": attempts,
                    "max_attempts": max_attempts,
                    "recovery_branch": details.get("recovery_branch"),
                },
            )
            return self._record(self._get_row(conn, task_id))

    def finish_delivery_reconciliation(
        self,
        task_id: str,
        *,
        actor: str,
        success: bool,
        evidence: Mapping[str, Any],
        lease_generation: int | None = None,
        now: datetime | None = None,
    ):
        """Atomically persist recovery evidence and its terminal delivery state."""

        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            current = TaskState(row["state"])
            if current is not TaskState.DELIVERY_RECONCILING:
                if success and current is TaskState.PR_READY:
                    return self._record(row)
                raise InvalidTransitionError(
                    f"{current.value} is not in delivery reconciliation"
                )
            if row["lease_owner"] is not None:
                self._assert_active_lease(
                    row,
                    worker_id=actor,
                    lease_generation=lease_generation,
                    states={TaskState.DELIVERY_RECONCILING.value},
                    now=now,
                )
            reconciliation = {}
            if row["reconciliation_json"]:
                try:
                    loaded = json.loads(row["reconciliation_json"])
                    if isinstance(loaded, dict):
                        reconciliation = loaded
                except (TypeError, ValueError, json.JSONDecodeError):
                    reconciliation = {}
            reconciliation.update(dict(evidence))
            reconciliation["finished_at"] = timestamp
            if success:
                required = ("commit_sha", "remote_sha", "pr_number", "pr_url")
                if any(not reconciliation.get(key) for key in required):
                    raise SafetyViolation(
                        "successful reconciliation requires commit, remote, and PR evidence"
                    )
                verification = reconciliation.get("verification") or {}
                if not isinstance(verification, Mapping) or verification.get("passed") is not True:
                    raise SafetyViolation(
                        "successful reconciliation requires passing verification evidence"
                    )
                delivery = reconciliation.get("delivery") or {}
                if not isinstance(delivery, Mapping):
                    delivery = {}
                delivery = dict(delivery)
                delivery.update(
                    {
                        "delivery_recovered": True,
                        "original_commit_sha": reconciliation.get("original_commit_sha"),
                        "recovery_commit_sha": reconciliation.get("commit_sha"),
                        "recovery_branch": reconciliation.get("recovery_branch"),
                        "recovery_base_sha": reconciliation.get("authoritative_base_sha"),
                    }
                )
                conn.execute(
                    """UPDATE tasks SET state = ?, branch = ?, commit_sha = ?, remote_sha = ?,
                       pr_number = ?, pr_url = ?, base_sha = ?, origin_sha = ?,
                       verification_json = ?, delivery_json = ?, reconciliation_json = ?,
                       last_error = NULL, failure_class = NULL, lease_owner = NULL,
                       lease_expires_at = NULL, process_id = NULL, updated_at = ?
                       WHERE task_id = ?""",
                    (
                        TaskState.PR_READY.value,
                        reconciliation.get("recovery_branch") or row["branch"],
                        reconciliation["commit_sha"],
                        reconciliation["remote_sha"],
                        reconciliation["pr_number"],
                        reconciliation["pr_url"],
                        reconciliation.get("authoritative_base_sha"),
                        reconciliation.get("authoritative_base_sha"),
                        self._json(dict(verification)),
                        self._json(delivery),
                        self._json(reconciliation),
                        timestamp,
                        task_id,
                    ),
                )
                self._append_event(
                    conn,
                    task_id,
                    EventType.DELIVERY_RECONCILIATION_VERIFIED,
                    actor,
                    timestamp,
                    current.value,
                    current.value,
                    {
                        "attempt": reconciliation.get("attempts"),
                        "recovery_commit_sha": reconciliation.get("commit_sha"),
                        "verification_passed": True,
                    },
                )
                self._append_event(
                    conn,
                    task_id,
                    EventType.DELIVERY_RECOVERED,
                    actor,
                    timestamp,
                    current.value,
                    TaskState.PR_READY.value,
                    {
                        "pr_number": reconciliation["pr_number"],
                        "recovery_branch": reconciliation.get("recovery_branch"),
                        "original_commit_sha": reconciliation.get("original_commit_sha"),
                        "recovery_commit_sha": reconciliation.get("commit_sha"),
                    },
                )
            else:
                source = str(reconciliation.get("source_state", TaskState.BLOCKED.value))
                final_state = (
                    TaskState.FAILED_SAFE
                    if source == TaskState.FAILED_SAFE.value
                    else TaskState.BLOCKED
                )
                reason = str(
                    reconciliation.get("failure_reason")
                    or "delivery reconciliation failed"
                )[:4000]
                conn.execute(
                    """UPDATE tasks SET state = ?, reconciliation_json = ?,
                       last_error = ?, failure_class = ?, lease_owner = NULL,
                       lease_expires_at = NULL, process_id = NULL, updated_at = ?
                       WHERE task_id = ?""",
                    (
                        final_state.value,
                        self._json(reconciliation),
                        f"DELIVERY_RECONCILIATION_FAILED: {reason}",
                        "DELIVERY_RECONCILIATION_FAILED",
                        timestamp,
                        task_id,
                    ),
                )
                self._append_event(
                    conn,
                    task_id,
                    EventType.DELIVERY_RECONCILIATION_FAILED,
                    actor,
                    timestamp,
                    current.value,
                    final_state.value,
                    {
                        "attempt": reconciliation.get("attempts"),
                        "max_attempts": reconciliation.get("max_attempts", 2),
                        "reason": reason,
                        "classification": reconciliation.get("classification"),
                    },
                )
            return self._record(self._get_row(conn, task_id))

    def update_delivery_reconciliation(
        self,
        task_id: str,
        *,
        actor: str,
        evidence: Mapping[str, Any],
        lease_generation: int | None = None,
        now: datetime | None = None,
    ):
        """Persist idempotent recovery progress without changing task state."""

        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            if TaskState(row["state"]) is not TaskState.DELIVERY_RECONCILING:
                raise InvalidTransitionError(
                    "reconciliation progress requires DELIVERY_RECONCILING"
                )
            if row["lease_owner"] is not None:
                self._assert_active_lease(
                    row,
                    worker_id=actor,
                    lease_generation=lease_generation,
                    states={TaskState.DELIVERY_RECONCILING.value},
                    now=now,
                )
            current = {}
            if row["reconciliation_json"]:
                try:
                    loaded = json.loads(row["reconciliation_json"])
                    if isinstance(loaded, dict):
                        current = loaded
                except (TypeError, ValueError, json.JSONDecodeError):
                    current = {}
            current.update(dict(evidence))
            conn.execute(
                "UPDATE tasks SET reconciliation_json = ?, updated_at = ? WHERE task_id = ?",
                (self._json(current), timestamp, task_id),
            )
            return self._record(self._get_row(conn, task_id))

    def retry_delivery_reconciliation(
        self,
        task_id: str,
        *,
        actor: str,
        evidence: Mapping[str, Any],
        lease_generation: int | None = None,
        max_attempts: int = 2,
        now: datetime | None = None,
    ):
        """Advance one bounded attempt while retaining an active worker lease."""

        if not 1 <= max_attempts <= 2:
            raise SafetyViolation("delivery reconciliation is bounded to two attempts")
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            if TaskState(row["state"]) is not TaskState.DELIVERY_RECONCILING:
                raise InvalidTransitionError(
                    "only DELIVERY_RECONCILING work can start another attempt"
                )
            if row["lease_owner"] is not None:
                self._assert_active_lease(
                    row,
                    worker_id=actor,
                    lease_generation=lease_generation,
                    states={TaskState.DELIVERY_RECONCILING.value},
                    now=now,
                )
            current = {}
            if row["reconciliation_json"]:
                try:
                    loaded = json.loads(row["reconciliation_json"])
                    if isinstance(loaded, dict):
                        current = loaded
                except (TypeError, ValueError, json.JSONDecodeError):
                    current = {}
            attempts = int(current.get("attempts", 0) or 0) + 1
            if attempts > max_attempts:
                raise SafetyViolation("delivery reconciliation attempt limit exhausted")
            current.update(dict(evidence))
            current.update({"attempts": attempts, "max_attempts": max_attempts})
            conn.execute(
                "UPDATE tasks SET reconciliation_json = ?, updated_at = ? WHERE task_id = ?",
                (self._json(current), timestamp, task_id),
            )
            self._append_event(
                conn,
                task_id,
                EventType.DELIVERY_BASE_DRIFT_DETECTED,
                actor,
                timestamp,
                TaskState.DELIVERY_RECONCILING.value,
                TaskState.DELIVERY_RECONCILING.value,
                {
                    "attempt": attempts,
                    "original_base_sha": current.get("original_base_sha"),
                    "authoritative_base_sha": current.get("authoritative_base_sha"),
                    "classification": current.get("classification"),
                },
            )
            self._append_event(
                conn,
                task_id,
                EventType.DELIVERY_RECONCILIATION_STARTED,
                actor,
                timestamp,
                TaskState.DELIVERY_RECONCILING.value,
                TaskState.DELIVERY_RECONCILING.value,
                {"attempt": attempts, "max_attempts": max_attempts},
            )
            return self._record(self._get_row(conn, task_id))

    def fail_safe(
        self,
        task_id: str,
        *,
        worker_id: str,
        summary: str,
        failure_class: str = "FAILED_SAFE",
        lease_generation: int | None = None,
        now: datetime | None = None,
    ):
        execution = ExecutionResult(
            False,
            summary=summary[:4000],
            data={"failure_class": failure_class},
            retryable=False,
            terminal_state=TaskState.FAILED_SAFE,
        )
        self.complete(
            task_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            execution=execution,
            now=now,
        )
        return self.get(task_id)

    def reconcile_delivery(
        self,
        task_id: str,
        *,
        actor: str,
        evidence: Mapping[str, Any],
        now: datetime | None = None,
    ):
        """Expose preserved implementation work after explicit verification.

        This is deliberately not the generic transition API: a historical
        FAILED_SAFE row can only leave that state through this evidence-gated
        recovery path, and it can only become PR_READY, never COMPLETED.
        """

        record = self.get(task_id)
        if record.state not in {TaskState.BLOCKED, TaskState.FAILED_SAFE}:
            raise SafetyViolation("task is not a delivery-blocked recovery candidate")
        failure = (record.failure_class or "").upper()
        if not record.delivery_blocked or (
            record.state is TaskState.FAILED_SAFE
            and not failure.startswith("DELIVERY")
        ):
            raise SafetyViolation("task is not marked as delivery-blocked")
        if record.state is TaskState.FAILED_SAFE and (
            not failure.startswith("DELIVERY")
            or "TIMEOUT" in failure
            or "DEAD_LETTER" in failure
        ):
            raise SafetyViolation("timeout/dead-letter work cannot be reconciled as delivery")
        if not isinstance(evidence, Mapping) or evidence.get("verified") is not True:
            raise SafetyViolation("delivery recovery requires verified GitHub facts")
        if evidence.get("worker_execution_success") is not True or evidence.get(
            "implementation_success"
        ) is not True:
            raise SafetyViolation("delivery recovery requires successful implementation evidence")
        commit_sha = evidence.get("commit_sha") or record.commit_sha
        remote_sha = evidence.get("remote_sha") or record.remote_sha or commit_sha
        pr_number = evidence.get("pr_number") or record.pr_number
        pr_url = evidence.get("pr_url") or record.pr_url
        if not isinstance(commit_sha, str) or not commit_sha.strip():
            raise SafetyViolation("delivery recovery is missing the preserved commit SHA")
        if not isinstance(remote_sha, str) or not remote_sha.strip():
            raise SafetyViolation("delivery recovery is missing the pushed remote SHA")
        if (
            isinstance(record.commit_sha, str)
            and record.commit_sha
            and record.commit_sha.lower() != commit_sha.lower()
        ):
            raise SafetyViolation("recovery commit SHA does not match the task")
        if (
            isinstance(record.remote_sha, str)
            and record.remote_sha
            and record.remote_sha.lower() != remote_sha.lower()
        ):
            raise SafetyViolation("recovery remote SHA does not match the task")
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
            raise SafetyViolation("delivery recovery requires a positive PR number")
        if not isinstance(pr_url, str) or not pr_url.strip():
            pr_url = f"https://github.com/{record.repo}/pull/{pr_number}"
        verification = evidence.get("verification_json") or record.verification
        if not isinstance(verification, Mapping) or not verification:
            raise SafetyViolation("delivery recovery requires preserved verification evidence")
        delivery = dict(record.delivery or {})
        delivery.update(
            {
                "delivery_blocked": True,
                "recovery_verified": True,
                "recovery_evidence": {
                    key: value
                    for key, value in evidence.items()
                    if key
                    in {
                        "repo",
                        "pr_number",
                        "head_ref",
                        "base_ref",
                        "base_sha",
                        "state",
                        "merged",
                    }
                },
            }
        )
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            actual = TaskState(row["state"])
            if actual is not record.state:
                raise InvalidTransitionError(
                    f"task {task_id} is {actual.value}, expected {record.state.value}"
                )
            conn.execute(
                """UPDATE tasks SET state = ?, commit_sha = ?, remote_sha = ?,
                   pr_number = ?, pr_url = ?, verification_json = ?, delivery_json = ?,
                   last_error = ?, failure_class = ?, lease_owner = NULL,
                   lease_expires_at = NULL, process_id = NULL, updated_at = ?
                   WHERE task_id = ?""",
                (
                    TaskState.PR_READY.value,
                    commit_sha,
                    remote_sha,
                    pr_number,
                    pr_url,
                    self._json(dict(verification)),
                    self._json(delivery),
                    "DELIVERY_BLOCKED: preserved work reconciled; awaiting merge verification",
                    "DELIVERY_BLOCKED",
                    timestamp,
                    task_id,
                ),
            )
            self._append_event(
                conn,
                task_id,
                EventType.DELIVERY_RECOVERED,
                actor,
                timestamp,
                record.state.value,
                TaskState.PR_READY.value,
                {
                    "pr_number": pr_number,
                    "commit_sha": commit_sha,
                    "remote_sha": remote_sha,
                    "verified": True,
                },
            )
            return self._record(self._get_row(conn, task_id))

    def mark_ceo_review(
        self,
        task_id: str,
        *,
        actor: str,
        evidence: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ):
        record = self.get(task_id)
        return self.transition(
            task_id,
            expected=record.state,
            new_state=TaskState.CEO_REVIEW,
            actor=actor,
            event_type=EventType.CEO_REVIEW,
            details={
                "source_state": record.state.value,
                "evidence": dict(evidence or {}),
            },
            now=now,
        )

    def mark_merge_verified(
        self,
        task_id: str,
        *,
        actor: str,
        evidence: Mapping[str, Any],
        now: datetime | None = None,
    ):
        """Complete PR-backed work only after structured merge evidence."""

        record = self.get(task_id)
        if record.state not in {TaskState.PR_READY, TaskState.CEO_REVIEW}:
            raise InvalidTransitionError(
                f"task {task_id} is not awaiting merge verification"
            )
        required = {
            "source": "github-readonly",
            "repository": record.repo,
            "merged": True,
            "pr_number": record.pr_number,
            "head_ref_name": record.branch,
            "head_ref_oid": record.commit_sha,
            "base_ref_name": record.base_branch,
            "base_ref_oid": record.base_sha,
        }
        if any(
            required[key] is None
            for key in (
                "repository",
                "pr_number",
                "head_ref_name",
                "head_ref_oid",
                "base_ref_name",
                "base_ref_oid",
            )
        ):
            raise SafetyViolation("task is missing complete merge binding evidence")
        if any(evidence.get(key) != value for key, value in required.items()):
            raise SafetyViolation("merge evidence does not match the task binding")
        if (
            not record.remote_sha
            or not isinstance(evidence.get("head_ref_oid"), str)
            or record.remote_sha.lower() != evidence["head_ref_oid"].lower()
        ):
            raise SafetyViolation("merge evidence does not match the remote SHA")
        if record.origin_sha and (
            not isinstance(evidence.get("base_ref_oid"), str)
            or record.origin_sha.lower() != evidence["base_ref_oid"].lower()
        ):
            raise SafetyViolation("merge evidence does not match the expected base SHA")
        merged_at = evidence.get("merged_at")
        if not isinstance(merged_at, str) or not merged_at.strip():
            raise SafetyViolation("merge evidence is missing merged_at")
        delivery = dict(record.delivery or {})
        delivery["merge_verification"] = dict(evidence)
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            actual = TaskState(row["state"])
            if actual is not record.state:
                raise InvalidTransitionError(
                    f"task {task_id} is {actual.value}, expected {record.state.value}"
                )
            conn.execute(
                """UPDATE tasks SET state = ?, delivery_json = ?, last_error = NULL,
                   failure_class = NULL, updated_at = ? WHERE task_id = ?""",
                (
                    TaskState.COMPLETED.value,
                    self._json(delivery),
                    timestamp,
                    task_id,
                ),
            )
            self._append_event(
                conn,
                task_id,
                EventType.MERGE_VERIFIED,
                actor,
                timestamp,
                record.state.value,
                TaskState.COMPLETED.value,
                {"merge_verification": dict(evidence)},
            )
            return self._record(self._get_row(conn, task_id))

    def approve(
        self, task_id: str, *, actor: str, now: datetime | None = None, reason: str = ""
    ):
        return self.transition(
            task_id,
            expected=TaskState.PENDING_APPROVAL,
            new_state=TaskState.QUEUED,
            actor=actor,
            event_type=EventType.APPROVED,
            details={"reason": reason},
            now=now,
        )

    def reject(
        self, task_id: str, *, actor: str, now: datetime | None = None, reason: str = ""
    ):
        return self.transition(
            task_id,
            expected=TaskState.PENDING_APPROVAL,
            new_state=TaskState.FAILED,
            actor=actor,
            event_type=EventType.REJECTED,
            details={"reason": reason},
            now=now,
            updates={"last_error": reason or "approval rejected"},
        )
