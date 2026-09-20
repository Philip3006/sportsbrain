"""Offline proof for candidate-only TheRundown cascade qualification."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from src.football.provider_cascade.candidate_eligibility import (
    CANDIDATE_PROVIDER_IDENTITIES,
    CandidateEligibilityError,
    CandidateProviderEligibilityV1,
)
from src.football.provider_cascade.contracts import (
    FOOTBALL_PROVIDER_REPERTOIRE,
    ProviderCascadeConfig,
    ProviderConfig,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    ObservationEvidenceKind,
    ProviderQualificationStatus,
    ProviderReadinessState,
    QualificationCode,
    QualificationContractError,
    bridge_real_observation_to_builder1_shadow_evidence,
    qualify_provider_observations,
)
from src.football.top5_provider_cascade_validation import evidence_digest
from src.football.top5_therundown_network_shadow import (
    NetworkShadowRunStatus,
    TheRundownNetworkShadowExecutorV1,
    TheRundownReplayTransportV1,
)
from tests.football.test_top5_controlled_shadow_provider_qualification import (
    EXPECTED,
    TIMING,
    _authorization,
    _cascade,
    _observation,
    _session,
)
from tests.football.test_top5_therundown_network_shadow import (
    NOW,
    _configuration,
    _quota_headroom,
    _NetworkStubTransport,
    _response,
    _targets,
)
from tests.football.test_top5_therundown_network_shadow import (
    _authorization as network_authorization,
)

CANDIDATE = "therundown_experimental"


def _candidate_network_run():
    targets = tuple(replace(target, provider=CANDIDATE) for target in _targets())
    configuration = _configuration(targets=targets, enabled=True)
    authorization = network_authorization(configuration, provider=CANDIDATE)
    transport = _NetworkStubTransport(
        lambda request: _response(
            request,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            network_execution=True,
        )
    )
    authorization, quota_headroom = _quota_headroom(authorization)
    result = TheRundownNetworkShadowExecutorV1(
        clock=lambda: NOW,
        allow_live_network=True,
    ).run(
        configuration,
        authorization,
        transport=transport,
        quota_headroom=quota_headroom,
    )
    return result, configuration


def _candidate_qualification_inputs():
    base_cascade = _cascade()
    base_attempt = base_cascade.attempts[0]
    candidate_attempt = replace(
        base_attempt,
        configured_provider_order=(CANDIDATE,),
        provider_identity=CANDIDATE,
        provider_record_id="therundown-event-1",
        request_identity="therundown-request-1",
        source_identity="therundown-feed",
        adapter_version="therundown-adapter-v1",
    )
    cascade = replace(
        base_cascade,
        configured_provider_order=(CANDIDATE,),
        attempts=(candidate_attempt,),
        selected_provider=CANDIDATE,
    )
    attestation = replace(
        _observation().capture_attestation,
        provider_identity=CANDIDATE,
        provider_event_id="therundown-event-1",
        provider_request_id="therundown-request-1",
        adapter_version="therundown-adapter-v1",
        cascade_evidence_digest=evidence_digest(cascade),
    )
    observation = replace(
        _observation(),
        provider_identity=CANDIDATE,
        provider_event_id="therundown-event-1",
        provider_request_id="therundown-request-1",
        source_identity="therundown-feed",
        adapter_version="therundown-adapter-v1",
        cascade_evidence=cascade,
        capture_attestation=attestation,
    )
    eligibility = CandidateProviderEligibilityV1(
        provider_identity=CANDIDATE,
        candidate_capability=True,
        shadow_capability=True,
        qualification_capability=True,
        active_production_capability=False,
        publication_capability=False,
        scheduler_capability=False,
        betting_capability=False,
        controlled_shadow_run_id="controlled-run-1",
        qualification_session_id="qualification-session-1",
        authorization_id="ceo-auth-1",
        ceo_authorization_identity="ceo:canonical",
        authorization_expires_at=observation.captured_at + timedelta(hours=1),
        adapter_version=observation.adapter_version,
        adapter_source_sha=observation.adapter_source_sha,
        configuration_digest="f" * 64,
        league_code=observation.league,
        fixture_key=observation.fixture_key,
        provider_event_id=observation.provider_event_id,
        home_participant_id="therundown-home-1",
        away_participant_id="therundown-away-1",
        request_identity=observation.provider_request_id,
        home_team=observation.home_team,
        away_team=observation.away_team,
        kickoff=observation.kickoff,
        bookmaker_identity=observation.bookmaker_identity,
        market_type="football:pre_match:1x2",
        market_phase="PRE_MATCH",
        home_odds=observation.home_odds,
        draw_odds=observation.draw_odds,
        away_odds=observation.away_odds,
        source_timestamp=observation.source_timestamp,
        captured_at=observation.captured_at,
        request_started_at=observation.request_started_at,
        request_finished_at=observation.request_finished_at,
        maximum_source_age_seconds=300,
        source_provenance=observation.source_identity,
        provider_timestamp_provenance="PROVIDER_SOURCE_TIMESTAMP",
        raw_response_digest=observation.raw_response_digest,
        provider_record_digest="f" * 64,
        normalized_record_digest=observation.normalized_record_digest,
        cascade_evidence_digest=evidence_digest(cascade),
        quota_before=10,
        quota_after=9,
        quota_cost_units=1.0,
        rate_limit_remaining=99,
        rate_limit_reset_at=observation.captured_at + timedelta(hours=1),
        account_tier="shadow-test-tier",
        provider_delay_seconds=0.0,
    )
    session = replace(
        _session(),
        configured_provider_order=(CANDIDATE,),
        adapter_version=observation.adapter_version,
    )
    authorization = replace(_authorization(), provider_scope=(CANDIDATE,))
    readiness = {
        CANDIDATE: ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION
    }
    return observation, eligibility, session, authorization, readiness, cascade


def test_network_capture_can_bind_all_five_candidate_leagues_without_authority():
    result, configuration = _candidate_network_run()

    assert result.all_five_succeeded is True
    assert {capture.target.league for capture in result.captures} == {
        "EPL",
        "BL1",
        "LL",
        "SA",
        "L1",
    }
    for capture in result.captures:
        eligibility = CandidateProviderEligibilityV1.from_network_capture(
            capture, now=NOW
        )
        restored = CandidateProviderEligibilityV1.from_payload(
            eligibility.as_payload()
        )
        assert restored.provider_identity == CANDIDATE
        assert restored.configuration_digest == configuration.configuration_digest
        assert restored.active_production_capability is False
        assert restored.publication_capability is False
        assert restored.scheduler_capability is False
        assert restored.betting_capability is False


def test_candidate_real_observation_requires_explicit_eligibility_and_qualifies():
    observation, eligibility, session, authorization, readiness, _ = (
        _candidate_qualification_inputs()
    )

    rejected = qualify_provider_observations(
        (observation,),
        session,
        EXPECTED,
        TIMING,
        readiness,
        authorization,
    )
    assert rejected.results[0].accepted is False
    assert (
        QualificationCode.CANDIDATE_ELIGIBILITY_REQUIRED.value
        in rejected.results[0].failure_codes
    )

    report = qualify_provider_observations(
        (observation,),
        session,
        EXPECTED,
        TIMING,
        readiness,
        authorization,
        candidate_eligibility=eligibility,
    )
    assert (
        report.qualification_status
        is ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
    )
    assert report.provider_statuses[CANDIDATE] is ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
    assert report.production_activation_authorized is False
    assert report.recommendation is None
    builder1_shadow = bridge_real_observation_to_builder1_shadow_evidence(
        observation,
        session,
        EXPECTED,
        TIMING,
        readiness,
        authorization,
        candidate_eligibility=eligibility,
    )
    assert builder1_shadow.no_bet is True
    assert builder1_shadow.publication_enabled is False


def test_candidate_eligibility_rejects_synthetic_evidence_and_binding_tampering():
    observation, eligibility, session, authorization, readiness, _ = (
        _candidate_qualification_inputs()
    )
    synthetic = replace(
        observation,
        evidence_kind=ObservationEvidenceKind.TEST_FIXTURE,
        network_request_count=0,
    )
    report = qualify_provider_observations(
        (synthetic,),
        session,
        EXPECTED,
        TIMING,
        readiness,
        authorization,
        candidate_eligibility=eligibility,
    )
    assert report.results[0].accepted is False
    assert report.results[0].real_observed is False
    assert not hasattr(report, "receipt")

    with pytest.raises(CandidateEligibilityError):
        replace(eligibility, provider_event_id="other-event").matches_observation(
            observation
        )


@pytest.mark.parametrize(
    "field",
    ["controlled_shadow_run_id", "qualification_session_id", "authorization_id"],
)
def test_candidate_qualification_rejects_run_session_or_authorization_rebinding(field):
    observation, eligibility, session, authorization, readiness, _ = (
        _candidate_qualification_inputs()
    )
    rebound = replace(eligibility, **{field: "rebound-value"})
    with pytest.raises(QualificationContractError, match="binding"):
        qualify_provider_observations(
            (observation,),
            session,
            EXPECTED,
            TIMING,
            readiness,
            authorization,
            candidate_eligibility=rebound,
        )


def test_candidate_provider_cannot_enter_active_production_routing():
    assert FOOTBALL_PROVIDER_REPERTOIRE == ("the_odds_api",)
    assert ProviderCascadeConfig.default().provider_order == ("the_odds_api",)
    with pytest.raises(ValueError, match="candidate-only provider"):
        ProviderCascadeConfig(
            provider_order=(CANDIDATE,),
            providers={
                CANDIDATE: ProviderConfig(
                    name=CANDIDATE,
                    credential_env=("THERUNDOWN_KEY",),
                    adapter_version="therundown-adapter-v1",
                )
            },
        ).validate()
    assert CANDIDATE in CANDIDATE_PROVIDER_IDENTITIES


def test_replay_cannot_upgrade_candidate_output_to_real_evidence():
    targets = tuple(replace(target, provider=CANDIDATE) for target in _targets())
    configuration = _configuration(targets=targets, enabled=True)
    authorization = network_authorization(configuration, provider=CANDIDATE)
    transport = TheRundownReplayTransportV1(
        lambda request: _response(
            request,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            network_execution=True,
        )
    )
    result = TheRundownNetworkShadowExecutorV1(clock=lambda: NOW).run(
        configuration, authorization, transport=transport
    )
    assert result.status is NetworkShadowRunStatus.PARTIAL
    assert result.captures == ()
    assert all("TRANSPORT_ERROR:NetworkShadowExecutionBlocked" in failure for failure in result.failures)


def test_candidate_manifest_is_accepted_by_b2_intake_only_with_binding():
    from src.football.top5_b2_shadow_qualification_intake import validate_intake
    from tests.football.test_top5_b2_shadow_qualification_intake import _manifest

    observation, eligibility, session, authorization, readiness, cascade = (
        _candidate_qualification_inputs()
    )
    manifest = _manifest(
        observation=observation,
        session=session,
        authorization=authorization,
        cascade_evidence=cascade,
        capture_attestation=observation.capture_attestation,
        provider_readiness=readiness,
        candidate_provider_eligibility=eligibility,
    )
    result = validate_intake(manifest)
    assert result.receipt.accepted is True
    assert result.receipt.no_bet is True
    assert result.receipt.publication is False
    assert result.receipt.production_activation is False
    restored = manifest.__class__.from_payload(manifest.as_payload())
    assert restored.candidate_provider_eligibility is not None
    assert restored.manifest_digest == manifest.manifest_digest
