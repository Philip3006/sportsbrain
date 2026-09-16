"""No-network tests for the Builder-2 informational decision packet."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path

import pytest

import src.football.top5_b2_shadow_decision_packet as packet_module
from src.football.top5_b2_shadow_decision_packet import (
    AUTHORITY_MISMATCH,
    EVIDENCE_COMPLETE,
    EVIDENCE_CONFLICT,
    EVIDENCE_INCOMPLETE,
    SAMPLE_BELOW_CALLER_POLICY,
    SAMPLE_MEETS_CALLER_POLICY,
    Builder2QualificationDecisionPacketError,
    Builder2QualificationDecisionPacketV1,
    Builder2QualificationIntakeArtifactV1,
    build_builder2_qualification_decision_packet,
    build_decision_packet_from_directory,
    load_decision_packet_inputs,
    main,
)
from src.football.top5_b2_shadow_qualification_intake import run_intake
from src.football.top5_builder2_qualification_receipt import (
    issue_builder2_qualification_receipt,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    MinimumSamplePolicy,
)
from tests.football.test_top5_b2_shadow_qualification_intake import _manifest
from tests.football.test_top5_builder2_qualification_receipt import _accepted
from tests.football.test_top5_qualification_sample_aggregator import (
    _receipt_for_fixture,
    _receipt_for_same_fixture_with_changed_provider_identity,
    _receipt_variant,
)


def _receipt_and_report():
    report, observation, result = _accepted()
    return report, issue_builder2_qualification_receipt(report, observation, result)


def _intake_directory(tmp_path: Path) -> Path:
    output = tmp_path / "external-evidence"
    run_intake(_manifest(), output)
    return output


def test_one_valid_receipt_is_summarized_without_production_authority() -> None:
    report, receipt = _receipt_and_report()
    packet = build_builder2_qualification_decision_packet(
        [receipt], qualification_reports=[report]
    )

    assert packet.status == EVIDENCE_INCOMPLETE
    assert packet.evidence_completeness == EVIDENCE_INCOMPLETE
    assert packet.total_valid_receipts == 1
    assert packet.eligible_real_observation_count == 1
    assert packet.eligible_fixture_count == 1
    assert packet.controlled_shadow_run_ids == (receipt.controlled_shadow_run_id,)
    assert packet.qualification_session_ids == (receipt.qualification_session_id,)
    assert packet.ceo_authorization_ids == (receipt.ceo_authorization_id,)
    assert packet.sample_sufficient is None
    assert packet.safety["production_activation_authorized"] is False


def test_complete_intake_directory_produces_json_and_markdown(tmp_path: Path) -> None:
    packet = build_decision_packet_from_directory(
        _intake_directory(tmp_path),
        minimum_sample_policy=MinimumSamplePolicy(1, 1),
    )

    assert packet.status == SAMPLE_MEETS_CALLER_POLICY
    assert packet.evidence_completeness == EVIDENCE_COMPLETE
    assert packet.sample_sufficient is True
    assert packet.packet_id.startswith("b2dp-")
    payload = packet.as_payload()
    assert payload["packet_digest"] == packet.packet_digest
    assert "CEO decisions still required" in packet.as_markdown()
    assert payload["safety"]["controlled_activation_authorized"] is False


def test_multi_receipt_sample_and_per_provider_league_counts() -> None:
    report, first = _receipt_and_report()
    second = _receipt_for_fixture(1)
    packet = build_builder2_qualification_decision_packet(
        [first, second],
        qualification_reports=[report],
        minimum_sample_policy=MinimumSamplePolicy(2, 2),
    )

    assert packet.sample_sufficient is True
    assert packet.status == EVIDENCE_INCOMPLETE
    assert packet.eligible_real_observation_count == 2
    assert packet.eligible_fixture_count == 2
    assert packet.per_provider_counts == {"the_odds_api": 2}
    assert packet.per_league_counts == {"EPL": 2}


def test_sample_below_caller_policy_is_measurement_only() -> None:
    report, receipt = _receipt_and_report()
    packet = build_builder2_qualification_decision_packet(
        [receipt],
        qualification_reports=[report],
        minimum_sample_policy=MinimumSamplePolicy(2, 2),
    )

    assert packet.status == EVIDENCE_INCOMPLETE
    assert packet.sample_state == SAMPLE_BELOW_CALLER_POLICY
    assert packet.sample_sufficient is False
    assert packet.safety["production_activation_authorized"] is False
    assert packet.safety["model_authorized"] is False
    assert packet.safety["signal_time_authorized"] is False
    assert packet.safety["publication_authorized"] is False
    assert packet.safety["betting_authorized"] is False


def test_exact_duplicate_receipt_is_visible_but_not_double_counted() -> None:
    report, receipt = _receipt_and_report()
    packet = build_builder2_qualification_decision_packet(
        [receipt, receipt], qualification_reports=[report]
    )

    assert packet.total_valid_receipts == 1
    assert packet.input_receipt_count == 2
    assert packet.duplicate_receipt_ids == (receipt.qualification_receipt_id,)
    assert packet.eligible_real_observation_count == 1


def test_divergent_receipt_is_conflict_and_never_eligible() -> None:
    report, receipt = _receipt_and_report()
    divergent = _receipt_variant(
        receipt,
        observation_id="different-observation",
        observation_digest="d" * 64,
    )
    packet = build_builder2_qualification_decision_packet(
        [receipt, divergent], qualification_reports=[report]
    )

    assert packet.status == EVIDENCE_CONFLICT
    assert packet.divergent_receipt_ids == (receipt.qualification_receipt_id,)
    assert packet.eligible_real_observation_count == 0
    assert "EVIDENCE_CONFLICT_REQUIRES_CEO_REVIEW" in packet.unresolved_items


def test_observation_identity_and_fixture_provider_conflicts_are_exposed() -> None:
    report, receipt = _receipt_and_report()
    same_id_new_digest = _receipt_variant(
        receipt,
        qualification_result_digest="a" * 64,
        observation_digest="b" * 64,
    )
    same_digest_new_id = _receipt_variant(
        receipt,
        qualification_result_digest="c" * 64,
        observation_id="observation-conflict-other-id",
    )
    fixture_provider_variant = (
        _receipt_for_same_fixture_with_changed_provider_identity()
    )
    packet = build_builder2_qualification_decision_packet(
        [receipt, same_id_new_digest, same_digest_new_id, fixture_provider_variant],
        qualification_reports=[report],
    )

    assert packet.status == EVIDENCE_CONFLICT
    assert packet.observation_identity_conflict_ids == (receipt.observation_id,)
    assert packet.fixture_provider_conflicts
    assert packet.eligible_real_observation_count == 0


def test_unattributed_fixture_is_excluded_without_invented_league() -> None:
    report, receipt = _receipt_and_report()
    raw = receipt.as_payload()
    raw["fixture_key"] = "fixture-without-canonical-league"
    raw.pop("receipt_digest")
    from src.football.top5_builder2_qualification_receipt import semantic_digest

    raw["receipt_digest"] = semantic_digest(raw)
    changed = type(receipt).from_payload(raw)
    packet = build_builder2_qualification_decision_packet(
        [changed], qualification_reports=[report]
    )

    assert packet.status == EVIDENCE_INCOMPLETE
    assert packet.per_league_counts == {}
    assert packet.eligible_fixture_count == 0
    assert packet.unattributed_fixture_keys == ("fixture-without-canonical-league",)


def test_missing_intake_and_mismatched_receipt_intake_are_not_authority(
    tmp_path: Path,
) -> None:
    report, receipt = _receipt_and_report()
    missing = build_builder2_qualification_decision_packet(
        [receipt], qualification_reports=[report]
    )
    assert "MISSING_INTAKE_ARTIFACT" in missing.evidence_completeness_reasons

    manifest = _manifest()
    run_result = run_intake(manifest, tmp_path / "mismatched-intake")
    artifact = Builder2QualificationIntakeArtifactV1(
        manifest=manifest,
        qualification_report=run_result.qualification_report,
        receipt=_receipt_variant(receipt, ceo_authorization_id="other-auth"),
        result=run_result.as_payload(),
        artifact_path="external-intake",
    )
    packet = build_builder2_qualification_decision_packet(intake_artifacts=[artifact])
    assert packet.status == AUTHORITY_MISMATCH
    assert packet.intake_summaries[0].binding_issues


def test_non_real_or_rejected_evidence_is_represented_by_upstream_report_only() -> None:
    report, receipt = _receipt_and_report()
    rejected = replace(
        report,
        unresolved=("EVIDENCE_REJECTED",),
    )
    packet = build_builder2_qualification_decision_packet(
        [receipt], qualification_reports=[rejected]
    )
    assert packet.status == EVIDENCE_INCOMPLETE
    assert "EVIDENCE_REJECTED" in packet.unresolved_items
    assert packet.eligible_real_observation_count == 1


def test_deterministic_input_order_and_serialized_round_trip_inputs(
    tmp_path: Path,
) -> None:
    report, first = _receipt_and_report()
    second = _receipt_for_fixture(1)
    left = build_builder2_qualification_decision_packet(
        [first, second],
        qualification_reports=[report],
        minimum_sample_policy=MinimumSamplePolicy(2, 2),
    )
    right = build_builder2_qualification_decision_packet(
        [second, first],
        qualification_reports=[report],
        minimum_sample_policy=MinimumSamplePolicy(2, 2),
    )
    assert left.as_payload() == right.as_payload()
    assert left.as_markdown() == right.as_markdown()

    root = _intake_directory(tmp_path)
    inputs = load_decision_packet_inputs(root)
    assert len(inputs["intake_artifacts"]) == 1
    serialized = json.dumps(left.as_payload(), sort_keys=True)
    assert serialized == json.dumps(right.as_payload(), sort_keys=True)
    restored = Builder2QualificationDecisionPacketV1.from_payload(left.as_payload())
    assert restored.as_payload() == left.as_payload()


def test_cli_writes_only_explicit_external_json_and_markdown_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_directory = _intake_directory(tmp_path)
    output_json = tmp_path / "decision-packet.json"
    output_markdown = tmp_path / "decision-packet.md"
    assert (
        main(
            [
                str(input_directory),
                "--output-json",
                str(output_json),
                "--output-markdown",
                str(output_markdown),
                "--minimum-real-observations",
                "1",
                "--minimum-distinct-fixtures",
                "1",
            ]
        )
        == 0
    )
    capsys.readouterr()
    payload = json.loads(output_json.read_text())
    assert Builder2QualificationDecisionPacketV1.from_payload(
        payload
    ).packet_id.startswith("b2dp-")
    assert "validation-only" in output_markdown.read_text()


def test_malformed_canonical_artifacts_are_rejected() -> None:
    report, receipt = _receipt_and_report()
    malformed = receipt.as_payload()
    malformed["unexpected"] = True
    with pytest.raises(Builder2QualificationDecisionPacketError):
        build_builder2_qualification_decision_packet(
            [malformed], qualification_reports=[report]
        )

    with pytest.raises(Builder2QualificationDecisionPacketError):
        Builder2QualificationIntakeArtifactV1.from_payload(
            {"receipt": {"schema_version": "unknown"}}
        )


def test_no_network_no_authority_issuance_or_production_mutation_path() -> None:
    source = inspect.getsource(packet_module)
    for forbidden in (
        "import requests",
        "import urllib",
        "import socket",
        "socket.socket",
    ):
        assert forbidden not in source
    assert "issue_builder2_qualification_receipt" not in source
    packet = build_builder2_qualification_decision_packet()
    assert packet.status == EVIDENCE_INCOMPLETE
    assert all(
        value is False
        for key, value in packet.safety.items()
        if key.endswith(("authorized", "bound"))
    )


def test_packet_output_does_not_claim_sample_sufficiency_is_activation() -> None:
    report, receipt = _receipt_and_report()
    packet = build_builder2_qualification_decision_packet(
        [receipt],
        qualification_reports=[report],
        minimum_sample_policy=MinimumSamplePolicy(1, 1),
    )
    text = json.dumps(packet.as_payload(), sort_keys=True).lower()
    assert packet.as_payload()["safety"]["production_activation_authorized"] is False
    assert packet.safety["controlled_activation_authorized"] is False
    assert "production authorized" not in text
