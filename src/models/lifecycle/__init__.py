"""Domain-neutral, append-only model lifecycle contracts.

The package deliberately contains no provider, publication, betting, ledger,
or scheduler integration.  Adapters supply immutable training evidence and
decide when to call it; this package only makes releases and active pointers
auditable and fail-closed.
"""

from .contracts import (
    ACTIVE,
    REJECTED,
    TRAINING,
    VALIDATED,
    ActivationReceipt,
    ActiveModelPointer,
    LifecycleError,
    ModelHealth,
    ModelLifecycle,
    ModelRelease,
    RetrainReceipt,
    TrainingSnapshot,
    canonical_digest,
    validate_causal_training_rows,
)

__all__ = [
    "ACTIVE",
    "REJECTED",
    "TRAINING",
    "VALIDATED",
    "ActivationReceipt",
    "ActiveModelPointer",
    "LifecycleError",
    "ModelHealth",
    "ModelLifecycle",
    "ModelRelease",
    "RetrainReceipt",
    "TrainingSnapshot",
    "canonical_digest",
    "validate_causal_training_rows",
]
