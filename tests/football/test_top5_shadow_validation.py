from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.top5_shadow_validation import (
    FROZEN_RESEARCH_SHA,
    CEOAuthorization,
    EvidenceProvenance,
    GateState,
    HealthEvidence,
    PredictionEvidence,
    ProviderEvidence,
    ProviderOutcome,
    QuotaCostEvidence,
    ResultAttachment,
    SafetyAssertions,
    ShadowEvidenceBundle,
    ShadowObservationEvidence,
    ShadowSafetyRejection,
    ShadowValidationGate,
    SignalTimeEvidence,
    assess_shadow_evidence,
    build_ceo_review_packet,
    calculate_performance_metrics,
    compare_signal_time_evidence,
    evaluate_provider_evidence,
    evidence_digest,
    ingest_shadow_evidence,
    validate_coverage,
)

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
SOURCE_SHA = "a" * 40


def _provenance(
    kind: str,
    *,
    fixture: str | None = "fixture-1",
    league: str = "BL1",
    candidate: str = "candidate-a",
    artifact_id: str | None = None,
    artifact_sha: str | None = None,
) -> EvidenceProvenance:
    artifact_id = artifact_id or f"{kind}-{fixture or league}-{candidate}"
    return EvidenceProvenance(
        evidence_id=f"evidence-{kind}-{artifact_id}",
        artifact_id=artifact_id,
        artifact_sha=artifact_sha or ("b" * 39 + str(len(artifact_id) % 10)),
        source_sha=SOURCE_SHA,
        research_sha=FROZEN_RESEARCH_SHA,
        league_code=league,
        fixture_key=fixture,
        candidate_id=candidate,
        model_identity="shadow-model-candidate",
        generated_at=NOW,
    )


def _observation(
    fixture: str = "fixture-1",
    *,
    league: str = "BL1",
    prediction_id: str | None = "prediction-fixture-1",
    candidate: str = "candidate-a",
    **changes: object,
) -> ShadowObservationEvidence:
    return ShadowObservationEvidence(
        provenance=_provenance(
            "observation", fixture=fixture, league=league, candidate=candidate
        ),
        discovered=True,
        eligible=True,
        valid_odds=True,
        prediction_id=prediction_id,
        provider_covered=True,
        **changes,
    )


def _prediction(
    fixture: str = "fixture-1", *, prediction_id: str | None = None
) -> PredictionEvidence:
    prediction_id = prediction_id or f"prediction-{fixture}"
    return PredictionEvidence(
        provenance=_provenance(
            "prediction", fixture=fixture, artifact_id=prediction_id
        ),
        prediction_id=prediction_id,
        signal_snapshot_id=f"signal-snapshot-{fixture}",
        input_snapshot_kinds=("signal_time",),
        probabilities={"away": 0.2, "draw": 0.3, "home": 0.5},
        market_probabilities={"away": 0.25, "draw": 0.25, "home": 0.5},
    )


def _provider(
    fixture: str = "fixture-1", *, outcome: ProviderOutcome = ProviderOutcome.SUCCESS
) -> ProviderEvidence:
    return ProviderEvidence(
        provenance=_provenance("provider", fixture=fixture),
        provider_name="offline-observed-provider",
        outcome=outcome,
        requested_fixture_count=1,
        covered_fixture_count=1 if outcome is ProviderOutcome.SUCCESS else 0,
        availability=outcome is ProviderOutcome.SUCCESS,
        latency_ms=120,
        odds_age_seconds=30,
        maximum_odds_age_seconds=300,
        bulk_requests=1,
        fallback_requests=0,
        retry_count=0 if outcome is ProviderOutcome.SUCCESS else 1,
        bulk_reused=True,
    )


def _signal(
    fixture: str = "fixture-1", *, candidate: str = "candidate-a"
) -> SignalTimeEvidence:
    return SignalTimeEvidence(
        provenance=_provenance("signal", fixture=fixture, candidate=candidate),
        candidate_name=candidate,
        eligible=True,
        odds_age_seconds=30,
        maximum_odds_age_seconds=300,
        request_load=1,
        fallback_used=False,
        stale_rejected=False,
        latency_ms=120,
        quota_cost_units=1,
        operational_complexity=2,
    )


