"""Typed contracts for the SportsBrain Night Shift dispatcher.

The models deliberately contain no worker discovery or execution logic. A
task is dispatchable only after the explicit registry and safety policy have
accepted its complete envelope.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .errors import InvalidTaskError

DISPATCHER_ID = "builder-5"
UTC = timezone.utc
_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{7,127}$")
_BRANCH_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{7,255}$")


class TaskState(str, Enum):
    """Persistent states in the queue state machine."""

    PENDING_APPROVAL = "pending_approval"
    QUEUED = "queued"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    PR_READY = "pr_ready"
    CEO_REVIEW = "ceo_review"
    FAILED = "failed"
    FAILED_SAFE = "failed_safe"
    DEAD_LETTER = "dead_letter"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


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


def _safe_relative_paths(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidTaskError(f"{field_name} must be an array")
    paths = tuple(value)
    for path in paths:
        if not isinstance(path, str) or not path.strip() or path.startswith("/"):
            raise InvalidTaskError(f"{field_name} must contain relative paths")
        parts = path.replace("\\", "/").split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise InvalidTaskError(f"{field_name} contains an unsafe path")
    if len(set(paths)) != len(paths):
        raise InvalidTaskError(f"{field_name} must not contain duplicates")
    return paths


def _safe_locks(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidTaskError("resource_locks must be an array")
    locks = tuple(value)
    if any(
        not isinstance(lock, str)
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9:._/-]{0,127}", lock)
        for lock in locks
    ):
        raise InvalidTaskError("resource_locks contain an unsafe identifier")
    if len(set(locks)) != len(locks):
        raise InvalidTaskError("resource_locks must be unique")
    return locks


def _json_safe(value: Any, *, path: str = "payload") -> Any:
    """Validate and normalize JSON data without accepting executable objects."""

    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidTaskError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise InvalidTaskError(f"{path} keys must be non-empty strings")
            normalized[key] = _json_safe(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _json_safe(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise InvalidTaskError(f"{path} contains unsupported value {type(value).__name__}")


def json_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe deep copy of a task payload."""

    normalized = _json_safe(value)
    if not isinstance(
        normalized, dict
    ):  # pragma: no cover - mapping always becomes dict
        raise InvalidTaskError("payload must be an object")
    try:
        json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:  # defensive boundary check
        raise InvalidTaskError("payload is not JSON serializable") from exc
    return normalized


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
        allowed = _safe_relative_paths(self.allowed_paths, "allowed_paths")
        prohibited = _safe_relative_paths(self.prohibited_paths, "prohibited_paths")
        if set(allowed) & set(prohibited):
            raise InvalidTaskError("allowed_paths and prohibited_paths overlap")
        object.__setattr__(self, "allowed_paths", allowed)
        object.__setattr__(self, "prohibited_paths", prohibited)
        object.__setattr__(self, "resource_locks", _safe_locks(self.resource_locks))
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
    worktree_path: str | None = None
    diagnostic_path: str | None = None
    failure_class: str | None = None

    @property
    def lease_active(self) -> bool:
        return self.state is TaskState.LEASED and self.lease_owner is not None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["risk_class"] = self.risk_class.value
        result["state"] = self.state.value
        result["payload"] = dict(self.payload)
        result["dependency_ids"] = list(self.dependency_ids)
        result["allowed_paths"] = list(self.allowed_paths)
        result["prohibited_paths"] = list(self.prohibited_paths)
        result["resource_locks"] = list(self.resource_locks)
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
        )
        serialized = self.as_dict()
        return {field: serialized[field] for field in fields}


@dataclass(frozen=True)
class AuditEvent:
    """One append-only, hash-chained audit event."""

    event_id: int
    task_id: str | None
    event_type: str
    actor: str
    created_at: str
    from_state: str | None
    to_state: str | None
    details: Mapping[str, Any]
    previous_hash: str
    event_hash: str

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["details"] = dict(self.details)
        return result
