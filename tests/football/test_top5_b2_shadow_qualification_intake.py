"""No-network tests for the operational Builder-2 qualification intake."""

from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path

import pytest

import src.football.top5_b2_shadow_qualification_intake as intake
from src.football.top5_b2_shadow_qualification_intake import (
    INTAKE_CONTRACT_VERSION,
    STRUCTURAL_INTAKE_CONTRACT_VERSION,
    Builder2QualificationIntakeError,
    Builder2QualificationIntakeManifestV1,
    Builder2QualificationSourceArtifactV1,
    main,
    project_manifest_to_structural_provider,
    run_intake,
    validate_intake,
)
from src.football.top5_builder2_qualification_receipt import semantic_digest
from src.football.top5_controlled_shadow_provider_qualification import (
    MinimumSamplePolicy,
    ObservationEvidenceKind,
    ProviderQualificationPurpose,
    QualificationContractError,
)
from src.football.top5_provider_cascade_validation import evidence_digest
from tests.football.test_top5_controlled_shadow_provider_qualification import (
    READY,
    TIMING,
    _authorization,
    _observation,
    _session,
)


def _manifest(**changes: object) -> Builder2QualificationIntakeManifestV1:
    observation = changes.pop("observation", _observation())
    session = changes.pop("session", _session())
    authorization = changes.pop("authorization", _authorization())
    cascade = changes.pop("cascade_evidence", observation.cascade_evidence)
    attestation = changes.pop("capture_attestation", observation.capture_attestation)
    assert attestation is not None
    observation = replace(
        observation,
        cascade_evidence=cascade,
        capture_attestation=attestation,
    )
    observation_payload = observation.as_payload()
    observation_payload.pop("capture_attestation", None)
    defaults = {
        "schema_version": INTAKE_CONTRACT_VERSION,
        "intake_id": "intake-1",
        "controlled_shadow_run_id": attestation.controlled_shadow_run_id,
        "qualification_session_id": session.qualification_session_id,
        "ceo_authorization_id": authorization.authorization_id,
        "fixture_key": observation.fixture_key,
        "provider_identity": observation.provider_identity,
        "provider_event_id": observation.provider_event_id,
        "provider_request_id": observation.provider_request_id,
        "observation_id": observation.observation_id,
        "observation_digest": semantic_digest(observation_payload),
        "normalized_record_digest": observation.normalized_record_digest,
        "cascade_evidence_digest": evidence_digest(cascade),
        "capture_attestation_digest": semantic_digest(attestation.as_payload()),
        "adapter_version": observation.adapter_version,
        "adapter_source_sha": observation.adapter_source_sha,
        "timing_policy_reference": "timing-policy-v1",
        "readiness_reference": "provider-readiness-v1",
        "source_artifacts": (
            Builder2QualificationSourceArtifactV1(
                role="observation-envelope",
                path="/private/tmp/sportsbrain-b2-captured/observation.json",
                digest="a" * 64,
            ),
        ),
        "observation": observation,
        "session": session,
        "authorization": authorization,
        "cascade_evidence": cascade,
        "capture_attestation": attestation,
        "timing_policy": TIMING,
        "provider_readiness": READY,
    }
    defaults.update(changes)
    return Builder2QualificationIntakeManifestV1(**defaults)


def test_valid_intake_derives_one_receipt_without_network_or_activation() -> None:
    result = validate_intake(_manifest())

    assert result.receipt.accepted is True
    assert result.receipt.no_bet is True
    assert result.receipt.publication is False
    assert result.receipt.production_activation is False
    assert result.receipt.monetary_spend_authorized is False
    assert result.qualification_report.production_activation_authorized is False
    assert result.qualification_report.signal_time_note
    assert result.sample_report is None


