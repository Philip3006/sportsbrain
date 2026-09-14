from datetime import datetime, timedelta, timezone

import pytest

from src.football.production_contracts import (
    ActivationMode,
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    RolloutEvidence,
    RolloutStage,
    SignalTimeContract,
)
from src.football.top5_activation_readiness import (
    ACTIVATION_STAGE_ORDER,
    ActivationPreflight,
    ActivationStage,
    ActivationState,
    ActivationStateMachine,
    CEODecisionRegister,
    ControlledActivationHarness,
    ControlledActivationRequest,
    ProductionVerification,
    RollbackController,
    RollbackTrigger,
)
from src.football.top5_observability import Top5OperationalHealth
from src.football.top5_provider_validation import (
    ProviderAuthority,
    ProviderOutcome,
    ProviderValidationFramework,
    ProviderValidationResponse,
    ProviderValidationScenario,
    provider_failure_matrix,
    simulate_provider_validation,
)
from src.football.top5_publisher import Top5PublisherContract, Top5PublisherPayload
from src.football.top5_pwa import Top5PwaData
from src.football.top5_quota import (
    ProductionQuotaScenario,
    QuotaAssumptions,
    QuotaHorizon,
    QuotaPlannerV3,
)
from src.football.top5_shadow_performance import (
    ShadowObservation,
    ShadowPerformanceGateConfig,
    evaluate_shadow_performance_gate,
    measure_shadow_performance,
)
from src.football.top5_signal_time_matrix import (
    SignalTimeCandidateConfig,
    compare_signal_time_candidates,
)

NOW = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
CONTRACT = SignalTimeContract(60, 180, 900)
HASH = "a" * 64


def _fixture(code: str = "BL1", key: str = "fixture-1") -> Fixture:
    return Fixture(key, code, "Home FC", "Away FC", NOW + timedelta(minutes=120))


def _snapshot(key: str = "fixture-1", *, kind: MarketSnapshotKind = MarketSnapshotKind.SIGNAL_TIME, odds=None) -> MarketSnapshot:
    return MarketSnapshot(
        fixture_key=key,
        captured_at=NOW,
        kind=kind,
        source="offline-provider",
        odds=odds or {"home": 2.0, "away": 2.5},
        snapshot_id=f"snapshot-{key}",
    )


def _evidence() -> RolloutEvidence:
    return RolloutEvidence(
        research_approved=True,
        adapter_ready=True,
        offline_compatible=True,
        shadow_inference=True,
        signal_time_validated=True,
        provider_validated=True,
        shadow_performance=True,
        ceo_approved=True,
    )


def test_provider_framework_covers_all_five_and_leaves_authority_unset():
    framework = ProviderValidationFramework.for_top5()
    framework.validate()
    assert set(framework.scenarios) == {"BL1", "EPL", "LL", "SA", "L1"}
    assert all(not scenario.authority.is_complete for scenario in framework.scenarios.values())
    reports = framework.run({code: (f"{code}-fixture",) for code in framework.scenarios})
    assert set(reports) == set(framework.scenarios)
    assert all(report.activation_allowed is False for report in reports.values())
    assert all(report.fail_closed is True for report in reports.values())


@pytest.mark.parametrize("outcome", provider_failure_matrix())
def test_provider_failure_matrix_fails_closed(outcome):
    response = ProviderValidationResponse(
        outcome=outcome,
        fixture_keys=("fixture-1",) if outcome is ProviderOutcome.PARTIAL_RESPONSE else (),
        status_code=403 if outcome is ProviderOutcome.HTTP_403 else 429 if outcome is ProviderOutcome.HTTP_429 else None,
    )
    report = simulate_provider_validation(
        ProviderValidationScenario("BL1", "soccer_germany_bundesliga", max_retries=1),
        ("fixture-1",),
        responses=(response,),
        result_delayed=outcome is ProviderOutcome.RESULT_DELAY,
    )
    assert report.fail_closed is True
    assert report.activation_allowed is False


def test_provider_success_still_cannot_authorize_activation():
    report = simulate_provider_validation(
        ProviderValidationScenario(
            "BL1",
            "soccer_germany_bundesliga",
            authority=ProviderAuthority("fixture-source", "odds-source", "result-source"),
        ),
        ("fixture-1",),
    )
    assert report.coverage == 1.0
    assert report.fail_closed is False
    assert report.activation_allowed is False


def test_quota_planner_v3_keeps_all_request_categories_and_named_horizons():
    horizons = tuple(
        QuotaHorizon(name, {"BL1": 2, "EPL": 1, "LL": 1, "SA": 0, "L1": 0})
        for name in ("matchday", "24h", "72h", "week")
    )
    assumptions = QuotaAssumptions(
        retry_count=2,
        cadence_seconds=300,
        signal_window_passes=2,
        fallback_rate_by_league={"BL1": 0.25},
        revalidation_requests_per_fixture=1,
        closing_capture_requests_per_fixture=1,
        markets=("h2h", "totals"),
        regions=("eu", "uk"),
    )
    plan = QuotaPlannerV3().plan((ProductionQuotaScenario("candidate-a", horizons, assumptions),))
    payload = plan.as_payload()["candidate-a"]
    assert {estimate["horizon"] for estimate in payload} == {"matchday", "24h", "72h", "week"}
    estimate = payload[0]
    assert estimate["total_fixtures"] == 4
    assert estimate["logical_signal_evaluations"] == 12
    assert estimate["raw_http_request_count"] >= estimate["bulk_odds_requests"]
    assert estimate["estimated_provider_cost_units"] >= 0


