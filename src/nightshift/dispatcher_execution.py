"""Worker-facing execution and delivery operations for Builder 5."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from .delivery import DeliveryBlocked, DeliveryError
from .dispatcher_helpers import claimed_to_spec
from .errors import (
    DispatcherRecursionError,
    ExecutorUnavailable,
    InvalidTaskError,
    LeaseError,
    SafetyViolation,
    ScopeViolation,
)
from .models import ExecutionResult, TaskRecord, TaskState


class _LeaseWatchdog:
    """Renew and independently fence a lease for the whole delivery phase."""

    def __init__(
        self,
        *,
        heartbeat: Callable[[], None],
        fence: Callable[[], None],
        interval_seconds: int,
    ) -> None:
        self._heartbeat = heartbeat
        self._fence = fence
        self._interval_seconds = max(1, interval_seconds)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._failure: Exception | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        try:
            self._heartbeat()
        except Exception as exc:  # noqa: BLE001 - fencing is fail-closed
            self._set_failure(exc)
            return
        self._thread = threading.Thread(
            target=self._run,
            name="nightshift-delivery-watchdog",
            daemon=True,
        )
        self._thread.start()

    def check(self) -> None:
        failure = self._failure_snapshot()
        if failure is not None:
            raise LeaseError("delivery lease watchdog fenced the worker") from failure
        try:
            self._fence()
        except Exception as exc:
            self._set_failure(exc)
            raise LeaseError("delivery lease is stale or expired") from exc

    def raise_if_failed(self) -> None:
        failure = self._failure_snapshot()
        if failure is not None:
            raise LeaseError("delivery lease watchdog fenced the worker") from failure

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(5, max(2, self._interval_seconds + 1)))

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._heartbeat()
            except Exception as exc:  # noqa: BLE001 - stale workers must stop
                self._set_failure(exc)
                return

    def _set_failure(self, failure: Exception) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = failure
        self._stop.set()

    def _failure_snapshot(self) -> Exception | None:
        with self._lock:
            return self._failure


class DispatcherExecutionMixin:
    """Claims, executes, verifies, and delivers worker tasks safely."""

    def claim_next(
        self, builder_id: str, *, worker_instance_id: str | None = None
    ) -> TaskRecord | None:
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
                base_branch=allocation.base_branch,
                base_sha=allocation.base_sha,
                origin_sha=allocation.origin_sha,
                lease_generation=claimed.lease_generation,
                now=self.clock(),
            )
        except Exception as exc:  # noqa: BLE001 - allocation fails closed
            return self.store.fail_safe(
                claimed.task_id,
                worker_id=worker_id,
                lease_generation=claimed.lease_generation,
                failure_class=(
                    "BASE_SHA_MISMATCH"
                    if "expected_base_sha" in str(exc)
                    else "WORKTREE_ALLOCATION_FAILED"
                ),
                summary=f"worktree allocation failed: {type(exc).__name__}: {str(exc)[:3800]}",
            )

    def heartbeat(
        self,
        task_id: str,
        *,
        worker_instance_id: str,
        lease_generation: int | None = None,
    ) -> TaskRecord:
        self._validate_worker_instance(worker_instance_id)
        return self.store.heartbeat(
            task_id,
            worker_id=worker_instance_id,
            lease_generation=lease_generation,
            lease_seconds=self.lease_seconds,
            now=self.clock(),
        )

    def complete(
        self,
        task_id: str,
        *,
        worker_instance_id: str,
        execution: ExecutionResult | Mapping[str, Any],
        lease_generation: int | None = None,
    ) -> TaskRecord:
        self._validate_worker_instance(worker_instance_id)
        normalized = self._normalize_execution(execution)
        record = self.store.get(task_id)
        if (
            normalized.success
            and record.pr_required
            and not (
                normalized.terminal_state is TaskState.PR_READY
                and record.pr_number
                and record.pr_url
            )
        ):
            raise SafetyViolation(
                "code-changing task completion requires the governed delivery pipeline"
            )
        retry_at = self.clock().replace(microsecond=0)
        retry_at = retry_at + timedelta(seconds=self._retry_delay(record.attempt_count))
        return self.store.complete(
            task_id,
            worker_id=worker_instance_id,
            lease_generation=lease_generation,
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
        """Run one task through execution, verification, and safe delivery."""

        record = self.claim_next(builder_id, worker_instance_id=worker_instance_id)
        if record is None:
            return None
        if record.state is TaskState.FAILED_SAFE and not record.lease_owner:
            return record
        owner = record.lease_owner
        generation = record.lease_generation
        if owner is None:
            raise SafetyViolation("claimed task has no lease owner")
        try:
            self.store.start_running(
                record.task_id,
                worker_id=owner,
                lease_generation=generation,
                now=self.clock(),
            )
            execution = self._execute_adapter(executor, record, owner, generation)
            if not execution.success:
                return self.complete(
                    record.task_id,
                    worker_instance_id=owner,
                    lease_generation=generation,
                    execution=execution,
                )
            self.store.begin_verifying(
                record.task_id,
                worker_id=owner,
                lease_generation=generation,
                now=self.clock(),
            )
            watchdog = _LeaseWatchdog(
                heartbeat=lambda: self.heartbeat(
                    record.task_id,
                    worker_instance_id=owner,
                    lease_generation=generation,
                ),
                fence=lambda: self.store.assert_active_lease(
                    record.task_id,
                    worker_id=owner,
                    lease_generation=generation,
                    now=self.clock(),
                ),
                interval_seconds=max(1, self.lease_seconds // 3),
            )
            try:
                watchdog.start()
                watchdog.check()
                current = self.store.get(record.task_id)
                if self.delivery_pipeline is None:
                    if (
                        current.pr_required
                        or current.required_tests
                        or current.verification_commands
                    ):
                        raise DeliveryBlocked("DELIVERY_BLOCKED_PIPELINE_UNAVAILABLE")
                    watchdog.check()
                    self.store.record_verification(
                        current.task_id,
                        worker_id=owner,
                        lease_generation=generation,
                        evidence={
                            "passed": True,
                            "commands": [],
                            "not_required": True,
                        },
                        now=self.clock(),
                    )
                    delivery = {
                        "verification": {"passed": True, "not_required": True}
                    }
                else:
                    delivery = self.delivery_pipeline.deliver(
                        current,
                        worker_id=owner,
                        lease_generation=generation,
                        lease_guard=watchdog.check,
                    )
                watchdog.raise_if_failed()
                terminal = (
                    TaskState.PR_READY if current.pr_required else TaskState.COMPLETED
                )
                watchdog.stop()
                return self.complete(
                    record.task_id,
                    worker_instance_id=owner,
                    lease_generation=generation,
                    execution=ExecutionResult(
                        True,
                        "execution and governed delivery verified",
                        data={**dict(execution.data), **delivery},
                        retryable=False,
                        terminal_state=terminal,
                        process_id=execution.process_id,
                    ),
                )
            finally:
                watchdog.stop()
        except DeliveryBlocked as exc:
            try:
                return self.store.block_delivery(
                    record.task_id,
                    worker_id=owner,
                    lease_generation=generation,
                    reason=str(exc),
                    failure_class=(
                        "DELIVERY_BLOCKED_GITHUB_AUTH"
                        if "GITHUB_AUTH" in str(exc)
                        else "DELIVERY_BLOCKED"
                    ),
                    evidence={"task_id": record.task_id},
                    now=self.clock(),
                )
            except LeaseError:
                return self.store.get(record.task_id)
        except DeliveryError as exc:
            try:
                return self.complete(
                    record.task_id,
                    worker_instance_id=owner,
                    lease_generation=generation,
                    execution=ExecutionResult(
                        False,
                        str(exc)[:4000],
                        data={"failure_class": "DELIVERY_FAILED"},
                        retryable=False,
                        terminal_state=TaskState.FAILED_SAFE,
                    ),
                )
            except LeaseError:
                return self.store.get(record.task_id)
        except LeaseError:
            # A recovery/reclaim may have fenced this worker. It cannot mutate
            # delivery state after losing its generation.
            return self.store.get(record.task_id)
        except Exception as exc:  # noqa: BLE001 - worker errors are bounded retries
            safe = isinstance(
                exc,
                (
                    ExecutorUnavailable,
                    InvalidTaskError,
                    SafetyViolation,
                    ScopeViolation,
                ),
            )
            try:
                return self.complete(
                    record.task_id,
                    worker_instance_id=owner,
                    lease_generation=generation,
                    execution=ExecutionResult(
                        False,
                        f"{type(exc).__name__}: {str(exc)[:3900]}",
                        data={
                            "failure_class": "SCOPE_VIOLATION"
                            if isinstance(exc, ScopeViolation)
                            else "EXECUTOR_UNAVAILABLE"
                            if isinstance(exc, ExecutorUnavailable)
                            else "WORKER_EXCEPTION"
                        },
                        retryable=not safe,
                        terminal_state=TaskState.FAILED_SAFE if safe else None,
                    ),
                )
            except LeaseError:
                return self.store.get(record.task_id)

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
        if record.state not in {TaskState.COMPLETED, TaskState.PR_READY}:
            raise SafetyViolation("only verified delivered work can enter CEO_REVIEW")
        return self.store.mark_ceo_review(
            task_id, actor=actor, evidence=dict(evidence or {}), now=self.clock()
        )

    def _execute_adapter(
        self,
        executor: Any,
        record: TaskRecord,
        owner: str,
        generation: int,
    ) -> ExecutionResult:
        runner = getattr(executor, "run_with_heartbeat", None)
        if runner is None:
            return self._normalize_execution(executor(record))
        return self._normalize_execution(
            runner(
                record,
                heartbeat=lambda: self.heartbeat(
                    record.task_id,
                    worker_instance_id=owner,
                    lease_generation=generation,
                ),
                process_started=lambda pid: self.store.record_process(
                    record.task_id,
                    worker_id=owner,
                    lease_generation=generation,
                    process_id=pid,
                    now=self.clock(),
                ),
                heartbeat_interval_seconds=max(1, self.lease_seconds // 3),
            )
        )

    def _retry_delay(self, attempt_count: int) -> int:
        if self.retry_base_seconds == 0:
            return 0
        return min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** max(0, attempt_count - 1)),
        )

    def _validate_worker_instance(self, worker_instance_id: str) -> None:
        if not isinstance(worker_instance_id, str) or not worker_instance_id.strip():
            raise SafetyViolation("worker_instance_id must be a non-empty string")
        if worker_instance_id == self.dispatcher_id or worker_instance_id.startswith(
            f"{self.dispatcher_id}:"
        ):
            raise DispatcherRecursionError(
                "Builder 5 identity cannot operate worker tasks"
            )
        self.policy.validate_worker_id(worker_instance_id, self.dispatcher_id)

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