def test_explicit_structural_projection_keeps_source_immutable_and_issues_typed_receipt():
    source = _manifest()
    source_payload = source.as_payload()
    projected = project_manifest_to_structural_provider(
        source,
        immutable_source_path="/private/tmp/shadow-artifact-004.json",
    )

    assert source.as_payload() == source_payload
    assert projected.schema_version == STRUCTURAL_INTAKE_CONTRACT_VERSION
    assert (
        projected.qualification_purpose
        is ProviderQualificationPurpose.STRUCTURAL_PROVIDER
    )
    assert projected.timing_policy is None
    assert projected.structural_policy is not None
    payload = projected.as_payload()
    assert "timing_policy" not in payload
    assert "timing_policy_reference" not in payload
    assert payload["qualification_purpose"] == "STRUCTURAL_PROVIDER"
    assert (
        payload["qualification_policy_reference"]
        == "top5-structural-provider-qualification-policy-v1"
    )
    assert (
        payload["structural_policy"]["production_signal_time_values_approved"] is False
    )
    assert payload["structural_policy"]["signal_time_approved"] is False
    assert (
        projected.authorization.authorization_id
        == source.authorization.authorization_id
    )
    assert projected.observation.as_payload() == source.observation.as_payload()
    source_binding = next(
        item
        for item in projected.source_artifacts
        if item.role == "immutable-source-manifest"
    )
    assert source_binding.digest == source.manifest_digest

    restored = Builder2QualificationIntakeManifestV1.from_payload(payload)
    result = validate_intake(restored)
    assert (
        result.receipt.qualification_purpose
        == ProviderQualificationPurpose.STRUCTURAL_PROVIDER
    )
    assert result.receipt.production_signal_time_values_approved is False
    assert result.receipt.signal_time_approved is False
    assert (
        result.qualification_report.as_payload()["qualification_purpose"]
        == "STRUCTURAL_PROVIDER"
    )


def test_manifest_round_trip_and_unknown_or_missing_fields_are_rejected() -> None:
    manifest = _manifest()
    restored = Builder2QualificationIntakeManifestV1.from_payload(manifest.as_payload())
    assert restored.as_payload() == manifest.as_payload()
    assert restored.manifest_digest == manifest.manifest_digest

    missing = manifest.as_payload()
    del missing["capture_attestation"]
    with pytest.raises(Builder2QualificationIntakeError):
        Builder2QualificationIntakeManifestV1.from_payload(missing)

    unknown = manifest.as_payload()
    unknown["unexpected_authority"] = True
    with pytest.raises(Builder2QualificationIntakeError):
        Builder2QualificationIntakeManifestV1.from_payload(unknown)


def test_manifest_preserves_forty_character_source_commit_sha() -> None:
    base = _manifest()
    source_sha = "d" * 40
    attestation = replace(
        base.capture_attestation,
        adapter_source_sha=source_sha,
    )
    observation = replace(
        base.observation,
        adapter_source_sha=source_sha,
        capture_attestation=attestation,
    )
    session = replace(base.session, adapter_source_sha=source_sha)
    manifest = _manifest(
        observation=observation,
        session=session,
        capture_attestation=attestation,
    )

    restored = Builder2QualificationIntakeManifestV1.from_payload(manifest.as_payload())
    result = validate_intake(restored)

    assert restored.adapter_source_sha == source_sha
    assert result.receipt.adapter_source_sha == source_sha


def test_manifest_rejects_malformed_source_commit_sha() -> None:
    payload = _manifest().as_payload()
    payload["adapter_source_sha"] = "z" * 40

    with pytest.raises(
        Builder2QualificationIntakeError,
        match="40-character commit SHA or 64-character source digest",
    ):
        Builder2QualificationIntakeManifestV1.from_payload(payload)


