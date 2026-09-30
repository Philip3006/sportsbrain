"""Corrected successor contract for the Nations League forward-shadow model.

``nations_league_v1`` remains a historical, byte-for-byte frozen contract.
This module is a separate migration seam: it reuses the validated causal Elo
algorithm and lifecycle boundaries, but requires the exact #215 canonical
Nations League timeline as its only training universe.  It never imports
runtime, provider, odds, publication, betting, or ledger code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from src.analysis.nations_league_v1 import (
    COMPETITION,
    DRAW_BAND,
    ELO_DEFAULT,
    HOME_ADVANTAGE,
    INITIAL_WINDOW,
    REFINEMENT_WINDOW,
    _metric_summary,
    fit_causal_elo,
    predict_1x2,
    validate_lifecycle_timestamp,
)
from src.config import ELO_K_BASE, TOURNAMENT_K_FACTORS

MODEL_VERSION = "nations_league_v1_1"
SCHEMA_VERSION = "nations-league-v1-1-forward-shadow-v1"
FORWARD_RUN_SCHEMA_VERSION = "nations-league-v1-1-forward-shadow-run-v1"
METRICS_SCHEMA_VERSION = "nations-league-v1-1-forward-shadow-metrics-v1"
OLD_MODEL_VERSION = "nations_league_v1"
OLD_FROZEN_MODEL_DIGEST = (
    "f55549e7225f55deac23c7a31b757acf509ad0b4810b93ba8244301d3395a8ee"
)

TRAINING_TIMELINE_SCHEMA = "uefa-nations-league-fixture-timeline-v1"
TRAINING_TIMELINE_DATASET_DIGEST = (
    "2c60c6b823b0cae948947fffe2a0e3456495c1fc5d510379ae95690e1c3fa6ef"
)
TRAINING_TIMELINE_HEAD_SHA = "065c6b40eb9911df3703d2e3079730a556136ee3"
TRAINING_TIMELINE_FIXTURE_COUNT = 512
TRAINING_RESULT_COUNT = 510
ADMINISTRATIVE_EXCEPTION_COUNT = 2
HISTORICAL_EARLIEST_KICKOFF_UTC = "2020-09-03T16:00:00Z"
HISTORICAL_EARLIEST_RESULT_SAFE_UTC = "2020-09-03T22:00:00Z"
HISTORICAL_LATEST_KICKOFF_UTC = "2025-06-08T19:00:00Z"
UTC = timezone.utc
INITIAL_WINDOW_UTC = INITIAL_WINDOW
REFINEMENT_WINDOW_UTC = REFINEMENT_WINDOW

MODEL_SPEC: dict[str, Any] = {
    "model_version": MODEL_VERSION,
    "supersedes": {
        "model_version": OLD_MODEL_VERSION,
        "model_digest": OLD_FROZEN_MODEL_DIGEST,
        "reason": "AMBIGUOUS_AND_INTERNALLY_INCONSISTENT_TRAINING_UNIVERSE",
    },
    "status": "RESEARCH_CANDIDATE_NOT_PRODUCTION_CERTIFIED",
    "family": "causal_elo",
    "algorithm": {
        "initial_rating": ELO_DEFAULT,
        "home_advantage_elo": HOME_ADVANTAGE,
        "draw_band_elo": DRAW_BAND,
        "competitive_k_base": ELO_K_BASE,
        "nations_league_k_factor": TOURNAMENT_K_FACTORS[COMPETITION],
        "goal_difference_update": "existing repository Elo update",
        "neutral_handling": "home advantage is zero when neutral=true",
        "calibration": "none",
    },
    "training_universe": {
        "competition": COMPETITION,
        "timeline_schema": TRAINING_TIMELINE_SCHEMA,
        "timeline_dataset_digest": TRAINING_TIMELINE_DATASET_DIGEST,
        "timeline_head_sha": TRAINING_TIMELINE_HEAD_SHA,
        "fixture_count": TRAINING_TIMELINE_FIXTURE_COUNT,
        "result_safe_training_count": TRAINING_RESULT_COUNT,
        "administrative_exception_count": ADMINISTRATIVE_EXCEPTION_COUNT,
        "earliest_kickoff_utc": HISTORICAL_EARLIEST_KICKOFF_UTC,
        "earliest_result_safe_available_at": HISTORICAL_EARLIEST_RESULT_SAFE_UTC,
        "latest_kickoff_utc": HISTORICAL_LATEST_KICKOFF_UTC,
        "result_safe_rule": "result_safe_available_at < prediction_cutoff",
        "administrative_exception_handling": "retain in timeline; exclude from training",
        "team_identity": "canonical timeline team names and canonical fixture_id",
        "chronological_update": "result-safe timestamp then canonical fixture_id",
        "neutral_source": "absent in #215 timeline training adapter; frozen default is false",
    },
    "lifecycle": {
        "initial": "22-26 hours before kickoff",
        "refinement": "60-120 minutes before kickoff",
    },
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
    return sha256_json(
        {
            "fixture_id": fixture_id,
            "phase": phase,
            "model_version": MODEL_VERSION,
            "model_digest": model_digest(),
        }
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
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def validate_legacy_contract_unchanged() -> None:
    """Assert that the historical v1 digest still matches its frozen value."""

    from src.analysis.nations_league_v1 import model_digest as legacy_model_digest

    if legacy_model_digest() != OLD_FROZEN_MODEL_DIGEST:
        raise ValueError("historical nations_league_v1 digest changed")


def _timeline_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_training_timeline(timeline: Mapping[str, Any]) -> None:
    """Validate the exact retained #215 timeline contract before adaptation."""

    if timeline.get("schema_version") != TRAINING_TIMELINE_SCHEMA:
        raise ValueError("unsupported Nations League training timeline schema")
    records = timeline.get("records")
    if not isinstance(records, list) or len(records) != TRAINING_TIMELINE_FIXTURE_COUNT:
        raise ValueError("training timeline must contain exactly 512 fixtures")
    if timeline.get("dataset_digest") != TRAINING_TIMELINE_DATASET_DIGEST:
        raise ValueError("training timeline digest is not the retained #215 digest")
    if (
        _timeline_digest({k: v for k, v in timeline.items() if k != "dataset_digest"})
        != timeline["dataset_digest"]
    ):
        raise ValueError("training timeline dataset digest mismatch")
    fixture_ids: set[str] = set()
    safe_count = 0
    exception_count = 0
    for record in records:
        fixture_id = record.get("fixture_id")
        if not isinstance(fixture_id, str) or fixture_id in fixture_ids:
            raise ValueError("training timeline fixture identity is not unique")
        fixture_ids.add(fixture_id)
        expected_record_digest = _timeline_digest(
            {k: v for k, v in record.items() if k != "record_digest"}
        )
        if record.get("record_digest") != expected_record_digest:
            raise ValueError(f"timeline record digest mismatch: {fixture_id}")
        if record.get("competition") != COMPETITION:
            raise ValueError("training timeline contains a non-Nations-League row")
        if record.get("administrative_exception") is not None:
            exception_count += 1
            if record.get("result_safe_available_at") is not None:
                raise ValueError("administrative exception has result-safe evidence")
            continue
        safe_at = record.get("result_safe_available_at")
        kickoff = record.get("kickoff_utc")
        if not safe_at or not kickoff:
            raise ValueError("ordinary timeline row lacks causal timestamps")
        if _parse_utc(kickoff, "kickoff_utc") < _parse_utc(
            safe_at, "result_safe_available_at"
        ):
            safe_count += 1
        else:
            raise ValueError("result-safe availability precedes kickoff")
    if (
        safe_count != TRAINING_RESULT_COUNT
        or exception_count != ADMINISTRATIVE_EXCEPTION_COUNT
    ):
        raise ValueError(
            "training timeline coverage counts do not match the frozen contract"
        )