def _result(
    fixture: str = "fixture-1", *, prediction_id: str | None = None
) -> ResultAttachment:
    prediction_id = prediction_id or f"prediction-{fixture}"
    return ResultAttachment(
        provenance=_provenance(
            "result", fixture=fixture, artifact_id=f"result-{fixture}"
        ),
        prediction_id=prediction_id,
        actual_outcome="home",
        resolved=True,
        result_source="observed-result-source",
        result_delay_seconds=300,
    )


def _bundle(*, include_results: bool = True) -> ShadowEvidenceBundle:
    predictions = (_prediction("fixture-1"), _prediction("fixture-2"))
    fixtures = ("fixture-1", "fixture-2")
    return ShadowEvidenceBundle(
        evidence_window_start=NOW - timedelta(hours=1),
        evidence_window_end=NOW + timedelta(hours=1),
        safety=SafetyAssertions(True, False, False, False, False, False, False),
        observations=tuple(
            _observation(fixture, prediction_id=f"prediction-{fixture}")
            for fixture in fixtures
        ),
        predictions=predictions,
        provider_evidence=tuple(_provider(fixture) for fixture in fixtures),
        signal_time_evidence=tuple(
            _signal(fixture, candidate=candidate)
            for fixture in fixtures
            for candidate in ("candidate-a", "candidate-b")
        ),
        quota_cost_evidence=(
            QuotaCostEvidence(
                provenance=_provenance("quota", fixture=None),
                horizon="matchday",
                total_fixture_count=2,
                logical_evaluations=2,
                bulk_odds_requests=2,
                fallback_event_requests=0,
                result_requests=1,
                revalidation_requests=0,
                closing_capture_requests=0,
                raw_http_requests=3,
                provider_cost_units=3,
            ),
        ),
        health_evidence=(
            HealthEvidence(
                provenance=_provenance("health", fixture=None),
                provider_health="observed",
                inference_health="shadow_only",
                publisher_health="disabled",
                result_source_health="observed",
                activation_state="shadow",
                registered=False,
                no_bet=True,
                publication_enabled=False,
            ),
        ),
        result_attachments=tuple(_result(fixture) for fixture in fixtures)
        if include_results
        else (),
    )


def test_valid_external_bundle_is_independently_ingested_and_measured():
    bundle = _bundle()
    bundle.validate()
    assessment = ingest_shadow_evidence(bundle)
    assert assessment.state is GateState.READY_FOR_CEO_GATE
    assert assessment.evidence_digest == evidence_digest(bundle)
    coverage = validate_coverage(bundle).by_league[0]
    assert coverage.discovered_fixtures == 2
    assert coverage.eligible_fixtures == 2
    assert coverage.valid_odds == 2
    assert coverage.predictions == 2
    assert coverage.provider_coverage == 1.0
    assert coverage.result_resolution_rate == 1.0


def test_payload_round_trip_is_deterministic_and_requires_explicit_safety_contract():
    bundle = _bundle()
    payload = bundle.as_payload()
    round_trip = ShadowEvidenceBundle.from_payload(payload)
    assert round_trip.as_payload() == payload
    assert evidence_digest(round_trip) == evidence_digest(bundle)
    missing_safety = dict(payload)
    missing_safety.pop("safety")
    assert ingest_shadow_evidence(missing_safety).state is GateState.REJECTED_SAFETY


def test_empty_and_partial_evidence_follow_formal_gate_states():
    assert assess_shadow_evidence({}).state is GateState.NO_EVIDENCE
    observing = ShadowEvidenceBundle(
        evidence_window_start=NOW,
        evidence_window_end=NOW + timedelta(hours=1),
        safety=SafetyAssertions(True, False, False, False, False, False, False),
        observations=(_observation(prediction_id=None),),
    )
    assert assess_shadow_evidence(observing).state is GateState.OBSERVING
    no_provider = replace(_bundle(), provider_evidence=())
    assert (
        assess_shadow_evidence(no_provider).state
        is GateState.PROVIDER_VALIDATION_PENDING
    )
    no_signal = replace(_bundle(), signal_time_evidence=())
    assert (
        assess_shadow_evidence(no_signal).state
        is GateState.SIGNAL_TIME_VALIDATION_PENDING
    )
    no_results = _bundle(include_results=False)
    assert (
        assess_shadow_evidence(no_results).state is GateState.SHADOW_PERFORMANCE_PENDING
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("no_bet", False),
        ("publication_enabled", True),
        ("real_bet_created", True),
        ("ledger_mutated", True),
        ("sealed_data_accessed", True),
        ("research_mutated", True),
        ("production_activation", True),
    ),
)
def test_safety_assertions_are_immediate_hard_rejections(field, value):
    safety = SafetyAssertions(True, False, False, False, False, False, False)
    bad = replace(safety, **{field: value})
    with pytest.raises(ShadowSafetyRejection):
        bad.validate()