def test_signal_time_matrix_compares_without_selecting_winner():
    candidates = (
        SignalTimeCandidateConfig("wide", CONTRACT, cadence_seconds=60, retry_count=1),
        SignalTimeCandidateConfig("tight", SignalTimeContract(30, 90, 300), cadence_seconds=300),
    )
    matrix = compare_signal_time_candidates(candidates, {"BL1": 3, "EPL": 2, "LL": 0, "SA": 0, "L1": 0})
    assert [estimate.candidate_name for estimate in matrix.estimates] == ["wide", "tight"]
    assert matrix.selected_candidate is None
    assert matrix.recommendation is None
    assert all(estimate.selected is False for estimate in matrix.estimates)


def test_shadow_measurement_keeps_closing_comparison_out_of_prediction_metrics():
    observations = (
        ShadowObservation(
            fixture_key="fixture-1",
            league_code="BL1",
            signal_time_success=True,
            inference_success=True,
            prediction_valid=True,
            candidate_available=True,
            signal_snapshot=_snapshot(),
            closing_snapshot=_snapshot(kind=MarketSnapshotKind.CLOSING, odds={"home": 1.8, "away": 2.7}),
            probabilities={"home": 0.6, "away": 0.4},
            actual_outcome="home",
            provider_latency_ms=100,
            inference_latency_ms=20,
        ),
        ShadowObservation(
            fixture_key="fixture-2",
            league_code="BL1",
            signal_time_success=False,
            inference_success=False,
            prediction_valid=False,
            candidate_available=False,
            stale_rejected=True,
            provider_failed=True,
            provenance_complete=True,
        ),
    )
    report = measure_shadow_performance(observations)
    assert report.inference_coverage == 0.5
    assert report.closing_comparison_count == 1
    assert report.market_comparison_count == 2
    assert report.closing_excluded is True
    assert report.brier_score is not None
    result = evaluate_shadow_performance_gate(
        report,
        ShadowPerformanceGateConfig(
            minimum_fixture_coverage=1.0,
            minimum_inference_coverage=0.5,
            maximum_stale_rate=0.6,
            maximum_provider_failure_rate=0.6,
        ),
    )
    assert result.passed is True
    assert result.safe_to_activate is False


def test_shadow_observation_rejects_closing_snapshot_as_signal_input():
    with pytest.raises(ValueError, match="closing values"):
        ShadowObservation(
            fixture_key="fixture-1",
            league_code="BL1",
            signal_time_success=True,
            inference_success=True,
            prediction_valid=True,
            candidate_available=True,
            signal_snapshot=_snapshot(kind=MarketSnapshotKind.CLOSING),
            probabilities={"home": 1.0},
        ).validate()


def test_activation_state_machine_is_cumulative_and_has_production_verification():
    machine = ActivationStateMachine()
    assert machine.check(ActivationStage.RESEARCH_APPROVED).passed is False
    with pytest.raises(ValueError, match="research_approved"):
        machine.require(ActivationStage.RESEARCH_APPROVED)
    evidence = _evidence()
    state = ActivationStateMachine(state=ActivationState(evidence))
    assert state.check(ActivationStage.CONTROLLED_ACTIVATION).passed is True
    state.enter(ActivationStage.CONTROLLED_ACTIVATION)
    assert state.state.controlled_activation is True
    assert state.check(ActivationStage.PRODUCTION_VERIFIED).passed is True
    state.enter(ActivationStage.PRODUCTION_VERIFIED)
    assert state.state.production_verified is True
    state.state.evidence.require(RolloutStage.PRODUCTION_VERIFIED)
    assert tuple(ACTIVATION_STAGE_ORDER)[-1] is ActivationStage.PRODUCTION_VERIFIED


def _activation_request() -> ControlledActivationRequest:
    return ControlledActivationRequest(
        league_code="BL1",
        candidate_id="candidate-1",
        model_identity="future-model-1",
        source_sha=HASH,
        research_sha=HASH,
        model_artifact_hash=HASH,
        provider_authority=ProviderAuthority("fixture-source", "odds-source", "result-source"),
        signal_time_contract=CONTRACT,
        rollback_pointer="safe-disabled:BL1",
        config_snapshot={"league": "BL1", "candidate": "candidate-1"},
        ceo_authorization_token="explicit-future-token",
        ceo_authorized=True,
    )