def training_records_from_timeline(timeline: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Adapt #215 rows into the exact v1.1 NL-only training representation."""

    validate_training_timeline(timeline)
    digest = str(timeline["dataset_digest"])
    rows: list[dict[str, Any]] = []
    for record in timeline["records"]:
        if record.get("administrative_exception") is not None:
            continue
        rows.append(
            {
                "fixture_id": record["fixture_id"],
                "edition": record["edition"],
                "evaluation_block": record["validation_period"],
                "home_team": record["home_team"],
                "away_team": record["away_team"],
                "kickoff_utc": record["kickoff_utc"],
                "result_safe_available_at": record["result_safe_available_at"],
                "home_score": record["home_score"],
                "away_score": record["away_score"],
                "competition": COMPETITION,
                "source_provenance": f"canonical-timeline:{digest}",
                "source_digest": record["record_digest"],
            }
        )
    if len(rows) != TRAINING_RESULT_COUNT:
        raise ValueError(
            "timeline adapter did not produce the frozen 510 training rows"
        )
    return rows


def validate_training_record(record: Mapping[str, Any]) -> None:
    """Training validator; intentionally separate from target validation."""

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
        "result_safe_available_at",
        "home_score",
        "away_score",
    }
    missing = sorted(required - record.keys())
    if missing:
        raise ValueError(f"training fixture missing fields: {', '.join(missing)}")
    if record["competition"] != COMPETITION:
        raise ValueError("training competition must be UEFA Nations League")
    if record["home_team"] == record["away_team"]:
        raise ValueError("training fixture cannot have identical teams")
    _parse_utc(record["kickoff_utc"], "kickoff_utc")
    _parse_utc(record["result_safe_available_at"], "result_safe_available_at")
    _require_digest(record["source_digest"], "source_digest")
    if (
        not isinstance(record["source_provenance"], str)
        or not record["source_provenance"].strip()
    ):
        raise ValueError("training source provenance is required")
    if type(record["home_score"]) is not int or type(record["away_score"]) is not int:
        raise TypeError("training scores must be integers")
    if min(record["home_score"], record["away_score"]) < 0:
        raise ValueError("training scores must be non-negative")


