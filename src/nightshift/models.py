"""Typed contracts for the SportsBrain Night Shift dispatcher.

The models deliberately contain no worker discovery or execution logic. A
task is dispatchable only after the explicit registry and safety policy have
accepted its complete envelope.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .audit_models import AuditEvent
from .errors import InvalidTaskError
from .task_states import TaskState, state_from_value
from .task_validation import (
    json_payload,
    safe_commands,
    safe_labels,
    safe_locks,
    safe_relative_paths,
)

__all__ = ["AuditEvent", "TaskState", "json_payload", "state_from_value"]

DISPATCHER_ID = "builder-5"
UTC = timezone.utc
_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{7,127}$")
_BRANCH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{7,255}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_BASE_BRANCH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,255}$")


class RiskClass(str, Enum):
    """Side-effect class used by the fail-closed dispatch policy."""

    READ_ONLY = "read_only"
    CODE_CHANGE = "code_change"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    DESTRUCTIVE = "destructive"


class EventType(str, Enum):
    """Auditable events emitted by the dispatcher and store."""

    CREATED = "created"
    APPROVED = "approved"
    REJECTED = "rejected"
    CLAIMED = "claimed"
    RUNNING = "running"
    VERIFYING = "verifying"
    WAITING_DEPENDENCY = "waiting_dependency"
    COMMITTED = "committed"
    PUSHED = "pushed"
    PR_CREATED = "pr_created"
    PR_REUSED = "pr_reused"
    COMPLETED = "completed"
    DELIVERY_BLOCKED = "delivery_blocked"
    FENCED = "fenced"
    HEARTBEAT = "heartbeat"
    SUCCEEDED = "succeeded"
    PR_READY = "pr_ready"
    CEO_REVIEW = "ceo_review"
    FAILED = "failed"
    FAILED_SAFE = "failed_safe"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    LEASE_EXPIRED = "lease_expired"
    BLOCKED = "blocked"
    RELEASED = "released"
    WORKTREE_ALLOCATED = "worktree_allocated"
    SCOPE_VIOLATION = "scope_violation"
    RESOURCE_LOCK_WAIT = "resource_lock_wait"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    RESUMED = "resumed"
    DRAINED = "drained"
    RESTART_REQUESTED = "restart_requested"


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    """Serialize a timestamp in canonical UTC form."""

    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    """Parse a canonical or standard ISO timestamp and normalize to UTC."""

    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class TaskSpec:
    """Immutable task request accepted by the dispatcher."""

    builder_id: str
    objective: str
    branch: str
    repo: str = "Philip3006/sportsbrain"
    task_type: str = ""
    template_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    risk_class: RiskClass = RiskClass.CODE_CHANGE
    requires_approval: bool | None = None
    priority: int = 0
    max_attempts: int = 3
    idempotency_key: str | None = None
    parent_task_id: str | None = None
    dependency_ids: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    prohibited_paths: tuple[str, ...] = ()
    resource_locks: tuple[str, ...] = ()
    expected_base_sha: str | None = None
    base_branch: str = "main"
    required_tests: tuple[str, ...] = ()
    verification_commands: tuple[tuple[str, ...], ...] = ()
    max_runtime_seconds: int = 15 * 60
    requires_pr: bool | None = None
    roadmap_item_id: str | None = None
    debug_budget: int = 0
    repeated_failure_limit: int = 2
    requested_by: str = "operator"
    task_id: str = field(default_factory=lambda: f"ns-{uuid.uuid4().hex}")

    def __post_init__(self) -> None:
        if not isinstance(self.builder_id, str) or not self.builder_id.strip():
            raise InvalidTaskError("builder_id is required")
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise InvalidTaskError("objective is required")
        if len(self.objective) > 4000:
            raise InvalidTaskError("objective exceeds 4000 characters")
        if not isinstance(self.branch, str) or not self.branch.strip():
            raise InvalidTaskError("branch is required")
        if (
            not _BRANCH_RE.fullmatch(self.branch)
            or ".." in self.branch
            or "//" in self.branch
        ):
            raise InvalidTaskError("branch contains unsafe path components")
        if not isinstance(self.repo, str) or not self.repo.strip():
            raise InvalidTaskError("repo is required")
        if self.expected_base_sha is not None and (
            not isinstance(self.expected_base_sha, str)
            or not _SHA_RE.fullmatch(self.expected_base_sha)
        ):
            raise InvalidTaskError(
                "expected_base_sha must be a 40-64 character hex SHA"
            )
        if (
            not isinstance(self.base_branch, str)
            or not _BASE_BRANCH_RE.fullmatch(self.base_branch)
            or ".." in self.base_branch
            or "//" in self.base_branch
            or self.base_branch.startswith("refs/")
        ):
            raise InvalidTaskError("base_branch contains unsafe path components")
        if self.task_type and (
            not isinstance(self.task_type, str) or len(self.task_type) > 128
        ):
            raise InvalidTaskError("task_type is invalid")
        try:
            risk = (
                self.risk_class
                if isinstance(self.risk_class, RiskClass)
                else RiskClass(self.risk_class)
            )
        except ValueError as exc:
            raise InvalidTaskError("risk_class is invalid") from exc
        object.__setattr__(self, "risk_class", risk)
        if self.requires_approval is not None and not isinstance(
            self.requires_approval, bool
        ):
            raise InvalidTaskError("requires_approval must be boolean or omitted")
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or not -100 <= self.priority <= 100
        ):
            raise InvalidTaskError("priority must be an integer between -100 and 100")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 10
        ):
            raise InvalidTaskError("max_attempts must be between 1 and 10")
        if not isinstance(self.requested_by, str) or not self.requested_by.strip():
            raise InvalidTaskError("requested_by is required")
        if not isinstance(self.task_id, str) or not _TASK_ID_RE.fullmatch(self.task_id):
            raise InvalidTaskError("task_id must be 8-128 safe identifier characters")
        if self.idempotency_key is not None and (
            not isinstance(self.idempotency_key, str)
            or not self.idempotency_key.strip()
            or len(self.idempotency_key) > 256
        ):
            raise InvalidTaskError("idempotency_key is invalid")
        if self.parent_task_id is not None and not _TASK_ID_RE.fullmatch(
            self.parent_task_id
        ):
            raise InvalidTaskError("parent_task_id is invalid")
        dependencies = tuple(self.dependency_ids)
        if len(dependencies) > 32 or any(
            not isinstance(item, str) or not _TASK_ID_RE.fullmatch(item)
            for item in dependencies
        ):
            raise InvalidTaskError(
                "dependency_ids must contain at most 32 valid task ids"
            )
        if len(set(dependencies)) != len(dependencies):
            raise InvalidTaskError("dependency_ids must be unique")
        if self.task_id in dependencies:
            raise InvalidTaskError("a task cannot depend on itself")
        allowed = safe_relative_paths(self.allowed_paths, "allowed_paths")
        prohibited = safe_relative_paths(self.prohibited_paths, "prohibited_paths")
        if set(allowed) & set(prohibited):
            raise InvalidTaskError("allowed_paths and prohibited_paths overlap")
        object.__setattr__(self, "allowed_paths", allowed)
        object.__setattr__(self, "prohibited_paths", prohibited)
        object.__setattr__(self, "resource_locks", safe_locks(self.resource_locks))
        object.__setattr__(
            self, "required_tests", safe_labels(self.required_tests, "required_tests")
        )
        object.__setattr__(
            self, "verification_commands", safe_commands(self.verification_commands)
        )
        if (
            isinstance(self.max_runtime_seconds, bool)
            or not isinstance(self.max_runtime_seconds, int)
            or not 1 <= self.max_runtime_seconds <= 24 * 60 * 60
        ):
            raise InvalidTaskError(
                "max_runtime_seconds must be between 1 second and 24 hours"
            )
        if self.requires_pr is not None and not isinstance(self.requires_pr, bool):
            raise InvalidTaskError("requires_pr must be boolean or omitted")
        if self.roadmap_item_id is not None and (
            not isinstance(self.roadmap_item_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,127}", self.roadmap_item_id)
        ):
            raise InvalidTaskError("roadmap_item_id is invalid")
        if (
            isinstance(self.debug_budget, bool)
            or not isinstance(self.debug_budget, int)
            or not 0 <= self.debug_budget <= 10
        ):
            raise InvalidTaskError("debug_budget must be between 0 and 10")
        if (
            isinstance(self.repeated_failure_limit, bool)
            or not isinstance(self.repeated_failure_limit, int)
            or not 1 <= self.repeated_failure_limit <= 10
        ):
            raise InvalidTaskError("repeated_failure_limit must be between 1 and 10")
        normalized_payload = json_payload(self.payload)
        if (
            len(json.dumps(normalized_payload, sort_keys=True, separators=(",", ":")))
            > 65536
        ):
            raise InvalidTaskError("payload exceeds 64 KiB")
        object.__setattr__(self, "payload", normalized_payload)
        object.__setattr__(self, "dependency_ids", dependencies)

    @property
    def approval_required(self) -> bool:
        """Return explicit approval setting, defaulting to risk-based safety."""

        if self.requires_approval is not None:
            return self.requires_approval
        return self.risk_class is not RiskClass.READ_ONLY

    @property
    def pr_required(self) -> bool:
        """Return whether successful work must produce a real pull request."""

        if self.requires_pr is not None:
            return self.requires_pr
        return self.risk_class is RiskClass.CODE_CHANGE

    def canonical(self) -> dict[str, Any]:
        """Return fields used for idempotency comparison."""

        return {
            "builder_id": self.builder_id,
            "objective": self.objective,
            "branch": self.branch,
            "repo": self.repo,
            "task_type": self.task_type,
            "template_id": self.template_id,
            "payload": self.payload,
            "risk_class": self.risk_class.value,
            "requires_approval": self.approval_required,
            "priority": self.priority,
            "max_attempts": self.max_attempts,
            "parent_task_id": self.parent_task_id,
            "dependency_ids": list(self.dependency_ids),
            "allowed_paths": list(self.allowed_paths),
            "prohibited_paths": list(self.prohibited_paths),
            "resource_locks": list(self.resource_locks),
            "expected_base_sha": self.expected_base_sha,
            "base_branch": self.base_branch,
            "required_tests": list(self.required_tests),
            "verification_commands": [
                list(command) for command in self.verification_commands
            ],
            "max_runtime_seconds": self.max_runtime_seconds,
            "requires_pr": self.pr_required,
            "roadmap_item_id": self.roadmap_item_id,
            "debug_budget": self.debug_budget,
            "repeated_failure_limit": self.repeated_failure_limit,
        }

    def as_dict(self) -> dict[str, Any]:
        """Serialize the complete request envelope."""

        return {
            **self.canonical(),
            "requested_by": self.requested_by,
            "task_id": self.task_id,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """Result returned by an injected worker adapter."""

    success: bool
    summary: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    retryable: bool = True
    terminal_state: TaskState | None = None
    process_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise InvalidTaskError("execution success must be boolean")
        if not isinstance(self.summary, str) or len(self.summary) > 4000:
            raise InvalidTaskError("execution summary is invalid")
        object.__setattr__(self, "data", json_payload(self.data))
        if not isinstance(self.retryable, bool):
            raise InvalidTaskError("execution retryable must be boolean")
        if self.terminal_state is not None:
            try:
                terminal = (
                    self.terminal_state
                    if isinstance(self.terminal_state, TaskState)
                    else TaskState(self.terminal_state)
                )
            except ValueError as exc:
                raise InvalidTaskError("execution terminal_state is invalid") from exc
            if terminal not in {
                TaskState.SUCCEEDED,
                TaskState.PR_READY,
                TaskState.CEO_REVIEW,
                TaskState.FAILED_SAFE,
            }:
                raise InvalidTaskError(
                    "execution terminal_state is not an executor outcome"
                )
            if (
                terminal
                in {TaskState.SUCCEEDED, TaskState.PR_READY, TaskState.CEO_REVIEW}
                and not self.success
            ):
                raise InvalidTaskError("successful terminal_state requires success")
            if terminal is TaskState.FAILED_SAFE and self.success:
                raise InvalidTaskError("FAILED_SAFE terminal_state requires failure")
            if terminal is TaskState.FAILED_SAFE and self.retryable:
                raise InvalidTaskError("FAILED_SAFE terminal_state cannot be retried")
            object.__setattr__(self, "terminal_state", terminal)
        if self.process_id is not None and (
            isinstance(self.process_id, bool)
            or not isinstance(self.process_id, int)
            or self.process_id <= 0
        ):
            raise InvalidTaskError("process_id must be a positive integer")

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.terminal_state is not None:
            result["terminal_state"] = self.terminal_state.value
        return result


@dataclass(frozen=True)
class TaskRecord:
    """Persisted task state returned by the store."""

    task_id: str
    builder_id: str
    objective: str
    branch: str
    repo: str
    task_type: str
    template_id: str | None
    payload: Mapping[str, Any]
    risk_class: RiskClass
    requires_approval: bool
    priority: int
    max_attempts: int
    attempt_count: int
    state: TaskState
    requested_by: str
    parent_task_id: str | None
    dependency_ids: tuple[str, ...]
    created_at: str
    updated_at: str
    available_at: str
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    last_error: str | None = None
    result: Mapping[str, Any] | None = None
    allowed_paths: tuple[str, ...] = ()
    prohibited_paths: tuple[str, ...] = ()
    resource_locks: tuple[str, ...] = ()
    expected_base_sha: str | None = None
    base_branch: str = "main"
    required_tests: tuple[str, ...] = ()
    verification_commands: tuple[tuple[str, ...], ...] = ()
    max_runtime_seconds: int = 15 * 60
    requires_pr: bool = False
    worktree_path: str | None = None
    base_sha: str | None = None
    origin_sha: str | None = None
    lease_generation: int = 0
    process_id: int | None = None
    commit_sha: str | None = None
    remote_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    verification: Mapping[str, Any] | None = None
    delivery: Mapping[str, Any] | None = None
    diagnostic_path: str | None = None
    failure_class: str | None = None
    roadmap_item_id: str | None = None
    debug_budget: int = 0
    debug_attempt_count: int = 0
    last_failure_signature: str | None = None
    failure_repeat_count: int = 0
    repeated_failure_limit: int = 2

    @property
    def lease_active(self) -> bool:
        return (
            self.state
            in {
                TaskState.CLAIMED,
                TaskState.RUNNING,
                TaskState.VERIFYING,
            }
            and self.lease_owner is not None
        )

    @property
    def pr_required(self) -> bool:
        """Return whether this persisted task requires a real pull request."""

        return self.requires_pr

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["risk_class"] = self.risk_class.value
        result["state"] = self.state.value
        result["payload"] = dict(self.payload)
        result["dependency_ids"] = list(self.dependency_ids)
        result["allowed_paths"] = list(self.allowed_paths)
        result["prohibited_paths"] = list(self.prohibited_paths)
        result["resource_locks"] = list(self.resource_locks)
        result["required_tests"] = list(self.required_tests)
        result["verification_commands"] = [
            list(command) for command in self.verification_commands
        ]
        if self.result is not None:
            result["result"] = dict(self.result)
        return result

    def canonical(self) -> dict[str, Any]:
        fields = (
            "builder_id",
            "objective",
            "branch",
            "repo",
            "task_type",
            "template_id",
            "payload",
            "risk_class",
            "requires_approval",
            "priority",
            "max_attempts",
            "parent_task_id",
            "dependency_ids",
            "allowed_paths",
            "prohibited_paths",
            "resource_locks",
            "expected_base_sha",
            "base_branch",
            "required_tests",
            "verification_commands",
            "max_runtime_seconds",
            "requires_pr",
            "roadmap_item_id",
            "debug_budget",
            "repeated_failure_limit",
        )
        serialized = self.as_dict()
        return {field: serialized[field] for field in fields}
