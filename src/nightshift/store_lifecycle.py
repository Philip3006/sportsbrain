"""Lifecycle operations for leased and approval-gated dispatcher tasks."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .errors import LeaseError
from .models import EventType, ExecutionResult, TaskState, isoformat, utc_now


class StoreLifecycleMixin:
    """State transitions that attach worktree and review lifecycle evidence."""

    def assign_worktree(
        self,
        task_id: str,
        *,
        worker_id: str,
        worktree_path: str,
        diagnostic_path: str,
        now: datetime | None = None,
    ):
        timestamp = isoformat(now or utc_now())
        with self._write() as conn:
            row = self._get_row(conn, task_id)
            if (
                row["state"] != TaskState.LEASED.value
                or row["lease_owner"] != worker_id
            ):
                raise LeaseError("worktree assignment requires the active lease owner")
            conn.execute(
                "UPDATE tasks SET worktree_path = ?, diagnostic_path = ?, updated_at = ? WHERE task_id = ?",
                (worktree_path, diagnostic_path, timestamp, task_id),
            )
            self._append_event(
                conn,
                task_id,
                EventType.WORKTREE_ALLOCATED,
                worker_id,
                timestamp,
                TaskState.LEASED.value,
                TaskState.LEASED.value,
                {"worktree_path": worktree_path},
            )
            return self._record(self._get_row(conn, task_id))

    def fail_safe(
        self,
        task_id: str,
        *,
        worker_id: str,
        summary: str,
        failure_class: str = "FAILED_SAFE",
        now: datetime | None = None,
    ):
        execution = ExecutionResult(
            False,
            summary=summary[:4000],
            data={"failure_class": failure_class},
            retryable=False,
            terminal_state=TaskState.FAILED_SAFE,
        )
        self.complete(task_id, worker_id=worker_id, execution=execution, now=now)
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
