"""Narrow, model-neutral seam from the cascade into Builder 1 shadow code."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.football.provider_cascade.contracts import (
    BUILDER2_VALIDATION_CONTRACT_VERSION,
    CascadeDecisionTrace,
    CascadeResult,
    NormalizedOddsObservation,
    TransportCapability,
)
from src.football.top5_builder2_qualification_receipt import (
    Builder2QualificationReceiptError,
    Builder2QualificationReceiptV1,
    validate_builder1_qualification_receipt,
    validate_builder4_qualification_receipt,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    RealProviderObservation,
)
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA


@dataclass(frozen=True)
class Builder1OddsInput:
    """Only accepted normalized odds plus routing provenance crosses this seam."""

    observation: NormalizedOddsObservation
    routing: CascadeDecisionTrace
    builder2_receipt: Builder2QualificationReceiptV1
    qualification_observation: RealProviderObservation

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
        result = CascadeResult(self.observation, self.routing)
        result.validate()
        _validate_external_builder2_context(
            result,
            self.builder2_receipt,
            self.qualification_observation,
            consumer_validator=validate_builder1_qualification_receipt,
        )

    def model_inputs(self):
        """Return the existing contract objects; no model is imported here."""

        self.validate()
        return self.observation.fixture, self.observation.as_market_snapshot()


def accepted_for_builder1(
    result: CascadeResult,
    validation_receipt: Builder2QualificationReceiptV1
    | Mapping[str, object]
    | None = None,
    *,
    qualification_observation: RealProviderObservation
    | Mapping[str, object]
    | None = None,
) -> Builder1OddsInput:
    """Cross the seam only with external Builder-2 V1 evidence and receipt."""

    result.validate()
    if not result.accepted or result.observation is None:
        raise ValueError("cascade failed closed; Builder 1 must not receive odds")
    if validation_receipt is None:
        raise ValueError(
            "independent Builder-2 validation receipt is required; candidate-only cascade cannot self-authorize"
        )
    if qualification_observation is None:
        raise ValueError(
            "exact external Builder-2 qualification observation is required; candidate-only cascade cannot self-authorize"
        )
    receipt = (
        validation_receipt
        if isinstance(validation_receipt, Builder2QualificationReceiptV1)
        else Builder2QualificationReceiptV1.from_payload(validation_receipt)
    )
    external_observation = (
        qualification_observation
        if isinstance(qualification_observation, RealProviderObservation)
        else RealProviderObservation.from_payload(qualification_observation)
    )
    _validate_external_builder2_context(
        result,
        receipt,
        external_observation,
        consumer_validator=validate_builder4_qualification_receipt,
    )
    input_value = Builder1OddsInput(
        result.observation,
        result.trace,
        receipt,
        external_observation,
    )
    input_value.validate()
    return input_value


def _validate_external_builder2_context(
    result: CascadeResult,
    receipt: Builder2QualificationReceiptV1,
    qualification_observation: RealProviderObservation,
    *,
    consumer_validator,
) -> None:
    """Validate the canonical receipt and bind its real observation to B4 output."""

    try:
        qualification_observation.validate_structural()
        consumer_validator(
            receipt,
            expected_observation=qualification_observation,
            expected_cascade_evidence=qualification_observation.cascade_evidence,
        )
    except Builder2QualificationReceiptError as exc:
        raise ValueError(
            "canonical Builder-2 qualification receipt is invalid or not bound to its observation"
        ) from exc

    if result.observation is None:
        raise ValueError("Builder-2 evidence cannot admit an empty cascade result")
    selected = tuple(
        attempt
        for attempt in result.trace.attempts
        if attempt.provider == result.trace.selected_provider
    )
    if len(selected) != 1:
        raise ValueError(
            "cascade selection is not bound to exactly one provider attempt"
        )
    selected_attempt = selected[0]
    if (
        TransportCapability(selected_attempt.transport_capability)
        is not TransportCapability.NETWORK_CAPABLE
    ):
        raise ValueError(
            "TEST_INJECTED cascade output cannot be promoted by a Builder-2 receipt"
        )
    if not selected_attempt.network_called:
        raise ValueError("qualified Builder-2 evidence requires a network observation")

    cascade_observation = result.observation
    exact_bindings = (
        ("league", cascade_observation.league_code, qualification_observation.league),
        (
            "fixture_key",
            cascade_observation.fixture_key,
            qualification_observation.fixture_key,
        ),
        (
            "home_team",
            cascade_observation.home_team,
            qualification_observation.home_team,
        ),
        (
            "away_team",
            cascade_observation.away_team,
            qualification_observation.away_team,
        ),
        ("kickoff", cascade_observation.kickoff_utc, qualification_observation.kickoff),
        (
            "provider_identity",
            cascade_observation.provider_identity,
            qualification_observation.provider_identity,
        ),
        (
            "provider_event_id",
            cascade_observation.provider_fixture_id,
            qualification_observation.provider_event_id,
        ),
        (
            "provider_request_id",
            cascade_observation.request_identity,
            qualification_observation.provider_request_id,
        ),
        (
            "home_odds",
            cascade_observation.home_odds,
            qualification_observation.home_odds,
        ),
        (
            "draw_odds",
            cascade_observation.draw_odds,
            qualification_observation.draw_odds,
        ),
        (
            "away_odds",
            cascade_observation.away_odds,
            qualification_observation.away_odds,
        ),
        (
            "bookmaker_identity",
            cascade_observation.bookmaker_identity,
            qualification_observation.bookmaker_identity,
        ),
        (
            "source_timestamp",
            cascade_observation.source_timestamp,
            qualification_observation.source_timestamp,
        ),
        (
            "captured_at",
            cascade_observation.captured_at,
            qualification_observation.captured_at,
        ),
        (
            "request_started_at",
            cascade_observation.request_started_at,
            qualification_observation.request_started_at,
        ),
        (
            "request_completed_at",
            cascade_observation.request_completed_at,
            qualification_observation.request_finished_at,
        ),
        (
            "adapter_version",
            cascade_observation.adapter_version,
            qualification_observation.adapter_version,
        ),
        (
            "raw_response_digest",
            cascade_observation.raw_record_digest.lower(),
            qualification_observation.raw_response_digest.lower(),
        ),
    )
    for name, actual, expected in exact_bindings:
        if actual != expected:
            raise ValueError(f"Builder-2 receipt is not bound to this {name}")

    attempt_bindings = (
        (
            "provider",
            selected_attempt.provider,
            qualification_observation.provider_identity,
        ),
        (
            "provider_event_id",
            selected_attempt.provider_record_id,
            qualification_observation.provider_event_id,
        ),
        (
            "provider_request_id",
            selected_attempt.request_identity,
            qualification_observation.provider_request_id,
        ),
        (
            "raw_response_digest",
            selected_attempt.raw_record_digest.lower(),
            qualification_observation.raw_response_digest.lower(),
        ),
        (
            "adapter_version",
            selected_attempt.adapter_version,
            qualification_observation.adapter_version,
        ),
    )
    for name, actual, expected in attempt_bindings:
        if actual != expected:
            raise ValueError(f"selected cascade attempt is not bound to this {name}")


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
            attempt.request_started_at or result.observation.request_started_at
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
                "source_timestamp": attempt.source_timestamp.isoformat()
                if attempt.source_timestamp
                else None,
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
