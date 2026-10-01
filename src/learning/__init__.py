"""Provider-neutral, append-only outcome-learning contracts.

This package is deliberately separate from betting, publication, provider
transport, and model activation.  It only validates immutable result and
prediction evidence and records their deterministic relationship.
"""

from .outcome_contracts import (
    ATTACHMENT_SCHEMA,
    AUTHORITATIVE_RESULT_SCHEMA,
    CAUSAL_TRAINING_ROW_SCHEMA,
    PREDICTION_SCHEMA,
    AppendOnlyOutcomeStore,
    AuthoritativeResultV1,
    CausalTrainingRowV1,
    InMemoryOutcomeStore,
    JsonlOutcomeStore,
    LifecycleError,
    OutcomeAttachmentV1,
    PredictionSnapshotV1,
    RetrainDecision,
    SettlementState,
    append_training_rows,
    build_outcome_attachment,
    canonical_digest,
    decide_retrain,
)

__all__ = [
    "ATTACHMENT_SCHEMA",
    "AUTHORITATIVE_RESULT_SCHEMA",
    "CAUSAL_TRAINING_ROW_SCHEMA",
    "PREDICTION_SCHEMA",
    "AppendOnlyOutcomeStore",
    "AuthoritativeResultV1",
    "CausalTrainingRowV1",
    "InMemoryOutcomeStore",
    "JsonlOutcomeStore",
    "LifecycleError",
    "OutcomeAttachmentV1",
    "PredictionSnapshotV1",
    "RetrainDecision",
    "SettlementState",
    "append_training_rows",
    "build_outcome_attachment",
    "canonical_digest",
    "decide_retrain",
]