def validate_point_in_time_training(
    records: Iterable[Mapping[str, Any]], prediction_cutoff: str
) -> list[dict[str, Any]]:
    """Return only NL rows whose result was safe strictly before cutoff."""

    cutoff = _parse_utc(prediction_cutoff, "prediction_cutoff")
    validated: list[dict[str, Any]] = []
    for source in records:
        record = deepcopy(dict(source))
        if record.get("administrative_exception") is not None:
            if record.get("result_safe_available_at") is not None:
                raise ValueError("administrative exception cannot be result-safe")
            continue
        validate_training_record(record)
        if (
            not _parse_utc(
                record["result_safe_available_at"], "result_safe_available_at"
            )
            < cutoff
        ):
            raise ValueError(
                "training result_safe_available_at must be strictly before prediction_cutoff"
            )
        validated.append(record)
    return sorted(
        validated, key=lambda row: (row["result_safe_available_at"], row["fixture_id"])
    )


def validate_target_fixture(fixture: Mapping[str, Any]) -> None:
    """Target validator; target competition semantics never validate training."""

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
    if fixture["competition"] != COMPETITION:
        raise ValueError("target fixture competition is not UEFA Nations League")
    if fixture["home_team"] == fixture["away_team"]:
        raise ValueError("target fixture cannot have identical teams")
    _parse_utc(fixture["kickoff_utc"], "kickoff_utc")
    _require_digest(fixture["source_digest"], "source_digest")
    if (
        not isinstance(fixture["source_provenance"], str)
        or not fixture["source_provenance"].strip()
    ):
        raise ValueError("target source provenance is required")
    if fixture.get("administrative_exception"):
        raise ValueError("administrative fixtures cannot be targets")


