"""Errors raised by the governed SportsBrain Night Shift dispatcher."""

from __future__ import annotations


class NightShiftError(Exception):
    """Base class for dispatcher errors."""


class ConfigurationError(NightShiftError):
    """The explicit governance configuration is invalid."""


class UnknownBuilderError(NightShiftError):
    """A task names a builder that is not in the governed registry."""


class DispatcherRecursionError(NightShiftError):
    """Builder 5 was addressed as a worker or execution target."""


class SafetyViolation(NightShiftError):
    """A task violates the dispatcher's fail-closed safety policy."""


class InvalidTaskError(NightShiftError):
    """A task envelope is malformed or outside configured limits."""


class InvalidTransitionError(NightShiftError):
    """A task state transition is not permitted."""


class TaskNotFoundError(NightShiftError):
    """The requested task does not exist."""


class LeaseError(NightShiftError):
    """A lease is missing, expired, or owned by another worker."""


class ApprovalError(NightShiftError):
    """An approval action cannot be applied to the task."""


class IdempotencyConflictError(NightShiftError):
    """An idempotency key was reused with a different task envelope."""


class ExecutorUnavailable(NightShiftError):
    """The requested worker executor is not available in this environment."""


class ExecutorTimeout(NightShiftError):
    """A worker executor exceeded its bounded timeout."""


class WorktreeSafetyError(NightShiftError):
    """A canonical checkout or isolated worktree failed a safety check."""


class ScopeViolation(WorktreeSafetyError):
    """A worker changed a path outside its allowed scope."""
