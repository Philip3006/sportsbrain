"""Frozen, research-only Nations League v1 contract.

This module is deliberately separate from the production football runtime.  It
adapts the repository's existing causal Elo implementation and adds the
point-in-time and shadow-only gates needed for a future forward evaluation.
"""

from __future__ import annotations

import hashlib
import json
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
COMPETITION = "UEFA Nations League"
IMPLEMENTATION_SOURCE = "src.models.elo:compute_elo_series,elo_win_probability"
IMPLEMENTATION_SOURCE_SHA = "cf014a08ffd1c32a766df05ce0c9afc2860047ae"

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