def test_missing_provenance_wrong_sha_and_unfrozen_research_fail_closed():
    bad_provenance = replace(
        _prediction(), provenance=replace(_prediction().provenance, artifact_sha="")
    )
    with pytest.raises(ValueError, match="artifact_sha"):
        bad_provenance.validate()
    with pytest.raises(ValueError, match="unfrozen Research"):
        replace(
            _prediction(),
            provenance=replace(_prediction().provenance, research_sha="c" * 40),
        ).validate()
    assert (
        assess_shadow_evidence(replace(_bundle(), predictions=(bad_provenance,))).state
        is GateState.EVIDENCE_INCOMPLETE
    )


def test_cross_league_contamination_duplicate_evidence_and_identity_ambiguity_reject():
    contaminated = replace(
        _bundle(),
        observations=(_observation(), _observation(league="EPL")),
    )
    with pytest.raises(ShadowSafetyRejection, match="cross-league"):
        contaminated.validate()
    duplicate = replace(_bundle(), observations=(_observation(), _observation()))
    with pytest.raises(ValueError, match="duplicate"):
        duplicate.validate()
    ambiguous = replace(
        _bundle(),
        predictions=(
            _prediction("fixture-1"),
            replace(
                _prediction("fixture-2"),
                provenance=replace(
                    _prediction("fixture-2").provenance,
                    artifact_id="prediction-fixture-1",
                    artifact_sha="d" * 40,
                ),
            ),
        ),
    )
    with pytest.raises(ShadowSafetyRejection, match="ambiguous"):
        ambiguous.validate()
    mixed_source = replace(
        _bundle(),
        predictions=(
            replace(
                _prediction("fixture-1"),
                provenance=replace(
                    _prediction("fixture-1").provenance,
                    source_sha="e" * 40,
                ),
            ),
            _prediction("fixture-2"),
        ),
    )
    with pytest.raises(ShadowSafetyRejection, match="mixes source"):
        mixed_source.validate()


def test_prediction_closing_leakage_and_result_mismatch_are_rejected():
    leaked = replace(_prediction(), input_snapshot_kinds=("signal_time", "closing"))
    with pytest.raises(ShadowSafetyRejection, match="closing leakage"):
        leaked.validate()
    bad_result = replace(_result(), actual_outcome="unknown")
    with pytest.raises(ValueError, match="invalid outcome"):
        bad_result.validate()
    mismatch = replace(
        _bundle(), result_attachments=(replace(_result(), prediction_id="missing"),)
    )
    with pytest.raises(ValueError, match="missing prediction"):
        mismatch.validate()


def test_provider_assessment_scores_failures_without_selecting_authority():
    bundle = replace(
        _bundle(),
        provider_evidence=(
            _provider(),
            replace(
                _provider("fixture-2", outcome=ProviderOutcome.HTTP_429),
                covered_fixture_count=0,
            ),
        ),
    )
    assessments = evaluate_provider_evidence(bundle)
    assert len(assessments) == 1
    assessment = assessments[0]
    assert assessment.availability_rate == 0.5
    assert assessment.completeness_rate == 0.5
    assert assessment.failure_taxonomy == {"http_429": 1}
    assert assessment.as_payload()["selected"] is False


@pytest.mark.parametrize("outcome", tuple(ProviderOutcome))
def test_provider_failure_taxonomy_accepts_every_declared_operational_outcome(outcome):
    item = _provider(outcome=outcome)
    if outcome is not ProviderOutcome.SUCCESS:
        item = replace(item, covered_fixture_count=0, availability=False)
    bundle = replace(_bundle(), provider_evidence=(item,))
    report = evaluate_provider_evidence(bundle)[0]
    if outcome is ProviderOutcome.SUCCESS:
        assert report.failure_taxonomy == {}
    else:
        assert report.failure_taxonomy == {outcome.value: 1}


def test_signal_time_comparison_is_comparative_only():
    comparison = compare_signal_time_evidence(_bundle())
    assert comparison is not None
    assert {item.candidate_name for item in comparison.candidates} == {
        "candidate-a",
        "candidate-b",
    }
    assert comparison.selected_candidate is None
    assert comparison.recommendation is None
    assert all(item.as_payload()["selected"] is False for item in comparison.candidates)


