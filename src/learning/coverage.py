"""Read-only coverage metrics for universal prediction settlement."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from statistics import median
from typing import Any

from src.learning.outcome_contracts import OutcomeAttachmentV1, PredictionSnapshotV1


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def build_coverage_report(
    predictions: Iterable[PredictionSnapshotV1],
    attachments: Iterable[OutcomeAttachmentV1],
    *,
    result_conflicts: int = 0,
) -> dict[str, Any]:
    """Return deterministic total and sport/competition/model segment metrics."""

    normalized_predictions = tuple(predictions)
    normalized_attachments = tuple(attachments)
    attachment_by_prediction = {
        attachment.prediction_id: attachment for attachment in normalized_attachments
    }
    settled_predictions = [
        prediction
        for prediction in normalized_predictions
        if prediction.prediction_id in attachment_by_prediction
    ]
    lags = [
        (
            _utc(attachment.settled_at) - _utc(prediction.prediction_timestamp)
        ).total_seconds()
        for prediction in settled_predictions
        for attachment in [attachment_by_prediction[prediction.prediction_id]]
    ]
    unresolved = [
        _utc(prediction.prediction_timestamp)
        for prediction in normalized_predictions
        if prediction.prediction_id not in attachment_by_prediction
    ]
    states = Counter(
        attachment.settlement_state for attachment in normalized_attachments
    )
    segments: dict[tuple[str, str, str, str, str], dict[str, int]] = defaultdict(
        lambda: {
            "predictions_emitted": 0,
            "predictions_represented": 0,
            "settled": 0,
        }
    )
    for prediction in normalized_predictions:
        key = (
            prediction.sport,
            prediction.competition,
            prediction.model_family,
            prediction.model_release_id,
            prediction.phase,
        )
        segments[key]["predictions_emitted"] += 1
        if prediction.prediction_id in attachment_by_prediction:
            segments[key]["predictions_represented"] += 1
            segments[key]["settled"] += 1

    segment_payload = []
    for key in sorted(segments):
        sport, competition, model_family, model_release_id, phase = key
        segment_payload.append(
            {
                "sport": sport,
                "competition": competition,
                "model_family": model_family,
                "model_release_id": model_release_id,
                "phase": phase,
                **segments[key],
            }
        )
    return {
        "schema": "sportsbrain-universal-outcome-coverage-v1",
        "predictions_emitted": len(normalized_predictions),
        "predictions_represented": len(
            set(attachment_by_prediction).intersection(
                {prediction.prediction_id for prediction in normalized_predictions}
            )
        ),
        "awaiting_result": len(normalized_predictions)
        - len(
            set(attachment_by_prediction).intersection(
                {prediction.prediction_id for prediction in normalized_predictions}
            )
        ),
        "settled": len(normalized_attachments),
        "settlement_states": dict(sorted(states.items())),
        "settlement_coverage_percent": (
            round(100 * len(settled_predictions) / len(normalized_predictions), 2)
            if normalized_predictions
            else 0.0
        ),
        "median_settlement_lag_seconds": median(lags) if lags else None,
        "oldest_unresolved_prediction_timestamp": (
            min(unresolved).isoformat().replace("+00:00", "Z") if unresolved else None
        ),
        "result_conflicts": result_conflicts,
        "segments": segment_payload,
    }
