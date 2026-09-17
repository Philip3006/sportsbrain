"""Focused fail-closed tests for controlled activation/publication V1."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from scripts.top5_real_shadow_session import main as session_cli
from src.football.production_contracts import RolloutEvidence, SignalTimeContract
from src.football.top5_activation_readiness import (
    ControlledActivationRequest,
    RollbackTrigger,
)
from src.football.top5_builder2_qualification_receipt import (
    Builder2QualificationReceiptV1,
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
    ControlledTop5PublicationPayload,
    FileControlledPublicationCapabilityStore,
    Top5PublicationAuthorization,
)
from src.football.top5_qualification_sample_aggregator import (
    aggregate_builder2_qualification_samples,
)
from src.football.top5_real_shadow_audit import audit_session_payload
from src.football.top5_real_shadow_measurement import measure_session_payload
from src.football.top5_real_shadow_session import RealShadowSession
from src.football.top5_real_shadow_session_evidence import build_shadow_evidence
from src.football.top5_research_binding import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    inventory_for,
)
from tests.football.test_top5_b2_shadow_qualification_intake import _manifest
from tests.football.test_top5_real_shadow_session import (
    BASE,
    INTEGRATION_SHA,
    closing_attachment,
    final_result,
)


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
    from src.football.top5_b2_shadow_qualification_intake import run_intake

    intake = run_intake(_manifest(), tmp_path / "b2-intake")
    intake_dir = intake.artifact_directory
    assert intake_dir is not None
    session_path = tmp_path / "session.json"
    evidence_path = tmp_path / "evidence.json"
    assert (
        session_cli(
            [
                "--b2-intake-dir",
                str(intake_dir),
                "--session-key",
                "shadow-session:controlled-release",
                "--integration-sha",
                INTEGRATION_SHA,
                "--experiment-id",
                "shadow-experiment:controlled-release-v1",
                "--created-at",
                BASE.isoformat(),
                "--min-lead-minutes",
                "30",
                "--max-lead-minutes",
                "180",
                "--max-odds-age-seconds",
                "300",
                "--kickoff-tolerance-seconds",
                "0",
                "--output",
                str(session_path),
                "--evidence-output",
                str(evidence_path),
            ]
        )
        == 0
    )
    session = RealShadowSession.from_payload(json.loads(session_path.read_text()))
    prediction = next(iter(session.predictions.values()))
    session.attach_result(final_result(prediction, "controlled-release"))
    session.attach_closing(closing_attachment(prediction, "controlled-release"))
    session_payload = session.as_payload()
    bundle = build_shadow_evidence(session)
    audit = audit_session_payload(session_payload, evidence_bundle=bundle)
    measurement = measure_session_payload(session_payload, evidence_bundle=bundle)
    receipt = session_payload["observations"][0]["independent_validation"]
    receipt_object = Builder2QualificationReceiptV1.from_payload(receipt)
    policy = MinimumSamplePolicy(1, 1)
    sample_report = aggregate_builder2_qualification_samples(
        (receipt_object,), minimum_sample_policy=policy
    )
    release_evidence = Top5ControlledReleaseEvidence(
        receipts=(receipt_object,),
        sample_report=sample_report,
        audit_report=audit,
        measurement_report=measurement,
    )
    return release_evidence, measurement, receipt_object


def _context(tmp_path):
    evidence, measurement, receipt = _evidence(tmp_path)
    receipt = evidence.receipts[0]
    league = receipt.fixture_key.split("|", 1)[0]
    proposal = ProviderAuthority(
        "fixture-source", receipt.provider_identity, "result-source"
    )
    approved_authority = ApprovedProviderResultAuthority(
        authority_decision_id="provider-auth:controlled-release",
        league_code=league,
        approved_odds_provider=receipt.provider_identity,
        approved_provider_set=(receipt.provider_identity,),
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
        config_snapshot={"league": league, "candidate": M5_CANDIDATE_ID},
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
        minimum_sample_policy=MinimumSamplePolicy(1, 1),
        provider_authority=approved_authority,
        controlled_shadow_run_id=receipt.controlled_shadow_run_id,
        qualification_session_id=receipt.qualification_session_id,
        fixture_scope=(receipt.fixture_key,),
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
        provider_authority=receipt.provider_identity,
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
    auth.provider_authority.binds_receipts(evidence.receipts)


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
    published = release.publish(
        artifact, publication_auth, now=BASE + timedelta(minutes=3)
    )
    assert published.public_product["football"]
    assert (
        published.public_product["health"]["top5_evidence_digest"]
        == artifact.evidence_digest
    )
    assert published.public_product["health"]["top5_provider_authority"] == (
        artifact.provider_authority
    )
    assert published.public_product["health"]["publication_enabled"] is True
    assert release.publication_store.current is published
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
    published = release.publish(
        artifact, publication_auth, now=BASE + timedelta(minutes=3)
    )
    records = published.public_product["football"]
    assert isinstance(records, list) and records
    record = records[0]
    assert {
        "league",
        "fixture_key",
        "home",
        "away",
        "match",
        "market",
        "model_prob",
        "model_identity",
        "model_version",
        "prediction_timestamp",
        "signal_timestamp",
        "signal_snapshot_id",
        "activation_state",
        "publication_status",
        "publication_enabled",
        "no_bet",
        "provenance",
        "result_status",
        "settlement_status",
        "run_id",
        "session_id",
    } <= record.keys()
    assert "probabilities" not in record
    assert record["league"] == artifact.league_code
    assert record["model_identity"] == artifact.model_identity
    assert record["activation_state"] == "CONTROLLED"
    assert record["publication_status"] == "PUBLISHED"
    assert record["publication_enabled"] is True
    assert record["no_bet"] is True
    assert record["provenance"]["source_sha"] == artifact.source_sha
    assert record["provenance"]["research_sha"] == artifact.research_sha
    assert record["run_id"] == artifact.controlled_shadow_run_id
    assert record["session_id"] == artifact.qualification_session_id


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
    release.publish(artifact, publication_auth, now=BASE + timedelta(minutes=3))
    with pytest.raises(ValueError, match="stale"):
        release.publish(artifact, publication_auth, now=BASE + timedelta(minutes=4))
    closing = dict(artifact.football_records[0], closing_used_for_prediction=True)
    unsafe = replace(
        artifact, football_records=(closing,), generated_at=BASE + timedelta(minutes=5)
    )
    with pytest.raises(ValueError, match="closing"):
        release.publish(unsafe, publication_auth, now=BASE + timedelta(minutes=6))


def test_rollback_disables_activation_and_publication_without_ledger_or_provider_state(
    tmp_path,
):
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
    release.publish(artifact, publication_auth, now=BASE + timedelta(minutes=3))
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
    attestation = release.issue_publication_attestation(
        artifact, publication_auth, now=BASE + timedelta(minutes=3)
    )
    public_product = artifact.as_public_product()
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
        release.issue_publication_attestation(
            artifact, publication_auth, now=BASE + timedelta(minutes=2)
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
        release.issue_publication_attestation(
            artifact,
            replace(publication_auth, publication_authorized=False),
            now=BASE + timedelta(minutes=3),
        )


def _capability_fixture(tmp_path):
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
    state_path = tmp_path / "operator-runtime" / "controlled-capability.json"
    store = FileControlledPublicationCapabilityStore(state_path)
    attestation, capability = release.issue_publication_capability(
        artifact,
        publication_auth,
        store,
        now=runtime_now,
    )
    product_path = tmp_path / artifact.artifact_path
    product_path.parent.mkdir(parents=True)
    product_path.write_text(json.dumps(artifact.as_public_product(), sort_keys=True))
    attestation_path = tmp_path / "attestation.json"
    attestation_path.write_text(json.dumps(attestation.as_payload(), sort_keys=True))
    capability_path = tmp_path / "capability-token.json"
    capability_path.write_text(json.dumps(capability.as_payload(), sort_keys=True))
    return {
        "artifact": artifact,
        "product": artifact.as_public_product(),
        "product_path": product_path,
        "attestation": attestation,
        "attestation_path": attestation_path,
        "capability": capability,
        "capability_path": capability_path,
        "state_path": state_path,
        "store": store,
        "runtime_now": runtime_now,
    }


def _validator_command(fixture, *, state_path=None, capability_path=None):
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
        str(state_path or fixture["state_path"]),
        str(capability_path or fixture["capability_path"]),
        "--consume",
    ], validator.parents[1]


def _run_validator(fixture, *, state_path=None, capability_path=None):
    command, root = _validator_command(
        fixture, state_path=state_path, capability_path=capability_path
    )
    return subprocess.run(
        command,
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
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


def test_legitimate_runtime_issued_capability_validates_and_consumes(tmp_path):
    fixture = _capability_fixture(tmp_path)
    accepted = _run_validator(fixture)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(fixture["state_path"].read_text())["consumed"] is True


def test_self_consistent_forged_attestation_fails_against_runtime_capability(tmp_path):
    fixture = _capability_fixture(tmp_path)
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


def test_missing_capability_fails_closed(tmp_path):
    fixture = _capability_fixture(tmp_path)
    rejected = _run_validator(
        fixture, state_path=tmp_path / "operator-runtime" / "missing.json"
    )
    assert rejected.returncode != 0


def test_wrong_capability_nonce_fails_closed(tmp_path):
    fixture = _capability_fixture(tmp_path)
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


def test_capability_replay_fails_after_successful_consumption(tmp_path):
    fixture = _capability_fixture(tmp_path)
    assert _run_validator(fixture).returncode == 0
    replay = _run_validator(fixture)
    assert replay.returncode != 0


def test_expired_capability_fails_without_consuming_state(tmp_path):
    fixture = _capability_fixture(tmp_path)
    with pytest.raises(ValueError, match="stale or expired"):
        fixture["store"].consume(
            fixture["capability"],
            fixture["attestation"],
            artifact=fixture["product"],
            artifact_path=fixture["artifact"].artifact_path,
            now=fixture["attestation"].expires_at + timedelta(seconds=1),
        )
    assert json.loads(fixture["state_path"].read_text())["consumed"] is False


def test_artifact_changed_after_capability_issue_fails_closed(tmp_path):
    fixture = _capability_fixture(tmp_path)
    fixture["product_path"].write_text(json.dumps({"tampered": True}))
    rejected = _run_validator(fixture)
    assert rejected.returncode != 0
    assert json.loads(fixture["state_path"].read_text())["consumed"] is False


def test_activation_binding_changed_after_capability_issue_fails_closed(tmp_path):
    fixture = _capability_fixture(tmp_path)
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
    tmp_path,
):
    fixture = _capability_fixture(tmp_path)
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
    tmp_path,
):
    request, auth, evidence, artifact, publication_auth, health = _context(tmp_path)
    store = FileControlledPublicationCapabilityStore(
        tmp_path / "operator-runtime" / "capability.json"
    )
    release = Top5ControlledRelease()
    with pytest.raises(ValueError, match="active activation"):
        release.issue_publication_capability(
            artifact, publication_auth, store, now=BASE + timedelta(minutes=2)
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
        release.issue_publication_capability(
            artifact,
            replace(publication_auth, publication_authorized=False),
            store,
            now=BASE + timedelta(minutes=3),
        )
