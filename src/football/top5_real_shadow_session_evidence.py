"""Builder-1 evidence producer for the independent shadow evidence contract."""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from src.football.top5_real_shadow_attachments import RealShadowResultStatus
from src.football.top5_real_shadow_contracts import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    _digest,
    _market_probabilities,
    _thaw,
)
from src.football.top5_shadow_validation import (
    TOP5_VALIDATION_CONTRACT_VERSION,
    ShadowEvidenceBundle,
)

if TYPE_CHECKING:
    from src.football.top5_real_shadow_session import RealShadowSession


def _provenance(*, kind: str, artifact_id: str, artifact_sha: str, source_sha: str, league: str,
                generated_at: object, fixture: str | None = None) -> dict[str, object]:
    evidence_id = f"{kind}:{artifact_id}"
    return {
        "evidence_id": evidence_id,
        "artifact_id": artifact_id,
        "artifact_sha": artifact_sha,
        "source_sha": source_sha,
        "research_sha": FROZEN_RESEARCH_SHA,
        "league_code": league,
        "fixture_key": fixture,
        "candidate_id": M5_CANDIDATE_ID,
        "model_identity": M5_CANDIDATE_ID,
        "generated_at": generated_at.isoformat(),
        "contract_version": TOP5_VALIDATION_CONTRACT_VERSION,
    }


def _closing_probabilities(odds: object) -> dict[str, float]:
    return dict(_market_probabilities(odds))


