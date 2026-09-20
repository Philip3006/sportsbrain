"""Offline integration proof for the complete Top-5 governance chain.

Every ``REAL_OBSERVED`` value in this module is a deterministic test fixture.
No provider transport, credential, quota, runtime, publication, or financial
side effect is exercised.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from src.football.provider_cascade.candidate_eligibility import (
    CANDIDATE_ONLY_PROVIDER_IDENTITIES,
)
from src.football.provider_cascade.contracts import FOOTBALL_PROVIDER_REPERTOIRE
from src.football.top5_controlled_release import Top5ControlledRelease
from src.football.top5_production_verification import (
    ACTIVE_FOOTBALL_AUTHORITY,
    ActivationIdentity,
    MatchObservation,
    PreActivationBaseline,
    ResourceEvidence,
    RollbackTrigger,
    RoutingEvidence,
    RuntimeEvidence,
    VerificationStatus,
    evaluate_publication_gate,
    verify_production,
)
from src.football.top5_provider_validation import ProviderAuthority
from src.football.top5_publisher import (
    ControlledPublicationAttestation,
    ControlledTop5PublicationBatch,
    ControlledTop5PublicationPayload,
)
from tests.football.test_top5_controlled_release import (
    _context,
    _rollout_evidence,
)
from tests.football.test_top5_real_shadow_session import BASE


def _post_activation_inputs(state, evidence):
    """Project the controlled state into read-only production verification input."""

    configuration_digest = evidence.five_league_package.dossier.configuration_digest
    activation = ActivationIdentity(
        activation_id=state.activation_id,
        activation_digest=configuration_digest,
        source_sha=state.source_sha,
        research_sha=state.research_sha,
        model_artifact_hash=state.model_artifact_hash,
        candidate_id=state.candidate_id,
    )
    observations = []
    expected_fixture_identities = {}
    for receipt in evidence.receipts:
        _league, home, away, _kickoff = receipt.fixture_key.split("|", 3)
        event_identity = receipt.provider_event_id
        expected_fixture_identities[receipt.fixture_key] = (
            event_identity,
            home,
            away,
        )
        observations.append(
            MatchObservation(
                fixture_key=receipt.fixture_key,
                event_identity=event_identity,
                home_team=home,
                away_team=away,
                kickoff_at=BASE + timedelta(minutes=35),
                observed_at=BASE + timedelta(minutes=2, seconds=30),
                source_sha=state.source_sha,
                config_digest=configuration_digest,
                market_kind="signal_time",
                odds={"home": 2.1, "draw": 3.2, "away": 3.4},
            )
        )
    baseline = PreActivationBaseline(
        captured_at=BASE + timedelta(minutes=2),
        activation=activation,
        worker_health="ok",
        pwa_health="ok",
        football_scheduler_state="active",
        active_provider_order=(ACTIVE_FOOTBALL_AUTHORITY,),
        launchd_expectations={"aggregate_health": "active"},
        github_workflow_expectations={"football_scan": "fallback-only"},
        runtime_writer_state={"runtime": "active"},
        ledger_writer_state={"financial": "active"},
        football_health_artifacts={"football": "ok", "top5": "ok"},
        request_counts={ACTIVE_FOOTBALL_AUTHORITY: 0},
        quota_remaining={ACTIVE_FOOTBALL_AUTHORITY: 100},
        spend_units={ACTIVE_FOOTBALL_AUTHORITY: 0.0},
        expected_fixture_identities=expected_fixture_identities,
    )
    routing = RoutingEvidence(
        expected_provider=ACTIVE_FOOTBALL_AUTHORITY,
        selected_provider=ACTIVE_FOOTBALL_AUTHORITY,
        active_provider_order=(ACTIVE_FOOTBALL_AUTHORITY,),
        observed_providers=(ACTIVE_FOOTBALL_AUTHORITY,),
    )
    runtime = RuntimeEvidence(
        scan_refresh_exit_code=0,
        worker_health="ok",
        pwa_health="ok",
        football_scheduler_state="active",
        runtime_writer_state={"runtime": "active"},
        ledger_writer_state={"financial": "active"},
        football_health_artifacts={"football": "ok", "top5": "ok"},
        retry_budget=0,
    )
    resources = ResourceEvidence(
        request_counts={ACTIVE_FOOTBALL_AUTHORITY: 0},
        request_budgets={ACTIVE_FOOTBALL_AUTHORITY: 0},
        quota_before={ACTIVE_FOOTBALL_AUTHORITY: 100},
        quota_after={ACTIVE_FOOTBALL_AUTHORITY: 100},
        spend_delta={ACTIVE_FOOTBALL_AUTHORITY: 0.0},
        spend_budgets={ACTIVE_FOOTBALL_AUTHORITY: 0.0},
    )
    return activation, baseline, routing, tuple(observations), runtime, resources


def _publication_batch(state, evidence, artifact, publication_auth):
    """Build the canonical five-league public artifact in test-only memory."""

    payloads = []
    authorizations = []
    for receipt in evidence.receipts:
        league, home, away, _kickoff = receipt.fixture_key.split("|", 3)
        payloads.append(
            ControlledTop5PublicationPayload(
                artifact_path=f"docs/data/top5/published/{league}/signals.json",
                activation_id=state.activation_id,
                league_code=league,
                candidate_id=state.candidate_id,
                model_identity=state.model_identity,
                signal_time_experiment_id=state.signal_time_experiment_id,
                source_sha=state.source_sha,
                research_sha=state.research_sha,
                model_artifact_hash=state.model_artifact_hash,
                provider_authority=state.provider_authority,
                result_authority=state.result_authority,
                evidence_digest=state.evidence_digest,
                controlled_shadow_run_id=state.controlled_shadow_run_id,
                qualification_session_id=state.qualification_session_id,
                generated_at=artifact.generated_at,
                football_records=(
                    {
                        "fixture": {
                            "fixture_key": receipt.fixture_key,
                            "home_team": home,
                            "away_team": away,
                            "kickoff": "2026-09-18T14:00:00Z",
                        },
                        "league": league,
                        "model_identity": state.model_identity,
                        "research_sha": state.research_sha,
                        "source_sha": state.source_sha,
                        "signal_time_experiment_id": state.signal_time_experiment_id,
                        "activation_id": state.activation_id,
                        "evidence_digest": state.evidence_digest,
                        "controlled_shadow_run_id": state.controlled_shadow_run_id,
                        "qualification_session_id": state.qualification_session_id,
                        "probabilities": {"home": 0.34, "draw": 0.33, "away": 0.33},
                        "no_bet": True,
                        "closing_used_for_prediction": False,
                    },
                ),
                health=dict(artifact.health),
            )
        )
        authorizations.append(
            replace(
                publication_auth,
                publication_authorization_id=f"{publication_auth.publication_authorization_id}:{league}",
                league_code=league,
            )
        )
    batch = ControlledTop5PublicationBatch(tuple(payloads))
    product = batch.as_public_product(
        published_at=artifact.generated_at,
        publication_authorization_id=authorizations[0].publication_authorization_id,
    )
    return payloads, authorizations, product


def test_complete_offline_governance_chain_reaches_read_only_publication_gate(
    tmp_path,
):
    """Exercise B2 -> activation -> verification without granting any authority."""

    request, authorization, evidence, artifact, publication_auth, health = _context(
        tmp_path
    )
    package = evidence.five_league_package
    assert tuple(receipt.fixture_key.split("|", 1)[0] for receipt in package.receipts) == (
        "BL1",
        "EPL",
        "LL",
        "SA",
        "L1",
    )
    assert len(package.receipts) == 5
    assert package.dossier.provider_identity == "therundown_experimental"
    assert package.dossier.authority_granted is False
    assert authorization.provider_authority.approved_odds_provider == "the_odds_api"
    assert authorization.provider_authority.approved_odds_provider in FOOTBALL_PROVIDER_REPERTOIRE
    assert authorization.provider_authority.approved_odds_provider not in CANDIDATE_ONLY_PROVIDER_IDENTITIES

    release = Top5ControlledRelease()
    state = release.activate(
        request,
        authorization,
        evidence,
        _rollout_evidence(),
        health_preconditions=health,
        now=BASE + timedelta(minutes=2),
    )
    assert state.no_bet is True
    assert state.publication_enabled is False
    assert state.scheduler_enabled is False
    assert state.provider_authority_created is False
    assert state.ledger_mutated is False

    activation, baseline, routing, observations, runtime, resources = (
        _post_activation_inputs(state, evidence)
    )
    report = verify_production(
        baseline,
        activation,
        routing,
        observations,
        runtime,
        resources,
        checked_at=BASE + timedelta(minutes=3),
    )
    assert report.status is VerificationStatus.PRODUCTION_VERIFIED
    assert report.triggers == ()

    blocked_publication = evaluate_publication_gate(
        report,
        activation,
        ceo_publication_authorization=None,
    )
    assert blocked_publication.eligible is False
    assert "separate CEO publication authorization is required" in blocked_publication.failures

    authorized_publication = evaluate_publication_gate(
        report,
        activation,
        ceo_publication_authorization=publication_auth.publication_authorization_id,
    )
    assert authorized_publication.eligible is True
    assert authorized_publication.as_payload()["publication_enabled"] is False

    payloads, publication_authorizations, public_product = _publication_batch(
        state, evidence, artifact, publication_auth
    )
    attestation = ControlledPublicationAttestation.issue(
        payloads[0],
        publication_authorizations[0],
        activation_bindings=state.as_bindings(),
        artifact=public_product,
        now=BASE + timedelta(minutes=3),
    )
    assert attestation.activation_id == state.activation_id
    assert attestation.provider_authority == state.provider_authority
    assert attestation.controlled_shadow_run_id == state.controlled_shadow_run_id
    assert attestation.qualification_session_id == state.qualification_session_id
    assert attestation.no_bet is True
    assert attestation.publication_authorized is True


def test_integrated_chain_rejects_candidate_production_authority_and_identity_drift(
    tmp_path,
):
    request, authorization, evidence, _artifact, _publication_auth, health = _context(
        tmp_path
    )
    candidate = evidence.five_league_package.dossier.provider_identity
    candidate_authority = replace(
        authorization.provider_authority,
        approved_odds_provider=candidate,
        approved_provider_set=(candidate,),
    )
    with pytest.raises(ValueError, match="candidate evidence provider"):
        evidence.validate(
            replace(
                request,
                provider_authority=ProviderAuthority(
                    "fixture-source", candidate, "result-source"
                ),
            ),
            replace(authorization, provider_authority=candidate_authority),
        )

    release = Top5ControlledRelease()
    state = release.activate(
        request,
        authorization,
        evidence,
        _rollout_evidence(),
        health_preconditions=health,
        now=BASE + timedelta(minutes=2),
    )
    activation, baseline, routing, observations, runtime, resources = (
        _post_activation_inputs(state, evidence)
    )
    drifted = verify_production(
        baseline,
        replace(activation, activation_digest="tampered"),
        routing,
        observations,
        runtime,
        resources,
        checked_at=BASE + timedelta(minutes=3),
    )
    assert drifted.status is VerificationStatus.ROLLBACK_REQUIRED
    assert RollbackTrigger.ACTIVATION_IDENTITY_MISMATCH in drifted.triggers
