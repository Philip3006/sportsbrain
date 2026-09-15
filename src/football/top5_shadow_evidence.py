"""Builder 1 producer for the independent Top-5 shadow evidence contract.

This module only serializes the public contract.  It deliberately does not
import Builder 2's validator, provider clients, schedulers, publishers, or
financial code.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from src.football.top5_research_binding import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    M5_FEATURE_SCHEMA_HASH,
)

CONTRACT_VERSION = "top5-shadow-evidence-v1"
_OUTCOMES = ("away", "draw", "home")


def build_evidence_payload(result: Any) -> dict[str, object]:
    """Convert one validated cycle into deterministic external evidence."""

    observations: list[dict[str, object]] = []
    predictions: list[dict[str, object]] = []
    provider_evidence: list[dict[str, object]] = []
    signal_time_evidence: list[dict[str, object]] = []
    health_evidence: list[dict[str, object]] = []
    failure_evidence: list[dict[str, object]] = []
    snapshot_by_fixture = {
        snapshot.fixture_key: snapshot
        for observation in result.observations
        for snapshot in observation.snapshots
    }
    prediction_by_fixture = {
        prediction.binding.fixture_key: prediction
        for integration in result.integrations
        for prediction in integration.predictions
    }

    for observation in result.observations:
        integration = next(
            (
                item
                for item in result.integrations
                if item.league_code == observation.league_code
            ),
            None,
        )
        health_evidence.append(
            _health_record(result, observation, integration is not None)
        )
        if observation.valid_fixtures:
            provider_evidence.append(_provider_record(result, observation))
        eligible_keys = {fixture.fixture_key for fixture in observation.fixtures}
        for fixture, source_timestamp in zip(
            observation.valid_fixtures,
            observation.source_timestamps,
            strict=True,
        ):
            snapshot = snapshot_by_fixture.get(fixture.fixture_key)
            prediction = prediction_by_fixture.get(fixture.fixture_key)
            generated_at = (
                prediction.generated_at
                if prediction is not None
                else observation.completed_at
            )
            model_identity = (
                prediction.binding.model_artifact_identity
                if prediction is not None
                else M5_CANDIDATE_ID
            )
            eligible = fixture.fixture_key in eligible_keys
            odds_age = (observation.completed_at - source_timestamp).total_seconds()
            stale = odds_age > result.timing.maximum_odds_age_seconds
            provenance = _provenance(
                "observation",
                f"{observation.request_id}:{fixture.fixture_key}",
                result,
                observation.league_code,
                fixture.fixture_key,
                generated_at,
                model_identity=model_identity,
            )
            observations.append({
                "provenance": provenance,
                "discovered": True,
                "eligible": eligible,
                "valid_odds": True,
                "prediction_id": prediction.prediction_id if prediction else None,
                "rejected": not eligible,
                "rejected_reason": (
                    "stale_odds"
                    if stale
                    else "outside_signal_window"
                    if not eligible
                    else None
                ),
                "provider_covered": True,
                "stale": stale,
                "fallback_used": False,
                "error": False,
                "duplicate_suppressed": False,
                "result_resolved": False,
                "no_bet": True,
                "publication_enabled": False,
            })
            signal_time_evidence.append(
                _signal_record(
                    result,
                    observation,
                    fixture.fixture_key,
                    eligible,
                    odds_age,
                    stale,
                )
            )
            if prediction is not None and snapshot is not None:
                predictions.append(_prediction_record(result, prediction, snapshot.odds))
        if not observation.fixtures:
            failure_evidence.append(_failure_record(result, observation))

    valid_fixture_count = sum(item.valid_fixture_count for item in result.observations)
    eligible_fixture_count = sum(item.eligible_fixture_count for item in result.observations)
    quota_provenance = _provenance(
        "quota",
        f"quota:{result.run_id}",
        result,
        "BL1",
        None,
        result.completed_at,
        model_identity=M5_CANDIDATE_ID,
    )
    quota_cost = {
        "provenance": quota_provenance,
        "horizon": "controlled-shadow-cycle",
        "total_fixture_count": valid_fixture_count,
        "logical_evaluations": eligible_fixture_count,
        "bulk_odds_requests": len(result.observations),
        "fallback_event_requests": 0,
        "result_requests": 0,
        "revalidation_requests": 0,
        "closing_capture_requests": 0,
        "raw_http_requests": len(result.observations),
        "provider_cost_units": float(len(result.observations)),
    }
    return {
        "contract_version": CONTRACT_VERSION,
        "evidence_window": {
            "start": _utc_iso(result.started_at),
            "end": _utc_iso(result.completed_at),
        },
        "safety": {
            "no_bet": True,
            "publication_enabled": False,
            "real_bet_created": False,
            "ledger_mutated": False,
            "sealed_data_accessed": False,
            "research_mutated": False,
            "production_activation": False,
        },
        "observations": observations,
        "predictions": predictions,
        "provider_evidence": provider_evidence,
        "signal_time_evidence": signal_time_evidence,
        "quota_cost_evidence": [quota_cost],
        "health_evidence": health_evidence,
        "result_attachments": [],
        "closing_benchmark_evidence": [],
        "failure_evidence": failure_evidence,
    }


def _provenance(
    kind: str,
    artifact_id: str,
    result: Any,
    league_code: str,
    fixture_key: str | None,
    generated_at: datetime,
    *,
    model_identity: str,
) -> dict[str, object]:
    artifact_sha = _digest(kind, artifact_id, result.integration_sha, fixture_key or "")
    evidence_id = _digest("evidence", kind, artifact_id)
    return {
        "evidence_id": evidence_id,
        "artifact_id": artifact_id,
        "artifact_sha": artifact_sha,
        "source_sha": result.integration_sha,
        "research_sha": FROZEN_RESEARCH_SHA,
        "league_code": league_code,
        "candidate_id": M5_CANDIDATE_ID,
        "model_identity": model_identity,
        "generated_at": _utc_iso(generated_at),
        "fixture_key": fixture_key,
        "feature_schema_hash": M5_FEATURE_SCHEMA_HASH,
        "contract_version": CONTRACT_VERSION,
    }


def _health_record(result: Any, observation: Any, inference_ran: bool) -> dict[str, object]:
    return {
        "provenance": _provenance(
            "health",
            f"health:{result.run_id}:{observation.league_code}",
            result,
            observation.league_code,
            None,
            observation.completed_at,
            model_identity=M5_CANDIDATE_ID,
        ),
        "provider_health": observation.status,
        "inference_health": "ok" if inference_ran else "not_run",
        "publisher_health": "disabled",
        "result_source_health": "pending",
        "activation_state": "shadow",
        "registered": False,
        "no_bet": True,
        "publication_enabled": False,
    }


def _provider_record(
    result: Any,
    observation: Any,
) -> dict[str, object]:
    source_ages = [
        (observation.completed_at - timestamp).total_seconds()
        for timestamp in observation.source_timestamps
    ]
    maximum_age = max(source_ages)
    outcome = (
        "stale"
        if maximum_age > result.timing.maximum_odds_age_seconds
        else "partial_response"
        if observation.status == "partial"
        else "success"
    )
    fixture_key = observation.valid_fixture_keys[0]
    return {
        "provenance": _provenance(
            "provider",
            f"provider:{observation.request_id}",
            result,
            observation.league_code,
            fixture_key,
            observation.completed_at,
            model_identity="the_odds_api:bulk:h2h:eu",
        ),
        "provider_name": "the_odds_api",
        "outcome": outcome,
        "requested_fixture_count": observation.valid_fixture_count,
        "covered_fixture_count": observation.valid_fixture_count,
        "availability": True,
        "latency_ms": observation.latency_ms,
        "odds_age_seconds": maximum_age,
        "maximum_odds_age_seconds": float(result.timing.maximum_odds_age_seconds),
        "bulk_requests": 1,
        "fallback_requests": 0,
        "retry_count": 0,
        "bulk_reused": True,
        "stale_rejections": 0,
        "wrong_market_rejections": observation.unsupported_market_count,
        "wrong_fixture_rejections": observation.wrong_league_count,
    }


def _signal_record(
    result: Any,
    observation: Any,
    fixture_key: str,
    eligible: bool,
    odds_age: float,
    stale: bool,
) -> dict[str, object]:
    return {
        "provenance": _provenance(
            "signal_time",
            f"signal-time:{observation.request_id}:{fixture_key}",
            result,
            observation.league_code,
            fixture_key,
            observation.completed_at,
            model_identity=M5_CANDIDATE_ID,
        ),
        "candidate_name": result.timing.evidence_candidate_name,
        "eligible": eligible,
        "odds_age_seconds": odds_age,
        "maximum_odds_age_seconds": float(result.timing.maximum_odds_age_seconds),
        "request_load": 1.0 / max(observation.valid_fixture_count, 1),
        "fallback_used": False,
        "stale_rejected": stale,
        "latency_ms": float(observation.latency_ms),
        "quota_cost_units": 1.0 / max(observation.valid_fixture_count, 1),
        "operational_complexity": 1.0,
    }


def _prediction_record(result: Any, prediction: Any, odds: Mapping[str, float]) -> dict[str, object]:
    prediction.validate()
    return {
        "provenance": _provenance(
            "prediction",
            prediction.prediction_id,
            result,
            prediction.binding.league_code,
            prediction.binding.fixture_key,
            prediction.generated_at,
            model_identity=prediction.binding.model_artifact_identity,
        ),
        "prediction_id": prediction.prediction_id,
        "signal_snapshot_id": prediction.snapshot_id,
        "input_snapshot_kinds": ["signal_time"],
        "closing_snapshot_ids": [],
        "probabilities": dict(prediction.probabilities),
        "market_probabilities": _market_probabilities(odds),
        "no_bet": True,
        "publication_enabled": False,
    }


def _failure_record(result: Any, observation: Any) -> dict[str, object]:
    status = observation.status
    code = {
        "timeout": "provider_timeout",
        "http_error": {
            403: "provider_403",
            429: "provider_429",
        }.get(observation.status_code, "incomplete_evidence"),
        "malformed": "malformed_response",
        "partial": "partial_response",
        "unsupported_market": "wrong_market",
        "no_eligible_fixtures": "incomplete_evidence",
        "empty": "incomplete_evidence",
    }.get(status, "incomplete_evidence")
    message = observation.failure_reason or status
    return {
        "provenance": _provenance(
            "failure",
            f"failure:{result.run_id}:{observation.league_code}",
            result,
            observation.league_code,
            None,
            observation.completed_at,
            model_identity=M5_CANDIDATE_ID,
        ),
        "code": code,
        "message": message,
        "blocking": True,
    }


def _market_probabilities(odds: Mapping[str, float]) -> dict[str, float]:
    inverse = {name: 1.0 / float(odds[name]) for name in _OUTCOMES}
    total = sum(inverse.values())
    return {name: inverse[name] / total for name in _OUTCOMES}


def _digest(prefix: str, *parts: str) -> str:
    encoded = json.dumps((prefix, *parts), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
