"""Canonical queue states and migration parsing for Night Shift."""

from __future__ import annotations

from enum import Enum

from .errors import InvalidTaskError


class TaskState(str, Enum):
    """Persistent states in the queue state machine."""

    BACKLOG = "BACKLOG"
    PENDING_APPROVAL = "BACKLOG"  # noqa: PIE796 - compatibility alias
    READY = "READY"
    QUEUED = "READY"  # noqa: PIE796 - compatibility alias
    RETRY_WAIT = "READY"  # noqa: PIE796 - compatibility alias
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    CLAIMED = "CLAIMED"
    LEASED = "CLAIMED"  # noqa: PIE796 - compatibility alias
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    PR_READY = "PR_READY"
    CEO_REVIEW = "CEO_REVIEW"
    BLOCKED = "BLOCKED"
    FAILED_SAFE = "FAILED_SAFE"
    FAILED = "FAILED_SAFE"  # noqa: PIE796 - compatibility alias
    DEAD_LETTER = "FAILED_SAFE"  # noqa: PIE796 - compatibility alias
    COMPLETED = "COMPLETED"
    SUCCEEDED = "COMPLETED"  # noqa: PIE796 - compatibility alias
    CANCELLED = "CANCELLED"


# A dependency is satisfied only when the task's work is actually present on
# the protected base.  PR_READY and CEO_REVIEW are delivery milestones, not
# proof that the change has been merged.
DEPENDENCY_SATISFIED_STATES = frozenset({TaskState.COMPLETED})


def state_from_value(value: str | TaskState) -> TaskState:
    """Parse canonical state values and migrate original V1 spellings."""

    if isinstance(value, TaskState):
        return value
    legacy = {
        "pending_approval": TaskState.BACKLOG,
        "queued": TaskState.READY,
        "retry_wait": TaskState.READY,
        "leased": TaskState.CLAIMED,
        "running": TaskState.RUNNING,
        "verifying": TaskState.VERIFYING,
        "succeeded": TaskState.COMPLETED,
        "pr_ready": TaskState.PR_READY,
        "ceo_review": TaskState.CEO_REVIEW,
        "failed": TaskState.FAILED_SAFE,
        "failed_safe": TaskState.FAILED_SAFE,
        "dead_letter": TaskState.FAILED_SAFE,
        "blocked": TaskState.BLOCKED,
        "cancelled": TaskState.CANCELLED,
    }
    try:
        if value in legacy:
            return legacy[value]
        return TaskState(value)
    except ValueError as exc:
        raise InvalidTaskError(f"unknown task state {value!r}") from exc
