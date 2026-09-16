"""Focused fail-closed tests for controlled activation/publication V1."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

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
    ControlledActivationAuthorization,
    Top5ControlledRelease,
    Top5ControlledReleaseEvidence,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    MinimumSamplePolicy,
)
from src.football.top5_provider_validation import ProviderAuthority
from src.football.top5_publisher import (
    ControlledTop5PublicationPayload,
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
    signal_time = SignalTimeContract(
        30,
        180,
        300,
        approval_ref="activation-auth:controlled-release",
    )
    authority = ProviderAuthority("fixture-source", "odds-source", "result-source")
    request = ControlledActivationRequest(
        league_code=league,
        candidate_id=M5_CANDIDATE_ID,
        model_identity=M5_CANDIDATE_ID,
        source_sha=INTEGRATION_SHA,
        research_sha=FROZEN_RESEARCH_SHA,
        model_artifact_hash=inventory_for("BL1", M5_CANDIDATE_ID).model_artifact_hash
        or "",
        provider_authority=authority,
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
        minimum_sample_policy=MinimumSamplePolicy(1, 1),
        provider_authority=authority,
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
        provider_authority="odds-source",
        result_authority="result-source",
        evidence_digest=evidence_digest,
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


def test_missing_activation_authorization_blocks(tmp_path):
    request, auth, evidence, _artifact, _, _health = _context(tmp_path)
    blocked = replace(auth, activation_authorized=False)
    with pytest.raises(ValueError, match="not approved"):
        Top5ControlledRelease().activate(
            request,
            blocked,
            evidence,
            _rollout_evidence(),
            now=BASE + timedelta(minutes=2),
        )


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
            request, auth, missing, _rollout_evidence(), now=BASE + timedelta(minutes=2)
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
            now=BASE + timedelta(minutes=2),
        )


def test_incomplete_policy_signal_time_and_authority_block(tmp_path):
    request, auth, _evidence, _artifact, _, _health = _context(tmp_path)
    with pytest.raises(ValueError, match="positive integer"):
        replace(auth, minimum_sample_policy=MinimumSamplePolicy(0, 1)).validate()
    with pytest.raises(ValueError, match="signal-time approval"):
        auth.binds_request(
            replace(request, signal_time_contract=SignalTimeContract(30, 180, 300))
        )
    with pytest.raises(ValueError, match="complete provider"):
        replace(
            auth, provider_authority=ProviderAuthority(None, "odds", "results")
        ).validate()
    with pytest.raises(ValueError, match="complete provider"):
        replace(
            auth, provider_authority=ProviderAuthority("fixture", "odds", None)
        ).validate()


def test_activation_does_not_imply_publication_and_publication_needs_separate_auth(
    tmp_path,
):
    request, auth, evidence, artifact, publication_auth, _health = _context(tmp_path)
    release = Top5ControlledRelease()
    state = release.activate(
        request, auth, evidence, _rollout_evidence(), now=BASE + timedelta(minutes=2)
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


def test_publication_cannot_cross_activation_evidence_or_authority_bindings(tmp_path):
    request, auth, evidence, artifact, publication_auth, _health = _context(tmp_path)
    release = Top5ControlledRelease()
    release.activate(
        request, auth, evidence, _rollout_evidence(), now=BASE + timedelta(minutes=2)
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
        request, auth, evidence, _rollout_evidence(), now=BASE + timedelta(minutes=2)
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
        request, auth, evidence, _rollout_evidence(), now=BASE + timedelta(minutes=2)
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
        request, auth, evidence, _rollout_evidence(), now=BASE + timedelta(minutes=2)
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