def validate_input_provenance(provenance: Mapping[str, Any]) -> None:
    if not isinstance(provenance, Mapping):
        raise TypeError("input_provenance must be an object")
    for field in ("timeline_digest", "fixture_source_digest"):
        _require_digest(provenance.get(field), f"input_provenance.{field}")
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
        if isinstance(value, Mapping):
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
    fixture: Mapping[str, Any],
    *,
    phase: str,
    prediction_timestamp: str,
    training_records: Iterable[Mapping[str, Any]],
    input_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a v1.1 prediction with new model/version/record bindings."""

    validate_target_fixture(fixture)
    validate_input_provenance(input_provenance)
    cutoff = _parse_utc(prediction_timestamp, "prediction_timestamp")
    kickoff = _parse_utc(fixture["kickoff_utc"], "kickoff_utc")
    if not cutoff < kickoff:
        raise ValueError("prediction cutoff must precede target kickoff")
    validate_lifecycle_timestamp(phase, prediction_timestamp, fixture["kickoff_utc"])
    rows = validate_point_in_time_training(training_records, prediction_timestamp)
    ratings = fit_causal_elo(rows, prediction_timestamp)
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
        "input_provenance": deepcopy(dict(input_provenance)),
        "input_provenance_digest": sha256_json(input_provenance),
        "source_evidence": ["canonical_fixture_timeline", "causal_elo_training_cutoff"],
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


def validate_shadow_record(record: Mapping[str, Any]) -> None:
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
        raise ValueError(f"v1.1 shadow record missing fields: {', '.join(missing)}")
    if (
        record["model_version"] != MODEL_VERSION
        or record["model_digest"] != model_digest()
    ):
        raise ValueError("shadow record must use the frozen v1.1 model")
    if record.get("competition") != COMPETITION:
        raise ValueError("shadow record target competition mismatch")
    if record.get("shadow") is not True or record.get("signal_status") != "SHADOW_ONLY":
        raise ValueError("shadow record must remain SHADOW_ONLY")
    if (
        record.get("no_bet") is not True
        or record.get("publication_enabled") is not False
    ):
        raise ValueError("shadow record must remain no-bet and unpublished")
    if (
        record.get("ledger_mutation") is not False
        or record.get("is_actionable_value_signal") is True
    ):
        raise ValueError("shadow record cannot mutate ledger or become actionable")
    if set(record["probabilities"]) != {"home", "draw", "away"} or any(
        not 0.0 <= float(value) <= 1.0 for value in record["probabilities"].values()
    ):
        raise ValueError("probabilities must contain bounded home/draw/away values")
    if (
        abs(sum(float(value) for value in record["probabilities"].values()) - 1.0)
        > 1e-9
    ):
        raise ValueError("probabilities must sum to one")
    if record.get("source_evidence") in (None, [], ""):
        raise ValueError("source evidence is required")
    validate_lifecycle_timestamp(
        record["phase"], record["prediction_timestamp"], record["kickoff_utc"]
    )
    forbidden = {
        "gbt",
        "context_features",
        "market_odds",
        "squad",
        "lineups",
        "motivation",
    }
    if forbidden.intersection(record):
        raise ValueError("v1.1 shadow record contains excluded inputs")


def append_shadow_record(
    existing: Iterable[Mapping[str, Any]], record: Mapping[str, Any]
) -> list[dict[str, Any]]:
    validate_shadow_record(record)
    result = deepcopy([dict(item) for item in existing])
    keys = {(item.get("fixture_id"), item.get("phase")) for item in result}
    if (
        record["record_id"] in {item.get("record_id") for item in result}
        or (record["fixture_id"], record["phase"]) in keys
    ):
        raise ValueError("v1.1 forward shadow records are append-only")
    result.append(deepcopy(dict(record)))
    return result


def _outcome(home_score: int, away_score: int) -> str:
    return (
        "home"
        if home_score > away_score
        else "away"
        if home_score < away_score
        else "draw"
    )


def build_shadow_settlement(
    prediction: Mapping[str, Any],
    *,
    home_score: int,
    away_score: int,
    result_safe_available_at: str,
    settled_at: str,
    result_provenance: str,
) -> dict[str, Any]:
    validate_shadow_record(prediction)
    if (
        type(home_score) is not int
        or type(away_score) is not int
        or min(home_score, away_score) < 0
    ):
        raise ValueError("scores must be non-negative integers")
    safe_at = _parse_utc(result_safe_available_at, "result_safe_available_at")
    if safe_at < _parse_utc(prediction["kickoff_utc"], "kickoff_utc"):
        raise ValueError("result-safe time cannot precede kickoff")
    if _parse_utc(settled_at, "settled_at") < safe_at:
        raise ValueError("settlement must occur after result-safe time")
    if not isinstance(result_provenance, str) or not result_provenance.strip():
        raise ValueError("result provenance is required")
    identity = {
        "prediction_record_id": prediction["record_id"],
        "model_digest": model_digest(),
        "result_safe_available_at": result_safe_available_at,
        "home_score": home_score,
        "away_score": away_score,
    }
    return {
        "record_type": "settlement",
        "settlement_id": sha256_json(identity),
        "prediction_record_id": prediction["record_id"],
        "fixture_id": prediction["fixture_id"],
        "phase": prediction["phase"],
        "model_version": MODEL_VERSION,
        "model_digest": model_digest(),
        "eventual_result": {
            "outcome": _outcome(home_score, away_score),
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
    existing: Iterable[Mapping[str, Any]], settlement: Mapping[str, Any]
) -> list[dict[str, Any]]:
    if (
        settlement.get("record_type") != "settlement"
        or settlement.get("model_digest") != model_digest()
    ):
        raise ValueError("v1.1 settlement contract mismatch")
    result = deepcopy([dict(item) for item in existing])
    prediction_ids = {
        item.get("record_id")
        for item in result
        if item.get("record_type", "prediction") == "prediction"
    }
    if settlement.get("prediction_record_id") not in prediction_ids:
        raise ValueError("settlement references an unknown prediction")
    if settlement.get("settlement_id") in {
        item.get("settlement_id") for item in result
    } or settlement.get("prediction_record_id") in {
        item.get("prediction_record_id")
        for item in result
        if item.get("record_type") == "settlement"
    }:
        raise ValueError("v1.1 settlements are append-only")
    result.append(deepcopy(dict(settlement)))
    return result


def calculate_forward_metrics(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = deepcopy([dict(item) for item in records])
    predictions = {
        row["record_id"]: row
        for row in rows
        if row.get("record_type", "prediction") == "prediction"
    }
    settlements = [row for row in rows if row.get("record_type") == "settlement"]
    pairs_by_phase: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    seen: set[str] = set()
    for settlement in settlements:
        prediction = predictions.get(settlement.get("prediction_record_id"))
        if prediction is None or settlement["prediction_record_id"] in seen:
            raise ValueError("v1.1 metrics contain an unknown or duplicate settlement")
        seen.add(settlement["prediction_record_id"])
        validate_shadow_record(prediction)
        if settlement.get("model_digest") != prediction.get(
            "model_digest"
        ) or settlement.get("phase") != prediction.get("phase"):
            raise ValueError("v1.1 metrics settlement binding mismatch")
        if (
            settlement.get("shadow") is not True
            or settlement.get("no_bet") is not True
            or settlement.get("publication_enabled") is not False
            or settlement.get("ledger_mutation") is not False
        ):
            raise ValueError("v1.1 metrics contain unsafe settlement")
        if settlement.get("eventual_result", {}).get("outcome") not in {
            "home",
            "draw",
            "away",
        }:
            raise ValueError("v1.1 settlement outcome is invalid")
        pairs_by_phase.setdefault(prediction["phase"], []).append(
            (prediction, settlement)
        )
    all_pairs = [
        pair for phase_pairs in pairs_by_phase.values() for pair in phase_pairs
    ]
    return {
        "schema": METRICS_SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "model_digest": model_digest(),
        "shadow": True,
        "no_bet": True,
        "publication_enabled": False,
        "ledger_mutation": False,
        "sample_count": len(all_pairs),
        "overall": _metric_summary(all_pairs),
        "lifecycle_stage_breakdown": {
            phase: _metric_summary(phase_pairs)
            for phase, phase_pairs in sorted(pairs_by_phase.items())
        },
    }


def build_supersession_artifact() -> dict[str, Any]:
    payload = {
        "schema_version": "nations-league-model-supersession-v1",
        "old_version": OLD_MODEL_VERSION,
        "old_digest": OLD_FROZEN_MODEL_DIGEST,
        "old_status": "SUPERSEDED_BEFORE_REAL_FORWARD_EVIDENCE",
        "reason": "AMBIGUOUS_AND_INTERNALLY_INCONSISTENT_TRAINING_UNIVERSE",
        "new_version": MODEL_VERSION,
        "new_digest": model_digest(),
        "historical_evidence": {
            "old_v1_real_forward_prediction_generated": False,
            "old_v1_real_forward_outcome_consumed": False,
            "promotion_evidence_transferred_as_real_forward_evidence": False,
            "historical_research_remains_background_only": True,
        },
    }
    return {**payload, "artifact_digest": sha256_json(payload)}
