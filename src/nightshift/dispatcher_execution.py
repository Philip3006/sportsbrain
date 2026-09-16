"""Worker-facing execution operations for the Builder 5 dispatcher."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from .dispatcher_helpers import claimed_to_spec, record_to_spec
from .errors import (
    DispatcherRecursionError,
    InvalidTaskError,
    SafetyViolation,
    ScopeViolation,
)
from .models import ExecutionResult, TaskRecord, TaskState


class DispatcherExecutionMixin:
    """Claims, executes, recovers, and advances worker tasks safely."""

    def claim_next(
        self, builder_id: str, *, worker_instance_id: str | None = None
    ) -> TaskRecord | None:
        """Lease the highest-priority ready task for one explicit Builder."""

        if builder_id == self.dispatcher_id:
            raise DispatcherRecursionError(
                "Builder 5 is the dispatcher and cannot claim worker tasks"
            )
        definition = self.registry.assert_worker_target(builder_id)
        self.policy.validate_worker_id(definition.builder_id, self.dispatcher_id)
        if self.store.is_paused():
            return None
        worker_id = worker_instance_id or definition.builder_id
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise SafetyViolation("worker_instance_id must be a non-empty string")
        if worker_id == self.dispatcher_id or worker_id.startswith(
            f"{self.dispatcher_id}:"
        ):
            raise DispatcherRecursionError(
                "Builder 5 identity cannot be used as a worker instance"
            )
        self.policy.validate_worker_id(worker_id, self.dispatcher_id)
        if worker_id != definition.builder_id and not worker_id.startswith(
            f"{definition.builder_id}:"
        ):
            raise SafetyViolation(
                "worker_instance_id must be scoped to its registered Builder"
            )
        claimed = self.store.claim_next(
            definition.builder_id,
            worker_id=worker_id,
            lease_seconds=self.lease_seconds,
            max_concurrency=definition.max_concurrency,
            now=self.clock(),
        )
        if claimed is None or self.worktree_manager is None or claimed.worktree_path:
            return claimed
        try:
            allocation = self.worktree_manager.allocate(claimed_to_spec(claimed))
            return self.store.assign_worktree(
                claimed.task_id,
                worker_id=worker_id,
                worktree_path=str(allocation.path),
                diagnostic_path=str(allocation.diagnostic_path),
                now=self.clock(),
            )
        except Exception as exc:  # noqa: BLE001 - allocation failure is a safe terminal outcome
            return self.store.fail_safe(
                claimed.task_id,
                worker_id=worker_id,
                summary=f"worktree allocation failed: {type(exc).__name__}: {str(exc)[:3800]}",
            )

    def heartbeat(self, task_id: str, *, worker_instance_id: str) -> TaskRecord:
        if not isinstance(worker_instance_id, str) or not worker_instance_id.strip():
            raise SafetyViolation("worker_instance_id must be a non-empty string")
        if worker_instance_id == self.dispatcher_id or worker_instance_id.startswith(
            f"{self.dispatcher_id}:"
        ):
            raise DispatcherRecursionError(
                "Builder 5 identity cannot heartbeat worker tasks"
            )
        self.policy.validate_worker_id(worker_instance_id, self.dispatcher_id)
        return self.store.heartbeat(
            task_id,
            worker_id=worker_instance_id,
            lease_seconds=self.lease_seconds,
            now=self.clock(),
        )

    def complete(
        self,
        task_id: str,
        *,
        worker_instance_id: str,
        execution: ExecutionResult | Mapping[str, Any],
    ) -> TaskRecord:
        if not isinstance(worker_instance_id, str) or not worker_instance_id.strip():
            raise SafetyViolation("worker_instance_id must be a non-empty string")
        if worker_instance_id == self.dispatcher_id or worker_instance_id.startswith(
            f"{self.dispatcher_id}:"
        ):
            raise DispatcherRecursionError(
                "Builder 5 identity cannot complete worker tasks"
            )
        self.policy.validate_worker_id(worker_instance_id, self.dispatcher_id)
        normalized = self._normalize_execution(execution)
        record = self.store.get(task_id)
        retry_at = self.clock() + timedelta(
            seconds=self._retry_delay(record.attempt_count)
        )
        return self.store.complete(
            task_id,
            worker_id=worker_instance_id,
            execution=normalized,
            retry_at=retry_at,
            now=self.clock(),
        )

    def run_once(
        self,
        builder_id: str,
        executor: Callable[[TaskRecord], ExecutionResult | Mapping[str, Any]],
        *,
        worker_instance_id: str | None = None,
    ) -> TaskRecord | None:
        """Run one claimed task through an injected, non-shell worker adapter."""

        record = self.claim_next(builder_id, worker_instance_id=worker_instance_id)
        if record is None:
            return None
        owner = record.lease_owner
        if owner is None and record.state is TaskState.FAILED_SAFE:
            return record
        if owner is None:  # pragma: no cover - claim always returns an active lease
            raise SafetyViolation("claimed task has no lease owner")
        try:
            execution = self._normalize_execution(executor(record))
            if self.worktree_manager is not None and record.worktree_path:
                changed = self.worktree_manager.verify_scope(
                    record_to_spec(record), Path(record.worktree_path)
                )
                if changed and execution.success:
                    execution = replace(
                        execution,
                        data={**dict(execution.data), "changed_paths": list(changed)},
                    )
        except Exception as exc:  # noqa: BLE001 - worker failures become auditable task failures
            if isinstance(exc, ScopeViolation):
                execution = ExecutionResult(
                    False,
                    f"scope violation: {str(exc)[:3800]}",
                    retryable=False,
                    terminal_state=TaskState.FAILED_SAFE,
                )
            else:
                execution = ExecutionResult(
                    success=False,
                    summary=f"{type(exc).__name__}: {str(exc)[:3900]}",
                    retryable=True,
                )
        return self.complete(
            record.task_id, worker_instance_id=owner, execution=execution
        )

    def run_until_idle(
        self,
        builder_id: str,
        executor: Callable[[TaskRecord], ExecutionResult | Mapping[str, Any]],
        *,
        max_tasks: int = 100,
    ) -> list[TaskRecord]:
        if not 1 <= max_tasks <= 1000:
            raise ValueError("max_tasks must be between 1 and 1000")
        completed: list[TaskRecord] = []
        for _ in range(max_tasks):
            result = self.run_once(builder_id, executor)
            if result is None:
                break
            completed.append(result)
        return completed

    def recover_expired(self) -> list[TaskRecord]:
        return self.store.recover_expired(now=self.clock(), actor=self.dispatcher_id)

    def mark_ceo_review(
        self, task_id: str, *, actor: str, evidence: Mapping[str, Any] | None = None
    ) -> TaskRecord:
        self._require_actor(actor)
        record = self.store.get(task_id)
        if record.state not in {TaskState.SUCCEEDED, TaskState.PR_READY}:
            raise SafetyViolation("only verified successful work can enter CEO_REVIEW")
        if evidence is not None and not isinstance(evidence, Mapping):
            raise InvalidTaskError("CEO_REVIEW evidence must be an object")
        return self.store.mark_ceo_review(
            task_id, actor=actor, evidence=dict(evidence or {}), now=self.clock()
        )

    def _retry_delay(self, attempt_count: int) -> int:
        if self.retry_base_seconds == 0:
            return 0
        return min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** max(0, attempt_count - 1)),
        )

    @staticmethod
    def _normalize_execution(
        value: ExecutionResult | Mapping[str, Any],
    ) -> ExecutionResult:
        if isinstance(value, ExecutionResult):
            return value
        if not isinstance(value, Mapping) or not isinstance(value.get("success"), bool):
            raise InvalidTaskError(
                "worker adapter must return ExecutionResult or {success: bool, ...}"
            )
        return ExecutionResult(
            success=value["success"],
            summary=value.get("summary", ""),
            data=value.get("data", {}),
            retryable=value.get("retryable", True),
        )