def test_performance_metrics_report_sample_size_without_profitability_claims():
    metrics = calculate_performance_metrics(_bundle())
    assert metrics.sample_size == 2
    assert metrics.resolved_result_count == 2
    assert metrics.brier_score is not None
    assert metrics.market_comparison_count == 2
    assert metrics.closing_comparison_count == 0
    payload = metrics.as_payload()
    assert payload["profitability_claim"] is False
    assert payload["significance_claim"] is False


def test_closing_benchmark_is_not_prediction_input():
    benchmark_payload = {
        "provenance": _provenance(
            "closing", artifact_id="closing-fixture-1"
        ).as_payload(),
        "prediction_id": "prediction-fixture-1",
        "signal_snapshot_id": "signal-snapshot-fixture-1",
        "closing_snapshot_id": "closing-snapshot-fixture-1",
        "signal_probabilities": {"away": 0.2, "draw": 0.3, "home": 0.5},
        "closing_probabilities": {"away": 0.25, "draw": 0.25, "home": 0.5},
        "used_for_prediction": False,
    }
    bundle = replace(
        _bundle(),
        closing_benchmark_evidence=(
            ShadowEvidenceBundle.from_payload(
                {
                    "evidence_window": {
                        "start": (NOW - timedelta(hours=1)).isoformat(),
                        "end": (NOW + timedelta(hours=1)).isoformat(),
                    },
                    "safety": _bundle().safety.as_payload(),
                    "predictions": [_prediction().as_payload()],
                    "observations": [_observation().as_payload()],
                    "closing_benchmark_evidence": [benchmark_payload],
                }
            ).closing_benchmark_evidence[0],
        ),
    )
    bundle.validate()
    assert calculate_performance_metrics(bundle).closing_comparison_count == 1
    unsafe = replace(
        bundle,
        closing_benchmark_evidence=(
            replace(bundle.closing_benchmark_evidence[0], used_for_prediction=True),
        ),
    )
    with pytest.raises(ShadowSafetyRejection, match="closing benchmark"):
        unsafe.validate()


def test_gate_requires_explicit_ceo_authorization_and_digest_match():
    assessment = ShadowValidationGate.assess(_bundle())
    gate_ready = ShadowValidationGate.require_ceo_decision(assessment)
    assert gate_ready.state is GateState.CEO_DECISION_REQUIRED
    with pytest.raises(ValueError, match="evidence digest"):
        ShadowValidationGate.apply_ceo_authorization(
            gate_ready,
            CEOAuthorization("ceo-1", True, "wrong", "BL1", NOW, "explicit"),
        )
    approved = ShadowValidationGate.apply_ceo_authorization(
        gate_ready,
        CEOAuthorization(
            "ceo-1",
            True,
            gate_ready.evidence_digest,
            "BL1",
            NOW,
            "explicit CEO authorization",
        ),
    )
    assert approved.state is GateState.APPROVED_FOR_CONTROLLED_ACTIVATION
    assert approved.recommendation == "CEO_AUTHORIZED_VALIDATION_ONLY"
    assert ShadowValidationGate.assess(_bundle()).state is GateState.READY_FOR_CEO_GATE


def test_ceo_packet_is_deterministic_and_never_recommends_activation_automatically():
    bundle = _bundle()
    assessment = assess_shadow_evidence(bundle)
    packet = build_ceo_review_packet(bundle, assessment)
    payload = packet.as_payload()
    assert payload["leagues_covered"] == ["BL1"]
    assert payload["recommendation"] == "CEO_DECISION_REQUIRED"
    assert payload["closing_clv_diagnostics"]["benchmark_only"] is True
    assert "provider_authority" in payload["unresolved_decisions"]
    assert "publication_policy" in payload["unresolved_decisions"]


def test_blocking_failure_evidence_prevents_ready_state_without_mutating_inputs():
    failure = {
        "provenance": _provenance(
            "failure", fixture="fixture-1", artifact_id="failure-1"
        ).as_payload(),
        "code": "stale_evidence",
        "message": "stale odds rejected",
        "blocking": True,
    }
    bundle = ShadowEvidenceBundle.from_payload(
        {**_bundle().as_payload(), "failure_evidence": [failure]}
    )
    assessment = assess_shadow_evidence(bundle)
    assert assessment.state is GateState.SHADOW_PERFORMANCE_PENDING
    assert "stale odds rejected" in assessment.blockers
    assert bundle.safety.no_bet is True
