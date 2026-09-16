"""Narrow, model-neutral seam from the cascade into Builder 1 shadow code."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.football.provider_cascade.contracts import (
    BUILDER2_VALIDATION_CONTRACT_VERSION,
    Builder2ValidationReceipt,
    CascadeDecisionTrace,
    CascadeResult,
    NormalizedOddsObservation,
)
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA


@dataclass(frozen=True)
class Builder1OddsInput:
    """Only accepted normalized odds plus routing provenance crosses this seam."""

    observation: NormalizedOddsObservation
    routing: CascadeDecisionTrace
    builder2_receipt: Builder2ValidationReceipt

    def validate(self) -> None:
        self.observation.validate()
        self.routing.validate()
        if self.routing.selected_provider != self.observation.provider_identity:
            raise ValueError("Builder 1 input provider differs from cascade selection")
        if (
            self.routing.fail_closed
            or not self.routing.no_bet
            or self.routing.publication
        ):
            raise ValueError("Builder 1 input violates the shadow safety boundary")
        self.builder2_receipt.assert_allows()
        if not self.builder2_receipt.matches(
            CascadeResult(self.observation, self.routing)
        ):
            raise ValueError("Builder-2 receipt is not bound to this observation")

    def model_inputs(self):
        """Return the existing contract objects; no model is imported here."""

        self.validate()
        return self.observation.fixture, self.observation.as_market_snapshot()


def accepted_for_builder1(
    result: CascadeResult,
    validation_receipt: Builder2ValidationReceipt | Mapping[str, object] | None = None,
) -> Builder1OddsInput:
    """Cross the seam only with an external, digest-bound Builder-2 receipt."""

    result.validate()
    if not result.accepted or result.observation is None:
        raise ValueError("cascade failed closed; Builder 1 must not receive odds")
    if validation_receipt is None:
        raise ValueError(
            "independent Builder-2 validation receipt is required; candidate-only cascade cannot self-authorize"
        )
    receipt = (
        validation_receipt
        if isinstance(validation_receipt, Builder2ValidationReceipt)
        else Builder2ValidationReceipt.from_payload(validation_receipt)
    )
    receipt.validate()
    if receipt.validation_contract_version != BUILDER2_VALIDATION_CONTRACT_VERSION:
        raise ValueError("unsupported Builder-2 validation contract")
    if not receipt.matches(result):
        raise ValueError(
            "Builder-2 validation receipt digest, fixture, or provider does not match"
        )
    receipt.assert_allows()
    input_value = Builder1OddsInput(result.observation, result.trace, receipt)
    input_value.validate()
    return input_value


def builder2_evidence_payload(result: CascadeResult) -> dict[str, object]:
    """Serialize a complete, non-authoritative payload for Builder-2 intake."""

    result.validate()
    order = result.trace.configured_provider_order or tuple(
        attempt.provider for attempt in result.trace.attempts
    )
    attempts: list[dict[str, object]] = []
    outcome_map = {
        "AVAILABLE": "SUCCESS",
        "QUOTA_EXHAUSTED": "QUOTA_EXHAUSTED",
        "RATE_LIMITED": "RATE_LIMITED",
        "AUTH_FAILED": "AUTH_FAILED",
        "TEMPORARILY_UNAVAILABLE": "PROVIDER_UNAVAILABLE",
        "HEALTH_UNKNOWN": "PROVIDER_UNAVAILABLE",
        "DISCOVERY_REQUIRED": "UNSUPPORTED_FIXTURE",
        "IDENTITY_UNRESOLVED": "UNSUPPORTED_FIXTURE",
        "IDENTITY_AMBIGUOUS": "QUALITY_REJECTED",
    }
    for attempt in result.trace.attempts:
        outcome = outcome_map.get(attempt.state.value, "MALFORMED")
        is_success = outcome == "SUCCESS" and attempt.home_odds is not None
        started = (
            attempt.request_started_at
            or result.observation.request_started_at
            if result.observation
            else None
        )
        completed = attempt.request_completed_at or started
        if started is None:
            started = result.trace.attempts[0].request_started_at
        if completed is None:
            completed = started
        if started is None or completed is None:
            raise ValueError("cascade trace is missing request timing evidence")
        attempts.append(
            {
                "league": (
                    attempt.league_code or result.observation.league_code
                    if result.observation
                    else ""
                ),
                "fixture_key": attempt.fixture_key or result.trace.fixture_key,
                "home_team": (
                    attempt.home_team or result.observation.home_team
                    if result.observation
                    else ""
                ),
                "away_team": (
                    attempt.away_team or result.observation.away_team
                    if result.observation
                    else ""
                ),
                "kickoff": (
                    attempt.kickoff.isoformat()
                    if attempt.kickoff
                    else result.observation.kickoff_utc.isoformat()
                ),
                "configured_provider_order": list(order),
                "provider_attempt_index": attempt.attempt_index,
                "fallback_depth": attempt.fallback_depth,
                "provider_identity": attempt.provider,
                "network_called": attempt.network_called,
                "start_timestamp": started.isoformat(),
                "end_timestamp": completed.isoformat(),
                "capture_timestamp": completed.isoformat(),
                "outcome": outcome,
                "failure_classification": None if is_success else outcome,
                "market_type": "h2h_1x2",
                "home_odds": attempt.home_odds,
                "draw_odds": attempt.draw_odds,
                "away_odds": attempt.away_odds,
                "bookmaker_identity": attempt.bookmaker_identity,
                "source_identity": attempt.source_identity,
                "market_phase": "PRE_MATCH",
                "source_timestamp": attempt.source_timestamp.isoformat() if attempt.source_timestamp else None,
                "source_timing_provenance": attempt.source_timing_provenance,
                "request_latency_ms": attempt.latency_ms,
                "quota_before": {
                    "authenticated": None,
                    "quota_used": attempt.quota_before.used,
                    "quota_remaining": attempt.quota_before.remaining,
                },
                "quota_after": {
                    "authenticated": None,
                    "quota_used": attempt.quota_after.used,
                    "quota_remaining": attempt.quota_after.remaining,
                },
                "preflight_allowed": attempt.preflight_allowed,
                "budget_decision": attempt.budget_decision or "REJECTED",
                "request_cost_classification": attempt.request_cost_classification,
                "network_request_count": attempt.network_request_count,
                "quota_cost_units": attempt.quota_cost_units,
                "credentials_available": attempt.credentials_present,
                "provider_record_id": attempt.provider_record_id,
                "adapter_version": attempt.adapter_version,
                "raw_record_digest": attempt.raw_record_digest,
                "request_identity": attempt.request_identity,
                "provider_readiness_state": attempt.provider_readiness_state.lower(),
            }
        )
    return {
        "contract_version": BUILDER2_VALIDATION_CONTRACT_VERSION,
        "cascade_trace_digest": result.cascade_trace_digest,
        "provenance": {
            "evidence_id": (
                f"cascade:{result.trace.fixture_key}:{result.cascade_trace_digest[:16]}"
            ),
            "artifact_id": f"cascade:{result.trace.fixture_key}",
            "artifact_sha": result.observation_digest or result.cascade_trace_digest,
            "source_sha": result.observation_digest or result.cascade_trace_digest,
            "research_sha": FROZEN_RESEARCH_SHA,
            "candidate_id": "top5-provider-cascade-candidate",
            "model_identity": "unbound-model-slot",
            "generated_at": (
                result.observation.captured_at.isoformat()
                if result.observation
                else attempts[-1]["end_timestamp"]
            ),
            "contract_version": BUILDER2_VALIDATION_CONTRACT_VERSION,
        },
        "configured_provider_order": list(order),
        "execution_mode": "SEQUENTIAL",
        "attempts": attempts,
        "skipped_providers": [],
        "selected_provider": result.trace.selected_provider,
        "prediction_input_allowed": False,
        "safety": {
            "no_bet": True,
            "publication_enabled": False,
            "ledger_mutated": False,
            "production_activation": False,
            "sealed_data_accessed": False,
            "research_mutated": False,
            "monetary_spend_authorized": False,
        },
    }
