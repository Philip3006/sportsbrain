"""Frozen, research-only Nations League v1 contract.

This module is deliberately separate from the production football runtime.  It
adapts the repository's existing causal Elo implementation and adds the
point-in-time and shadow-only gates needed for a future forward evaluation.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from src.config import ELO_K_BASE, TOURNAMENT_K_FACTORS
from src.models.elo import (
    DRAW_BAND,
    ELO_DEFAULT,
    HOME_ADVANTAGE,
    compute_elo_series,
    elo_win_probability,
)

MODEL_VERSION = "nations_league_v1"
SCHEMA_VERSION = "nations-league-v1-forward-shadow-v1"
FORWARD_RUN_SCHEMA_VERSION = "nations-league-v1-forward-shadow-run-v1"
METRICS_SCHEMA_VERSION = "nations-league-v1-forward-shadow-metrics-v1"
COMPETITION = "UEFA Nations League"
IMPLEMENTATION_SOURCE = "src.models.elo:compute_elo_series,elo_win_probability"
IMPLEMENTATION_SOURCE_SHA = "289ccd07e266763aa0b869487bd3abb1ac4d7966"

UTC = timezone.utc
INITIAL_WINDOW = (timedelta(hours=22), timedelta(hours=26))
REFINEMENT_WINDOW = (timedelta(minutes=60), timedelta(minutes=120))

ALLOWED_COMPETITIVE_TRAINING = frozenset(
    {
        "UEFA Nations League",
        "UEFA Euro qualification",
        "UEFA Euro",
        "FIFA World Cup qualification",
        "UEFA competitive",
    }
)

MODEL_SPEC: dict[str, Any] = {
    "model_version": MODEL_VERSION,
    "status": "RESEARCH_CANDIDATE_NOT_PRODUCTION_CERTIFIED",
    "family": "causal_elo",
    "implementation_source": IMPLEMENTATION_SOURCE,
    "implementation_source_sha": IMPLEMENTATION_SOURCE_SHA,
    "initial_rating": ELO_DEFAULT,
    "home_advantage_elo": HOME_ADVANTAGE,
    "draw_band_elo": DRAW_BAND,
    "competitive_k_base": ELO_K_BASE,
    "nations_league_k_factor": TOURNAMENT_K_FACTORS[COMPETITION],
    "friendly_k": 20.0,
    "neutral_handling": "home advantage is zero when neutral=true",
    "edition_handling": "one chronological rating state; edition is a provenance field, never a reset trigger",
    "result_update": "existing repository Elo goal-difference multiplier and 1X2 score update",
    "probability_generation": "existing elo_win_probability returns normalized (home, draw, away)",
    "calibration": "none; no unvalidated post-hoc calibration",
    "training_scope": sorted(ALLOWED_COMPETITIVE_TRAINING),
    "friendlies": "excluded from v1 training; no sensitivity result is promoted",
    "excluded_inputs": [
        "GBT",
        "context features",
        "market odds",
        "squad or lineup state",
        "subjective motivation",
        "unsupported tie-break state",
    ],
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def model_digest() -> str:
    return sha256_json(MODEL_SPEC)


def deterministic_record_id(fixture_id: str, phase: str) -> str:
    """Stable forward-shadow record identity; it is never a random UUID."""
    return sha256_json(
        {"fixture_id": fixture_id, "phase": phase, "model_digest": model_digest()}
    )


def _parse_utc(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must carry UTC timezone")
    return parsed.astimezone(UTC)


def _require_digest(value: Any, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def validate_fixture_record(record: dict[str, Any]) -> None:
    required = {
        "fixture_id",
        "edition",
        "evaluation_block",
        "home_team",
        "away_team",
        "kickoff_utc",
        "competition",
        "source_provenance",
        "source_digest",
    }
    missing = sorted(required - record.keys())
    if missing:
        raise ValueError(f"fixture missing fields: {', '.join(missing)}")
    for field in (
        "fixture_id",
        "edition",
        "evaluation_block",
        "home_team",
        "away_team",
    ):
        if not isinstance(record[field], str) or not record[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    if record["competition"] != COMPETITION:
        raise ValueError("fixture competition is not UEFA Nations League")
    if record["home_team"] == record["away_team"]:
        raise ValueError("fixture cannot have identical teams")
    _parse_utc(record["kickoff_utc"], "kickoff_utc")
    if (
        not isinstance(record["source_provenance"], str)
        or not record["source_provenance"].strip()
    ):
        raise ValueError("source_provenance is required")
    _require_digest(record["source_digest"], "source_digest")
    if "neutral" in record and not isinstance(record["neutral"], bool):
        raise ValueError("neutral must be boolean when present")
    if record.get("administrative_exception"):
        if record.get("result_safe_available_at") is not None:
            raise ValueError(
                "administrative exception cannot claim result-safe availability"
            )
        if (
            not isinstance(record.get("administrative_exception_reason"), str)
            or not record["administrative_exception_reason"].strip()
        ):
            raise ValueError("administrative exception reason is required")
    else:
        if "result_safe_available_at" not in record:
            raise ValueError("result_safe_available_at is required")
        _parse_utc(record["result_safe_available_at"], "result_safe_available_at")


def validate_point_in_time_training(
    records: Iterable[dict[str, Any]], prediction_cutoff: str
) -> list[dict[str, Any]]:
    """Return only result-safe, causal training rows; reject any leakage."""
    cutoff = _parse_utc(prediction_cutoff, "prediction_cutoff")
    validated: list[dict[str, Any]] = []
    for record in records:
        validate_fixture_record(record)
        if record.get("administrative_exception"):
            continue
        safe_at = _parse_utc(
            record["result_safe_available_at"], "result_safe_available_at"
        )
        if not safe_at < cutoff:
            raise ValueError(
                "training result_safe_available_at must be strictly before prediction_cutoff"
            )
        if record["competition"] not in ALLOWED_COMPETITIVE_TRAINING:
            raise ValueError(
                f"unsupported training competition: {record['competition']}"
            )
        if not isinstance(record.get("home_score"), int) or not isinstance(
            record.get("away_score"), int
        ):
            raise TypeError("causal training rows require integer final scores")
        validated.append(record)
    return sorted(
        validated, key=lambda row: (row["result_safe_available_at"], row["fixture_id"])
    )


def validate_target_cutoff(target_kickoff: str, prediction_cutoff: str) -> None:
    cutoff = _parse_utc(prediction_cutoff, "prediction_cutoff")
    kickoff = _parse_utc(target_kickoff, "target_kickoff")
    if not cutoff < kickoff:
        raise ValueError("prediction_cutoff must be strictly before target kickoff")


def fit_causal_elo(
    records: Iterable[dict[str, Any]], prediction_cutoff: str
) -> dict[str, float]:
    rows = validate_point_in_time_training(records, prediction_cutoff)
    frame = pd.DataFrame(
        [
            {
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "home_score": row["home_score"],
                "away_score": row["away_score"],
                "tournament": row["competition"],
                "neutral": bool(row.get("neutral", False)),
            }
            for row in rows
        ]
    )
    if frame.empty:
        return {}
    series = compute_elo_series(frame)
    ratings: dict[str, float] = {}
    for _, row in series.iterrows():
        ratings[row["home_team"]] = float(row["elo_home_post"])
        ratings[row["away_team"]] = float(row["elo_away_post"])
    return ratings


def predict_1x2(
    ratings: dict[str, float], home_team: str, away_team: str, neutral: bool = False
) -> dict[str, float]:
    if not home_team or not away_team or home_team == away_team:
        raise ValueError("distinct home and away teams are required")
    p_home, p_draw, p_away = elo_win_probability(
        ratings.get(home_team, ELO_DEFAULT),
        ratings.get(away_team, ELO_DEFAULT),
        neutral=neutral,
    )
    return {"home": p_home, "draw": p_draw, "away": p_away}


def validate_lifecycle_timestamp(
    phase: str, prediction_timestamp: str, kickoff_utc: str
) -> None:
    prediction = _parse_utc(prediction_timestamp, "prediction_timestamp")
    kickoff = _parse_utc(kickoff_utc, "kickoff_utc")
    delta = kickoff - prediction
    if phase == "initial":
        if not INITIAL_WINDOW[0] <= delta <= INITIAL_WINDOW[1]:
            raise ValueError("initial prediction is outside the 22-26 hour window")
    elif phase == "refinement":
        if not REFINEMENT_WINDOW[0] <= delta <= REFINEMENT_WINDOW[1]:
            raise ValueError(
                "refinement prediction is outside the 60-120 minute window"
            )
    elif phase == "closing_benchmark":
        if delta <= timedelta(0):
            raise ValueError("closing benchmark must be pre-kickoff")
    else:
        raise ValueError(f"unknown lifecycle phase: {phase}")


def validate_shadow_record(record: dict[str, Any]) -> None:
    required = {
        "record_id",
        "fixture_id",
        "phase",
        "kickoff_utc",
        "prediction_timestamp",
        "model_version",
        "model_digest",
        "probabilities",
        "source_evidence",
    }
    missing = sorted(required - record.keys())
    if missing:
        raise ValueError(f"shadow record missing fields: {', '.join(missing)}")
    if (
        record["model_version"] != MODEL_VERSION
        or record["signal_status"] != "SHADOW_ONLY"
    ):
        raise ValueError(
            "shadow record must use the frozen v1 model and SHADOW_ONLY status"
        )
    if record.get("shadow") is not True or record.get("no_bet") is not True:
        raise ValueError("shadow record must remain shadow and no-bet")
    if (
        record.get("publication_enabled") is not False
        or record.get("ledger_mutation") is not False
    ):
        raise ValueError("shadow record cannot publish or mutate ledger")
    if record.get("is_actionable_value_signal") is True:
        raise ValueError("shadow record cannot be actionable")
    forbidden_inputs = {
        "gbt",
        "context_features",
        "market_odds",
        "squad",
        "lineups",
        "motivation",
    }
    unexpected = sorted(forbidden_inputs.intersection(record))
    if unexpected:
        raise ValueError(
            f"frozen v1 record contains excluded inputs: {', '.join(unexpected)}"
        )
    if record["model_digest"] != model_digest():
        raise ValueError("shadow record model digest mismatch")
    validate_lifecycle_timestamp(
        record["phase"], record["prediction_timestamp"], record["kickoff_utc"]
    )
    probabilities = record["probabilities"]
    if set(probabilities) != {"home", "draw", "away"} or any(
        not 0.0 <= float(v) <= 1.0 for v in probabilities.values()
    ):
        raise ValueError("probabilities must contain bounded home/draw/away values")
    if abs(sum(float(v) for v in probabilities.values()) - 1.0) > 1e-9:
        raise ValueError("probabilities must sum to one")
    if not isinstance(record["source_evidence"], list) or not record["source_evidence"]:
        raise ValueError("source evidence is required")


def append_shadow_record(
    existing: Iterable[dict[str, Any]], record: dict[str, Any]
) -> list[dict[str, Any]]:
    """Append once; never replace an existing fixture/phase record."""
    validate_shadow_record(record)
    result = deepcopy(list(existing))
    keys = {(item.get("fixture_id"), item.get("phase")) for item in result}
    if (
        record["record_id"] in {item.get("record_id") for item in result}
        or (record["fixture_id"], record["phase"]) in keys
    ):
        raise ValueError("forward shadow records are append-only")
    result.append(deepcopy(record))
    return result


def _validate_digest(value: Any, field: str) -> None:
    _require_digest(value, field)


def validate_target_fixture(fixture: dict[str, Any]) -> None:
    """Validate the canonical identity needed for a future target fixture.

    Future targets do not have a result-safe timestamp yet.  That field is
    therefore optional here and is validated only when settlement occurs.
    """

    required = {
        "fixture_id",
        "edition",
        "evaluation_block",
        "home_team",
        "away_team",
        "kickoff_utc",
        "competition",
        "source_provenance",
        "source_digest",
    }
    missing = sorted(required - fixture.keys())
    if missing:
        raise ValueError(f"target fixture missing fields: {', '.join(missing)}")
    for field in (
        "fixture_id",
        "edition",
        "evaluation_block",
        "home_team",
        "away_team",
        "source_provenance",
    ):
        if not isinstance(fixture[field], str) or not fixture[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    if fixture["competition"] != COMPETITION:
        raise ValueError("fixture competition is not UEFA Nations League")
    if fixture["home_team"] == fixture["away_team"]:
        raise ValueError("fixture cannot have identical teams")
    _parse_utc(fixture["kickoff_utc"], "kickoff_utc")
    _validate_digest(fixture["source_digest"], "source_digest")
    if fixture.get("administrative_exception"):
        raise ValueError("administrative fixtures cannot be forward-shadow targets")
    if "neutral" in fixture and not isinstance(fixture["neutral"], bool):
        raise ValueError("neutral must be boolean when present")


def _validate_input_provenance(provenance: dict[str, Any]) -> None:
    if not isinstance(provenance, dict):
        raise TypeError("input_provenance must be an object")
    for field in ("timeline_digest", "fixture_source_digest"):
        _validate_digest(provenance.get(field), f"input_provenance.{field}")
    forbidden = {
        "odds",
        "market_odds",
        "closing_odds",
        "provider_response",
        "provider_request",
        "api_key",
        "credential",
    }

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in forbidden:
                    raise ValueError(
                        f"input provenance contains forbidden field: {key}"
                    )
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(provenance)


def build_forward_shadow_prediction(
    fixture: dict[str, Any],
    *,
    phase: str,
    prediction_timestamp: str,
    training_records: Iterable[dict[str, Any]],
    input_provenance: dict[str, Any],
) -> dict[str, Any]:
    """Build one deterministic v1 prediction without network or market input."""

    validate_target_fixture(fixture)
    _validate_input_provenance(input_provenance)
    validate_target_cutoff(fixture["kickoff_utc"], prediction_timestamp)
    validate_lifecycle_timestamp(phase, prediction_timestamp, fixture["kickoff_utc"])
    ratings = fit_causal_elo(training_records, prediction_timestamp)
    probabilities = predict_1x2(
        ratings,
        fixture["home_team"],
        fixture["away_team"],
        neutral=bool(fixture.get("neutral", False)),
    )
    record = {
        "record_type": "prediction",
        "record_id": deterministic_record_id(fixture["fixture_id"], phase),
        "fixture_id": fixture["fixture_id"],
        "edition": fixture["edition"],
        "evaluation_block": fixture["evaluation_block"],
        "competition": COMPETITION,
        "home_team": fixture["home_team"],
        "away_team": fixture["away_team"],
        "kickoff_utc": fixture["kickoff_utc"],
        "phase": phase,
        "prediction_timestamp": prediction_timestamp,
        "model_version": MODEL_VERSION,
        "model_digest": model_digest(),
        "probabilities": probabilities,
        "neutral": bool(fixture.get("neutral", False)),
        "source_provenance": fixture["source_provenance"],
        "source_digest": fixture["source_digest"],
        "target_result_safe_available_at": fixture.get("result_safe_available_at"),
        "input_provenance": deepcopy(input_provenance),
        "input_provenance_digest": sha256_json(input_provenance),
        "source_evidence": [
            "canonical_fixture_timeline",
            "causal_elo_training_cutoff",
        ],
        "eventual_result": None,
        "brier_score": None,
        "log_loss": None,
        "calibration_bucket": None,
        "shadow": True,
        "signal_status": "SHADOW_ONLY",
        "no_bet": True,
        "publication_enabled": False,
        "ledger_mutation": False,
        "is_actionable_value_signal": False,
    }
    validate_shadow_record(record)
    return record


def _outcome(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "home"
    if home_score < away_score:
        return "away"
    return "draw"


def build_shadow_settlement(
    prediction: dict[str, Any],
    *,
    home_score: int,
    away_score: int,
    result_safe_available_at: str,
    settled_at: str,
    result_provenance: str,
) -> dict[str, Any]:
    """Create an append-only settlement event; the prediction is never edited."""

    validate_shadow_record(prediction)
    if prediction.get("record_type", "prediction") != "prediction":
        raise ValueError("settlement requires a prediction record")
    if (
        not isinstance(home_score, int)
        or isinstance(home_score, bool)
        or home_score < 0
        or not isinstance(away_score, int)
        or isinstance(away_score, bool)
        or away_score < 0
    ):
        raise ValueError("scores must be non-negative integers")
    safe_at = _parse_utc(result_safe_available_at, "result_safe_available_at")
    settled = _parse_utc(settled_at, "settled_at")
    kickoff = _parse_utc(prediction["kickoff_utc"], "kickoff_utc")
    if safe_at < kickoff:
        raise ValueError("result-safe time cannot precede kickoff")
    expected_safe_at = prediction.get("target_result_safe_available_at")
    if expected_safe_at is not None and result_safe_available_at != expected_safe_at:
        raise ValueError(
            "settlement result-safe time does not match fixture provenance"
        )
    if settled < safe_at:
        raise ValueError("settlement must occur after result-safe time")
    if not isinstance(result_provenance, str) or not result_provenance.strip():
        raise ValueError("result provenance is required")
    outcome = _outcome(home_score, away_score)
    settlement_identity = {
        "prediction_record_id": prediction["record_id"],
        "result_safe_available_at": result_safe_available_at,
        "home_score": home_score,
        "away_score": away_score,
    }
    return {
        "record_type": "settlement",
        "settlement_id": sha256_json(settlement_identity),
        "prediction_record_id": prediction["record_id"],
        "fixture_id": prediction["fixture_id"],
        "phase": prediction["phase"],
        "model_version": MODEL_VERSION,
        "model_digest": model_digest(),
        "eventual_result": {
            "outcome": outcome,
            "home_score": home_score,
            "away_score": away_score,
        },
        "result_safe_available_at": result_safe_available_at,
        "settled_at": settled_at,
        "result_provenance": result_provenance,
        "shadow": True,
        "signal_status": "SHADOW_ONLY",
        "no_bet": True,
        "publication_enabled": False,
        "ledger_mutation": False,
        "is_actionable_value_signal": False,
    }


def append_shadow_settlement(
    existing: Iterable[dict[str, Any]], settlement: dict[str, Any]
) -> list[dict[str, Any]]:
    """Append one settlement event while preserving all prior records exactly."""

    if settlement.get("record_type") != "settlement":
        raise ValueError("only settlement records may be appended here")
    if settlement.get("model_digest") != model_digest():
        raise ValueError("settlement model digest mismatch")
    if settlement.get("shadow") is not True or settlement.get("no_bet") is not True:
        raise ValueError("settlement must remain shadow and no-bet")
    result = deepcopy(list(existing))
    prediction_ids = {
        item.get("record_id")
        for item in result
        if item.get("record_type", "prediction") == "prediction"
    }
    if settlement.get("prediction_record_id") not in prediction_ids:
        raise ValueError("settlement references an unknown prediction")
    settlement_ids = {item.get("settlement_id") for item in result}
    settled_predictions = {
        item.get("prediction_record_id")
        for item in result
        if item.get("record_type") == "settlement"
    }
    if (
        settlement.get("settlement_id") in settlement_ids
        or settlement.get("prediction_record_id") in settled_predictions
    ):
        raise ValueError("settlements are append-only and cannot conflict")
    result.append(deepcopy(settlement))
    return result


def _metric_summary(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    if not pairs:
        return {
            "sample_count": 0,
            "brier_score": None,
            "log_loss": None,
            "accuracy": None,
            "calibration": {"ece": None, "by_outcome": {}},
        }
    classes = ("home", "draw", "away")
    brier_values: list[float] = []
    log_loss_values: list[float] = []
    correct = 0
    by_outcome: dict[str, list[tuple[float, float]]] = {
        outcome: [] for outcome in classes
    }
    for prediction, settlement in pairs:
        probs = prediction["probabilities"]
        actual = settlement["eventual_result"]["outcome"]
        brier_values.append(
            sum((float(probs[name]) - float(name == actual)) ** 2 for name in classes)
        )
        log_loss_values.append(-math.log(max(float(probs[actual]), 1e-15)))
        correct += int(max(classes, key=lambda name: float(probs[name])) == actual)
        for name in classes:
            by_outcome[name].append((float(probs[name]), float(name == actual)))

    calibration: dict[str, Any] = {"by_outcome": {}, "ece": 0.0}
    total_components = 0
    for name, values in by_outcome.items():
        mean_predicted = sum(p for p, _ in values) / len(values)
        observed_rate = sum(y for _, y in values) / len(values)
        gap = abs(mean_predicted - observed_rate)
        calibration["by_outcome"][name] = {
            "sample_count": len(values),
            "mean_predicted": mean_predicted,
            "observed_rate": observed_rate,
            "absolute_gap": gap,
        }
        calibration["ece"] += gap * len(values)
        total_components += len(values)
    calibration["ece"] /= total_components
    return {
        "sample_count": len(pairs),
        "brier_score": sum(brier_values) / len(brier_values),
        "log_loss": sum(log_loss_values) / len(log_loss_values),
        "accuracy": correct / len(pairs),
        "calibration": calibration,
    }


def calculate_forward_metrics(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Calculate settled forward evidence without modifying any record."""

    rows = deepcopy(list(records))
    predictions = {
        row["record_id"]: row
        for row in rows
        if row.get("record_type", "prediction") == "prediction"
    }
    settlements = [row for row in rows if row.get("record_type") == "settlement"]
    settled_prediction_ids: set[str] = set()
    pairs_by_phase: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for settlement in settlements:
        prediction_id = settlement.get("prediction_record_id")
        if prediction_id not in predictions:
            raise ValueError("metrics contain settlement without prediction")
        if prediction_id in settled_prediction_ids:
            raise ValueError("metrics contain duplicate settlement")
        settled_prediction_ids.add(prediction_id)
        prediction = predictions[prediction_id]
        validate_shadow_record(prediction)
        if settlement.get("model_digest") != prediction.get("model_digest"):
            raise ValueError("metrics contain a model-digest mismatch")
        if (
            settlement.get("shadow") is not True
            or settlement.get("no_bet") is not True
            or settlement.get("publication_enabled") is not False
            or settlement.get("ledger_mutation") is not False
            or settlement.get("eventual_result", {}).get("outcome")
            not in {"home", "draw", "away"}
        ):
            raise ValueError("metrics contain an unsafe settlement")
        if settlement.get("phase") != prediction.get("phase"):
            raise ValueError("metrics settlement phase mismatch")
        pairs_by_phase.setdefault(prediction["phase"], []).append(
            (prediction, settlement)
        )
    overall_pairs = [pair for pairs in pairs_by_phase.values() for pair in pairs]
    return {
        "schema": METRICS_SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "model_digest": model_digest(),
        "shadow": True,
        "no_bet": True,
        "publication_enabled": False,
        "ledger_mutation": False,
        "sample_count": len(overall_pairs),
        "overall": _metric_summary(overall_pairs),
        "lifecycle_stage_breakdown": {
            phase: _metric_summary(pairs)
            for phase, pairs in sorted(pairs_by_phase.items())
        },
    }
