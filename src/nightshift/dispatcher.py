"""Safe orchestration façade for the SportsBrain Night Shift queue."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .backpressure import (
    ACTIVE_TASK_STATES,
    paths_overlap,
    record_changed_paths,
    summarize_pull_requests,
)
from .delivery import DeliveryPipeline, GhPullRequestClient, PullRequestClient
from .dispatcher_execution import DispatcherExecutionMixin
from .errors import (
    DeliveryBlocked,
    DeliveryError,
    IdempotencyConflictError,
    InvalidTaskError,
    SafetyViolation,
    TaskNotFoundError,
    UnknownBuilderError,
)
from .models import (
    DISPATCHER_ID,
    EventType,
    ExecutionResult,
    RiskClass,
    TaskRecord,
    TaskSpec,
    TaskState,
    utc_now,
)
from .policy import SafetyPolicy
from .recovery import DeliveryVerificationError, verify_github_pull_request
from .registry import BuilderRegistry
from .roadmap import RoadmapRegistry
from .status import operator_snapshot, task_summary
from .store import DispatcherStore
from .task_states import DEPENDENCY_SATISFIED_STATES
from .templates import TemplateRegistry
from .worktree import (
    WorktreeManager,
    default_control_remote_urls,
    default_control_repo_paths,
    default_repo_paths,
    default_runtime_dirty_policy,
    default_worktree_root,
)

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
        lease_seconds: int = 30 * 60,
        retry_base_seconds: int = 60,
        retry_max_seconds: int = 60 * 60,
        clock: Callable[[], datetime] = utc_now,
        worktree_manager: WorktreeManager | None = None,
        delivery_pipeline: DeliveryPipeline | None = None,
        roadmap: RoadmapRegistry | None = None,
        merge_backpressure_limit: int = 12,
    ) -> None:
        if dispatcher_id != DISPATCHER_ID:
            raise SafetyViolation("Builder 5 is the only supported dispatcher identity")
        if not 30 <= lease_seconds <= 24 * 60 * 60:
            raise SafetyViolation(
                "lease_seconds must be between 30 seconds and 24 hours"
            )
        if not 0 <= retry_base_seconds <= retry_max_seconds:
            raise SafetyViolation("retry delay bounds are invalid")
        if not 1 <= merge_backpressure_limit <= 100:
            raise SafetyViolation("merge_backpressure_limit must be between 1 and 100")
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
        self.roadmap = roadmap or RoadmapRegistry(())
        self.merge_backpressure_limit = merge_backpressure_limit
        self.roadmap.validate_against(self.registry, self.templates)
        self.store.sync_roadmap(self.roadmap, now=self.clock())

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
        roadmap: RoadmapRegistry | None = None,
        **kwargs: Any,
    ) -> NightShiftDispatcher:
        directory = config_dir or _default_config_dir()
        registry = BuilderRegistry.from_file(directory / "builders.json")
        templates = TemplateRegistry.from_file(directory / "templates.json")
        roadmap_path = directory / "roadmap.json"
        configured_roadmap = roadmap or (
            RoadmapRegistry.from_file(roadmap_path)
            if roadmap_path.is_file()
            else RoadmapRegistry(())
        )
        manager = worktree_manager
        if require_isolated_worktrees and manager is None:
            manager = WorktreeManager(
                (state_path or _default_state_path()).parent,
                default_repo_paths(Path(__file__).resolve().parents[2]),
                control_repo_paths=default_control_repo_paths(),
                worktrees_dir=default_worktree_root(),
                runtime_dirty_policy=default_runtime_dirty_policy(
                    Path(__file__).resolve().parents[2]
                ),
                expected_remote_urls=default_control_remote_urls(),
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
            roadmap=configured_roadmap,
            merge_backpressure_limit=configured_roadmap.merge_backpressure_limit,
            **kwargs,
        )

    def submit(self, task: TaskSpec) -> TaskRecord:
        """Validate and persist a task, applying idempotency when supplied."""

        definition = self.registry.validate_task(task, enforce_risk=False)
        blocked_reason = self.policy.block_reason(task)
        if blocked_reason is None:
            self.policy.validate(task, definition)
        if task.builder_id != definition.builder_id:
            task = replace(
                task,
                builder_id=definition.builder_id,
                execution_worker=definition.builder_id,
            )
        if task.template_id is not None:
            template = self.templates.resolve(task.template_id)
            if (
                template.task_type != task.task_type
                or (template.app_owner and template.app_owner != task.app_owner)
            ):
                raise InvalidTaskError(
                    "template does not match the explicit APP owner/type"
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
        roadmap_item_id: str | None = None,
        debug_budget: int = 0,
        repeated_failure_limit: int = 2,
        execution_worker: str | None = None,
        app_owner: str | None = None,
        required_capabilities: tuple[str, ...] | None = None,
        authority_requirements: tuple[str, ...] | None = None,
        verification_matrix_version: str | None = None,
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
                roadmap_item_id=roadmap_item_id,
                debug_budget=debug_budget,
                repeated_failure_limit=repeated_failure_limit,
                execution_worker=execution_worker,
                app_owner=app_owner,
                required_capabilities=required_capabilities,
                authority_requirements=authority_requirements,
                verification_matrix_version=verification_matrix_version,
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
            not in DEPENDENCY_SATISFIED_STATES
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

    def drain(self, *, actor: str) -> None:
        """Stop selecting new roadmap work while allowing active work to finish."""

        self._require_actor(actor)
        self.store.set_draining(True, actor=actor, now=self.clock())

    def stop_drain(self, *, actor: str) -> None:
        self._require_actor(actor)
        self.store.set_draining(False, actor=actor, now=self.clock())

    def restart(self, *, actor: str) -> None:
        """Record an operator restart request; process supervision owns restart."""

        self._require_actor(actor)
        self.store.request_restart(actor=actor, now=self.clock())

    def reevaluate_blocked(self) -> list[str]:
        return self.store.release_eligible_blocked(
            now=self.clock(), actor=self.dispatcher_id
        )

    def reconcile_merged(
        self,
        task_id: str,
        *,
        actor: str,
        github: PullRequestClient | None = None,
    ) -> TaskRecord:
        """Complete PR work only after independent read-only merge verification."""

        self._require_actor(actor)
        record = self.store.get(task_id)
        if record.state is TaskState.COMPLETED:
            merge_evidence = (record.delivery or {}).get("merge_verification")
            if isinstance(merge_evidence, Mapping) and merge_evidence.get("merged") is True:
                return record
            raise SafetyViolation("task is already completed without merge reconciliation")
        if record.state not in {TaskState.PR_READY, TaskState.CEO_REVIEW}:
            raise SafetyViolation("task is not awaiting merge verification")
        if not record.requires_pr or not record.pr_number:
            raise SafetyViolation("merge verification requires a recorded pull request")
        client = github
        if client is None:
            client = self.delivery_pipeline.github if self.delivery_pipeline else GhPullRequestClient()
        verifier = getattr(client, "verify_merged", None)
        if verifier is None:
            raise DeliveryBlocked("MERGE_VERIFICATION_UNAVAILABLE")
        try:
            evidence = verifier(record)
        except DeliveryError:
            raise
        except Exception as exc:
            raise DeliveryError("pull request merge verification failed") from exc
        if not isinstance(evidence, Mapping) or evidence.get("merged") is not True:
            raise DeliveryError("merge verification did not prove merged=true")
        completed = self.store.mark_merge_verified(
            task_id,
            actor=actor,
            evidence=dict(evidence),
            now=self.clock(),
        )
        self._refresh_roadmap()
        return completed

    def reconcile_delivery(
        self,
        task_id: str,
        *,
        actor: str,
        facts: Mapping[str, Any] | None = None,
        pr_number: int | None = None,
        commit_sha: str | None = None,
        remote_sha: str | None = None,
    ) -> TaskRecord:
        """Recover preserved delivery work after independently checking GitHub.

        Production callers omit ``facts`` and this method performs a read-only
        ``gh api`` verification.  A facts mapping is accepted for deterministic
        tests and still undergoes task-binding validation below.
        """

        self._require_actor(actor)
        record = self.store.get(task_id)
        failure = (record.failure_class or "").upper()
        if record.state is TaskState.FAILED_SAFE and (
            not failure.startswith("DELIVERY")
            or "TIMEOUT" in failure
            or "DEAD_LETTER" in failure
        ):
            raise DeliveryVerificationError(
                "timeout/dead-letter work cannot be reconciled as delivery"
            )
        selected_pr = pr_number or record.pr_number
        selected_commit = commit_sha or record.commit_sha
        selected_remote = remote_sha or record.remote_sha
        if facts is None:
            if selected_pr is None or selected_commit is None:
                raise DeliveryVerificationError(
                    "recovery requires the recorded or explicitly supplied PR and commit"
                )
            facts = verify_github_pull_request(
                record.repo,
                selected_pr,
                expected_commit_sha=selected_commit,
                expected_base=record.base_branch,
                expected_branch=record.branch,
                expected_remote_sha=selected_remote,
            )
        if not isinstance(facts, Mapping) or facts.get("verified") is not True:
            raise DeliveryVerificationError("delivery recovery requires verified GitHub facts")
        if facts.get("worker_execution_success") is not True or facts.get(
            "implementation_success"
        ) is not True:
            raise DeliveryVerificationError("delivery recovery requires successful implementation evidence")
        self._validate_recovery_binding(record, facts)
        if facts.get("merged") is True:
            raise DeliveryVerificationError(
                "verified merged PR must use reconcile-merged; no automatic completion"
            )
        recovered = self.store.reconcile_delivery(
            task_id,
            actor=actor,
            evidence=facts,
            now=self.clock(),
        )
        self._refresh_roadmap()
        return recovered

    @staticmethod
    def _validate_recovery_binding(
        record: TaskRecord, facts: Mapping[str, Any]
    ) -> None:
        for key, expected in (
            ("repo", record.repo),
            ("head_ref", record.branch),
            ("base_ref", record.base_branch),
            ("pr_number", record.pr_number),
            ("commit_sha", record.commit_sha),
            ("remote_sha", record.remote_sha),
        ):
            actual = facts.get(key)
            if actual is None:
                continue
            if expected is None:
                continue
            if key in {"commit_sha", "remote_sha"}:
                if not isinstance(actual, str) or actual.lower() != expected.lower():
                    raise DeliveryVerificationError(
                        f"recovery {key} does not match the preserved task evidence"
                    )
            elif actual != expected:
                raise DeliveryVerificationError(
                    f"recovery {key} does not match the preserved task evidence"
                )
        if record.pr_number is None and (
            isinstance(facts.get("pr_number"), bool)
            or not isinstance(facts.get("pr_number"), int)
        ):
            raise DeliveryVerificationError("recovery is missing a valid pull-request number")

    def _refresh_roadmap(self) -> None:
        """Reflect task outcomes without inventing or deleting roadmap items."""

        for item in self.store.roadmap_records():
            task_id = item.get("task_id")
            if not task_id:
                continue
            try:
                task = self.store.get(task_id)
            except TaskNotFoundError:
                continue
            if task.state is TaskState.COMPLETED:
                self.store.set_roadmap_status(
                    item["item_id"], status="COMPLETED", now=self.clock()
                )
            elif task.state in {TaskState.PR_READY, TaskState.CEO_REVIEW}:
                self.store.set_roadmap_status(
                    item["item_id"],
                    status="BLOCKED",
                    reason="awaiting verified GitHub merge",
                    next_eligible_at=datetime.fromisoformat(
                        task.available_at.replace("Z", "+00:00")
                    ),
                    now=self.clock(),
                )
            elif task.state is TaskState.BLOCKED:
                self.store.set_roadmap_status(
                    item["item_id"],
                    status="BLOCKED",
                    reason=task.last_error,
                    next_eligible_at=datetime.fromisoformat(
                        task.available_at.replace("Z", "+00:00")
                    ),
                    now=self.clock(),
                )
            elif task.state in {TaskState.READY, TaskState.WAITING_DEPENDENCY}:
                self.store.set_roadmap_status(
                    item["item_id"], status="ENQUEUED", now=self.clock()
                )
            elif task.state is TaskState.FAILED_SAFE:
                self.store.set_roadmap_status(
                    item["item_id"],
                    status="BLOCKED",
                    reason=task.last_error,
                    next_eligible_at=datetime.fromisoformat(
                        task.available_at.replace("Z", "+00:00")
                    ),
                    now=self.clock(),
                )
            elif task.state is TaskState.PAUSED_QUOTA:
                self.store.set_roadmap_status(
                    item["item_id"],
                    status="BLOCKED",
                    reason="AI quota paused",
                    next_eligible_at=datetime.fromisoformat(
                        task.available_at.replace("Z", "+00:00")
                    ),
                    now=self.clock(),
                )

    def _roadmap_item_is_read_only(self, item: Any) -> bool:
        """Return whether a configured item may bypass PR backpressure."""

        template = self.templates.resolve(item.template_id)
        return (
            template.risk_class is RiskClass.READ_ONLY
            and template.requires_pr is not True
        )

    def _roadmap_overlap_reason(
        self, item: Any, records: list[TaskRecord]
    ) -> str | None:
        """Return an individual overlap blocker, never a global PR blocker."""

        governed_paths = tuple(getattr(item, "governed_paths", ()))
        resource_locks = tuple(getattr(item, "resource_locks", ()))
        if governed_paths:
            for record in records:
                if record.pr_number is None or record.state not in {
                    TaskState.PR_READY,
                    TaskState.CEO_REVIEW,
                }:
                    continue
                if paths_overlap(governed_paths, record_changed_paths(record)):
                    return f"open_pr_overlap:{record.pr_number}"
        if resource_locks:
            for record in records:
                if record.state in ACTIVE_TASK_STATES and set(resource_locks).intersection(
                    record.resource_locks
                ):
                    return f"active_resource_lock:{record.task_id}"
        return None

    def _record_roadmap_skip(self, item: Any, reason: str) -> None:
        signature = f"{item.item_id}:{reason}"
        self.store.record_roadmap_skip(
            item.item_id,
            reason=reason,
            signature=signature,
            actor=self.dispatcher_id,
            now=self.clock(),
        )

    def _select_execution_worker(self, item: Any) -> str | None:
        """Choose free terminal capacity from the reviewed worker pool."""

        template = self.templates.resolve(item.template_id)
        active = {
            record.terminal_worker_id
            for record in self.store.list_tasks(limit=1000)
            if record.lease_active
        }
        preferred = [item.builder_id, *self.registry.builder_ids]
        seen: set[str] = set()
        for candidate in preferred:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                definition = self.registry.assert_worker_target(candidate)
            except (UnknownBuilderError, SafetyViolation):
                continue
            if definition.builder_id in active:
                continue
            if template.task_type not in definition.task_types and template.task_type not in definition.delegated_task_types:
                continue
            if not set(template.required_capabilities).issubset(definition.capabilities):
                continue
            if template.risk_class not in definition.allowed_risk_classes:
                continue
            return definition.builder_id
        return None

    def select_next_roadmap_task(
        self, *, builder_id: str | None = None
    ) -> TaskRecord | None:
        """Materialize one explicit roadmap item into the normal task queue."""

        if builder_id is not None:
            builder_id = self.registry.assert_worker_target(builder_id).builder_id
        if self.store.is_paused() or self.store.is_draining():
            return None
        self._refresh_roadmap()
        records = {item["item_id"]: item for item in self.store.roadmap_records()}
        task_records = self.store.list_tasks(limit=1000)
        soft_backpressure = (
            summarize_pull_requests(task_records)["active_substantive_pr_count"]
            >= self.merge_backpressure_limit
        )
        candidates: list[tuple[Any, dict[str, Any], bool]] = []
        for item in sorted(
            self.roadmap.items, key=lambda value: (-value.priority, value.item_id)
        ):
            row = records.get(item.item_id)
            if (
                row is None
                or not item.enabled
                or row["status"] in {"DISABLED", "COMPLETED"}
            ):
                continue
            if builder_id is not None and item.builder_id != builder_id:
                # ``builder_id`` is a compatibility filter for callers that
                # still pin a worker.  The pool path below is capability based.
                continue
            if row.get("task_id"):
                existing = self.store.get(row["task_id"])
                if existing.state not in {
                    TaskState.READY,
                    TaskState.WAITING_DEPENDENCY,
                }:
                    self._record_roadmap_skip(
                        item, f"task_state:{existing.state.value}"
                    )
                    continue
                if row["status"] == "BLOCKED":
                    self.store.set_roadmap_status(
                        item.item_id, status="ENQUEUED", now=self.clock()
                    )
                    row["status"] = "ENQUEUED"
                elif row["status"] == "ENQUEUED":
                    self._record_roadmap_skip(item, "task_already_enqueued")
                    continue
            overlap_reason = self._roadmap_overlap_reason(item, task_records)
            if overlap_reason is not None:
                self._record_roadmap_skip(item, overlap_reason)
                continue
            dependencies = [records.get(dep) for dep in item.dependency_item_ids]
            if any(dep is None or dep["status"] != "COMPLETED" for dep in dependencies):
                reason = "dependency roadmap item incomplete"
                if row["status"] != "BLOCKED" or row.get("blocked_reason") != reason:
                    self.store.set_roadmap_status(
                        item.item_id,
                        status="BLOCKED",
                        reason=reason,
                        next_eligible_at=self.clock(),
                        now=self.clock(),
                    )
                self._record_roadmap_skip(item, reason)
                continue
            candidates.append((item, row, self._roadmap_item_is_read_only(item)))

        if not candidates:
            return None
        selected_index = 0
        if soft_backpressure:
            selected_index = next(
                (
                    index
                    for index, (_, _, read_only) in enumerate(candidates)
                    if read_only
                ),
                0,
            )
            for index, (item, _, read_only) in enumerate(candidates):
                if index != selected_index and not read_only:
                    self._record_roadmap_skip(
                        item, "soft_backpressure_preference_for_safe_work"
                    )
        item, row, _ = candidates[selected_index]
        selected_worker = builder_id or self._select_execution_worker(item)
        if selected_worker is None:
            self._record_roadmap_skip(item, "no_free_capable_terminal_worker")
            return None
        definition = self.registry.assert_worker_target(selected_worker)
        task = self.submit_template(
                item.template_id,
                branch=f"{definition.branch_prefix}{item.item_id}",
                payload=item.payload,
                requested_by="nightshift-dispatcher:roadmap",
                idempotency_key=item.idempotency_key,
                priority=item.priority,
                debug_budget=item.debug_budget,
                repeated_failure_limit=item.repeated_failure_limit,
                execution_worker=selected_worker,
                app_owner=(
                    getattr(item, "app_owner", None)
                    or self.templates.resolve(item.template_id).app_owner
                    or f"APP_B{item.builder_id.rsplit('-', 1)[-1]}"
                ),
                allowed_paths=tuple(getattr(item, "governed_paths", ())),
                resource_locks=tuple(getattr(item, "resource_locks", ())),
                roadmap_item_id=item.item_id,
            )
        status = "BLOCKED" if task.state is TaskState.BLOCKED else "ENQUEUED"
        self.store.set_roadmap_status(
            item.item_id,
            status=status,
            task_id=task.task_id,
            reason=task.last_error if status == "BLOCKED" else None,
            now=self.clock(),
        )
        return task

    def run_autonomous_cycle(
        self,
        builder_id: str,
        executor: WorkerExecutor,
        *,
        max_tasks: int = 1,
    ) -> list[TaskRecord]:
        """Run a finite worker cycle; unlimited mode never removes this bound."""

        if isinstance(max_tasks, bool) or not 1 <= max_tasks <= 100:
            raise SafetyViolation("max_tasks must be between 1 and 100")
        completed: list[TaskRecord] = []
        self.reevaluate_blocked()
        for _ in range(max_tasks):
            self.select_next_roadmap_task(builder_id=builder_id)
            result = self.run_once(builder_id, executor)
            if result is None:
                break
            completed.append(result)
            self._refresh_roadmap()
        return completed

    def run_debug_loop(
        self,
        builder_id: str,
        executor: WorkerExecutor,
        *,
        max_cycles: int = 3,
    ) -> list[TaskRecord]:
        """Run bounded diagnose/retest cycles; repeated failures are parked."""

        if isinstance(max_cycles, bool) or not 1 <= max_cycles <= 10:
            raise SafetyViolation("max_cycles must be between 1 and 10")
        return self.run_autonomous_cycle(builder_id, executor, max_tasks=max_cycles)

    def run_autonomous(
        self,
        builder_id: str,
        executor: WorkerExecutor,
        *,
        mode: str | None = None,
        max_cycles: int | None = None,
    ) -> list[TaskRecord]:
        """Run a bounded autonomous session in the configured roadmap mode."""

        selected_mode = self.roadmap.mode if mode is None else mode
        if selected_mode not in {"bounded", "unlimited"}:
            raise SafetyViolation("autonomous mode must be bounded or unlimited")
        if max_cycles is not None and isinstance(max_cycles, bool):
            raise SafetyViolation("autonomous sessions must be bounded to 100 cycles")
        limit = self.roadmap.max_cycles if max_cycles is None else max_cycles
        if selected_mode == "unlimited":
            # "Unlimited" means no roadmap item count is imposed by the
            # configuration; every invocation still has an operator-visible
            # finite cap so a bad worker cannot spin forever.
            limit = 100 if max_cycles is None else max_cycles
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise SafetyViolation("autonomous sessions must be bounded to 100 cycles")
        return self.run_autonomous_cycle(builder_id, executor, max_tasks=limit)

    def run_worker_pool(
        self,
        executors: Mapping[str, WorkerExecutor] | WorkerExecutor,
        *,
        max_tasks: int = 100,
    ) -> list[TaskRecord]:
        """Continuously dispatch eligible work to free terminal capacity.

        The dispatcher, rather than a human or a fixed Builder number, chooses
        the next capable worker.  Executors are injected adapters; this method
        never grants APP authority or starts an external provider by itself.
        """

        if isinstance(max_tasks, bool) or not 1 <= max_tasks <= 1000:
            raise SafetyViolation("max_tasks must be between 1 and 1000")
        completed: list[TaskRecord] = []
        for _ in range(max_tasks):
            task = self.select_next_roadmap_task()
            if task is not None:
                worker = task.terminal_worker_id
                result = self.run_once(
                    worker, self._pool_executor(executors, worker)
                )
            else:
                result = None
                for worker in self.registry.builder_ids:
                    candidate = self.run_once(
                        worker, self._pool_executor(executors, worker)
                    )
                    if candidate is not None:
                        result = candidate
                        break
                if result is None:
                    break
            if result is None:
                break
            completed.append(result)
            self._refresh_roadmap()
        return completed

    @staticmethod
    def _pool_executor(
        executors: Mapping[str, WorkerExecutor] | WorkerExecutor,
        worker: str,
    ) -> WorkerExecutor:
        if not isinstance(executors, Mapping):
            return executors
        executor = executors.get(worker) or executors.get("default")
        if executor is None:
            raise SafetyViolation(
                f"no executor adapter registered for terminal worker {worker}"
            )
        return executor

    # Explicit alias for integrations that call the control-plane operation by
    # its scheduling name.
    dispatch_worker_pool = run_worker_pool

    def status(self) -> dict[str, Any]:
        result = self.store.stats()
        roadmap = self.store.roadmap_records()
        records = self.store.list_tasks(limit=1000)
        pr_counts = summarize_pull_requests(records)
        merge_count = pr_counts["active_substantive_pr_count"]
        merge_backpressure = merge_count >= self.merge_backpressure_limit
        actionable = sum(
            result["by_state"].get(state.value, 0)
            for state in (
                TaskState.BACKLOG,
                TaskState.READY,
                TaskState.WAITING_DEPENDENCY,
                TaskState.CLAIMED,
                TaskState.RUNNING,
                TaskState.VERIFYING,
            )
        )
        if result["paused"]:
            queue_mode = "PAUSED"
        elif result.get("draining"):
            queue_mode = "DRAINING"
        elif any(record.state is TaskState.PAUSED_QUOTA for record in records):
            queue_mode = "PAUSED_QUOTA" if actionable == 0 else "ACTIVE"
        elif actionable == 0 and all(
            item["status"] in {"COMPLETED", "DISABLED"} for item in roadmap
        ):
            queue_mode = "INTENTIONAL_IDLE" if roadmap else "IDLE_SAFE"
        else:
            queue_mode = "CONTINUOUS_AUTONOMOUS"
        roadmap_summary = [
            {
                key: item.get(key)
                for key in (
                    "item_id",
                    "title",
                    "builder_id",
                    "template_id",
                    "dependency_item_ids",
                    "priority",
                    "status",
                    "task_id",
                    "blocked_reason",
                    "next_eligible_at",
                    "enabled",
                    "generation",
                    "governed_paths",
                    "resource_locks",
                    "skip_reason",
                    "skip_signature",
                    "skip_count",
                )
            }
            for item in roadmap
        ]
        configured_items = {item.item_id: item for item in self.roadmap.items}
        for summary in roadmap_summary:
            template = self.templates.resolve(summary["template_id"])
            summary["risk_class"] = template.risk_class.value
            configured_item = configured_items.get(summary["item_id"])
            read_only = (
                self._roadmap_item_is_read_only(configured_item)
                if configured_item is not None
                else template.risk_class is RiskClass.READ_ONLY
                and template.requires_pr is not True
            )
            summary["merge_backpressure_blocked"] = False
            summary["soft_backpressure_preferred"] = merge_backpressure and not read_only
        operator = operator_snapshot(
            records,
            roadmap_summary,
            merge_backpressure=merge_backpressure,
            pr_counts=pr_counts,
            builders=self.registry.builder_ids,
            paused=result["paused"],
            draining=result["draining"],
        )
        active_states = {
            TaskState.CLAIMED,
            TaskState.RUNNING,
            TaskState.VERIFYING,
            TaskState.DELIVERY_RECONCILING,
        }
        app_workstreams = {}
        for app_owner in ("APP_B1", "APP_B2", "APP_B3", "APP_B4", "APP_B5"):
            owned = [record for record in records if record.app_owner == app_owner]
            app_workstreams[app_owner] = {
                "status": "active"
                if any(record.state in active_states for record in owned)
                else "blocked"
                if any(record.state in {TaskState.BLOCKED, TaskState.FAILED_SAFE} for record in owned)
                else "idle",
                "active_tasks": [task_summary(record) for record in owned if record.state in active_states],
                "blockers": [
                    task_summary(record)
                    for record in owned
                    if record.state in {TaskState.BLOCKED, TaskState.FAILED_SAFE, TaskState.PAUSED_QUOTA}
                ],
                "dependencies": [
                    record.task_id
                    for record in owned
                    if record.state is TaskState.WAITING_DEPENDENCY
                ],
                "hard_gates": sorted(
                    {
                        requirement
                        for record in owned
                        for requirement in record.authority_requirements
                    }
                ),
            }
        terminal_capacity = {}
        for definition in self.registry.builders:
            owned = [record for record in records if record.terminal_worker_id == definition.builder_id]
            current = next((record for record in owned if record.state in active_states), None)
            terminal_capacity[definition.builder_id] = {
                "status": current.state.value.lower() if current else "idle",
                "current_task": task_summary(current) if current else None,
                "app_owner": current.app_owner if current else None,
                "lease": current.lease_expires_at if current else None,
                "heartbeat": current.updated_at if current else None,
                "capabilities": list(definition.capabilities),
                "max_concurrency": definition.max_concurrency,
            }
        if (
            queue_mode == "CONTINUOUS_AUTONOMOUS"
            and not operator["next_eligible_explicit_task"]
            and not operator["running"]
            and not operator["eligible_roadmap_items"]
        ):
            queue_mode = "GLOBAL_IDLE"
        result.update(
            {
                "dispatcher_id": self.dispatcher_id,
                "dispatcher_health": {
                    "status": "paused" if result["paused"] else "draining" if result["draining"] else "healthy",
                    "control_plane_identity": self.dispatcher_id,
                    "is_terminal_worker": False,
                },
                "registered_builders": list(self.registry.builder_ids),
                "app_workstreams": app_workstreams,
                "terminal_capacity": terminal_capacity,
                "registered_templates": [
                    template.template_id for template in self.templates.templates
                ],
                "state_path": str(self.store.path),
                "queue_mode": queue_mode,
                "merge_backpressure_count": merge_count,
                "merge_backpressure_limit": self.merge_backpressure_limit,
                "active_substantive_pr_count": merge_count,
                "pr_classifications": pr_counts,
                "backpressure_mode": "SOFT" if merge_backpressure else "NONE",
                "backpressure_is_hard": False,
                "roadmap": roadmap_summary,
                "roadmap_mode": self.roadmap.mode,
                "roadmap_max_cycles": self.roadmap.max_cycles,
                "operator": operator,
            }
        )
        return result

    @staticmethod
    def _require_actor(actor: str) -> None:
        if not isinstance(actor, str) or not actor.strip():
            raise SafetyViolation("actor identity is required")