@pytest.mark.parametrize(
    "field",
    [
        "controlled_shadow_run_id",
        "qualification_session_id",
        "ceo_authorization_id",
        "fixture_key",
        "provider_identity",
        "provider_event_id",
        "provider_request_id",
        "observation_id",
        "observation_digest",
        "normalized_record_digest",
        "cascade_evidence_digest",
        "capture_attestation_digest",
        "adapter_version",
        "adapter_source_sha",
    ],
)
def test_manifest_binding_tampering_fails_closed(field: str) -> None:
    manifest = _manifest()
    value = "f" * 64 if field.endswith(("digest", "sha")) else "changed"
    with pytest.raises(Builder2QualificationIntakeError):
        replace(manifest, **{field: value}).validate()


def test_nested_identity_and_provenance_changes_fail_closed() -> None:
    manifest = _manifest()
    assert manifest.capture_attestation is not None
    with pytest.raises(Builder2QualificationIntakeError):
        replace(
            manifest,
            capture_attestation=replace(
                manifest.capture_attestation,
                controlled_shadow_run_id="run-b",
            ),
        ).validate()

    with pytest.raises(Builder2QualificationIntakeError):
        replace(
            manifest,
            authorization=replace(manifest.authorization, authorization_id="auth-b"),
        ).validate()

    changed_cascade = replace(
        manifest.cascade_evidence,
        provenance=replace(
            manifest.cascade_evidence.provenance,
            artifact_id="cascade-artifact-b",
        ),
    )
    with pytest.raises(Builder2QualificationIntakeError):
        replace(manifest, cascade_evidence=changed_cascade).validate()

    changed_observation = replace(
        manifest.observation,
        provider_request_id="request-b",
    )
    with pytest.raises(Builder2QualificationIntakeError):
        replace(manifest, observation=changed_observation).validate()

    changed_embedded = manifest.observation.as_payload()
    changed_embedded["cascade_evidence"] = {
        **manifest.cascade_evidence.as_payload(),
        "selected_provider": "unapproved-provider",
    }
    with pytest.raises(Builder2QualificationIntakeError):
        intake.Builder2QualificationIntakeManifestV1.from_payload(
            {**manifest.as_payload(), "observation": changed_embedded}
        )


@pytest.mark.parametrize(
    "kind",
    [
        ObservationEvidenceKind.TEST_FIXTURE,
        ObservationEvidenceKind.MOCK,
        ObservationEvidenceKind.OFFLINE_REPLAY,
    ],
)
def test_non_real_evidence_never_enters_operational_intake(
    kind: ObservationEvidenceKind,
) -> None:
    observation = replace(_observation(), evidence_kind=kind, network_request_count=0)
    manifest = _manifest(observation=observation)
    with pytest.raises(Builder2QualificationIntakeError):
        validate_intake(manifest)


def test_rejected_observation_prediction_input_and_safety_fail_closed() -> None:
    rejected = _manifest(observation=replace(_observation(), market_phase="IN_PLAY"))
    with pytest.raises(Builder2QualificationIntakeError):
        validate_intake(rejected)

    unsafe_cascade = replace(
        _manifest().cascade_evidence, prediction_input_allowed=False
    )
    unsafe_observation = replace(_observation(), cascade_evidence=unsafe_cascade)
    with pytest.raises(Builder2QualificationIntakeError):
        _manifest(
            observation=unsafe_observation, cascade_evidence=unsafe_cascade
        ).validate()

    manifest = _manifest()
    assert manifest.capture_attestation is not None
    unsafe_attestation = replace(manifest.capture_attestation, network_execution=False)
    with pytest.raises(QualificationContractError):
        replace(manifest, capture_attestation=unsafe_attestation).validate()


