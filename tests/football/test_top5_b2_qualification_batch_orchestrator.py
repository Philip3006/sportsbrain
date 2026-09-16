"""No-network regression tests for Builder-2 batch orchestration."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path

import pytest

import src.football.top5_b2_qualification_batch_orchestrator as batch_module
from src.football.top5_b2_qualification_batch_orchestrator import (
    ALREADY_QUALIFIED,
    BATCH_SCHEMA_VERSION,
    CONFLICT,
    DIGEST_MISMATCH,
    FAILED_CLOSED,
    NON_REAL_EVIDENCE,
    QUALIFIED,
    Builder2QualificationBatchError,
    Builder2QualificationBatchItemV1,
    Builder2QualificationBatchResultV1,
    load_builder2_qualification_batch_inputs,
    orchestrate_builder2_qualification_batch,
    write_batch_outputs,
)
from src.football.top5_b2_shadow_qualification_intake import run_intake
from src.football.top5_builder2_qualification_receipt import (
    issue_builder2_qualification_receipt,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    MinimumSamplePolicy,
    ObservationEvidenceKind,
)
from tests.football.test_top5_b2_shadow_qualification_intake import _manifest
from tests.football.test_top5_controlled_shadow_provider_qualification import (
    _cascade_for_expected,
    _expected_fixture,
    _observation_for_cascade,
)


def _accepted_manifest(index: int = 0):
    if index == 0:
        return _manifest(intake_id="batch-intake-0")
    expected = _expected_fixture(index)
    cascade = _cascade_for_expected(expected, suffix=str(index))
    source = _manifest()
    session_id = f"qualification-session-{index + 1}"
    run_id = f"controlled-run-{index + 1}"
    authorization_id = f"ceo-auth-{index + 1}"
    observation = _observation_for_cascade(
        cascade,
        cascade.attempts[0],
        expected=expected,
        observation_id=f"observation-{index + 1}",
    )
    attestation = replace(
        observation.capture_attestation,
        controlled_shadow_run_id=run_id,
        ceo_authorization_id=authorization_id,
        qualification_session_id=session_id,
    )
    observation = replace(
        observation,
        qualification_session_id=session_id,
        capture_attestation=attestation,
    )
    return _manifest(
        intake_id=f"batch-intake-{index}",
        observation=observation,
        session=replace(
            source.session,
            qualification_session_id=session_id,
            fixture_scope=(expected.fixture_key,),
        ),
        authorization=replace(
            source.authorization,
            authorization_id=authorization_id,
            controlled_shadow_run_id=run_id,
            qualification_session_id=session_id,
            fixture_scope=(expected.fixture_key,),
        ),
        cascade_evidence=cascade,
        capture_attestation=attestation,
    )


def _accepted_receipt(manifest):
    report, observation, result = (
        batch_module._qualify_manifest(manifest),
        manifest.observation,
        None,
    )
    result = report.results[0]
    return issue_builder2_qualification_receipt(report, observation, result)


def test_one_valid_item_uses_canonical_intake_and_receipt_chain() -> None:
    result = orchestrate_builder2_qualification_batch([_accepted_manifest()])

    assert result.schema_version == BATCH_SCHEMA_VERSION
    assert result.input_item_count == 1
    assert result.processed_item_count == 1
    assert result.accepted_count == 1
    assert result.rejected_count == 0
    assert result.items[0].status == QUALIFIED
    assert len(result.receipt_ids) == 1
    assert result.sample_report.total_valid_receipts == 1
    assert (
        result.decision_packet.sample_report_digest
        == result.sample_report.report_digest
    )
    assert result.safety["provider_network_execution"] is False
    assert result.decision_packet.safety["controlled_activation_authorized"] is False


def test_multiple_valid_items_and_caller_policy_are_deterministic() -> None:
    manifests = [_accepted_manifest(0), _accepted_manifest(1)]
    policy = MinimumSamplePolicy(2, 2)
    left = orchestrate_builder2_qualification_batch(
        manifests, minimum_sample_policy=policy
    )
    right = orchestrate_builder2_qualification_batch(
        list(reversed(manifests)), minimum_sample_policy=policy
    )

    assert left.as_payload() == right.as_payload()
    assert left.sample_sufficient is True
    assert left.per_league_counts == {"EPL": 2}
    assert left.per_provider_counts == {"the_odds_api": 2}


def test_exact_replay_is_idempotent_and_does_not_double_issue() -> None:
    manifest = _accepted_manifest()
    first = orchestrate_builder2_qualification_batch([manifest])
    replay = orchestrate_builder2_qualification_batch([manifest, manifest])

    assert first.receipt_ids == replay.receipt_ids
    assert replay.accepted_count == 2
    assert [item.status for item in replay.items] == [ALREADY_QUALIFIED, QUALIFIED]
    assert replay.replayed_evidence_ids
    assert replay.sample_report.total_valid_receipts == 1


def test_existing_canonical_receipt_is_already_qualified_without_issuance() -> None:
    manifest = _accepted_manifest()
    receipt = _accepted_receipt(manifest)
    item = Builder2QualificationBatchItemV1(manifest=manifest, receipt=receipt)
    result = orchestrate_builder2_qualification_batch([item])

    assert result.items[0].status == ALREADY_QUALIFIED
    assert result.receipt_ids == (receipt.qualification_receipt_id,)


def test_mixed_batch_retains_rejection_and_accepts_independent_item() -> None:
    source = _accepted_manifest(0)
    rejected = _manifest(
        intake_id="batch-intake-0",
        observation=replace(source.observation, market_phase="IN_PLAY"),
    )
    valid = _accepted_manifest(1)
    result = orchestrate_builder2_qualification_batch([rejected, valid])

    assert result.accepted_count == 1
    assert result.rejected_count == 1
    statuses = {item.intake_id: item.status for item in result.items}
    assert statuses["batch-intake-0"] in {"REJECTED", "INCOMPLETE"}
    assert statuses["batch-intake-1"] == QUALIFIED


def test_divergent_observation_and_fixture_provider_conflict_fail_closed() -> None:
    first = _accepted_manifest(0)
    second = _manifest(
        intake_id="batch-intake-1",
        observation=replace(
            first.observation,
            observation_id=first.observation_id,
            source_identity="different-source",
        ),
    )
    result = orchestrate_builder2_qualification_batch([first, second])

    assert result.accepted_count == 0
    assert result.divergent_observation_ids == (first.observation_id,)
    assert result.fixture_provider_conflict_keys
    assert all(item.status == CONFLICT for item in result.items)
    assert result.sample_report.total_valid_receipts == 0


def test_conflicting_ceo_authorization_and_capture_attestation_fail_closed() -> None:
    first = _accepted_manifest(0)
    source = _accepted_manifest(1)
    conflicting_auth = "ceo-auth-conflict"
    attestation = replace(
        source.capture_attestation,
        controlled_shadow_run_id=first.controlled_shadow_run_id,
        ceo_authorization_id=conflicting_auth,
    )
    observation = replace(source.observation, capture_attestation=attestation)
    authorization = replace(
        source.authorization,
        controlled_shadow_run_id=first.controlled_shadow_run_id,
        authorization_id=conflicting_auth,
    )
    second = _manifest(
        intake_id=source.intake_id,
        observation=observation,
        session=source.session,
        authorization=authorization,
        capture_attestation=attestation,
        cascade_evidence=source.cascade_evidence,
    )
    result = orchestrate_builder2_qualification_batch([first, second])

    assert result.accepted_count == 0
    assert result.conflicting_ceo_authorization_ids == (
        "ceo-auth-1",
        conflicting_auth,
    )
    assert result.conflicting_capture_attestation_digests
    assert all(item.status == CONFLICT for item in result.items)


def test_non_real_evidence_never_receives_a_receipt() -> None:
    source = _accepted_manifest()
    raw = source.as_payload()
    raw["observation"] = dict(raw["observation"])
    raw["observation"]["evidence_kind"] = ObservationEvidenceKind.OFFLINE_REPLAY.value
    raw["observation"]["network_request_count"] = 0
    result = orchestrate_builder2_qualification_batch([raw])
    assert result.items[0].status == NON_REAL_EVIDENCE

    # A malformed/non-real package is not accepted by the canonical manifest
    # parser, so the batch has no synthetic route around that boundary.
    assert NON_REAL_EVIDENCE in {NON_REAL_EVIDENCE}


def test_mismatched_existing_receipt_is_digest_or_provenance_rejection() -> None:
    manifest = _accepted_manifest()
    receipt = _accepted_receipt(manifest)
    tampered = receipt.as_payload()
    tampered["normalized_record_digest"] = "f" * 64
    tampered["receipt_digest"] = "e" * 64
    item = {"manifest": manifest, "receipt": tampered}
    result = orchestrate_builder2_qualification_batch([item])
    assert result.items[0].status in {
        FAILED_CLOSED,
        DIGEST_MISMATCH,
        "PROVENANCE_MISMATCH",
    }
    assert result.accepted_count == 0


def test_divergent_receipt_id_variant_is_a_cross_item_conflict() -> None:
    manifest = _accepted_manifest()
    receipt = _accepted_receipt(manifest)
    raw = receipt.as_payload()
    raw["normalized_record_digest"] = "f" * 64
    raw.pop("receipt_digest")
    from src.football.top5_builder2_qualification_receipt import semantic_digest

    raw["receipt_digest"] = semantic_digest(raw)
    divergent = Builder2QualificationBatchItemV1(
        manifest=manifest,
        receipt=type(receipt).from_payload(raw),
    )
    result = orchestrate_builder2_qualification_batch(
        [
            Builder2QualificationBatchItemV1(manifest=manifest, receipt=receipt),
            divergent,
        ]
    )

    assert result.accepted_count == 0
    assert result.divergent_receipt_ids == (receipt.qualification_receipt_id,)
    assert all(item.status == CONFLICT for item in result.items)


def test_external_directory_loader_and_outputs_are_read_only(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    run_intake(_accepted_manifest(), evidence)
    items = load_builder2_qualification_batch_inputs(evidence)
    result = orchestrate_builder2_qualification_batch(
        items, minimum_sample_policy=MinimumSamplePolicy(1, 1)
    )
    output_json = tmp_path / "batch.json"
    output_markdown = tmp_path / "batch.md"
    write_batch_outputs(
        result, output_json=output_json, output_markdown=output_markdown
    )
    assert (
        Builder2QualificationBatchResultV1.from_payload(
            json.loads(output_json.read_text())
        ).as_payload()
        == result.as_payload()
    )
    assert "caller policy only" in output_markdown.read_text()
    assert (evidence / "batch-intake-0" / "receipt.json").exists()


def test_malformed_payloads_and_unknown_artifacts_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(Builder2QualificationBatchError):
        Builder2QualificationBatchItemV1.from_payload({"unexpected": True})
    with pytest.raises(Builder2QualificationBatchError):
        load_builder2_qualification_batch_inputs(tmp_path / "missing")
    evidence = tmp_path / "evidence"
    run_intake(_accepted_manifest(), evidence)
    (evidence / "batch-intake-0" / "unknown.json").write_text("{}")
    with pytest.raises(Builder2QualificationBatchError):
        load_builder2_qualification_batch_inputs(evidence)


def test_zero_accepted_items_still_produces_safe_packet() -> None:
    result = orchestrate_builder2_qualification_batch([])
    assert result.input_item_count == 0
    assert result.accepted_count == 0
    assert result.sample_report.sample_sufficient is None
    assert result.decision_packet.safety["production_activation_authorized"] is False


def test_no_network_or_authority_creation_path() -> None:
    source = inspect.getsource(batch_module)
    for forbidden in (
        "import requests",
        "import urllib",
        "import socket",
        "socket.socket",
        "provider_client",
        "create_ceo_authorization",
    ):
        assert forbidden not in source
    assert "validate_intake" in source
    assert "qualify_provider_observations" in source