def test_controlled_activation_harness_prepares_but_cannot_execute():
    preflight = ActivationPreflight(
        source_sha_matches=True,
        research_sha_matches=True,
        model_hash_matches=True,
        league_matches=True,
        signal_time_configured=True,
        provider_authority_configured=True,
        rollback_ready=True,
        no_closing_leakage=True,
    )
    plan = ControlledActivationHarness().prepare(
        _activation_request(),
        _evidence(),
        preflight,
        prepared_at=NOW,
    )
    assert plan.executed is False
    with pytest.raises(ValueError, match="execution is disabled"):
        ControlledActivationHarness().execute(plan)


def test_post_activation_verification_is_complete_only_with_every_check():
    verification = ProductionVerification(**{field: True for field in (
        "source_sha_matches", "research_sha_matches", "model_hash_matches", "league_matches",
        "signal_time_matches", "provider_authority_matches", "no_closing_leakage", "pwa_available",
        "publisher_healthy", "ledger_safe", "settlement_compatible", "rollback_ready",
    )})
    assert verification.passed is True
    assert ControlledActivationHarness().verify(
        ControlledActivationHarness().prepare(
            _activation_request(),
            _evidence(),
            ActivationPreflight(
                source_sha_matches=True,
                research_sha_matches=True,
                model_hash_matches=True,
                league_matches=True,
                signal_time_configured=True,
                provider_authority_configured=True,
                rollback_ready=True,
                no_closing_leakage=True,
            ),
            prepared_at=NOW,
        ),
        verification,
    ).passed is True


def test_ceo_decision_register_keeps_all_policy_decisions_unresolved():
    register = CEODecisionRegister()
    register.validate()
    assert len(register.unresolved) == 9


@pytest.mark.parametrize("trigger", tuple(RollbackTrigger))
def test_every_rollback_trigger_restores_safe_disabled_state(trigger):
    result = RollbackController().rollback(trigger)
    assert result.restored_disabled is True
    assert result.activation_mode is ActivationMode.DISABLED
    assert result.no_bet is True
    assert result.publication_enabled is False
    assert result.scheduler_enabled is False
    assert result.ledger_mutated is False


def test_publisher_contract_is_narrow_shadow_only_and_provenance_complete():
    payload = Top5PublisherPayload(
        artifact_path="docs/data/top5/shadow/BL1/signal-1.json",
        league_code="BL1",
        fixture_key="fixture-1",
        candidate_id="candidate-1",
        model_identity="future-model-1",
        signal_id="signal-1",
        signal_generated_at=NOW,
        source_sha=HASH,
        research_sha=HASH,
        model_artifact_hash=HASH,
        probabilities={"home": 0.6, "away": 0.4},
        snapshot_age_seconds=30,
        provenance={"source_sha": HASH, "research_sha": HASH, "model_artifact_hash": HASH},
    )
    staged = Top5PublisherContract().stage_shadow(payload)
    assert staged.published is False
    with pytest.raises(ValueError, match="publication"):
        Top5PublisherContract().publish(payload)


def test_pwa_and_operational_health_contracts_remain_disabled():
    pwa = Top5PwaData(
        league="BL1",
        fixture="fixture-1",
        kickoff=NOW + timedelta(hours=2),
        probabilities={"home": 0.6, "away": 0.4},
        model_identity="future-model-1",
        signal_timestamp=NOW,
        provenance={"source_sha": HASH, "research_sha": HASH, "model_artifact_hash": HASH},
    )
    pwa.validate()
    health = Top5OperationalHealth(
        league_code="BL1",
        provider_health="not_configured",
        fixture_coverage=1.0,
        odds_freshness=1.0,
        signal_time_coverage=1.0,
        inference_health="shadow_only",
        publisher_health="disabled",
        result_source_health="not_configured",
        fallback_rate=0.0,
        quota_cost_usage=0.0,
        stale_rate=0.0,
        retry_rate=0.0,
        duplicate_suppression_count=0,
        last_successful_cycle=NOW,
    )
    payload = health.as_payload()
    assert payload["activation_state"] == "disabled"
    assert payload["registered"] is False
    assert payload["no_bet"] is True


def test_core_rollout_evidence_exposes_production_verified_without_changing_old_gate_contract():
    evidence = RolloutEvidence(**{field: True for field in (
        "research_approved", "adapter_ready", "offline_compatible", "shadow_inference",
        "signal_time_validated", "provider_validated", "shadow_performance", "ceo_approved",
    )}, controlled_activation=True, production_verified=True)
    evidence.require(RolloutStage.PRODUCTION_VERIFIED)


def test_core_production_verified_requires_explicit_controlled_activation():
    evidence = RolloutEvidence(**{field: True for field in (
        "research_approved", "adapter_ready", "offline_compatible", "shadow_inference",
        "signal_time_validated", "provider_validated", "shadow_performance", "ceo_approved",
    )}, production_verified=True)
    with pytest.raises(ValueError, match="production_verified"):
        evidence.require(RolloutStage.PRODUCTION_VERIFIED)
