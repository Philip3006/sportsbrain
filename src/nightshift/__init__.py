"""Builder 5 Night Shift dispatcher for governed SportsBrain development."""

from .audit import AuditIntegrityError
from .bootstrap import (
    BootstrapProvider,
    MemoryV4BootstrapProvider,
    StaticBootstrapProvider,
)
from .delivery import DeliveryPipeline, GhPullRequestClient
from .dispatcher import NightShiftDispatcher
from .errors import (
    ApprovalError,
    ConfigurationError,
    DeliveryBlocked,
    DeliveryError,
    DispatcherRecursionError,
    ExecutorTimeout,
    ExecutorUnavailable,
    IdempotencyConflictError,
    InvalidTaskError,
    InvalidTransitionError,
    LeaseError,
    NightShiftError,
    SafetyViolation,
    ScopeViolation,
    TaskNotFoundError,
    UnknownBuilderError,
    WorktreeSafetyError,
)
from .executors import CodexExecutor, FakeExecutor, redact
from .models import (
    DISPATCHER_ID,
    AuditEvent,
    EventType,
    ExecutionResult,
    RiskClass,
    TaskRecord,
    TaskSpec,
    TaskState,
)
from .policy import SafetyPolicy
from .registry import BuilderDefinition, BuilderRegistry
from .roadmap import RoadmapItem, RoadmapRegistry
from .store import ALLOWED_TRANSITIONS, DispatcherStore
from .templates import TaskTemplate, TemplateRegistry
from .verification import VerificationResult, VerificationRunner
from .worktree import (
    RuntimeDirtyEvidence,
    RuntimeDirtyPolicy,
    WorktreeAllocation,
    WorktreeManager,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "DISPATCHER_ID",
    "ApprovalError",
    "AuditEvent",
    "AuditIntegrityError",
    "BootstrapProvider",
    "BuilderDefinition",
    "BuilderRegistry",
    "CodexExecutor",
    "ConfigurationError",
    "DeliveryBlocked",
    "DeliveryError",
    "DeliveryPipeline",
    "DispatcherRecursionError",
    "DispatcherStore",
    "EventType",
    "ExecutionResult",
    "ExecutorTimeout",
    "ExecutorUnavailable",
    "FakeExecutor",
    "GhPullRequestClient",
    "IdempotencyConflictError",
    "InvalidTaskError",
    "InvalidTransitionError",
    "LeaseError",
    "MemoryV4BootstrapProvider",
    "NightShiftDispatcher",
    "NightShiftError",
    "RiskClass",
    "RoadmapItem",
    "RoadmapRegistry",
    "RuntimeDirtyEvidence",
    "RuntimeDirtyPolicy",
    "SafetyPolicy",
    "SafetyViolation",
    "ScopeViolation",
    "StaticBootstrapProvider",
    "TaskNotFoundError",
    "TaskRecord",
    "TaskSpec",
    "TaskState",
    "TaskTemplate",
    "TemplateRegistry",
    "UnknownBuilderError",
    "VerificationResult",
    "VerificationRunner",
    "WorktreeAllocation",
    "WorktreeManager",
    "WorktreeSafetyError",
    "redact",
]
