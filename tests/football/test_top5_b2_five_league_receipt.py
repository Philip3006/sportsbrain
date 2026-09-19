"""Offline proof for the five-league B4 -> B2 receipt seam."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from src.football.top5_b2_shadow_qualification_intake import (
    Builder2QualificationIntakeError,
    qualify_five_league_shadow_run,
)
from src.football.top5_builder2_qualification_receipt import semantic_digest
from src.football.top5_controlled_shadow_provider_qualification import (
    ControlledShadowCaptureAttestation,
    ObservationEvidenceKind,
    ProviderReadinessState,
)
from src.football.top5_provider_cascade_validation import evidence_digest
from src.football.top5_therundown_network_shadow import (
    NetworkShadowRunStatus,
)
from tests.football.test_top5_b2_shadow_qualification_intake import _manifest
from tests.football.test_top5_candidate_provider_eligibility import (
    CANDIDATE,
    _candidate_network_run,
)
from tests.football.test_top5_controlled_shadow_provider_qualification import (
    _authorization,
    _cascade,
    _observation,
    _session,
)


def _canonical_run_and_manifests():
    run, _ = _candidate_network_run()
    manifests = []
    captures = []
    for capture in run.captures:
        # The B4 response must carry the digest of the canonical cascade
        # evidence that is supplied to B2.  The executor itself only transports
        # that digest and never constructs the B2 cascade contract.
        base_observation = _observation()
        base_cascade = _cascade()
        base_attempt = base_cascade.attempts[0]
        response = capture.response
        target = capture.target
        request = capture.request
        candidate_attempt = replace(
            base_attempt,
            league=target.league,
            fixture_key=target.fixture_key,
            home_team=target.home_team,
            away_team=target.away_team,
            kickoff=target.kickoff,
            configured_provider_order=(CANDIDATE,),
            provider_identity=CANDIDATE,
            network_called=True,
            start_timestamp=response.request_started_at,
            end_timestamp=response.request_finished_at,
            capture_timestamp=response.captured_at,
            market_type="h2h_1x2",
            home_odds=response.home_odds,
            draw_odds=response.draw_odds,
            away_odds=response.away_odds,
            bookmaker_identity=response.bookmaker_identity,
            source_identity=response.source_identity,
            source_timestamp=response.source_timestamp,
            request_latency_ms=0,
            provider_record_id=response.provider_event_id,
            raw_record_digest=response.raw_response_digest,
            request_identity=response.provider_request_id,
            adapter_version=response.adapter_version,
        )
        cascade = replace(
            base_cascade,
            configured_provider_order=(CANDIDATE,),
            attempts=(candidate_attempt,),
            selected_provider=CANDIDATE,
        )
        cascade_digest = evidence_digest(cascade)
        # Reparse through the canonical attestation contract rather than
        # trusting the transport mapping.
        attestation = ControlledShadowCaptureAttestation.from_payload(
            capture.canonical_capture_attestation
        )
        attestation = replace(attestation, cascade_evidence_digest=cascade_digest)
        response = replace(response, cascade_evidence_digest=cascade_digest)
        capture = replace(
            capture,
            response=response,
            capture_attestation_input=attestation.as_payload(),
            capture_attestation_digest=semantic_digest(attestation.as_payload()),
        )
        observation = replace(
            base_observation,
            observation_id=capture.observation_id,
            qualification_session_id=request.qualification_session_id,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            provider_identity=CANDIDATE,
            provider_event_id=response.provider_event_id,
            provider_request_id=response.provider_request_id,
            league=target.league,
            fixture_key=target.fixture_key,
            home_team=target.home_team,
            away_team=target.away_team,
            kickoff=target.kickoff,
            home_odds=response.home_odds,
            draw_odds=response.draw_odds,
            away_odds=response.away_odds,
            bookmaker_identity=response.bookmaker_identity,
            source_identity=response.source_identity,
            source_timestamp=response.source_timestamp,
            captured_at=response.captured_at,
            request_started_at=response.request_started_at,
            request_finished_at=response.request_finished_at,
            adapter_version=response.adapter_version,
            adapter_source_sha=response.adapter_source_sha,
            raw_response_digest=response.raw_response_digest,
            normalized_record_digest=response.normalized_record_digest,
            cascade_evidence=cascade,
            quota_before=response.quota_before,
            quota_after=response.quota_after,
            quota_cost_units=response.quota_cost_units,
            network_request_count=1,
            capture_attestation=attestation,
        )
        session = replace(
            _session(),
            qualification_session_id=request.qualification_session_id,
            league_scope=(target.league,),
            fixture_scope=(target.fixture_key,),
            configured_provider_order=(CANDIDATE,),
            adapter_version=response.adapter_version,
            adapter_source_sha=response.adapter_source_sha,
        )
        authorization = replace(
            _authorization(),
            authorization_id=request.authorization_id,
            controlled_shadow_run_id=request.controlled_shadow_run_id,
            qualification_session_id=request.qualification_session_id,
            provider_scope=(CANDIDATE,),
            league_scope=(target.league,),
            fixture_scope=(target.fixture_key,),
            issued_at=response.captured_at - timedelta(minutes=1),
            expires_at=request.authorization_expires_at,
        )
        from src.football.provider_cascade.candidate_eligibility import (
            CandidateProviderEligibilityV1,
        )

        eligibility = CandidateProviderEligibilityV1.from_network_capture(
            capture, now=response.captured_at
        )
        manifest = _manifest(
            intake_id=f"intake-{target.league}",
            observation=observation,
            session=session,
            authorization=authorization,
            cascade_evidence=cascade,
            capture_attestation=attestation,
            provider_readiness={
                CANDIDATE: ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION
            },
            candidate_provider_eligibility=eligibility,
        )
        captures.append(capture)
        manifests.append(manifest)
    return replace(run, captures=tuple(captures)), tuple(manifests)


def test_complete_five_league_run_derives_real_b2_receipts_in_scope_order():
    run, manifests = _canonical_run_and_manifests()

    results = qualify_five_league_shadow_run(run, manifests)

    assert [item.qualification_report.results[0].league for item in results] == [
        "BL1",
        "EPL",
        "LL",
        "SA",
        "L1",
    ]
    assert all(
        item.receipt.qualification_status.value == "REAL_OBSERVATION_VALIDATED"
        and item.receipt.accepted is True
        and item.receipt.no_bet is True
        and item.receipt.publication is False
        and item.receipt.production_activation is False
        and item.receipt.monetary_spend_authorized is False
        for item in results
    )


def test_partial_five_league_run_never_enters_receipt_intake():
    run, manifests = _canonical_run_and_manifests()
    partial = replace(
        run,
        status=NetworkShadowRunStatus.PARTIAL,
        failures=("LL:RATE_LIMITED",),
    )

    with pytest.raises(Builder2QualificationIntakeError, match="completed network"):
        qualify_five_league_shadow_run(partial, manifests)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda manifest: replace(
            manifest,
            provider_request_id="tampered-request",
        ),
        lambda manifest: replace(
            manifest,
            capture_attestation=replace(
                manifest.capture_attestation,
                provider_event_id="tampered-event",
            ),
        ),
        lambda manifest: replace(
            manifest,
            candidate_provider_eligibility=replace(
                manifest.candidate_provider_eligibility,
                home_participant_id="tampered-participant",
            ),
        ),
    ],
)
def test_run_fixture_and_authority_bindings_fail_closed(tamper):
    run, manifests = _canonical_run_and_manifests()
    changed = list(manifests)
    changed[0] = tamper(changed[0])

    with pytest.raises(Builder2QualificationIntakeError):
        qualify_five_league_shadow_run(run, tuple(changed))


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_kind": ObservationEvidenceKind.TEST_FIXTURE},
        {"http_status": 500},
        {"home_participant_id": "tampered-participant"},
        {"retry_count": 1},
    ],
)
def test_untrusted_capture_response_bindings_fail_closed(changes):
    run, manifests = _canonical_run_and_manifests()
    capture = run.captures[0]
    changed_response = replace(capture.response, **changes)
    changed_capture = replace(capture, response=changed_response)
    changed_run = replace(
        run,
        captures=(changed_capture, *run.captures[1:]),
    )

    with pytest.raises(Builder2QualificationIntakeError):
        qualify_five_league_shadow_run(changed_run, manifests)


def test_wrapper_has_no_provider_or_receipt_issuer_authority():
    run, manifests = _canonical_run_and_manifests()
    assert all(capture.network_execution for capture in run.captures)
    assert all(manifest.candidate_provider_eligibility for manifest in manifests)
    assert all(manifest.capture_attestation.network_execution for manifest in manifests)
    assert run.authority_changed is False
    assert run.publication is False
    assert run.production_activation is False
    assert run.monetary_spend_authorized is False
