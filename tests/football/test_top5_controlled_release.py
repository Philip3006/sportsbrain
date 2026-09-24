"""Focused fail-closed tests for controlled activation/publication V1."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import src.football.top5_publisher as top5_publisher_module
from scripts.validate_controlled_top5_publication import main as validate_publication
from src.football.production_contracts import RolloutEvidence, SignalTimeContract
from src.football.provider_cascade.contracts import FOOTBALL_PROVIDER_REPERTOIRE
from src.football.top5_activation_readiness import (
    ControlledActivationRequest,
    RollbackTrigger,
)
from src.football.top5_b2_qualification_batch_orchestrator import (
    build_five_league_shadow_package,
    consume_five_league_shadow_package,
)
from src.football.top5_controlled_release import (
    BLOCKED,
    READY_FOR_CONTROLLED_ACTIVATION,
    ApprovedProviderResultAuthority,
    ControlledActivationAuthorization,
    Top5ControlledRelease,
    Top5ControlledReleaseEvidence,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    MinimumSamplePolicy,
)
from src.football.top5_provider_validation import ProviderAuthority
from src.football.top5_publisher import (
    ControlledPublicationAttestation,
    ControlledPublicationCapabilityIssuanceProof,
    ControlledTop5PublicationPayload,
    FileControlledPublicationCapabilityStore,
    Top5PublicationAuthorization,
    controlled_publication_capability_state_path,
    controlled_publication_issuer_public_key_path,
)
from src.football.top5_qualification_sample_aggregator import (
    aggregate_builder2_qualification_samples,
)
from src.football.top5_research_binding import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    inventory_for,
)
from tests.football.test_top5_b2_five_league_receipt import (
    _canonical_run_and_manifests,
)
from tests.football.test_top5_public_delivery import _controlled_public_product
from tests.football.test_top5_real_shadow_session import (
    BASE,
    INTEGRATION_SHA,
)


def _complete_public_product() -> dict[str, object]:
    """Use the canonical five-league product fixture for public-boundary tests."""
    return _controlled_public_product()


def _rollout_evidence() -> RolloutEvidence:
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


def _evidence(tmp_path):
    run, manifests = _canonical_run_and_manifests()
    package = consume_five_league_shadow_package(
        build_five_league_shadow_package(run, manifests)
    )
    receipts = package.receipts
    signal_time_experiment_id = "shadow-experiment:controlled-release-v1"
    audit = {
        "overall_state": "COMPLETE",
        "integration_sha": INTEGRATION_SHA,
        "research_sha": FROZEN_RESEARCH_SHA,
        "model_identity": M5_CANDIDATE_ID,
        "predictions": [
            {
                "overall_state": "COMPLETE",
                "evidence_mode": "REAL_OBSERVED",
                "league": receipt.fixture_key.split("|", 1)[0],
                "signal_time_experiment_id": signal_time_experiment_id,
                "qualification_receipt_id": receipt.qualification_receipt_id,
                "qualification_receipt_digest": receipt.receipt_digest,
                "controlled_shadow_run_id": receipt.controlled_shadow_run_id,
                "qualification_session_id": receipt.qualification_session_id,
            }
            for receipt in receipts
        ],
    }
    eligible_predictions = [
        {
            "evidence_mode": "REAL_OBSERVED",
            "league": receipt.fixture_key.split("|", 1)[0],
            "model_identity": M5_CANDIDATE_ID,
            "research_sha": FROZEN_RESEARCH_SHA,
            "signal_time_experiment_id": signal_time_experiment_id,
            "qualification_receipt_id": receipt.qualification_receipt_id,
            "qualification_receipt_digest": receipt.receipt_digest,
            "controlled_shadow_run_id": receipt.controlled_shadow_run_id,
            "qualification_session_id": receipt.qualification_session_id,
            "fixture": receipt.fixture_key,
            "probabilities": {"home": 0.34, "draw": 0.33, "away": 0.33},
        }
        for receipt in receipts
    ]
    measurement = {
        "overall_state": "COMPLETE",
        "research_sha": FROZEN_RESEARCH_SHA,
        "model_identity": M5_CANDIDATE_ID,
        "eligible_count": len(eligible_predictions),
        "eligible_predictions": eligible_predictions,
        "safety_invariants": {
            "production_activation_authorized": False,
            "publication_authorized": False,
            "betting_authorized": False,
            "model_approved_for_production": False,
            "signal_time_approved_for_production": False,
            "sealed_data_accessed": False,
            "closing_used_for_prediction": False,
        },
    }
    receipt_object = receipts[0]
    policy = MinimumSamplePolicy(5, 5)
    sample_report = aggregate_builder2_qualification_samples(
        receipts, minimum_sample_policy=policy
    )
    release_evidence = Top5ControlledReleaseEvidence(
        receipts=receipts,
        sample_report=sample_report,
        audit_report=audit,
        measurement_report=measurement,
        five_league_package=package,
    )
    return release_evidence, measurement, receipt_object


def _context(tmp_path):
    evidence, measurement, receipt = _evidence(tmp_path)
    receipt = evidence.receipts[0]
    league = receipt.fixture_key.split("|", 1)[0]
    production_provider = FOOTBALL_PROVIDER_REPERTOIRE[0]
    proposal = ProviderAuthority(
        "fixture-source", production_provider, "result-source"
    )
    approved_authority = ApprovedProviderResultAuthority(
        authority_decision_id="provider-auth:controlled-release",
        league_code=league,
        approved_odds_provider=production_provider,
        approved_provider_set=(production_provider,),
        approved_result_source="result-source",
        issued_at=BASE - timedelta(minutes=1),
        expires_at=BASE + timedelta(days=1),
    )
    signal_time = SignalTimeContract(
        30,
        180,
        300,
        approval_ref="activation-auth:controlled-release",
    )
    request = ControlledActivationRequest(
        league_code=league,
        candidate_id=M5_CANDIDATE_ID,
        model_identity=M5_CANDIDATE_ID,
        source_sha=INTEGRATION_SHA,
        research_sha=FROZEN_RESEARCH_SHA,
        model_artifact_hash=inventory_for("BL1", M5_CANDIDATE_ID).model_artifact_hash
        or "",
        provider_authority=proposal,
        signal_time_contract=signal_time,
        rollback_pointer=f"safe-disabled:{league}",
        config_snapshot={
            "league": league,
            "candidate": M5_CANDIDATE_ID,
            "configuration_digest": evidence.five_league_package.dossier.configuration_digest,
        },
        ceo_authorization_token="activation-token",
        ceo_authorized=True,
    )
    authorization = ControlledActivationAuthorization(
        authorization_id="activation-auth:controlled-release",
        activation_id="activation:controlled-release",
        league_code=league,
        candidate_id=M5_CANDIDATE_ID,
        model_identity=M5_CANDIDATE_ID,
        source_sha=INTEGRATION_SHA,
        research_sha=FROZEN_RESEARCH_SHA,
        model_artifact_hash=request.model_artifact_hash,
        signal_time_experiment_id="shadow-experiment:controlled-release-v1",
        signal_time_contract=signal_time,
        minimum_sample_policy=MinimumSamplePolicy(5, 5),
        provider_authority=approved_authority,
        controlled_shadow_run_id=receipt.controlled_shadow_run_id,
        qualification_session_id=receipt.qualification_session_id,
        ceo_shadow_authorization_id=evidence.five_league_package.dossier.ceo_authorization_id,
        fixture_scope=tuple(item.fixture_key for item in evidence.receipts),
        rollback_pointer=f"safe-disabled:{league}",
        authorization_token="activation-specific-token",
        issued_at=BASE - timedelta(minutes=1),
        expires_at=BASE + timedelta(days=1),
    )
    candidate = measurement["eligible_predictions"][0]
    evidence_digest = evidence.evidence_digest()
    artifact = ControlledTop5PublicationPayload(
        artifact_path=f"docs/data/top5/published/{league}/signals.json",
        activation_id=authorization.activation_id,
        league_code=league,
        candidate_id=M5_CANDIDATE_ID,
        model_identity=M5_CANDIDATE_ID,
        signal_time_experiment_id="shadow-experiment:controlled-release-v1",
        source_sha=INTEGRATION_SHA,
        research_sha=FROZEN_RESEARCH_SHA,
        model_artifact_hash=request.model_artifact_hash,
        provider_authority=production_provider,
        result_authority="result-source",
        evidence_digest=evidence_digest,
        controlled_shadow_run_id=receipt.controlled_shadow_run_id,
        qualification_session_id=receipt.qualification_session_id,
        generated_at=BASE + timedelta(minutes=1),
        football_records=(
            {
                "fixture": candidate["fixture"],
                "league": league,
                "model_identity": M5_CANDIDATE_ID,
                "research_sha": FROZEN_RESEARCH_SHA,
                "source_sha": INTEGRATION_SHA,
                "signal_time_experiment_id": "shadow-experiment:controlled-release-v1",
                "activation_id": authorization.activation_id,
                "evidence_digest": evidence_digest,
                "controlled_shadow_run_id": receipt.controlled_shadow_run_id,
                "qualification_session_id": receipt.qualification_session_id,
                "probabilities": candidate["probabilities"],
                "no_bet": True,
                "closing_used_for_prediction": False,
            },
        ),
        health={
            "status": "ok",
            "activation_state": "controlled",
            "no_bet": True,
            "provider_health": "ok",
            "result_source_health": "ok",
        },
    )
    publication_authorization = Top5PublicationAuthorization(
        publication_authorization_id="publication-auth:controlled-release",
        activation_id=authorization.activation_id,
        league_code=league,
        candidate_id=M5_CANDIDATE_ID,
        model_identity=M5_CANDIDATE_ID,
        source_sha=INTEGRATION_SHA,
        research_sha=FROZEN_RESEARCH_SHA,
        model_artifact_hash=request.model_artifact_hash,
        signal_time_experiment_id="shadow-experiment:controlled-release-v1",
        publication_token="publication-specific-token",
        issued_at=BASE - timedelta(minutes=1),
        expires_at=BASE + timedelta(days=1),
    )
    health = {
        "provider_healthy": True,
        "fixture_coverage_valid": True,
        "odds_freshness_valid": True,
        "signal_time_coverage_valid": True,
        "inference_healthy": True,
        "publisher_healthy": True,
        "result_source_healthy": True,
        "rollback_ready": True,
    }
    return (
        request,
        authorization,
        evidence,
        artifact,
        publication_authorization,
        health,
    )


def test_dry_run_reaches_ready_state_without_mutation(tmp_path):
    request, auth, evidence, artifact, publication_auth, health = _context(tmp_path)
    assert evidence.five_league_package.dossier.provider_identity == "therundown_experimental"
    assert auth.provider_authority.approved_odds_provider == "the_odds_api"
    assert (
        evidence.five_league_package.dossier.provider_identity
        != auth.provider_authority.approved_odds_provider
    )
    release = Top5ControlledRelease()
    result = release.dry_run(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        artifact,
        publication_authorization=publication_auth,
        health_preconditions=health,
        now=BASE + timedelta(minutes=2),
    )
    assert result.final_state == READY_FOR_CONTROLLED_ACTIVATION
    assert result.no_production_mutation is True
    assert release.activation_runtime.state is None
    assert release.publication_store.current is None


def test_activation_preflight_is_independent_of_publication_artifact(tmp_path):
    request, auth, evidence, artifact, publication_auth, health = _context(tmp_path)
    release = Top5ControlledRelease()
    activation_only = release.dry_run(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        health_preconditions=health,
        now=BASE + timedelta(minutes=2),
    )
    assert activation_only.final_state == READY_FOR_CONTROLLED_ACTIVATION
    assert activation_only.public_artifact_valid is False
    assert activation_only.publication_authorized is False
    assert activation_only.publication_ready is False
    assert (
        "publication artifact was not supplied" in activation_only.publication_reasons
    )

    invalid_artifact = replace(artifact, evidence_digest="f" * 64)
    still_ready = release.dry_run(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        invalid_artifact,
        publication_authorization=publication_auth,
        health_preconditions=health,
        now=BASE + timedelta(minutes=2),
    )
    assert still_ready.final_state == READY_FOR_CONTROLLED_ACTIVATION
    assert still_ready.publication_ready is False
    assert still_ready.publication_reasons


def test_publication_preflight_requires_its_separate_authorization(tmp_path):
    request, auth, evidence, artifact, publication_auth, health = _context(tmp_path)
    missing = Top5ControlledRelease().dry_run(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        artifact,
        health_preconditions=health,
        now=BASE + timedelta(minutes=2),
    )
    assert missing.final_state == READY_FOR_CONTROLLED_ACTIVATION
    assert missing.public_artifact_valid is True
    assert missing.publication_ready is False
    assert "publication authorization was not supplied" in missing.publication_reasons

    invalid = Top5ControlledRelease().dry_run(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        artifact,
        publication_authorization=replace(
            publication_auth, publication_authorized=False
        ),
        health_preconditions=health,
        now=BASE + timedelta(minutes=2),
    )
    assert invalid.final_state == READY_FOR_CONTROLLED_ACTIVATION
    assert invalid.publication_ready is False
    assert invalid.publication_reasons


def test_missing_activation_authorization_blocks(tmp_path):
    request, auth, evidence, _artifact, _, _health = _context(tmp_path)
    blocked = replace(auth, activation_authorized=False)
    with pytest.raises(ValueError, match="not approved"):
        Top5ControlledRelease().activate(
            request,
            blocked,
            evidence,
            _rollout_evidence(),
            health_preconditions=_health,
            now=BASE + timedelta(minutes=2),
        )


@pytest.mark.parametrize(
    ("health_field", "remove_field"),
    tuple(
        (field, remove)
        for field in (
            "provider_healthy",
            "fixture_coverage_valid",
            "odds_freshness_valid",
            "signal_time_coverage_valid",
            "inference_healthy",
            "publisher_healthy",
            "result_source_healthy",
            "rollback_ready",
        )
        for remove in (False, True)
    ),
)
def test_direct_activation_requires_each_health_gate(
    tmp_path, health_field, remove_field
):
    request, auth, evidence, _artifact, _, health = _context(tmp_path)
    incomplete = dict(health)
    if remove_field:
        incomplete.pop(health_field)
    else:
        incomplete[health_field] = False
    release = Top5ControlledRelease()
    with pytest.raises(
        ValueError, match=health_field if not remove_field else "health/monitoring"
    ):
        release.activate(
            request,
            auth,
            evidence,
            _rollout_evidence(),
            health_preconditions=incomplete,
            now=BASE + timedelta(minutes=2),
        )
    assert release.activation_runtime.state is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("model_identity", "wrong-model"),
        ("research_sha", "a" * 40),
        ("controlled_shadow_run_id", "wrong-run"),
        ("qualification_session_id", "wrong-session"),
    ),
)
def test_wrong_exact_binding_blocks(tmp_path, field, value):
    request, auth, evidence, artifact, _, health = _context(tmp_path)
    blocked = replace(auth, **{field: value})
    result = Top5ControlledRelease().dry_run(
        request,
        blocked,
        evidence,
        _rollout_evidence(),
        artifact,
        health_preconditions=health,
        now=BASE + timedelta(minutes=2),
    )
    assert result.final_state == BLOCKED


def test_missing_receipt_and_injected_evidence_block(tmp_path):
    request, auth, evidence, _artifact, _, _health = _context(tmp_path)
    missing = replace(evidence, receipts=())
    with pytest.raises(ValueError):
        Top5ControlledRelease().activate(
            request,
            auth,
            missing,
            _rollout_evidence(),
            health_preconditions=_health,
            now=BASE + timedelta(minutes=2),
        )
    audit = deepcopy(dict(evidence.audit_report))
    audit["predictions"] = [
        dict(audit["predictions"][0], evidence_mode="TEST_INJECTED")
    ]
    injected = replace(evidence, audit_report=audit)
    with pytest.raises(ValueError, match="non-real"):
        Top5ControlledRelease().activate(
            request,
            auth,
            injected,
            _rollout_evidence(),
            health_preconditions=_health,
            now=BASE + timedelta(minutes=2),
        )


def test_top5_activation_requires_exact_five_receipt_package(tmp_path):
    request, auth, evidence, _artifact, _, _health = _context(tmp_path)
    missing_package = replace(evidence, five_league_package=None)
    with pytest.raises(ValueError, match="five-league receipt package"):
        Top5ControlledRelease().activate(
            request,
            auth,
            missing_package,
            _rollout_evidence(),
            health_preconditions=_health,
            now=BASE + timedelta(minutes=2),
        )


@pytest.mark.parametrize(
    "package_mutation",
    [
        pytest.param(
            lambda package: replace(package, receipts=package.receipts[:1]),
            id="one-receipt",
        ),
        pytest.param(
            lambda package: replace(package, receipts=package.receipts[:4]),
            id="four-receipts",
        ),
        pytest.param(
            lambda package: replace(
                package,
                receipts=(
                    package.receipts[0],
                    package.receipts[0],
                    *package.receipts[2:],
                ),
            ),
            id="duplicate-league-receipt",
        ),
        pytest.param(
            lambda package: replace(
                package,
                dossier=replace(
                    package.dossier,
                    leagues=("BL1", "EPL", "LL", "SA", "SIX"),
                ),
            ),
            id="unknown-sixth-league",
        ),
        pytest.param(
            lambda package: replace(
                package,
                dossier=replace(
                    package.dossier,
                    receipt_digests=("f" * 64, *package.dossier.receipt_digests[1:]),
                ),
            ),
            id="receipt-digest-not-in-dossier",
        ),
        pytest.param(
            lambda package: replace(
                package,
                dossier=replace(
                    package.dossier,
                    dossier_digest="f" * 64,
                ),
            ),
            id="modified-dossier-digest",
        ),
        pytest.param(
            lambda package: replace(
                package,
                dossier=replace(package.dossier, authority_granted=True),
            ),
            id="dossier-claims-authority",
        ),
        pytest.param(
            lambda package: replace(
                package,
                receipts=(
                    replace(
                        package.receipts[0],
                        controlled_shadow_run_id="mixed-run",
                    ),
                    *package.receipts[1:],
                ),
            ),
            id="mixed-controlled-shadow-run",
        ),
        pytest.param(
            lambda package: replace(
                package,
                receipts=(
                    replace(
                        package.receipts[0],
                        qualification_session_id="mixed-session",
                    ),
                    *package.receipts[1:],
                ),
            ),
            id="mixed-qualification-session",
        ),
        pytest.param(
            lambda package: replace(
                package,
                dossier=replace(
                    package.dossier,
                    provider_identity="the_odds_api",
                    candidate_provider_identity="the_odds_api",
                ),
            ),
            id="provider-candidate-mismatch",
        ),
        pytest.param(
            lambda package: replace(
                package,
                receipts=(
                    replace(package.receipts[0], provider_identity="other-provider"),
                    *package.receipts[1:],
                ),
            ),
            id="mixed-receipt-providers",
        ),
    ],
)
def test_top5_activation_rejects_partial_or_tampered_dossier(
    tmp_path, package_mutation
):
    request, auth, evidence, _artifact, _, _health = _context(tmp_path)
    altered = replace(
        evidence,
        five_league_package=package_mutation(evidence.five_league_package),
    )
    with pytest.raises(ValueError):
        Top5ControlledRelease().activate(
            request,
            auth,
            altered,
            _rollout_evidence(),
            health_preconditions=_health,
            now=BASE + timedelta(minutes=2),
        )


def test_top5_activation_rejects_receipt_missing_from_supplied_package(tmp_path):
    request, auth, evidence, _artifact, _, _health = _context(tmp_path)
    altered = replace(evidence, receipts=evidence.receipts[:-1])
    with pytest.raises(ValueError, match="exactly match"):
        Top5ControlledRelease().activate(
            request,
            auth,
            altered,
            _rollout_evidence(),
            health_preconditions=_health,
            now=BASE + timedelta(minutes=2),
        )


def test_incomplete_policy_signal_time_and_authority_block(tmp_path):
    request, auth, evidence, _artifact, _, _health = _context(tmp_path)
    with pytest.raises(ValueError, match="positive integer"):
        replace(auth, minimum_sample_policy=MinimumSamplePolicy(0, 1)).validate()
    with pytest.raises(ValueError, match="signal-time approval"):
        auth.binds_request(
            replace(request, signal_time_contract=SignalTimeContract(30, 180, 300))
        )
    for altered in (
        replace(auth.signal_time_contract, minimum_minutes_before_kickoff=31),
        replace(auth.signal_time_contract, maximum_minutes_before_kickoff=179),
        replace(auth.signal_time_contract, maximum_odds_age_seconds=301),
    ):
        with pytest.raises(ValueError, match="exact signal-time contract"):
            auth.binds_request(replace(request, signal_time_contract=altered))
    with pytest.raises(ValueError, match="approved provider/result authority"):
        replace(
            auth, provider_authority=ProviderAuthority("fixture", "odds", "results")
        ).validate()
    with pytest.raises(ValueError, match="approved provider/result authority"):
        replace(auth, provider_authority=None).validate()
    with pytest.raises(ValueError, match="approved_result_source"):
        replace(
            auth,
            provider_authority=replace(
                auth.provider_authority, approved_result_source=""
            ),
        ).validate()
    with pytest.raises(ValueError, match="approved_odds_provider"):
        replace(
            auth,
            provider_authority=replace(
                auth.provider_authority, approved_odds_provider=""
            ),
        ).validate()
    with pytest.raises(ValueError, match="approved result source binding"):
        replace(
            auth,
            provider_authority=replace(
                auth.provider_authority, approved_result_source="other-results"
            ),
        ).binds_request(request)
    with pytest.raises(ValueError, match="approved odds provider binding"):
        replace(
            auth,
            provider_authority=replace(
                auth.provider_authority,
                approved_odds_provider="other-provider",
                approved_provider_set=("other-provider",),
            ),
        ).binds_request(request)
    with pytest.raises(ValueError, match="does not match REAL_OBSERVED"):
        replace(
            auth,
            provider_authority=replace(
                auth.provider_authority,
                approved_odds_provider="other-provider",
                approved_provider_set=("other-provider",),
            ),
        ).provider_authority.binds_receipts(evidence.receipts)
    auth.provider_authority.binds_request(request)


def test_candidate_evidence_and_production_authority_are_distinct(tmp_path):
    request, auth, evidence, _artifact, _publication_auth, _health = _context(tmp_path)
    candidate = evidence.five_league_package.dossier.provider_identity
    candidate_authority = replace(
        auth.provider_authority,
        approved_odds_provider=candidate,
        approved_provider_set=(candidate,),
    )
    candidate_request = replace(
        request,
        provider_authority=ProviderAuthority(
            "fixture-source", candidate, "result-source"
        ),
    )
    candidate_activation = replace(auth, provider_authority=candidate_authority)
    with pytest.raises(ValueError, match="candidate evidence provider"):
        evidence.validate(candidate_request, candidate_activation)

    evidence.validate(request, auth)


def test_missing_production_authority_blocks_controlled_release(tmp_path):
    request, auth, evidence, _artifact, _publication_auth, _health = _context(tmp_path)
    with pytest.raises(ValueError, match="provider/result authority"):
        evidence.validate(request, replace(auth, provider_authority=None))


def test_activation_does_not_imply_publication_and_publication_needs_separate_auth(
    tmp_path,
):
    request, auth, evidence, artifact, publication_auth, _health = _context(tmp_path)
    release = Top5ControlledRelease()
    state = release.activate(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        health_preconditions=_health,
        now=BASE + timedelta(minutes=2),
    )
    assert state.publication_enabled is False
    with pytest.raises(ValueError, match="publication authorization"):
        release.publish(
            artifact,
            replace(publication_auth, publication_authorized=False),
            now=BASE + timedelta(minutes=3),
        )
    with pytest.raises(ValueError, match="top5_release|exactly 15 records"):
        release.publish(artifact, publication_auth, now=BASE + timedelta(minutes=3))
    assert state.provider_authority_created is False
    assert state.scheduler_enabled is False
    assert state.ledger_mutated is False


def test_controlled_publication_uses_canonical_pwa_football_shape(tmp_path):
    request, auth, evidence, artifact, publication_auth, _health = _context(tmp_path)
    release = Top5ControlledRelease()
    release.activate(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        health_preconditions=_health,
        now=BASE + timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="top5_release|exactly 15 records"):
        release.publish(artifact, publication_auth, now=BASE + timedelta(minutes=3))


def test_publication_cannot_cross_activation_evidence_or_authority_bindings(tmp_path):
    request, auth, evidence, artifact, publication_auth, _health = _context(tmp_path)
    release = Top5ControlledRelease()
    release.activate(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        health_preconditions=_health,
        now=BASE + timedelta(minutes=2),
    )
    foreign = replace(
        artifact, evidence_digest="f" * 64, generated_at=BASE + timedelta(minutes=4)
    )
    with pytest.raises(ValueError, match="evidence_digest"):
        release.publish(foreign, publication_auth, now=BASE + timedelta(minutes=5))
    wrong_authority = replace(
        artifact,
        provider_authority="other-provider",
        generated_at=BASE + timedelta(minutes=6),
    )
    with pytest.raises(ValueError, match="activation binding"):
        release.publish(
            wrong_authority, publication_auth, now=BASE + timedelta(minutes=7)
        )


@pytest.mark.parametrize("field", ["source_sha", "research_sha"])
def test_publication_requires_record_provenance_to_match_artifact(tmp_path, field):
    request, auth, evidence, artifact, publication_auth, _health = _context(tmp_path)
    release = Top5ControlledRelease()
    release.activate(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        health_preconditions=_health,
        now=BASE + timedelta(minutes=2),
    )
    record = dict(artifact.football_records[0], **{field: "f" * 40})
    foreign = replace(
        artifact,
        football_records=(record,),
        generated_at=BASE + timedelta(minutes=4),
    )
    with pytest.raises(ValueError, match=f"football record binding mismatch: {field}"):
        release.publish(foreign, publication_auth, now=BASE + timedelta(minutes=5))


def test_stale_publication_closing_and_unsafe_authority_fail_closed(tmp_path):
    request, auth, evidence, artifact, publication_auth, _health = _context(tmp_path)
    release = Top5ControlledRelease()
    release.activate(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        health_preconditions=_health,
        now=BASE + timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="top5_release|exactly 15 records"):
        release.publish(artifact, publication_auth, now=BASE + timedelta(minutes=3))
    closing = dict(artifact.football_records[0], closing_used_for_prediction=True)
    unsafe = replace(
        artifact, football_records=(closing,), generated_at=BASE + timedelta(minutes=5)
    )
    with pytest.raises(ValueError, match="closing"):
        release.publish(unsafe, publication_auth, now=BASE + timedelta(minutes=6))


def test_rollback_disables_activation_and_publication_without_ledger_or_provider_state(
    tmp_path,
):
    request, auth, evidence, _artifact, _publication_auth, _health = _context(tmp_path)
    release = Top5ControlledRelease()
    release.activate(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        health_preconditions=_health,
        now=BASE + timedelta(minutes=2),
    )
    activation_rollback, publication_rollback = release.rollback(
        RollbackTrigger.PUBLICATION_ERROR
    )
    assert activation_rollback.restored_disabled is True
    assert activation_rollback.no_bet is True
    assert activation_rollback.ledger_mutated is False
    assert publication_rollback.restored_unpublished is True
    assert publication_rollback.active_artifact_digest is None
    assert release.activation_runtime.state is None
    assert release.publication_store.current is None


def test_controlled_publication_attestation_binds_active_authorized_artifact(tmp_path):
    request, auth, evidence, artifact, publication_auth, health = _context(tmp_path)
    release = Top5ControlledRelease()
    release.activate(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        health_preconditions=health,
        now=BASE + timedelta(minutes=2),
    )
    public_product = _complete_public_product()
    attestation = ControlledPublicationAttestation.issue(
        artifact,
        publication_auth,
        activation_bindings=release.activation_runtime.state.as_bindings(),
        artifact=public_product,
        now=BASE + timedelta(minutes=3),
    )
    assert attestation.active_activation is True
    assert attestation.publication_authorized is True
    assert attestation.no_bet is True
    assert attestation.activation_id == auth.activation_id
    assert (
        attestation.provider_authority == auth.provider_authority.approved_odds_provider
    )
    round_trip = ControlledPublicationAttestation.from_mapping(attestation.as_payload())
    round_trip.validate(
        artifact=public_product,
        artifact_path=artifact.artifact_path,
        now=BASE + timedelta(minutes=4),
    )

    with pytest.raises(ValueError, match="active activation"):
        replace(attestation, active_activation=False).validate(
            artifact=public_product,
            artifact_path=artifact.artifact_path,
            now=BASE + timedelta(minutes=4),
        )
    with pytest.raises(ValueError, match="separate publication authorization"):
        replace(attestation, publication_authorized=False).validate(
            artifact=public_product,
            artifact_path=artifact.artifact_path,
            now=BASE + timedelta(minutes=4),
        )
    with pytest.raises(ValueError, match="artifact hash"):
        round_trip.validate(
            artifact={**public_product, "updated": "tampered"},
            artifact_path=artifact.artifact_path,
            now=BASE + timedelta(minutes=4),
        )
    with pytest.raises(ValueError, match="stale or expired"):
        round_trip.validate(
            artifact=public_product,
            artifact_path=artifact.artifact_path,
            now=BASE + timedelta(days=2),
        )


def test_controlled_publication_attestation_requires_active_state_and_separate_auth(
    tmp_path,
):
    request, auth, evidence, artifact, publication_auth, _health = _context(tmp_path)
    release = Top5ControlledRelease()
    with pytest.raises(ValueError, match="active activation"):
        ControlledPublicationAttestation.issue(
            artifact,
            publication_auth,
            activation_bindings={},
            artifact=_complete_public_product(),
            now=BASE + timedelta(minutes=2),
        )
    health = _health
    release.activate(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        health_preconditions=health,
        now=BASE + timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="not approved"):
        ControlledPublicationAttestation.issue(
            artifact,
            replace(publication_auth, publication_authorized=False),
            activation_bindings=release.activation_runtime.state.as_bindings(),
            artifact=_complete_public_product(),
            now=BASE + timedelta(minutes=3),
        )


def _capability_fixture(tmp_path, monkeypatch):
    request, auth, evidence, artifact, publication_auth, health = _context(tmp_path)
    release = Top5ControlledRelease()
    release.activate(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        health_preconditions=health,
        now=BASE + timedelta(minutes=2),
    )
    runtime_now = datetime.now(timezone.utc)
    publication_auth = replace(
        publication_auth,
        issued_at=runtime_now - timedelta(minutes=1),
        expires_at=runtime_now + timedelta(days=1),
    )
    operator_home = tmp_path / "operator-home"
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.setattr(
        top5_publisher_module.pwd,
        "getpwuid",
        lambda uid: type("PasswdEntry", (), {"pw_dir": str(operator_home)})(),
    )
    capability_issuer = _SyntheticCapabilityIssuer("trusted")
    _install_synthetic_issuer_key(capability_issuer, monkeypatch)
    state_path = controlled_publication_capability_state_path()
    store = FileControlledPublicationCapabilityStore(state_path)
    product = _complete_public_product()
    activation_bindings = release.activation_runtime.state.as_bindings()
    attestation = ControlledPublicationAttestation.issue(
        artifact,
        publication_auth,
        activation_bindings=activation_bindings,
        artifact=product,
        now=runtime_now,
    )
    capability = store.issue(
        attestation,
        issuer_proof=capability_issuer.issue_proof(attestation),
    )

    def issue_next_capability():
        next_attestation = ControlledPublicationAttestation.issue(
            artifact,
            publication_auth,
            activation_bindings=activation_bindings,
            artifact=product,
            now=runtime_now,
        )
        next_capability = store.issue(
            next_attestation,
            issuer_proof=capability_issuer.issue_proof(next_attestation),
        )
        return next_attestation, next_capability

    product_path = tmp_path / artifact.artifact_path
    product_path.parent.mkdir(parents=True)
    product_path.write_text(json.dumps(product, sort_keys=True))
    attestation_path = tmp_path / "attestation.json"
    attestation_path.write_text(json.dumps(attestation.as_payload(), sort_keys=True))
    capability_path = tmp_path / "capability-token.json"
    capability_path.write_text(json.dumps(capability.as_payload(), sort_keys=True))
    return {
        "artifact": artifact,
        "product": product,
        "product_path": product_path,
        "attestation": attestation,
        "attestation_path": attestation_path,
        "capability": capability,
        "capability_path": capability_path,
        "state_path": state_path,
        "store": store,
        "release": release,
        "publication_authorization": publication_auth,
        "capability_issuer": capability_issuer,
        "runtime_now": runtime_now,
        "issue_capability": issue_next_capability,
    }


def _validator_command(fixture, *, capability_path=None):
    validator = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "validate_controlled_top5_publication.py"
    )
    return [
        sys.executable,
        str(validator),
        str(fixture["product_path"]),
        str(fixture["attestation_path"]),
        fixture["artifact"].artifact_path,
        str(capability_path or fixture["capability_path"]),
        "--consume",
    ], validator.parents[1]


def _run_validator(fixture, *, capability_path=None):
    command, _ = _validator_command(fixture, capability_path=capability_path)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        returncode = validate_publication([command[1], *command[2:]])
    return subprocess.CompletedProcess(
        command,
        returncode,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )


class _SyntheticCapabilityIssuer:
    """Test-only signer; no production key material is committed."""

    def __init__(self, label):
        self._label = label
        self._counter = 0
        self._private_key = Ed25519PrivateKey.generate()
        self.last_proof = None

    @property
    def public_key_hex(self):
        return (
            self._private_key.public_key()
            .public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
            .hex()
        )

    def issue_proof(self, attestation):
        self._counter += 1
        issuance_id = f"test-issuance:{self._label}:{self._counter}"
        attestation_digest = _ordinary_digest(attestation.as_payload())
        signature = self._private_key.sign(
            ControlledPublicationCapabilityIssuanceProof.message_for(
                attestation_digest, issuance_id
            )
        ).hex()
        self.last_proof = ControlledPublicationCapabilityIssuanceProof(
            attestation_digest=attestation_digest,
            issuance_id=issuance_id,
            signature=signature,
        )
        return self.last_proof


def _install_synthetic_issuer_key(issuer, monkeypatch):
    issuer_key_path = controlled_publication_issuer_public_key_path()
    issuer_key_path.parent.mkdir(parents=True, exist_ok=True)
    issuer_key_bytes = bytes.fromhex(issuer.public_key_hex)
    issuer_key_path.write_text(issuer.public_key_hex)
    issuer_key_path.chmod(0o600)
    monkeypatch.setattr(
        top5_publisher_module,
        "CONTROLLED_PUBLICATION_ISSUER_PUBLIC_KEY_SHA256",
        sha256(issuer_key_bytes).hexdigest(),
    )


def _ordinary_digest(value):
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def _self_consistent_forgery(attestation, **changes):
    payload = dict(attestation.as_payload())
    payload.update(changes)
    binding_fields = (
        "activation_id",
        "league_code",
        "candidate_id",
        "model_identity",
        "source_sha",
        "research_sha",
        "model_artifact_hash",
        "signal_time_experiment_id",
        "provider_authority",
        "result_authority",
        "evidence_digest",
        "controlled_shadow_run_id",
        "qualification_session_id",
    )
    payload["activation_binding_digest"] = _ordinary_digest(
        {"active": True, **{name: payload[name] for name in binding_fields}}
    )
    publication_fields = (
        "publication_authorization_id",
        "activation_id",
        "league_code",
        "candidate_id",
        "model_identity",
        "source_sha",
        "research_sha",
        "model_artifact_hash",
        "signal_time_experiment_id",
        "publication_authorized",
        "no_bet",
    )
    payload["publication_authorization_digest"] = _ordinary_digest(
        {name: payload[name] for name in publication_fields}
    )
    return payload


def test_legitimate_runtime_issued_capability_validates_and_consumes(
    tmp_path, monkeypatch
):
    fixture = _capability_fixture(tmp_path, monkeypatch)
    accepted = _run_validator(fixture)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(fixture["state_path"].read_text())["consumed"] is True


def test_self_consistent_forged_attestation_fails_against_runtime_capability(
    tmp_path, monkeypatch
):
    fixture = _capability_fixture(tmp_path, monkeypatch)
    forged_payload = _self_consistent_forgery(
        fixture["attestation"], activation_id="activation:forged"
    )
    forged = ControlledPublicationAttestation.from_mapping(forged_payload)
    forged.validate(
        artifact=fixture["product"],
        artifact_path=fixture["artifact"].artifact_path,
        now=fixture["runtime_now"],
    )
    fixture["attestation_path"].write_text(json.dumps(forged_payload, sort_keys=True))
    rejected = _run_validator(fixture)
    assert rejected.returncode != 0
    assert json.loads(fixture["state_path"].read_text())["consumed"] is False


def test_self_consistent_forged_external_state_is_ignored(tmp_path, monkeypatch):
    fixture = _capability_fixture(tmp_path, monkeypatch)
    forged_payload = _self_consistent_forgery(
        fixture["attestation"], activation_id="activation:attacker"
    )
    forged = ControlledPublicationAttestation.from_mapping(forged_payload)
    forged.validate(
        artifact=fixture["product"],
        artifact_path=fixture["artifact"].artifact_path,
        now=fixture["runtime_now"],
    )

    attacker_nonce = "attacker-nonce-" + "x" * 32
    attacker_capability_id = "top5-capability:attacker"
    attacker_state_path = tmp_path / "attacker-state" / "capability.json"
    attacker_state_path.parent.mkdir(parents=True)
    attacker_state_path.write_text(
        json.dumps(
            {
                "schema": "top5-controlled-publication-capability-v1",
                "capability_id": attacker_capability_id,
                "nonce_digest": sha256(attacker_nonce.encode()).hexdigest(),
                "attestation": forged_payload,
                "consumed": False,
            },
            sort_keys=True,
        )
    )
    attacker_token_path = tmp_path / "attacker-token.json"
    attacker_token_path.write_text(
        json.dumps(
            {
                "capability_id": attacker_capability_id,
                "capability_nonce": attacker_nonce,
            },
            sort_keys=True,
        )
    )
    fixture["attestation_path"].write_text(json.dumps(forged_payload, sort_keys=True))

    rejected = _run_validator(fixture, capability_path=attacker_token_path)

    assert rejected.returncode != 0
    assert json.loads(fixture["state_path"].read_text())["consumed"] is False
    assert json.loads(attacker_state_path.read_text())["consumed"] is False


def test_runtime_state_environment_cannot_redirect_capability_authority(
    tmp_path, monkeypatch
):
    fixture = _capability_fixture(tmp_path, monkeypatch)
    forged_payload = _self_consistent_forgery(
        fixture["attestation"], activation_id="activation:env-attacker"
    )
    forged = ControlledPublicationAttestation.from_mapping(forged_payload)
    forged.validate(
        artifact=fixture["product"],
        artifact_path=fixture["artifact"].artifact_path,
        now=fixture["runtime_now"],
    )

    attacker_root = tmp_path / "attacker-runtime"
    attacker_state_path = (
        attacker_root / "football" / "top5" / "controlled_publication_capability.json"
    )
    attacker_state_path.parent.mkdir(parents=True)
    attacker_nonce = "environment-attacker-nonce-" + "x" * 32
    attacker_capability_id = "top5-capability:environment-attacker"
    attacker_state_path.write_text(
        json.dumps(
            {
                "schema": "top5-controlled-publication-capability-v1",
                "capability_id": attacker_capability_id,
                "nonce_digest": sha256(attacker_nonce.encode()).hexdigest(),
                "attestation": forged_payload,
                "consumed": False,
            },
            sort_keys=True,
        )
    )
    attacker_token_path = tmp_path / "environment-attacker-token.json"
    attacker_token_path.write_text(
        json.dumps(
            {
                "capability_id": attacker_capability_id,
                "capability_nonce": attacker_nonce,
            },
            sort_keys=True,
        )
    )
    fixture["attestation_path"].write_text(json.dumps(forged_payload, sort_keys=True))
    monkeypatch.setenv("HOME", str(attacker_root))
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_STATE_DIR", str(attacker_root))

    rejected = _run_validator(fixture, capability_path=attacker_token_path)

    assert rejected.returncode != 0
    assert fixture["state_path"].is_file()
    assert json.loads(fixture["state_path"].read_text())["consumed"] is False
    assert json.loads(attacker_state_path.read_text())["consumed"] is False


def test_consumed_capability_rotates_without_manual_deletion(tmp_path, monkeypatch):
    fixture = _capability_fixture(tmp_path, monkeypatch)
    store = fixture["store"]
    capability_a = fixture["capability"]
    attestation = fixture["attestation"]
    artifact_path = fixture["artifact"].artifact_path
    now = fixture["runtime_now"]

    with pytest.raises(ValueError, match="still unconsumed"):
        fixture["issue_capability"]()
    assert json.loads(fixture["state_path"].read_text())["consumed"] is False

    store.consume(
        capability_a,
        attestation,
        artifact=fixture["product"],
        artifact_path=artifact_path,
        now=now,
    )
    with pytest.raises(ValueError):
        store.consume(
            capability_a,
            attestation,
            artifact=fixture["product"],
            artifact_path=artifact_path,
            now=now,
        )

    attestation_b, capability_b = fixture["issue_capability"]()
    state_after_rotation = json.loads(fixture["state_path"].read_text())
    assert capability_b.capability_id != capability_a.capability_id
    assert state_after_rotation["consumed"] is False
    assert state_after_rotation["rotation_history"][0]["capability_id"] == (
        capability_a.capability_id
    )

    store.consume(
        capability_b,
        attestation_b,
        artifact=fixture["product"],
        artifact_path=artifact_path,
        now=now,
    )
    for capability in (capability_a, capability_b):
        with pytest.raises(ValueError):
            store.consume(
                capability,
                attestation_b,
                artifact=fixture["product"],
                artifact_path=artifact_path,
                now=now,
            )


def test_direct_store_cannot_issue_from_self_consistent_forged_attestation(
    tmp_path, monkeypatch
):
    _request, _auth, _evidence, artifact, publication_auth, _health = _context(tmp_path)
    runtime_now = datetime.now(timezone.utc)
    publication_auth = replace(
        publication_auth,
        issued_at=runtime_now - timedelta(minutes=1),
        expires_at=runtime_now + timedelta(days=1),
    )
    monkeypatch.setattr(
        top5_publisher_module.pwd,
        "getpwuid",
        lambda uid: type(
            "PasswdEntry", (), {"pw_dir": str(tmp_path / "operator-home")}
        )(),
    )
    store = FileControlledPublicationCapabilityStore()
    state_path = controlled_publication_capability_state_path()
    trusted_issuer = _SyntheticCapabilityIssuer("trusted-direct-test")
    _install_synthetic_issuer_key(trusted_issuer, monkeypatch)
    public_product = _complete_public_product()
    fake_active_bindings = {
        "active": True,
        "activation_id": artifact.activation_id,
        "league_code": artifact.league_code,
        "candidate_id": artifact.candidate_id,
        "model_identity": artifact.model_identity,
        "source_sha": artifact.source_sha,
        "research_sha": artifact.research_sha,
        "model_artifact_hash": artifact.model_artifact_hash,
        "signal_time_experiment_id": artifact.signal_time_experiment_id,
        "provider_authority": artifact.provider_authority,
        "result_authority": artifact.result_authority,
        "evidence_digest": artifact.evidence_digest,
        "controlled_shadow_run_id": artifact.controlled_shadow_run_id,
        "qualification_session_id": artifact.qualification_session_id,
    }
    forged = ControlledPublicationAttestation.issue(
        artifact,
        publication_auth,
        activation_bindings=fake_active_bindings,
        artifact=public_product,
        now=runtime_now,
    )
    forged.validate(
        artifact=public_product,
        artifact_path=artifact.artifact_path,
        now=runtime_now,
    )
    product_path = tmp_path / artifact.artifact_path
    product_path.parent.mkdir(parents=True)
    product_path.write_text(json.dumps(public_product, sort_keys=True))
    attestation_path = tmp_path / "direct-forged-attestation.json"
    attestation_path.write_text(json.dumps(forged.as_payload(), sort_keys=True))
    capability_path = tmp_path / "direct-forged-capability.json"
    capability_path.write_text(
        json.dumps(
            {
                "capability_id": "top5-capability:direct-forgery",
                "capability_nonce": "direct-forgery-nonce-" + "x" * 32,
            },
            sort_keys=True,
        )
    )
    fixture = {
        "artifact": artifact,
        "product_path": product_path,
        "attestation_path": attestation_path,
        "capability_path": capability_path,
        "state_path": state_path,
    }

    with pytest.raises(ValueError, match="trusted signed proof"):
        store.issue(forged)
    assert not hasattr(top5_publisher_module, "_create_capability_issuance_proof")
    attacker_issuer = _SyntheticCapabilityIssuer("attacker")
    issuer_key_path = controlled_publication_issuer_public_key_path()
    issuer_key_path.write_text(attacker_issuer.public_key_hex)
    issuer_key_path.chmod(0o600)
    attacker_proof = attacker_issuer.issue_proof(forged)
    with pytest.raises(ValueError, match="fingerprint mismatch|signature"):
        store.issue(forged, issuer_proof=attacker_proof)

    rejected = _run_validator(fixture)
    assert rejected.returncode != 0
    assert not fixture["state_path"].exists()


def test_capability_store_rejects_noncanonical_state_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        top5_publisher_module.pwd,
        "getpwuid",
        lambda uid: type(
            "PasswdEntry", (), {"pw_dir": str(tmp_path / "operator-home")}
        )(),
    )
    with pytest.raises(ValueError, match="canonical operator path"):
        FileControlledPublicationCapabilityStore(tmp_path / "attacker-state.json")


def test_missing_capability_fails_closed(tmp_path, monkeypatch):
    fixture = _capability_fixture(tmp_path, monkeypatch)
    fixture["state_path"].unlink()
    rejected = _run_validator(fixture)
    assert rejected.returncode != 0


def test_wrong_capability_nonce_fails_closed(tmp_path, monkeypatch):
    fixture = _capability_fixture(tmp_path, monkeypatch)
    wrong_path = tmp_path / "wrong-capability.json"
    wrong_path.write_text(
        json.dumps(
            {
                "capability_id": fixture["capability"].capability_id,
                "capability_nonce": "wrong-nonce-" + "x" * 32,
            },
            sort_keys=True,
        )
    )
    rejected = _run_validator(fixture, capability_path=wrong_path)
    assert rejected.returncode != 0


def test_capability_replay_fails_after_successful_consumption(tmp_path, monkeypatch):
    fixture = _capability_fixture(tmp_path, monkeypatch)
    assert _run_validator(fixture).returncode == 0
    replay = _run_validator(fixture)
    assert replay.returncode != 0


def test_expired_capability_fails_without_consuming_state(tmp_path, monkeypatch):
    fixture = _capability_fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="stale or expired"):
        fixture["store"].consume(
            fixture["capability"],
            fixture["attestation"],
            artifact=fixture["product"],
            artifact_path=fixture["artifact"].artifact_path,
            now=fixture["attestation"].expires_at + timedelta(seconds=1),
        )
    assert json.loads(fixture["state_path"].read_text())["consumed"] is False


def test_artifact_changed_after_capability_issue_fails_closed(tmp_path, monkeypatch):
    fixture = _capability_fixture(tmp_path, monkeypatch)
    fixture["product_path"].write_text(json.dumps({"tampered": True}))
    rejected = _run_validator(fixture)
    assert rejected.returncode != 0
    assert json.loads(fixture["state_path"].read_text())["consumed"] is False


def test_activation_binding_changed_after_capability_issue_fails_closed(
    tmp_path, monkeypatch
):
    fixture = _capability_fixture(tmp_path, monkeypatch)
    fixture["attestation_path"].write_text(
        json.dumps(
            _self_consistent_forgery(
                fixture["attestation"], activation_id="activation:changed"
            ),
            sort_keys=True,
        )
    )
    rejected = _run_validator(fixture)
    assert rejected.returncode != 0


def test_publication_authorization_changed_after_capability_issue_fails_closed(
    tmp_path, monkeypatch
):
    fixture = _capability_fixture(tmp_path, monkeypatch)
    fixture["attestation_path"].write_text(
        json.dumps(
            _self_consistent_forgery(
                fixture["attestation"],
                publication_authorization_id="publication-auth:changed",
            ),
            sort_keys=True,
        )
    )
    rejected = _run_validator(fixture)
    assert rejected.returncode != 0


def test_capability_issue_requires_active_activation_and_valid_publication_auth(
    tmp_path, monkeypatch
):
    request, auth, evidence, artifact, publication_auth, health = _context(tmp_path)
    monkeypatch.setattr(
        top5_publisher_module.pwd,
        "getpwuid",
        lambda uid: type(
            "PasswdEntry", (), {"pw_dir": str(tmp_path / "operator-home")}
        )(),
    )
    store = FileControlledPublicationCapabilityStore()
    release = Top5ControlledRelease()
    with pytest.raises(ValueError, match="active activation"):
        ControlledPublicationAttestation.issue(
            artifact,
            publication_auth,
            activation_bindings={},
            artifact=_complete_public_product(),
            now=BASE + timedelta(minutes=2),
        )
    release.activate(
        request,
        auth,
        evidence,
        _rollout_evidence(),
        health_preconditions=health,
        now=BASE + timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="not approved"):
        ControlledPublicationAttestation.issue(
            artifact,
            replace(publication_auth, publication_authorized=False),
            activation_bindings=release.activation_runtime.state.as_bindings(),
            artifact=_complete_public_product(),
            now=BASE + timedelta(minutes=3),
        )
    with pytest.raises(ValueError, match="top5_release|exactly 15 records"):
        release.issue_publication_capability(
            artifact,
            publication_auth,
            store,
            now=BASE + timedelta(minutes=3),
        )
