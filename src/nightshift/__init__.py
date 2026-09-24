"""Builder 5 Night Shift dispatcher for governed SportsBrain development."""

from .audit import AuditIntegrityError
from .backpressure import (
    PullRequestClassification,
    classify_pull_request,
    paths_overlap,
    summarize_pull_requests,
)
from .bootstrap import (
    BootstrapProvider,
    MemoryV4BootstrapProvider,
    StaticBootstrapProvider,
)
from .control_repo import (
    ControlRepoLock,
    ControlRepoLockTimeout,
    control_repo_lock_path,
    fetch_control_repo,
    run_locked_control_repo_operation,
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
    APP_OWNERS,
    DISPATCHER_ID,
    AuditEvent,
    EventType,
    ExecutionResult,
    RiskClass,
    TaskRecord,
    TaskSpec,
    TaskState,
)
from .notifications import (
    NightShiftNotification,
    NightShiftNotificationWatcher,
    send_macos_notification,
)
from .policy import SafetyPolicy
from .quota import (
    DEFAULT_QUOTA_BACKOFF_SECONDS,
    MAX_QUOTA_BACKOFF_SECONDS,
    QuotaDetection,
    classify_quota_exhaustion,
    is_quota_failure_class,
    next_quota_eligible_at,
    normalize_reset_at,
)
from .reconciliation import (
    DeliveryBaseDrift,
    DeliveryReconciliationError,
    DriftClassification,
    classify_drift,
)
from .recovery import (
    DeliveryVerificationError,
    facts_verifier,
    verify_github_pull_request,
)
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
    "APP_OWNERS",
    "DEFAULT_QUOTA_BACKOFF_SECONDS",
    "DISPATCHER_ID",
    "MAX_QUOTA_BACKOFF_SECONDS",
    "ApprovalError",
    "AuditEvent",
    "AuditIntegrityError",
    "BootstrapProvider",
    "BuilderDefinition",
    "BuilderRegistry",
    "CodexExecutor",
    "ConfigurationError",
    "ControlRepoLock",
    "ControlRepoLockTimeout",
    "DeliveryBaseDrift",
    "DeliveryBlocked",
    "DeliveryError",
    "DeliveryPipeline",
    "DeliveryReconciliationError",
    "DeliveryVerificationError",
    "DispatcherRecursionError",
    "DispatcherStore",
    "DriftClassification",
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
    "NightShiftNotification",
    "NightShiftNotificationWatcher",
    "PullRequestClassification",
    "QuotaDetection",
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
    "classify_drift",
    "classify_pull_request",
    "classify_quota_exhaustion",
    "control_repo_lock_path",
    "facts_verifier",
    "fetch_control_repo",
    "is_quota_failure_class",
    "next_quota_eligible_at",
    "normalize_reset_at",
    "paths_overlap",
    "redact",
    "run_locked_control_repo_operation",
    "send_macos_notification",
    "summarize_pull_requests",
    "verify_github_pull_request",
]
