"""Deterministic, provider-free universal settlement orchestration.

The orchestrator consumes already validated prediction and result contracts.  It
does not discover fixtures, fetch results, publish signals, train models, or
write a betting ledger.  Persistence is opt-in and is performed only through
the append-only outcome-store protocol after the complete batch has been
validated in memory.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.learning.adapters import attach_prediction
from src.learning.outcome_contracts import (
    AppendOnlyOutcomeStore,
    AuthoritativeResultV1,
    CausalTrainingRowV1,
    InMemoryOutcomeStore,
    LifecycleError,
    OutcomeAttachmentV1,
    PredictionSnapshotV1,
    canonical_digest,
)


def _utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LifecycleError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise LifecycleError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _unique_predictions(
    predictions: Iterable[PredictionSnapshotV1],
) -> tuple[PredictionSnapshotV1, ...]:
    by_id: dict[str, PredictionSnapshotV1] = {}
    by_source: dict[str, PredictionSnapshotV1] = {}
    for prediction in predictions:
        prior = by_id.get(prediction.prediction_id)
        if prior is not None and prior.to_payload() != prediction.to_payload():
            raise LifecycleError("conflicting prediction replay")
        by_id[prediction.prediction_id] = prediction
        if prediction.source_record_id is not None:
            source_prior = by_source.get(prediction.source_record_id)
            if (
                source_prior is not None
                and source_prior.to_payload() != prediction.to_payload()
            ):
                raise LifecycleError("conflicting prediction source identity")
            by_source[prediction.source_record_id] = prediction
    return tuple(by_id[key] for key in sorted(by_id))


def _unique_results(
    results: Iterable[AuthoritativeResultV1],
) -> tuple[AuthoritativeResultV1, ...]:
    by_id: dict[str, AuthoritativeResultV1] = {}
    by_fixture: dict[str, AuthoritativeResultV1] = {}
    by_source: dict[tuple[str, str], AuthoritativeResultV1] = {}
    for result in results:
        prior = by_id.get(result.result_id)
        if prior is not None and prior.to_payload() != result.to_payload():
            raise LifecycleError("conflicting authoritative result replay")
        fixture_prior = by_fixture.get(result.fixture_id)
        if (
            fixture_prior is not None
            and fixture_prior.to_payload() != result.to_payload()
        ):
            raise LifecycleError("conflicting authoritative results for fixture")
        source_key = (result.source, result.source_record_id)
        source_prior = by_source.get(source_key)
        if (
            source_prior is not None
            and source_prior.to_payload() != result.to_payload()
        ):
            raise LifecycleError("conflicting authoritative result source identity")
        by_id[result.result_id] = result
        by_fixture[result.fixture_id] = result
        by_source[source_key] = result
    return tuple(by_id[key] for key in sorted(by_id))


def _attachment_provenance_digest(
    prediction: PredictionSnapshotV1, result: AuthoritativeResultV1
) -> str:
    return canonical_digest(
        {
            "schema": "sportsbrain-settlement-provenance-v1",
            "prediction_id": prediction.prediction_id,
            "result_id": result.result_id,
            "result_provenance_digest": result.provenance_digest,
        }
    )


def _causal_row(
    prediction: PredictionSnapshotV1,
    result: AuthoritativeResultV1,
) -> CausalTrainingRowV1 | None:
    payload = prediction.prediction
    causal_fields = {"features", "feature_available_at", "training_cutoff"}
    present = causal_fields.intersection(payload)
    if not present:
        return None
    if present != causal_fields:
        raise LifecycleError("causal feature provenance is incomplete")
    features = payload.get("features")
    if not isinstance(features, Mapping):
        raise LifecycleError("causal features must be an object")
    feature_available_at = str(payload["feature_available_at"])
    training_cutoff = str(payload["training_cutoff"])
    feature_digest = canonical_digest(features)
    identity = {
        "schema": "sportsbrain-causal-training-row-v1",
        "fixture_id": prediction.fixture_id,
        "sport": prediction.sport,
        "competition": prediction.competition,
        "training_cutoff": training_cutoff,
        "feature_available_at": feature_available_at,
        "event_completed_at": result.completed_at,
        "result_safe_available_at": result.result_safe_available_at,
        "feature_digest": feature_digest,
        "result_id": result.result_id,
    }
    row_id = canonical_digest(identity)
    without_digest = {
        **identity,
        "features": dict(features),
        "label": dict(result.actual_result),
        "row_id": row_id,
    }
    return CausalTrainingRowV1(
        row_id=row_id,
        fixture_id=prediction.fixture_id,
        sport=prediction.sport,
        competition=prediction.competition,
        training_cutoff=training_cutoff,
        feature_available_at=feature_available_at,
        event_completed_at=result.completed_at,
        result_safe_available_at=result.result_safe_available_at,
        features=dict(features),
        label=dict(result.actual_result),
        result_id=result.result_id,
        feature_digest=feature_digest,
        row_digest=canonical_digest(without_digest),
    )


@dataclass(frozen=True)
class SettlementRunReport:
    """Deterministic summary of one offline settlement pass."""

    predictions_emitted: int
    predictions_represented: int
    awaiting_result: int
    settled: int
    settlement_states: dict[str, int]
    causal_training_rows_generated: int
    result_conflicts: int
    prediction_ids: tuple[str, ...]
    attachment_ids: tuple[str, ...]
    causal_row_ids: tuple[str, ...]
    attachments: tuple[OutcomeAttachmentV1, ...]
    causal_rows: tuple[CausalTrainingRowV1, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "sportsbrain-universal-settlement-report-v1",
            "predictions_emitted": self.predictions_emitted,
            "predictions_represented": self.predictions_represented,
            "awaiting_result": self.awaiting_result,
            "settled": self.settled,
            "settlement_states": dict(sorted(self.settlement_states.items())),
            "causal_training_rows_generated": self.causal_training_rows_generated,
            "result_conflicts": self.result_conflicts,
            "prediction_ids": list(self.prediction_ids),
            "attachment_ids": list(self.attachment_ids),
            "causal_row_ids": list(self.causal_row_ids),
            "attachments": [item.to_payload() for item in self.attachments],
            "causal_rows": [item.to_payload() for item in self.causal_rows],
        }


def _existing_store_records(
    store: AppendOnlyOutcomeStore | None,
) -> tuple[dict[str, AuthoritativeResultV1], dict[str, OutcomeAttachmentV1]]:
    if store is None:
        return {}, {}
    if isinstance(store, InMemoryOutcomeStore):
        return dict(store.results), dict(store.attachments)
    loader = getattr(store, "load", None)
    if loader is None:
        return {}, {}
    existing_results, existing_attachments = loader()
    return (
        {item.result_id: item for item in existing_results},
        {item.attachment_id: item for item in existing_attachments},
    )


def settle_predictions(
    predictions: Iterable[PredictionSnapshotV1],
    results: Iterable[AuthoritativeResultV1],
    *,
    settled_at: str,
    store: AppendOnlyOutcomeStore | None = None,
    write: bool = False,
) -> SettlementRunReport:
    """Validate and optionally append one provider-free settlement batch.

    All predictions, results, attachments, and causal rows are built before an
    explicit write.  An omitted store or ``write=False`` is always read-only.
    """

    _utc(settled_at, "settled_at")
    if write and store is None:
        raise LifecycleError("explicit write requires an outcome store")
    normalized_predictions = _unique_predictions(predictions)
    normalized_results = _unique_results(results)
    prediction_fixtures = {item.fixture_id for item in normalized_predictions}
    if any(item.fixture_id not in prediction_fixtures for item in normalized_results):
        raise LifecycleError("result fixture is not represented by a prediction")
    results_by_fixture = {item.fixture_id: item for item in normalized_results}
    attachments: list[OutcomeAttachmentV1] = []
    causal_rows: list[CausalTrainingRowV1] = []
    awaiting = 0
    for prediction in normalized_predictions:
        result = results_by_fixture.get(prediction.fixture_id)
        if result is None:
            awaiting += 1
            continue
        attachment = attach_prediction(
            prediction,
            result,
            settled_at=settled_at,
            provenance_digest=_attachment_provenance_digest(prediction, result),
        )
        attachments.append(attachment)
        row = _causal_row(prediction, result)
        if row is not None:
            causal_rows.append(row)

    existing_results, existing_attachments = _existing_store_records(store)
    for result in normalized_results:
        prior = existing_results.get(result.result_id)
        if prior is not None and prior.to_payload() != result.to_payload():
            raise LifecycleError("conflicting authoritative result replay")
        for existing in existing_results.values():
            if (
                existing.fixture_id == result.fixture_id
                and existing.to_payload() != result.to_payload()
            ):
                raise LifecycleError("conflicting authoritative results for fixture")
            if (
                existing.source == result.source
                and existing.source_record_id == result.source_record_id
                and existing.to_payload() != result.to_payload()
            ):
                raise LifecycleError("conflicting authoritative result source identity")
    for attachment in attachments:
        prior = existing_attachments.get(attachment.attachment_id)
        if prior is not None and prior.to_payload() != attachment.to_payload():
            raise LifecycleError("conflicting outcome attachment replay")
        for existing in existing_attachments.values():
            if (
                existing.prediction_id == attachment.prediction_id
                and existing.to_payload() != attachment.to_payload()
            ):
                raise LifecycleError("prediction already has a conflicting attachment")

    if write and store is not None:
        for result in normalized_results:
            store.append_result(result)
        for attachment in attachments:
            store.append_attachment(attachment)

    represented_ids = {
        attachment.prediction_id for attachment in existing_attachments.values()
    }
    represented_ids.update(attachment.prediction_id for attachment in attachments)
    states = Counter(attachment.settlement_state for attachment in attachments)
    return SettlementRunReport(
        predictions_emitted=len(normalized_predictions),
        predictions_represented=len(
            represented_ids.intersection(
                {item.prediction_id for item in normalized_predictions}
            )
        ),
        awaiting_result=awaiting,
        settled=len(attachments),
        settlement_states=dict(states),
        causal_training_rows_generated=len(causal_rows),
        result_conflicts=0,
        prediction_ids=tuple(item.prediction_id for item in normalized_predictions),
        attachment_ids=tuple(item.attachment_id for item in attachments),
        causal_row_ids=tuple(item.row_id for item in causal_rows),
        attachments=tuple(attachments),
        causal_rows=tuple(causal_rows),
    )
