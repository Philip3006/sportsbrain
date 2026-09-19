"""Offline, deterministic tests for the Champions League evidence seam."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.champions_league_candidate_evidence import (
    CalibrationEvidence,
    CandidateArtifactIdentity,
    ChampionsLeagueCandidateEvidenceError,
    ContaminationPass,
    DatasetManifestBinding,
    FeatureSchemaIdentity,
    FinalEvaluationEvidence,
    RuntimeInputs,
    SourceProvenance,
    TrainingPartitionIdentity,
    OfflineQualificationReport,
    qualify_offline_candidate,
    validate_offline_qualification,
)

UTC = timezone.utc
BASE = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def _bundle() -> dict[str, object]:
    manifest = DatasetManifestBinding.build(
        manifest_id="manifest-1",
        dataset_id="dataset-1",
        manifest_sha="a" * 64,
        partition_ids=("train-1", "cal-1", "holdout-1"),
    )
    candidate = CandidateArtifactIdentity.build(
        candidate_id="candidate-1",
        artifact_id="artifact-1",
        artifact_sha="b" * 64,
        version="v1",
        dataset_manifest_id=manifest.manifest_id,
        dataset_manifest_sha=manifest.manifest_sha,
    )
    schema = FeatureSchemaIdentity.build(
        schema_id="schema-1",
        schema_version="v1",
        feature_names=("pre_match_form", "elo_rating"),
    )
    training = TrainingPartitionIdentity.build(
        partition_id="train-1",
        dataset_id=manifest.dataset_id,
        start_at=_at(0),
        end_at=_at(10),
        row_count=100,
        dataset_manifest_id=manifest.manifest_id,
        dataset_manifest_sha=manifest.manifest_sha,
    )
    calibration = CalibrationEvidence.build(
        evidence_id="calibration-1",
        partition_id="cal-1",
        partition_sha="c" * 64,
        candidate_artifact_id=candidate.artifact_id,
        artifact_sha=candidate.artifact_sha,
        feature_schema_sha=schema.schema_sha,
        provenance_id="source-calibration",
        metric_name="brier_score",
        metric_value=0.18,
        sample_count=100,
        dataset_manifest_id=manifest.manifest_id,
        dataset_manifest_sha=manifest.manifest_sha,
    )
    final = FinalEvaluationEvidence.build(
        evidence_id="evaluation-1",
        evaluation_id="holdout-evaluation-1",
        partition_id="holdout-1",
        partition_sha="d" * 64,
        candidate_artifact_id=candidate.artifact_id,
        artifact_sha=candidate.artifact_sha,
        feature_schema_sha=schema.schema_sha,
        provenance_id="source-final",
        metric_name="log_loss",
        metric_value=0.42,
        sample_count=100,
        result_source_id="settled-results-1",
        evaluated_at=_at(20),
        dataset_manifest_id=manifest.manifest_id,
        dataset_manifest_sha=manifest.manifest_sha,
    )
    provenance = tuple(
        SourceProvenance.build(
            provenance_id=provenance_id,
            source_name=provenance_id,
            source_sha=source_sha,
            dataset_id=manifest.dataset_id,
            available_at=_at(1),
            retrieved_at=_at(2),
            row_count=100,
            role=role,
            manifest_id=manifest.manifest_id,
            manifest_sha=manifest.manifest_sha,
        )
        for provenance_id, source_sha, role in (
            ("source-training", "e" * 64, "training"),
            ("source-calibration", "f" * 64, "calibration"),
            ("source-final", "1" * 64, "final_evaluation"),
            ("source-runtime", "2" * 64, "runtime"),
        )
    )
    contamination = ContaminationPass.build(
        check_id="contamination-1",
        checked_at=_at(30),
        method="source-and-fixture-digest-intersection-v1",
        training_source_sha="e" * 64,
        calibration_source_sha="f" * 64,
        final_evaluation_source_sha="1" * 64,
        manifest_sha=manifest.manifest_sha,
    )
    runtime = RuntimeInputs.build(
        runtime_id="runtime-1",
        runtime_version="v1",
        candidate_artifact_id=candidate.artifact_id,
        artifact_sha=candidate.artifact_sha,
        feature_schema_sha=schema.schema_sha,
        input_names=schema.feature_names,
        config_sha="3" * 64,
        provenance_id="source-runtime",
        dataset_manifest_id=manifest.manifest_id,
        dataset_manifest_sha=manifest.manifest_sha,
    )
    return {
        "report_id": "report-1",
        "candidate_artifact_identity": candidate,
        "dataset_manifest_binding": manifest,
        "feature_schema_identity": schema,
        "training_partition_identity": training,
        "calibration_evidence": (calibration,),
        "final_evaluation_evidence": (final,),
        "source_provenance": provenance,
        "contamination_pass": contamination,
        "runtime_inputs": runtime,
    }


def test_complete_bundle_is_accepted_and_round_trips_deterministically() -> None:
    first = qualify_offline_candidate(**_bundle())
    second = qualify_offline_candidate(**_bundle())

    assert first.accepted is True
    assert first.errors == ()
    assert first.report_sha == second.report_sha
    assert validate_offline_qualification(first) == first
    restored = OfflineQualificationReport.from_mapping(first.as_payload())
    assert restored.as_payload() == first.as_payload()

    tampered = first.as_payload()
    tampered["accepted"] = False
    tampered["errors"] = ["manual override"]
    with pytest.raises(ChampionsLeagueCandidateEvidenceError, match="digest changed"):
        validate_offline_qualification(tampered)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("dataset_manifest_binding", None),
        ("calibration_evidence", ()),
        ("final_evaluation_evidence", ()),
        ("source_provenance", ()),
        ("contamination_pass", None),
        ("runtime_inputs", None),
    ),
)
def test_missing_required_evidence_rejects(field: str, replacement: object) -> None:
    bundle = _bundle()
    bundle[field] = replacement
    report = qualify_offline_candidate(**bundle)

    assert report.accepted is False
    with pytest.raises(ChampionsLeagueCandidateEvidenceError):
        validate_offline_qualification(report)


def test_candidate_and_manifest_identity_tampering_fail_closed() -> None:
    bundle = _bundle()
    candidate = bundle["candidate_artifact_identity"]
    assert isinstance(candidate, CandidateArtifactIdentity)
    bundle["candidate_artifact_identity"] = replace(candidate, artifact_sha="9" * 64)
    assert "identity digest changed" in qualify_offline_candidate(**bundle).errors[0]

    bundle = _bundle()
    manifest = bundle["dataset_manifest_binding"]
    assert isinstance(manifest, DatasetManifestBinding)
    bundle["dataset_manifest_binding"] = replace(manifest, manifest_sha="9" * 64)
    report = qualify_offline_candidate(**bundle)
    assert report.accepted is False
    assert "binding digest changed" in report.errors[0]


def test_partition_calibration_contamination_and_runtime_bindings_are_checked() -> None:
    bundle = _bundle()
    training = bundle["training_partition_identity"]
    assert isinstance(training, TrainingPartitionIdentity)
    bundle["training_partition_identity"] = replace(training, partition_id="foreign-partition")
    assert "absent from the dataset manifest" in qualify_offline_candidate(**bundle).errors[0]

    bundle = _bundle()
    calibration = bundle["calibration_evidence"]
    assert isinstance(calibration, tuple)
    bundle["calibration_evidence"] = (replace(calibration[0], passed=False),)
    assert "calibration evidence did not pass" in qualify_offline_candidate(**bundle).errors[0]

    bundle = _bundle()
    contamination = bundle["contamination_pass"]
    assert isinstance(contamination, ContaminationPass)
    bundle["contamination_pass"] = replace(contamination, overlap_count=1)
    assert "contamination overlap is non-zero" in qualify_offline_candidate(**bundle).errors[0]

    bundle = _bundle()
    runtime = bundle["runtime_inputs"]
    assert isinstance(runtime, RuntimeInputs)
    bundle["runtime_inputs"] = replace(runtime, input_names=("closing_odds",))
    assert "future or closing-market" in qualify_offline_candidate(**bundle).errors[0]


def test_runtime_schema_and_future_input_names_are_incompatible() -> None:
    bundle = _bundle()
    runtime = bundle["runtime_inputs"]
    assert isinstance(runtime, RuntimeInputs)
    changed = RuntimeInputs.build(
        runtime_id=runtime.runtime_id,
        runtime_version=runtime.runtime_version,
        candidate_artifact_id=runtime.candidate_artifact_id,
        artifact_sha=runtime.artifact_sha,
        feature_schema_sha=runtime.feature_schema_sha,
        input_names=("pre_match_form", "unknown_feature"),
        config_sha=runtime.config_sha,
        provenance_id=runtime.provenance_id,
        dataset_manifest_id=runtime.dataset_manifest_id,
        dataset_manifest_sha=runtime.dataset_manifest_sha,
    )
    bundle["runtime_inputs"] = changed
    report = qualify_offline_candidate(**bundle)
    assert report.accepted is False
    assert "incompatible" in report.errors[0]

    schema = bundle["feature_schema_identity"]
    assert isinstance(schema, FeatureSchemaIdentity)
    with pytest.raises(ChampionsLeagueCandidateEvidenceError, match="future"):
        FeatureSchemaIdentity.build(
            schema_id=schema.schema_id,
            schema_version=schema.schema_version,
            feature_names=("future_result",),
        ).validate()


def test_mapping_input_is_supported_but_missing_binding_is_not() -> None:
    payload = _bundle()
    payload = {
        key: [item.as_payload() for item in value]
        if isinstance(value, tuple) and value and hasattr(value[0], "as_payload")
        else value.as_payload() if hasattr(value, "as_payload") else value
        for key, value in payload.items()
    }
    assert qualify_offline_candidate(payload).accepted is True

    payload.pop("dataset_manifest_binding")
    rejected = qualify_offline_candidate(payload)
    assert rejected.accepted is False
    assert "dataset manifest binding" in rejected.errors[0]
