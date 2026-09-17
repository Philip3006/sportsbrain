"""Lifecycle operations for leased and approval-gated dispatcher tasks."""

from __future__ import annotations

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
            conn.execute(
                """UPDATE tasks SET state = ?, last_error = ?, failure_class = ?,
                   delivery_json = ?, lease_owner = NULL, lease_expires_at = NULL,
                   updated_at = ? WHERE task_id = ?""",
                (
                    TaskState.BLOCKED.value,
                    reason[:4000],
                    failure_class,
                    self._json(dict(evidence or {})),
                    timestamp,
                    task_id,
                ),
            )
            self._append_event(
                conn,
                task_id,
                EventType.DELIVERY_BLOCKED,
                worker_id,
                timestamp,
                TaskState.VERIFYING.value,
                TaskState.BLOCKED.value,
                {"failure_class": failure_class},
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
