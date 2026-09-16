"""No-network tests for the controlled provider qualification gate."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.top5_controlled_shadow_provider_qualification import (
    CAPTURE_ATTESTATION_CONTRACT_VERSION,
    NO_PRODUCTION_SIGNAL_TIME_VALUES,
    QUALIFICATION_CONTRACT_VERSION,
    CEOAuthorization,
    ControlledShadowCaptureAttestation,
    MinimumSamplePolicy,
    ObservationEvidenceKind,
    ProviderQualificationSession,
    ProviderQualificationStatus,
    ProviderTimestampProvenance,
    QualificationCode,
    QualificationContractError,
    QualificationTimingPolicy,
    RealProviderObservation,
    bridge_real_observation_to_builder1_shadow_evidence,
    offline_fixture_catalog,
    qualify_provider_observations,
)
from src.football.top5_provider_cascade_validation import (
    BudgetDecision,
    CascadeAttempt,
    CascadeEvidence,
    CascadeOutcome,
    CascadeProvenance,
    CascadeQuotaSnapshot,
    CascadeSafety,
    ExecutionMode,
    ExpectedCascadeFixture,
    MarketPhase,
    RequestCostClassification,
    evidence_digest,
)
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA
from src.football.top5_shadow_provider_redundancy import (
    ProviderReadinessState,
    make_fixture_key,
)

UTC = timezone.utc
CAPTURED = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
KICKOFF = CAPTURED + timedelta(hours=2)
EXPECTED = ExpectedCascadeFixture(
    league="EPL",
    fixture_key=make_fixture_key("EPL", "Manchester United", "Arsenal", KICKOFF),
    home_team="Manchester United",
    away_team="Arsenal",
    kickoff=KICKOFF,
)
ORDER = (
    "the_odds_api",
    "odds_api_io",
    "api_football",
    "betfair_delayed",
)
READY = {
    provider: ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION
    for provider in ORDER
}
TIMING = QualificationTimingPolicy(900, 300, 600, 10_800)
SOURCE_SHA = "c" * 64
RAW_DIGEST = "d" * 64


def _cascade() -> CascadeEvidence:
    start = CAPTURED - timedelta(seconds=2)
    end = CAPTURED
    attempt = CascadeAttempt(
        league="EPL",
        fixture_key=EXPECTED.fixture_key,
        home_team=EXPECTED.home_team,
        away_team=EXPECTED.away_team,
        kickoff=KICKOFF,
        configured_provider_order=ORDER,
        provider_attempt_index=0,
        fallback_depth=0,
        provider_identity="the_odds_api",
        network_called=True,
        start_timestamp=start,
        end_timestamp=end,
        capture_timestamp=end,
        outcome=CascadeOutcome.SUCCESS,
        failure_classification=None,
        market_type="h2h_1x2",
        home_odds=2.2,
        draw_odds=3.4,
        away_odds=3.0,
        bookmaker_identity="bookmaker-1",
        source_identity="the_odds_api_feed",
        market_phase=MarketPhase.PRE_MATCH,
        source_timestamp=CAPTURED - timedelta(minutes=5),
        request_latency_ms=2000,
        quota_before=CascadeQuotaSnapshot(True, 500, 10),
        quota_after=CascadeQuotaSnapshot(True, 501, 9),
        preflight_allowed=True,
        budget_decision=BudgetDecision.ALLOWED,
        request_cost_classification=RequestCostClassification.QUOTA_CONSUMING_REQUEST,
        network_request_count=1,
        quota_cost_units=1.0,
        credentials_available=True,
        provider_record_id="the_odds_api-event-1",
        adapter_version="the_odds_api-adapter-v1",
        raw_record_digest=RAW_DIGEST,
        request_identity="request-1",
        provider_readiness_state=ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION,
    )
    return CascadeEvidence(
        provenance=CascadeProvenance(
            evidence_id="cascade-evidence-1",
            artifact_id="cascade-artifact-1",
            artifact_sha="a" * 64,
            source_sha="b" * 64,
            research_sha=FROZEN_RESEARCH_SHA,
            candidate_id="candidate-shadow-v1",
            model_identity="unbound-model-slot",
            generated_at=CAPTURED,
        ),
        configured_provider_order=ORDER,
        execution_mode=ExecutionMode.SEQUENTIAL,
        attempts=(attempt,),
        skipped_providers=(),
        selected_provider="the_odds_api",
        prediction_input_allowed=True,
        safety=CascadeSafety(True, False, False, False, False, False, False),
    )


def _session(
    *,
    fixture_scope: tuple[str, ...] = (EXPECTED.fixture_key,),
    qualification_state: ProviderReadinessState = ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION,
) -> ProviderQualificationSession:
    return ProviderQualificationSession(
        qualification_session_id="qualification-session-1",
        schema_version=QUALIFICATION_CONTRACT_VERSION,
        created_at=CAPTURED,
        provider_identity="top5_cascade",
        league_scope=("EPL",),
        fixture_scope=fixture_scope,
        configured_provider_order=ORDER,
        adapter_version="the_odds_api-adapter-v1",
        adapter_source_sha=SOURCE_SHA,
        qualification_state=qualification_state,
    )


def _authorization(
    *,
    fixture_scope: tuple[str, ...] = (EXPECTED.fixture_key,),
    maximum_network_requests: int = 1,
) -> CEOAuthorization:
    return CEOAuthorization(
        authorization_id="ceo-auth-1",
        controlled_shadow_run_id="controlled-run-1",
        qualification_session_id="qualification-session-1",
        provider_scope=("the_odds_api",),
        league_scope=("EPL",),
        fixture_scope=fixture_scope,
        maximum_network_requests=maximum_network_requests,
        monetary_spend_authorized=False,
        issued_at=CAPTURED - timedelta(hours=1),
        expires_at=CAPTURED + timedelta(hours=1),
    )


def _attestation() -> ControlledShadowCaptureAttestation:
    return ControlledShadowCaptureAttestation(
        controlled_shadow_run_id="controlled-run-1",
        ceo_authorization_id="ceo-auth-1",
        qualification_session_id="qualification-session-1",
        provider_identity="the_odds_api",
        fixture_key=EXPECTED.fixture_key,
        provider_event_id="the_odds_api-event-1",
        provider_request_id="request-1",
        adapter_version="the_odds_api-adapter-v1",
        adapter_source_sha=SOURCE_SHA,
        cascade_evidence_digest=evidence_digest(_cascade()),
        raw_response_digest=RAW_DIGEST,
        normalized_record_digest="e" * 64,
        captured_at=CAPTURED,
        network_execution=True,
        no_bet=True,
        publication=False,
        monetary_spend_authorized=False,
        schema_version=CAPTURE_ATTESTATION_CONTRACT_VERSION,
    )


def _observation(
    kind: ObservationEvidenceKind = ObservationEvidenceKind.REAL_OBSERVED,
) -> RealProviderObservation:
    return RealProviderObservation(
        observation_id="observation-1",
        qualification_session_id="qualification-session-1",
        evidence_kind=kind,
        provider_identity="the_odds_api",
        provider_event_id="the_odds_api-event-1",
        provider_request_id="request-1",
        league="EPL",
        fixture_key=EXPECTED.fixture_key,
        home_team=EXPECTED.home_team,
        away_team=EXPECTED.away_team,
        kickoff=KICKOFF,
        market_type="h2h_1x2",
        market_phase="PRE_MATCH",
        home_odds=2.2,
        draw_odds=3.4,
        away_odds=3.0,
        bookmaker_identity="bookmaker-1",
        source_identity="the_odds_api_feed",
        source_timestamp=CAPTURED - timedelta(minutes=5),
        provider_timestamp_provenance=ProviderTimestampProvenance.PROVIDER_SOURCE_TIMESTAMP,
        captured_at=CAPTURED,
        request_started_at=CAPTURED - timedelta(seconds=2),
        request_finished_at=CAPTURED,
        latency_ms=2000,
        adapter_version="the_odds_api-adapter-v1",
        adapter_source_sha=SOURCE_SHA,
        raw_response_digest=RAW_DIGEST,
        normalized_record_digest="e" * 64,
        cascade_evidence=_cascade(),
        quota_before=10,
        quota_after=9,
        quota_cost_units=1.0 if kind is ObservationEvidenceKind.REAL_OBSERVED else 0.0,
        network_request_count=1 if kind is ObservationEvidenceKind.REAL_OBSERVED else 0,
        capture_attestation=(
            _attestation() if kind is ObservationEvidenceKind.REAL_OBSERVED else None
        ),
    )


def _observation_for_cascade(
    cascade: CascadeEvidence,
    selected: CascadeAttempt,
    *,
    expected: ExpectedCascadeFixture = EXPECTED,
    observation_id: str = "observation-1",
) -> RealProviderObservation:
    captured_at = selected.capture_timestamp
    assert captured_at is not None
    attestation = replace(
        _attestation(),
        provider_identity=selected.provider_identity,
        fixture_key=expected.fixture_key,
        provider_event_id=selected.provider_record_id,
        provider_request_id=selected.request_identity,
        adapter_version=selected.adapter_version,
        cascade_evidence_digest=evidence_digest(cascade),
        raw_response_digest=selected.raw_record_digest,
        captured_at=captured_at,
    )
    return replace(
        _observation(),
        observation_id=observation_id,
        provider_identity=selected.provider_identity,
        provider_event_id=selected.provider_record_id,
        provider_request_id=selected.request_identity,
        league=expected.league,
        fixture_key=expected.fixture_key,
        home_team=expected.home_team,
        away_team=expected.away_team,
        kickoff=expected.kickoff,
        bookmaker_identity=selected.bookmaker_identity or "",
        source_identity=selected.source_identity or "",
        source_timestamp=selected.source_timestamp,
        captured_at=captured_at,
        request_started_at=selected.start_timestamp,
        request_finished_at=selected.end_timestamp,
        latency_ms=selected.request_latency_ms,
        adapter_version=selected.adapter_version,
        raw_response_digest=selected.raw_record_digest,
        cascade_evidence=cascade,
        quota_before=(
            selected.quota_before.quota_remaining
            if selected.quota_before is not None
            else None
        ),
        quota_after=(
            selected.quota_after.quota_remaining
            if selected.quota_after is not None
            else None
        ),
        quota_cost_units=selected.quota_cost_units or 0.0,
        network_request_count=selected.network_request_count or 0,
        capture_attestation=attestation,
    )


def _expected_fixture(index: int) -> ExpectedCascadeFixture:
    kickoff = KICKOFF + timedelta(minutes=5 * index)
    home = f"Home Team {index}"
    away = f"Away Team {index}"
    return ExpectedCascadeFixture(
        league="EPL",
        fixture_key=make_fixture_key("EPL", home, away, kickoff),
        home_team=home,
        away_team=away,
        kickoff=kickoff,
    )


def _cascade_for_expected(
    expected: ExpectedCascadeFixture, *, suffix: str = "1"
) -> CascadeEvidence:
    attempt = replace(
        _cascade().attempts[0],
        league=expected.league,
        fixture_key=expected.fixture_key,
        home_team=expected.home_team,
        away_team=expected.away_team,
        kickoff=expected.kickoff,
        provider_record_id=f"the_odds_api-event-{suffix}",
        request_identity=f"request-{suffix}",
    )
    return replace(
        _cascade(),
        attempts=(attempt,),
        selected_provider=attempt.provider_identity,
    )


def _cascade_attempt(
    provider: str,
    index: int,
    outcome: CascadeOutcome,
    *,
    quota_cost_units: float | None = None,
) -> CascadeAttempt:
    base = _cascade().attempts[0]
    start = CAPTURED + timedelta(seconds=index * 3)
    network_called = outcome is not CascadeOutcome.QUOTA_EXHAUSTED
    if quota_cost_units is None:
        quota_cost_units = 1.0 if network_called else 0.0
    quota_before = CascadeQuotaSnapshot(True, 500 + index, 10 if network_called else 0)
    quota_after = CascadeQuotaSnapshot(
        True, 501 + index if network_called else 500 + index, 9 if network_called else 0
    )
    return replace(
        base,
        provider_identity=provider,
        provider_attempt_index=index,
        fallback_depth=index,
        start_timestamp=start,
        end_timestamp=start + timedelta(seconds=1),
        capture_timestamp=start + timedelta(seconds=1),
        outcome=outcome,
        failure_classification=None if outcome is CascadeOutcome.SUCCESS else outcome,
        network_called=network_called,
        quota_before=quota_before,
        quota_after=quota_after,
        preflight_allowed=True,
        budget_decision=BudgetDecision.ALLOWED,
        network_request_count=int(network_called),
        quota_cost_units=quota_cost_units,
        provider_record_id=f"{provider}-event-{index}",
        adapter_version=f"{provider}-adapter-v1",
        request_identity=f"request-{provider}-{index}",
        provider_readiness_state=(
            ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION
            if outcome is CascadeOutcome.SUCCESS
            else ProviderReadinessState.CONTRACT_SUPPORTED
        ),
    )


def _observation_with_outcomes(
    outcomes: tuple[CascadeOutcome, ...],
    *,
    expected: ExpectedCascadeFixture = EXPECTED,
    observation_id: str = "observation-1",
    quota_cost_units: tuple[float | None, ...] | None = None,
) -> RealProviderObservation:
    order = ORDER[: len(outcomes)]
    attempts = tuple(
        _cascade_attempt(
            provider,
            index,
            outcome,
            quota_cost_units=(quota_cost_units[index] if quota_cost_units else None),
        )
        for index, (provider, outcome) in enumerate(zip(order, outcomes, strict=True))
    )
    success = next(item for item in attempts if item.outcome is CascadeOutcome.SUCCESS)
    cascade = replace(
        _cascade(),
        attempts=attempts,
        selected_provider=success.provider_identity,
    )
    return _observation_for_cascade(
        cascade, success, expected=expected, observation_id=observation_id
    )


_DEFAULT_AUTHORIZATION = object()


def _qualify(observations, *, authorization=_DEFAULT_AUTHORIZATION):
    if authorization is _DEFAULT_AUTHORIZATION:
        authorization = _authorization()
    return qualify_provider_observations(
        observations,
        _session(),
        EXPECTED,
        TIMING,
        READY,
        authorization,
    )


def test_valid_real_observation_requires_proof_and_is_qualified() -> None:
    report = _qualify((_observation(),))
    result = report.results[0]
    assert (
        report.qualification_status
        is ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
    )
    assert result.status is ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
    assert (
        report.session.qualification_state
        is ProviderReadinessState.REAL_OBSERVATION_VALIDATED
    )
    assert report.session.real_observation_count == 1
    assert report.session.accepted_observation_count == 1
    assert report.coverage[0].coverage_rate is None
    epl = next(
        item
        for item in report.coverage
        if item.provider_identity == "the_odds_api" and item.league == "EPL"
    )
    assert epl.coverage_rate == 1.0
    assert epl.freshness.n == 1
    assert report.signal_time_note == NO_PRODUCTION_SIGNAL_TIME_VALUES
    assert report.production_activation_authorized is False


@pytest.mark.parametrize(
    "kind",
    [
        ObservationEvidenceKind.TEST_FIXTURE,
        ObservationEvidenceKind.MOCK,
        ObservationEvidenceKind.OFFLINE_REPLAY,
    ],
)
def test_non_real_evidence_is_contract_only_and_never_promotes(
    kind: ObservationEvidenceKind,
) -> None:
    report = _qualify((_observation(kind),), authorization=None)
    assert (
        report.qualification_status
        is ProviderQualificationStatus.OBSERVED_VALID_CONTRACT
    )
    assert report.session.real_observation_count == 0
    assert report.accepted_observation_ids == ()
    assert report.results[0].real_observed is False


def test_missing_authorization_rejects_real_evidence() -> None:
    result = _qualify((_observation(),), authorization=None).results[0]
    assert result.status is ProviderQualificationStatus.OBSERVED_REJECTED
    assert QualificationCode.AUTHORIZATION_MISSING.value in result.failure_codes


def test_authorization_request_budget_is_fail_closed() -> None:
    second = replace(_observation(), observation_id="observation-2")
    authorization = replace(_authorization(), maximum_network_requests=1)
    report = _qualify((_observation(), second), authorization=authorization)
    assert all(
        QualificationCode.NETWORK_BUDGET_EXCEEDED.value in result.failure_codes
        for result in report.results
        if result.real_observed
    )


def test_real_proof_fields_are_required() -> None:
    observation = replace(_observation(), provider_event_id="", raw_response_digest="")
    result = _qualify((observation,)).results[0]
    assert result.status is ProviderQualificationStatus.OBSERVED_REJECTED
    assert QualificationCode.INVALID_OBSERVATION.value in result.failure_codes


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("source_timestamp", None, QualificationCode.SOURCE_TIME_REQUIRED),
        (
            "provider_timestamp_provenance",
            ProviderTimestampProvenance.CAPTURE_TIME_ONLY,
            QualificationCode.CAPTURE_TIME_ONLY,
        ),
        (
            "provider_timestamp_provenance",
            ProviderTimestampProvenance.UNKNOWN,
            QualificationCode.UNKNOWN_SOURCE_TIME,
        ),
        ("draw_odds", None, QualificationCode.MISSING_DRAW),
        ("market_phase", "IN_PLAY", QualificationCode.IN_PLAY_MARKET),
        ("synthetic_reconstruction", True, QualificationCode.SYNTHETIC_RECONSTRUCTION),
    ],
)
def test_quality_failure_matrix_is_fail_closed(
    field: str, value: object, code: QualificationCode
) -> None:
    result = _qualify((replace(_observation(), **{field: value}),)).results[0]
    assert result.status is ProviderQualificationStatus.OBSERVED_REJECTED
    assert (
        code.value in result.failure_codes
        or QualificationCode.INVALID_OBSERVATION.value in result.failure_codes
    )


def test_stale_future_and_lead_timing_failures() -> None:
    stale = replace(_observation(), source_timestamp=CAPTURED - timedelta(hours=2))
    future = replace(_observation(), source_timestamp=CAPTURED + timedelta(minutes=1))
    too_late = replace(
        _observation(),
        captured_at=KICKOFF - timedelta(seconds=30),
        request_started_at=KICKOFF - timedelta(seconds=32),
        request_finished_at=KICKOFF - timedelta(seconds=30),
    )
    for observation, code in (
        (stale, QualificationCode.STALE_OBSERVATION),
        (future, QualificationCode.FUTURE_SOURCE_TIMESTAMP),
        (too_late, QualificationCode.MINIMUM_LEAD_NOT_MET),
    ):
        result = _qualify((observation,)).results[0]
        assert result.status is ProviderQualificationStatus.OBSERVED_REJECTED
        assert code.value in result.failure_codes


def test_exact_fixture_and_team_orientation_are_enforced() -> None:
    wrong = replace(_observation(), home_team="Arsenal", away_team="Manchester United")
    result = _qualify((wrong,)).results[0]
    assert QualificationCode.HOME_AWAY_INVERSION.value in result.failure_codes
    wrong_league = replace(_observation(), league="BL1")
    assert (
        QualificationCode.WRONG_LEAGUE.value
        in _qualify((wrong_league,)).results[0].failure_codes
    )


def test_duplicate_idempotency_and_conflicting_duplicate_rejection() -> None:
    report = _qualify((_observation(), _observation()))
    assert report.session.accepted_observation_count == 1
    assert any(result.duplicate_suppressed for result in report.results)
    conflict = replace(_observation(), provider_request_id="request-conflict")
    conflicting = _qualify((_observation(), conflict))
    assert all(
        result.status is ProviderQualificationStatus.OBSERVED_REJECTED
        for result in conflicting.results
    )
    assert any(
        QualificationCode.CONFLICTING_DUPLICATE.value in result.failure_codes
        for result in conflicting.results
    )


def test_archive_is_distinct_and_serialization_is_stable() -> None:
    report = _qualify((_observation(),))
    payload = report.as_payload()
    assert payload["session"]["archive"]["manifest"].startswith(
        "top5-provider-qualification/"
    )
    assert "replay" not in payload["session"]["archive"]["manifest"]
    assert (
        RealProviderObservation.from_payload(_observation().as_payload()).as_payload()
        == _observation().as_payload()
    )


def test_builder_one_bridge_is_no_bet_and_does_not_create_prediction() -> None:
    evidence = bridge_real_observation_to_builder1_shadow_evidence(
        _observation(), _session(), EXPECTED, TIMING, READY, _authorization()
    )
    assert evidence.no_bet is True
    assert evidence.publication_enabled is False
    assert evidence.prediction_id is None


def test_bridge_rejects_fixture_evidence() -> None:
    with pytest.raises(QualificationContractError):
        bridge_real_observation_to_builder1_shadow_evidence(
            _observation(ObservationEvidenceKind.TEST_FIXTURE),
            _session(),
            EXPECTED,
            TIMING,
            READY,
        )


def test_timing_policy_has_no_production_defaults() -> None:
    with pytest.raises(TypeError):
        QualificationTimingPolicy()  # type: ignore[call-arg]
    assert TIMING.as_payload()["note"] == NO_PRODUCTION_SIGNAL_TIME_VALUES


def test_one_real_observation_is_technically_valid_but_not_a_production_sample() -> (
    None
):
    report = qualify_provider_observations(
        (_observation(),),
        _session(),
        EXPECTED,
        TIMING,
        READY,
        _authorization(),
        minimum_sample_policy=MinimumSamplePolicy(2),
    )
    assert (
        report.qualification_status
        is ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
    )
    assert report.production_sample_sufficient is False
    assert "INSUFFICIENT_PRODUCTION_SAMPLE" in report.unresolved


def test_authorization_is_scoped_expiring_single_use_and_never_paid() -> None:
    authorization = _authorization()
    assert authorization.single_use is True
    assert authorization.monetary_spend_authorized is False
    expired = replace(authorization, expires_at=CAPTURED - timedelta(minutes=1))
    result = _qualify((_observation(),), authorization=expired).results[0]
    assert QualificationCode.AUTHORIZATION_EXPIRED.value in result.failure_codes


def test_betfair_requires_explicit_delayed_provenance() -> None:
    observation = replace(
        _observation(),
        provider_identity="betfair_delayed",
        delayed_observation=False,
        cascade_evidence=replace(
            _cascade(),
            configured_provider_order=("betfair_delayed",),
            attempts=(
                replace(
                    _cascade().attempts[0],
                    configured_provider_order=("betfair_delayed",),
                    provider_identity="betfair_delayed",
                    provider_record_id="betfair_delayed-event-1",
                    request_identity="request-betfair",
                    adapter_version="betfair-adapter-v1",
                ),
            ),
            skipped_providers=(),
            selected_provider="betfair_delayed",
        ),
    )
    result = _qualify((observation,)).results[0]
    assert result.status is ProviderQualificationStatus.OBSERVED_REJECTED


def test_metrics_have_no_fake_denominator_and_cover_only_top5_leagues() -> None:
    report = _qualify(())
    assert all(item.coverage_rate is None for item in report.coverage)
    assert {item.league for item in report.coverage} == {"BL1", "EPL", "LL", "SA", "L1"}
    assert all(item.league != "CL" for item in report.coverage)


def test_offline_catalog_is_explicitly_non_real() -> None:
    catalog = offline_fixture_catalog()
    assert {item["provider_identity"] for item in catalog} == set(ORDER)
    assert all(
        item["evidence_kind"] == "TEST_FIXTURE" and item["counts_as_real"] is False
        for item in catalog
    )


def test_real_observed_marker_without_controlled_capture_attestation_rejects() -> None:
    result = _qualify((replace(_observation(), capture_attestation=None),)).results[0]
    assert result.status is ProviderQualificationStatus.OBSERVED_REJECTED
    assert QualificationCode.CAPTURE_ATTESTATION_MISSING.value in result.failure_codes


def test_fixture_with_fake_attestation_never_becomes_real() -> None:
    report = _qualify(
        (
            replace(
                _observation(ObservationEvidenceKind.TEST_FIXTURE),
                capture_attestation=_attestation(),
            ),
        ),
        authorization=None,
    )
    assert (
        report.results[0].status is ProviderQualificationStatus.OBSERVED_VALID_CONTRACT
    )
    assert report.results[0].real_observed is False
    assert report.accepted_real_observation_count == 0


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        (
            "ceo_authorization_id",
            "ceo-auth-other",
            QualificationCode.AUTHORIZATION_ID_MISMATCH,
        ),
        (
            "controlled_shadow_run_id",
            "controlled-run-other",
            QualificationCode.AUTHORIZATION_RUN_MISMATCH,
        ),
        (
            "qualification_session_id",
            "qualification-session-other",
            QualificationCode.AUTHORIZATION_SESSION_MISMATCH,
        ),
        (
            "provider_identity",
            "odds_api_io",
            QualificationCode.CAPTURE_ATTESTATION_MISMATCH,
        ),
        ("fixture_key", "EPL::wrong-fixture", QualificationCode.WRONG_FIXTURE),
        (
            "provider_event_id",
            "wrong-event",
            QualificationCode.PROVIDER_EVENT_ID_MISMATCH,
        ),
        (
            "provider_request_id",
            "wrong-request",
            QualificationCode.PROVIDER_REQUEST_ID_MISMATCH,
        ),
        ("raw_response_digest", "f" * 64, QualificationCode.RAW_DIGEST_MISMATCH),
        (
            "normalized_record_digest",
            "f" * 64,
            QualificationCode.NORMALIZED_DIGEST_MISMATCH,
        ),
        (
            "cascade_evidence_digest",
            "f" * 64,
            QualificationCode.CASCADE_DIGEST_MISMATCH,
        ),
    ],
)
def test_controlled_capture_attestation_is_exactly_bound(
    field: str, value: object, code: QualificationCode
) -> None:
    attestation = replace(_attestation(), **{field: value})
    result = _qualify(
        (replace(_observation(), capture_attestation=attestation),)
    ).results[0]
    assert result.status is ProviderQualificationStatus.OBSERVED_REJECTED
    assert code.value in result.failure_codes


def test_exact_serialized_controlled_capture_attestation_is_eligible() -> None:
    observation = _observation()
    payload = observation.as_payload()
    restored = RealProviderObservation.from_payload(payload)
    report = _qualify((restored,))
    assert report.accepted_observation_ids == (observation.observation_id,)
    assert restored.capture_attestation is not None
    assert ControlledShadowCaptureAttestation.from_payload(
        restored.as_payload()["capture_attestation"]
    ).cascade_evidence_digest == evidence_digest(observation.cascade_evidence)


@pytest.mark.parametrize(
    ("outcomes", "maximum", "expected_network", "accepted"),
    [
        (
            (CascadeOutcome.QUOTA_EXHAUSTED, CascadeOutcome.SUCCESS),
            1,
            1,
            True,
        ),
        ((CascadeOutcome.TIMEOUT, CascadeOutcome.SUCCESS), 2, 2, True),
        (
            (CascadeOutcome.TIMEOUT, CascadeOutcome.HTTP_403, CascadeOutcome.SUCCESS),
            3,
            3,
            True,
        ),
        ((CascadeOutcome.TIMEOUT, CascadeOutcome.SUCCESS), 1, 2, False),
    ],
)
def test_authorization_budget_counts_every_cascade_attempt(
    outcomes: tuple[CascadeOutcome, ...],
    maximum: int,
    expected_network: int,
    accepted: bool,
) -> None:
    observation = _observation_with_outcomes(outcomes)
    authorization = replace(
        _authorization(
            maximum_network_requests=maximum,
        ),
        provider_scope=ORDER,
    )
    report = _qualify((observation,), authorization=authorization)
    assert report.cascade_network_request_count == expected_network
    assert report.session.network_request_count == expected_network
    assert report.session.selected_observation_network_request_count == 1
    assert report.results[0].accepted is accepted


def test_quota_units_are_reported_independently_from_network_requests() -> None:
    observation = _observation_with_outcomes(
        (CascadeOutcome.TIMEOUT, CascadeOutcome.SUCCESS),
        quota_cost_units=(2.5, 0.25),
    )
    report = _qualify(
        (observation,),
        authorization=replace(
            _authorization(maximum_network_requests=2), provider_scope=ORDER
        ),
    )
    assert report.cascade_network_request_count == 2
    assert report.selected_observation_network_request_count == 1
    assert report.quota_units_observed == 2.75
    assert report.session.quota_units_observed == 2.75


def test_authorization_single_use_is_external_and_run_bound() -> None:
    same_run_usage = {
        "ceo-auth-1": {
            "controlled_shadow_run_id": "controlled-run-1",
            "qualification_session_id": "qualification-session-1",
        }
    }
    same_run = _qualify(
        (_observation(),),
        authorization=_authorization(),
    )
    assert same_run.accepted_observation_ids == ("observation-1",)
    allowed_again = qualify_provider_observations(
        (_observation(),),
        _session(),
        EXPECTED,
        TIMING,
        READY,
        _authorization(),
        authorization_usage=same_run_usage,
    )
    assert allowed_again.accepted_observation_ids == ("observation-1",)
    consumed_elsewhere = qualify_provider_observations(
        (_observation(),),
        _session(),
        EXPECTED,
        TIMING,
        READY,
        _authorization(),
        authorization_usage={
            "ceo-auth-1": {
                "controlled_shadow_run_id": "controlled-run-other",
                "qualification_session_id": "qualification-session-other",
            }
        },
    )
    assert (
        QualificationCode.AUTHORIZATION_ALREADY_CONSUMED.value
        in consumed_elsewhere.results[0].failure_codes
    )
    consumed_by_id = _qualify(
        (_observation(),),
        authorization=_authorization(),
    )
    consumed_by_id = qualify_provider_observations(
        (_observation(),),
        _session(),
        EXPECTED,
        TIMING,
        READY,
        _authorization(),
        consumed_authorization_ids=("ceo-auth-1",),
    )
    assert (
        QualificationCode.AUTHORIZATION_ALREADY_CONSUMED.value
        in consumed_by_id.results[0].failure_codes
    )


def test_real_session_cannot_serialize_inconsistent_state() -> None:
    invalid_sessions = (
        replace(
            _session(),
            qualification_state=ProviderReadinessState.REAL_OBSERVATION_VALIDATED,
        ),
        replace(
            _session(),
            qualification_state=ProviderReadinessState.REAL_OBSERVATION_VALIDATED,
            real_observation_count=1,
        ),
        replace(
            _session(),
            qualification_state=ProviderReadinessState.REAL_OBSERVATION_VALIDATED,
            real_observation_count=1,
            accepted_observation_count=2,
        ),
        replace(_session(), network_request_count=1, cascade_network_request_count=0),
        replace(_session(), monetary_spend_authorized=True),
    )
    for session in invalid_sessions:
        with pytest.raises(QualificationContractError):
            session.as_payload()


def test_sample_policy_requires_real_count_and_distinct_fixture_count() -> None:
    repeated = tuple(
        _observation_for_cascade(
            _cascade_for_expected(EXPECTED, suffix=str(index)),
            _cascade_for_expected(EXPECTED, suffix=str(index)).attempts[0],
            observation_id=f"observation-{index}",
        )
        for index in range(10)
    )
    report = qualify_provider_observations(
        repeated,
        _session(),
        EXPECTED,
        TIMING,
        READY,
        replace(_authorization(), maximum_network_requests=10),
        minimum_sample_policy=MinimumSamplePolicy(10, 5),
    )
    assert report.accepted_real_observation_count == 10
    assert report.accepted_distinct_fixture_count == 1
    assert report.production_sample_sufficient is False

    fixtures = tuple(_expected_fixture(index) for index in range(5))
    observations = tuple(
        _observation_for_cascade(
            _cascade_for_expected(expected, suffix=str(index)),
            _cascade_for_expected(expected, suffix=str(index)).attempts[0],
            expected=expected,
            observation_id=f"fixture-observation-{index}",
        )
        for index, expected in enumerate(fixtures)
    )
    session = _session(fixture_scope=tuple(item.fixture_key for item in fixtures))
    authorization = replace(
        _authorization(),
        fixture_scope=tuple(item.fixture_key for item in fixtures),
        maximum_network_requests=5,
    )
    report = qualify_provider_observations(
        observations,
        session,
        {item.fixture_key: item for item in fixtures},
        TIMING,
        READY,
        authorization,
        minimum_sample_policy=MinimumSamplePolicy(5, 5),
    )
    assert report.accepted_real_observation_count == 5
    assert report.accepted_distinct_fixture_count == 5
    assert report.production_sample_sufficient is True