def test_run_is_idempotent_and_conflicting_same_intake_is_rejected(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    evidence_dir = tmp_path / "external-evidence"
    first = run_intake(manifest, evidence_dir)
    second = run_intake(manifest, evidence_dir)

    assert first.receipt.receipt_digest == second.receipt.receipt_digest
    assert first.artifact_directory == second.artifact_directory
    assert (evidence_dir / "intake-1" / "receipt.json").exists()

    changed = replace(manifest, timing_policy_reference="timing-policy-v2")
    with pytest.raises(Builder2QualificationIntakeError):
        run_intake(changed, evidence_dir)
    assert (evidence_dir / "intake-1" / "receipt.json").read_text()

    duplicate = replace(manifest, intake_id="intake-2")
    with pytest.raises(Builder2QualificationIntakeError):
        run_intake(duplicate, evidence_dir)


def test_atomic_staging_failure_leaves_no_partial_intake(
    monkeypatch, tmp_path: Path
) -> None:
    def fail_once(*args: object, **kwargs: object) -> None:
        raise OSError("injected staged write failure")

    monkeypatch.setattr(intake, "atomic_write_json", fail_once)
    evidence_dir = tmp_path / "external-evidence"
    with pytest.raises(OSError):
        run_intake(_manifest(), evidence_dir)

    assert not (evidence_dir / "intake-1").exists()
    assert list(evidence_dir.iterdir()) == []


def test_protected_output_and_source_paths_are_rejected() -> None:
    manifest = _manifest()
    with pytest.raises(Builder2QualificationIntakeError):
        run_intake(manifest, "/private/tmp/runtime")
    with pytest.raises(Builder2QualificationIntakeError):
        replace(
            manifest,
            source_artifacts=(
                Builder2QualificationSourceArtifactV1(
                    "credentials", "/private/tmp/credentials.json", "a" * 64
                ),
            ),
        ).validate()


def test_optional_sample_update_is_receipt_only_and_never_authorizes(
    tmp_path: Path,
) -> None:
    source = _manifest()
    receipt = validate_intake(source).receipt
    result = run_intake(
        source,
        tmp_path / "sample-output",
        existing_receipts=(receipt,),
        minimum_sample_policy=MinimumSamplePolicy(1, 1),
    )
    assert result.sample_report is not None
    assert result.sample_report.sample_sufficient is True
    assert result.sample_report.duplicate_receipt_ids == (
        receipt.qualification_receipt_id,
    )
    assert result.sample_report.production_activation_authorized is False
    assert result.sample_report.no_bet is True
    assert result.sample_report.publication is False


def test_cli_validate_and_run_emit_explicit_safety_banners(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = tmp_path / "manifest.json"
    intake.atomic_write_json(manifest_path, _manifest().as_payload(), sort_keys=True)

    assert main(["validate", str(manifest_path)]) == 0
    output = capsys.readouterr().out
    assert "NO NETWORK EXECUTION" in output
    assert "NO BET" in output
    assert "NO PUBLICATION" in output
    assert "NO PRODUCTION ACTIVATION" in output

    assert (
        main(["run", str(manifest_path), "--evidence-dir", str(tmp_path / "evidence")])
        == 0
    )
    assert (tmp_path / "evidence" / "intake-1" / "result.json").exists()


def test_cli_aggregate_and_inspect_consume_canonical_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = tmp_path / "manifest.json"
    intake.atomic_write_json(manifest_path, _manifest().as_payload(), sort_keys=True)
    evidence_dir = tmp_path / "evidence"
    assert main(["run", str(manifest_path), "--evidence-dir", str(evidence_dir)]) == 0
    capsys.readouterr()

    assert main(["inspect", str(evidence_dir / "intake-1" / "receipt.json")]) == 0
    assert "receipt" in capsys.readouterr().out
    assert main(["aggregate", str(evidence_dir)]) == 0
    aggregate_output = capsys.readouterr().out
    assert "AGGREGATED" in aggregate_output
    assert "NO NETWORK EXECUTION" in aggregate_output


def test_intake_module_has_no_network_execution_path() -> None:
    source = inspect.getsource(intake)
    for forbidden in (
        "import requests",
        "import urllib",
        "http.client",
        "provider_client",
        "socket.socket",
    ):
        assert forbidden not in source
    assert '"production_activation": True' not in source
