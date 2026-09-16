"""Safe orchestration façade for the SportsBrain Night Shift queue."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .delivery import DeliveryPipeline, GhPullRequestClient
from .dispatcher_execution import DispatcherExecutionMixin
from .errors import (
    IdempotencyConflictError,
    InvalidTaskError,
    SafetyViolation,
    TaskNotFoundError,
)
from .models import (
    DISPATCHER_ID,
    EventType,
    ExecutionResult,
    TaskRecord,
    TaskSpec,
    TaskState,
    utc_now,
)
from .policy import SafetyPolicy
from .registry import BuilderRegistry
from .store import DispatcherStore
from .templates import TemplateRegistry
from .worktree import WorktreeManager, default_repo_paths

WorkerExecutor = Callable[[TaskRecord], ExecutionResult | Mapping[str, Any]]


def _default_config_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "night_shift"


def _default_state_path() -> Path:
    configured = os.getenv("SPORTSBRAIN_NIGHTSHIFT_STATE")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise SafetyViolation(
                "SPORTSBRAIN_NIGHTSHIFT_STATE must be an absolute path"
            )
        return path
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "SportsBrain"
        / "runtime-state"
        / "nightshift.sqlite3"
    )


class NightShiftDispatcher(DispatcherExecutionMixin):
    """Builder 5 dispatcher backed by an explicit registry and durable queue.

    This class coordinates worker adapters supplied by the caller. It never
    discovers, creates, or executes a worker on its own. In particular,
    ``builder-5`` is rejected by every worker-target and claim boundary.
    """

    def __init__(
        self,
        *,
        registry: BuilderRegistry,
        templates: TemplateRegistry,
        store: DispatcherStore,
        policy: SafetyPolicy | None = None,
        dispatcher_id: str = DISPATCHER_ID,
        lease_seconds: int = 15 * 60,
        retry_base_seconds: int = 60,
        retry_max_seconds: int = 60 * 60,
        clock: Callable[[], datetime] = utc_now,
        worktree_manager: WorktreeManager | None = None,
        delivery_pipeline: DeliveryPipeline | None = None,
    ) -> None:
        if dispatcher_id != DISPATCHER_ID:
            raise SafetyViolation("Builder 5 is the only supported dispatcher identity")
        if not 30 <= lease_seconds <= 24 * 60 * 60:
            raise SafetyViolation(
                "lease_seconds must be between 30 seconds and 24 hours"
            )
        if not 0 <= retry_base_seconds <= retry_max_seconds:
            raise SafetyViolation("retry delay bounds are invalid")
        templates.validate_against(registry)
        self.registry = registry
        self.templates = templates
        self.store = store
        self.policy = policy or SafetyPolicy()
        self.dispatcher_id = dispatcher_id
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.clock = clock
        self.worktree_manager = worktree_manager
        self.delivery_pipeline = delivery_pipeline

    @classmethod
    def from_config(
        cls,
        *,
        config_dir: Path | None = None,
        state_path: Path | None = None,
        policy: SafetyPolicy | None = None,
        worktree_manager: WorktreeManager | None = None,
        require_isolated_worktrees: bool = True,
        delivery_pipeline: DeliveryPipeline | None = None,
        **kwargs: Any,
    ) -> NightShiftDispatcher:
        directory = config_dir or _default_config_dir()
        registry = BuilderRegistry.from_file(directory / "builders.json")
        templates = TemplateRegistry.from_file(directory / "templates.json")
        manager = worktree_manager
        if require_isolated_worktrees and manager is None:
            manager = WorktreeManager(
                (state_path or _default_state_path()).parent,
                default_repo_paths(Path(__file__).resolve().parents[2]),
            )
        store = DispatcherStore(state_path or _default_state_path())
        pipeline = delivery_pipeline
        if manager is not None and pipeline is None:
            pipeline = DeliveryPipeline(
                store,
                manager,
                GhPullRequestClient(),
                clock=kwargs.get("clock", utc_now),
            )
        elif pipeline is not None and pipeline.store is None:
            pipeline.store = store
        if pipeline is not None and "clock" in kwargs:
            pipeline.clock = kwargs["clock"]
        return cls(
            registry=registry,
            templates=templates,
            store=store,
            policy=policy,
            worktree_manager=manager,
            delivery_pipeline=pipeline,
            **kwargs,
        )

    def submit(self, task: TaskSpec) -> TaskRecord:
        """Validate and persist a task, applying idempotency when supplied."""

        definition = self.registry.validate_task(task, enforce_risk=False)
        blocked_reason = self.policy.block_reason(task)
        if blocked_reason is None:
            self.policy.validate(task, definition)
        if task.builder_id != definition.builder_id:
            task = replace(task, builder_id=definition.builder_id)
        if task.template_id is not None:
            template = self.templates.resolve(task.template_id)
            if (
                template.builder_id != task.builder_id
                or template.task_type != task.task_type
            ):
                raise InvalidTaskError(
                    "template does not match the explicit task builder/type"
                )
        self._validate_dependencies(task)
        existing = (
            self.store.get_by_idempotency(task.idempotency_key)
            if task.idempotency_key
            else None
        )
        if existing is not None:
            if existing.canonical() == task.canonical():
                return existing
            raise IdempotencyConflictError(
                "idempotency key was reused with a different task"
            )
        if self.store.count_pending() >= self.policy.max_pending_tasks:
            raise SafetyViolation("dispatcher queue capacity reached")
        initial = (
            TaskState.BLOCKED
            if blocked_reason
            else TaskState.PENDING_APPROVAL
            if task.approval_required
            else TaskState.QUEUED
        )
        return self.store.create(
            task, initial_state=initial, initial_error=blocked_reason, now=self.clock()
        )

    def submit_template(
        self,
        template_id: str,
        *,
        branch: str,
        payload: Mapping[str, Any],
        requested_by: str = "operator",
        idempotency_key: str | None = None,
        priority: int | None = None,
        max_attempts: int | None = None,
        dependency_ids: tuple[str, ...] = (),
        parent_task_id: str | None = None,
        allowed_paths: tuple[str, ...] = (),
        prohibited_paths: tuple[str, ...] = (),
        resource_locks: tuple[str, ...] = (),
        expected_base_sha: str | None = None,
        base_branch: str = "main",
        required_tests: tuple[str, ...] | None = None,
        verification_commands: tuple[tuple[str, ...], ...] | None = None,
        max_runtime_seconds: int | None = None,
        requires_pr: bool | None = None,
    ) -> TaskRecord:
        template = self.templates.resolve(template_id)
        return self.submit(
            template.instantiate(
                registry=self.registry,
                branch=branch,
                payload=payload,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                priority=priority,
                max_attempts=max_attempts,
                dependency_ids=dependency_ids,
                parent_task_id=parent_task_id,
                allowed_paths=allowed_paths,
                prohibited_paths=prohibited_paths,
                resource_locks=resource_locks,
                expected_base_sha=expected_base_sha,
                base_branch=base_branch,
                required_tests=required_tests,
                verification_commands=verification_commands,
                max_runtime_seconds=max_runtime_seconds,
                requires_pr=requires_pr,
            )
        )

    def _validate_dependencies(self, task: TaskSpec) -> None:
        if task.parent_task_id == task.task_id:
            raise InvalidTaskError("parent_task_id cannot equal task_id")
        for dependency_id in task.dependency_ids:
            try:
                self.store.get(dependency_id)
            except TaskNotFoundError as exc:
                raise InvalidTaskError(
                    f"dependency {dependency_id!r} does not exist"
                ) from exc
        if task.parent_task_id is not None:
            try:
                self.store.get(task.parent_task_id)
            except TaskNotFoundError as exc:
                raise InvalidTaskError(
                    f"parent task {task.parent_task_id!r} does not exist"
                ) from exc
        # Existing dependency links must not be able to walk back to the new
        # task. This keeps dependency blocking deterministic without a worker
        # needing to understand the graph.
        pending = list(task.dependency_ids)
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            record = self.store.get(current)
            if task.task_id in record.dependency_ids:
                raise InvalidTaskError("dependency graph contains a cycle")
            pending.extend(record.dependency_ids)

    def approve(self, task_id: str, *, approver: str, reason: str = "") -> TaskRecord:
        self._require_actor(approver)
        return self.store.approve(
            task_id, actor=approver, reason=reason, now=self.clock()
        )

    def reject(self, task_id: str, *, approver: str, reason: str = "") -> TaskRecord:
        self._require_actor(approver)
        return self.store.reject(
            task_id, actor=approver, reason=reason, now=self.clock()
        )

    def cancel(self, task_id: str, *, actor: str, reason: str = "") -> TaskRecord:
        self._require_actor(actor)
        return self.store.cancel(task_id, actor=actor, reason=reason, now=self.clock())

    def unblock(self, task_id: str, *, actor: str) -> TaskRecord:
        """Release dependency-blocked work only after all prerequisites succeed."""

        self._require_actor(actor)
        record = self.store.get(task_id)
        if record.state is not TaskState.BLOCKED:
            raise SafetyViolation("only blocked tasks can be released")
        if not record.last_error or not record.last_error.startswith("dependency"):
            raise SafetyViolation(
                "CEO authorization or prohibited work cannot be released autonomously"
            )
        if any(
            self.store.get(dependency).state
            not in {TaskState.SUCCEEDED, TaskState.PR_READY, TaskState.CEO_REVIEW}
            for dependency in record.dependency_ids
        ):
            raise SafetyViolation("blocked task still has an incomplete dependency")
        return self.store.transition(
            task_id,
            expected=TaskState.BLOCKED,
            new_state=TaskState.QUEUED,
            actor=actor,
            event_type=EventType.RELEASED,
            details={"dependencies": list(record.dependency_ids)},
            now=self.clock(),
        )

    def pause(self, *, actor: str) -> None:
        self._require_actor(actor)
        self.store.set_paused(True, actor=actor, now=self.clock())

    def resume(self, *, actor: str) -> None:
        self._require_actor(actor)
        self.store.set_paused(False, actor=actor, now=self.clock())

    def status(self) -> dict[str, Any]:
        result = self.store.stats()
        result.update(
            {
                "dispatcher_id": self.dispatcher_id,
                "registered_builders": list(self.registry.builder_ids),
                "registered_templates": [
                    template.template_id for template in self.templates.templates
                ],
                "state_path": str(self.store.path),
                "queue_mode": "IDLE_SAFE"
                if result["by_state"].get(TaskState.BACKLOG.value, 0)
                + result["by_state"].get(TaskState.READY.value, 0)
                + result["by_state"].get(TaskState.WAITING_DEPENDENCY.value, 0)
                + result["by_state"].get(TaskState.CLAIMED.value, 0)
                + result["by_state"].get(TaskState.RUNNING.value, 0)
                + result["by_state"].get(TaskState.VERIFYING.value, 0)
                == 0
                else "ACTIVE",
            }
        )
        return result

    @staticmethod
    def _require_actor(actor: str) -> None:
        if not isinstance(actor, str) or not actor.strip():
            raise SafetyViolation("actor identity is required")