def build_shadow_evidence(session: RealShadowSession) -> dict[str, object]:
    """Serialize a session without importing Builder-2/provider internals.

    Validation is performed by the independent v1 consumer before this payload
    leaves the producer boundary.  The extra session and cascade sections keep
    durable runtime provenance without changing the Builder-2 contract.
    """
    session.validate()
    observations = []
    predictions = []
    provider_evidence = []
    signal_time_evidence = []
    health_evidence = []
    result_attachments = []
    closing_evidence = []
    failures = []
    for fixture_key in sorted(session.observations):
        observation = session.observations[fixture_key]
        observation_digest = observation.observation_digest()
        rejection = session.rejections.get(fixture_key)
        prediction = next((item for item in session.predictions.values() if item.fixture_key == fixture_key), None)
        observations.append({
            "provenance": _provenance(kind="observation", artifact_id=f"observation:{fixture_key}", artifact_sha=observation_digest, source_sha=session.integration_sha, league=observation.league_code, generated_at=observation.captured_at, fixture=fixture_key),
            "discovered": True,
            "eligible": prediction is not None,
            "valid_odds": observation.eligibility_state == "eligible",
            "prediction_id": prediction.prediction_id if prediction else None,
            "rejected": rejection is not None,
            "rejected_reason": rejection.reason if rejection else None,
            "provider_covered": observation.eligibility_state == "eligible",
            "stale": bool(rejection and "stale" in rejection.reason),
            "fallback_used": observation.fallback_depth > 0,
            "error": False,
            "duplicate_suppressed": False,
            "result_resolved": bool(prediction and prediction.prediction_id in session.results and RealShadowResultStatus(session.results[prediction.prediction_id].status) is RealShadowResultStatus.FINAL),
            "no_bet": True,
            "publication_enabled": False,
        })
        if prediction is not None:
            predictions.append({
                "provenance": _provenance(kind="prediction", artifact_id=prediction.prediction_id, artifact_sha=prediction.artifact_sha, source_sha=session.integration_sha, league=prediction.league_code, generated_at=prediction.captured_at, fixture=fixture_key),
                "prediction_id": prediction.prediction_id,
                "signal_snapshot_id": prediction.signal_snapshot_id,
                "input_snapshot_kinds": ["signal_time"],
                "closing_snapshot_ids": [],
                "probabilities": dict(prediction.probabilities),
                "market_probabilities": dict(prediction.market_probabilities),
                "no_bet": True,
                "publication_enabled": False,
            })
            provider_evidence.append({
                "provenance": _provenance(kind="provider", artifact_id=f"provider:{fixture_key}", artifact_sha=observation_digest, source_sha=session.integration_sha, league=observation.league_code, generated_at=observation.captured_at, fixture=fixture_key),
                "provider_name": observation.provider_identity,
                "outcome": "success",
                "requested_fixture_count": 1,
                "covered_fixture_count": 1,
                "availability": True,
                "latency_ms": observation.latency_ms,
                "odds_age_seconds": max(0.0, (observation.captured_at - observation.source_timestamp).total_seconds()),
                "maximum_odds_age_seconds": float(session.experiment.maximum_odds_age_seconds),
                "bulk_requests": observation.network_request_count,
                "network_request_count": observation.network_request_count,
                "quota_cost_units": observation.request_cost_units,
                "fallback_requests": observation.fallback_depth,
                "retry_count": 0,
                "bulk_reused": True,
            })
            signal_time_evidence.append({
                "provenance": _provenance(kind="signal-time", artifact_id=f"signal-time:{fixture_key}", artifact_sha=observation_digest, source_sha=session.integration_sha, league=observation.league_code, generated_at=observation.captured_at, fixture=fixture_key),
                "candidate_name": M5_CANDIDATE_ID,
                "eligible": True,
                "odds_age_seconds": max(0.0, (observation.captured_at - observation.source_timestamp).total_seconds()),
                "maximum_odds_age_seconds": float(session.experiment.maximum_odds_age_seconds),
                "request_load": float(observation.network_request_count),
                "fallback_used": observation.fallback_depth > 0,
                "stale_rejected": False,
                "latency_ms": float(observation.latency_ms),
                "quota_cost_units": observation.request_cost_units,
                "operational_complexity": 1.0,
            })
            health_evidence.append({
                "provenance": _provenance(kind="health", artifact_id=f"health:{fixture_key}", artifact_sha=observation_digest, source_sha=session.integration_sha, league=observation.league_code, generated_at=observation.captured_at, fixture=fixture_key),
                "provider_health": "eligible",
                "inference_health": "ok",
                "publisher_health": "disabled",
                "result_source_health": "resolved" if prediction.prediction_id in session.results else "pending",
                "activation_state": "shadow",
                "registered": False,
                "no_bet": True,
                "publication_enabled": False,
            })
        if rejection is not None:
            failures.append({
                "provenance": _provenance(kind="failure", artifact_id=f"failure:{fixture_key}", artifact_sha=rejection.observation_digest, source_sha=session.integration_sha, league=observation.league_code, generated_at=rejection.rejected_at, fixture=fixture_key),
                "code": "incomplete_evidence",
                "message": rejection.reason,
                "blocking": True,
            })
    for prediction_id in sorted(session.results):
        result = session.results[prediction_id]
        prediction = session.predictions[prediction_id]
        final = RealShadowResultStatus(result.status) is RealShadowResultStatus.FINAL
        result_attachments.append({
            "provenance": _provenance(kind="result", artifact_id=f"result:{prediction_id}", artifact_sha=result.attachment_sha, source_sha=session.integration_sha, league=result.league_code, generated_at=result.attached_at, fixture=result.fixture_key),
            "prediction_id": prediction_id,
            "actual_outcome": result.actual_outcome,
            "resolved": final,
            "result_source": result.result_source,
            "result_delay_seconds": max(0.0, (result.attached_at - prediction.kickoff).total_seconds()),
        })
    for prediction_id in sorted(session.closings):
        closing = session.closings[prediction_id]
        prediction = session.predictions[prediction_id]
        closing_evidence.append({
            "provenance": _provenance(kind="closing", artifact_id=f"closing:{prediction_id}", artifact_sha=closing.attachment_sha, source_sha=session.integration_sha, league=closing.league_code, generated_at=closing.attached_at, fixture=closing.fixture_key),
            "prediction_id": prediction_id,
            "signal_snapshot_id": prediction.signal_snapshot_id,
            "closing_snapshot_id": closing.closing_snapshot_id,
            "signal_probabilities": dict(prediction.market_probabilities),
            "closing_probabilities": _closing_probabilities(closing.odds),
            "used_for_prediction": False,
        })
    start = session.started_at or session.created_at
    end = session.completed_at or start + timedelta(microseconds=1)
    if end <= start:
        end = start + timedelta(microseconds=1)
    total_cost = sum(item.request_cost_units for item in session.observations.values())
    quota_provenance = _provenance(kind="quota", artifact_id=f"quota:{session.session_id}", artifact_sha=_digest(session.manifest()), source_sha=session.integration_sha, league=session.league_scope[0], generated_at=session.created_at)
    payload = {
        "contract_version": TOP5_VALIDATION_CONTRACT_VERSION,
        "evidence_window": {"start": start.isoformat(), "end": end.isoformat()},
        "safety": {"no_bet": True, "publication_enabled": False, "real_bet_created": False, "ledger_mutated": False, "sealed_data_accessed": False, "research_mutated": False, "production_activation": False},
        "observations": observations,
        "predictions": predictions,
        "provider_evidence": provider_evidence,
        "signal_time_evidence": signal_time_evidence,
        "quota_cost_evidence": [{
            "provenance": quota_provenance, "horizon": "session", "total_fixture_count": len(session.observations),
            "logical_evaluations": len(session.predictions), "bulk_odds_requests": sum(item.network_request_count for item in session.observations.values()),
            "fallback_event_requests": sum(item.fallback_depth for item in session.observations.values()), "result_requests": 0,
            "revalidation_requests": 0, "closing_capture_requests": len(session.closings),
            "raw_http_requests": sum(item.network_request_count for item in session.observations.values()) + len(session.closings),
            "provider_cost_units": total_cost,
        }],
        "health_evidence": health_evidence,
        "result_attachments": result_attachments,
        "closing_benchmark_evidence": closing_evidence,
        "failure_evidence": failures,
        "real_shadow_session": session.manifest(),
        "cascade_traces": [{
            "fixture_key": item.fixture_key,
            "provider_identity": item.provider_identity,
            "trace": _thaw(item.cascade_trace),
            "observation_digest": item.observation_digest(),
            "cascade_trace_digest": item.cascade_trace_digest(),
            "validation_receipt": _thaw(item.independent_validation) if item.independent_validation else None,
            "network_request_count": item.network_request_count,
            "quota_cost_units": item.request_cost_units,
            "observation_mode": item.observation_mode,
        } for item in session.observations.values()],
    }
    ShadowEvidenceBundle.from_payload(payload).validate()
    return payload


__all__ = ["build_shadow_evidence"]
