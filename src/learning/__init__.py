"""Provider-neutral, append-only outcome-learning contracts.

This package is deliberately separate from betting, publication, provider
transport, and model activation.  It only validates immutable result and
prediction evidence and records their deterministic relationship.
"""

from .adapters import (
    AdapterError,
    attach_prediction,
    bundesliga2_prediction_from_record,
    football_result_from_record,
    generic_football_prediction_from_record,
    legacy_signal_prediction_from_record,
    nations_league_prediction_from_record,
    prediction_from_record,
    resolve_prediction_outcome,
    tennis_prediction_from_record,
    tennis_result_from_record,
    top5_prediction_from_record,
)
from .coverage import build_coverage_report
from .migration import MigrationPreview, preview_jsonl, preview_sources
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
from .prediction_store import (
    InMemoryPredictionStore,
    JsonlPredictionEvidenceStore,
    PredictionEvidenceStore,
    capture_predictions,
)
from .settlement import SettlementRunReport, settle_predictions

__all__ = [
    "ATTACHMENT_SCHEMA",
    "AUTHORITATIVE_RESULT_SCHEMA",
    "CAUSAL_TRAINING_ROW_SCHEMA",
    "PREDICTION_SCHEMA",
    "AdapterError",
    "AppendOnlyOutcomeStore",
    "AuthoritativeResultV1",
    "CausalTrainingRowV1",
    "InMemoryOutcomeStore",
    "InMemoryPredictionStore",
    "JsonlOutcomeStore",
    "JsonlPredictionEvidenceStore",
    "LifecycleError",
    "MigrationPreview",
    "OutcomeAttachmentV1",
    "PredictionEvidenceStore",
    "PredictionSnapshotV1",
    "RetrainDecision",
    "SettlementRunReport",
    "SettlementState",
    "append_training_rows",
    "attach_prediction",
    "build_coverage_report",
    "build_outcome_attachment",
    "bundesliga2_prediction_from_record",
    "canonical_digest",
    "capture_predictions",
    "decide_retrain",
    "football_result_from_record",
    "generic_football_prediction_from_record",
    "legacy_signal_prediction_from_record",
    "nations_league_prediction_from_record",
    "prediction_from_record",
    "preview_jsonl",
    "preview_sources",
    "resolve_prediction_outcome",
    "settle_predictions",
    "tennis_prediction_from_record",
    "tennis_result_from_record",
    "top5_prediction_from_record",
]
