"""No-network tests for the canonical Builder-2 qualification receipt."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.football.top5_builder2_qualification_receipt import (
    BUILDER2_QUALIFICATION_RECEIPT_CONTRACT_VERSION,
    Builder2QualificationReceiptError,
    Builder2QualificationReceiptV1,
    issue_builder2_qualification_receipt,
    semantic_digest,
    validate_builder1_qualification_receipt,
    validate_builder2_qualification_receipt,
    validate_builder4_qualification_receipt,
)
from src.football.top5_provider_cascade_validation import evidence_digest
from tests.football.test_top5_controlled_shadow_provider_qualification import (
    EXPECTED,
    _attestation,
    _authorization,
    _cascade_for_expected,
    _expected_fixture,
    _observation,
    _observation_for_cascade,
    _qualify,
)


def _accepted():
    observation = _observation()
    report = _qualify((observation,))
    return report, observation, report.results[0]


def _observation_b():
    expected = _expected_fixture(7)
    cascade = _cascade_for_expected(expected, suffix="b")
    return _observation_for_cascade(
        cascade,
        cascade.attempts[0],
        expected=expected,
        observation_id="observation-b",
    )


def test_valid_pr66_real_qualification_issues_canonical_receipt() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)

    assert receipt.schema_version == BUILDER2_QUALIFICATION_RECEIPT_CONTRACT_VERSION
    assert receipt.qualification_session_id == report.session.qualification_session_id
    assert receipt.controlled_shadow_run_id == "controlled-run-1"
    assert receipt.ceo_authorization_id == "ceo-auth-1"
    assert receipt.fixture_key == EXPECTED.fixture_key
    assert receipt.provider_identity == "the_odds_api"
    assert receipt.provider_event_id == "the_odds_api-event-1"
    assert receipt.provider_request_id == "request-1"
    assert receipt.normalized_record_digest == observation.normalized_record_digest
    assert receipt.cascade_evidence_digest == evidence_digest(
        observation.cascade_evidence
    )
    assert receipt.accepted is True
    assert receipt.prediction_input_allowed is True
    assert receipt.no_bet is True
    assert receipt.publication is False
    assert receipt.production_activation is False
    assert receipt.monetary_spend_authorized is False
    assert receipt.failure_codes == ()
    assert receipt.qualification_receipt_id.startswith("b2qr-")


def test_marker_fixture_rejected_and_fixture_cannot_issue_real_receipt() -> None:
    marker = replace(_observation(), capture_attestation=None)
    marker_report = _qualify((marker,))
    with pytest.raises(Builder2QualificationReceiptError):
        issue_builder2_qualification_receipt(
            marker_report, marker, marker_report.results[0]
        )

    fixture = replace(
        _observation("TEST_FIXTURE"),
        capture_attestation=_attestation(),
    )
    fixture_report = _qualify((fixture,), authorization=None)
    with pytest.raises(Builder2QualificationReceiptError):
        issue_builder2_qualification_receipt(
            fixture_report, fixture, fixture_report.results[0]
        )


def test_rejected_observation_and_result_report_mismatch_cannot_issue() -> None:
    rejected = replace(_observation(), market_phase="IN_PLAY")
    rejected_report = _qualify((rejected,))
    with pytest.raises(Builder2QualificationReceiptError):
        issue_builder2_qualification_receipt(
            rejected_report, rejected, rejected_report.results[0]
        )

    report, observation, result = _accepted()
    with pytest.raises(Builder2QualificationReceiptError):
        issue_builder2_qualification_receipt(
            report,
            observation,
            replace(result, provider_identity="different-provider"),
        )


def test_receipt_copied_from_observation_a_to_b_is_rejected() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    with pytest.raises(Builder2QualificationReceiptError):
        validate_builder1_qualification_receipt(
            receipt,
            expected_observation=_observation_b(),
        )


@pytest.mark.parametrize(
    "field",
    [
        "controlled_shadow_run_id",
        "qualification_session_id",
        "ceo_authorization_id",
        "provider_identity",
        "provider_event_id",
        "provider_request_id",
        "adapter_version",
        "adapter_source_sha",
        "observation_digest",
        "cascade_evidence_digest",
        "capture_attestation_digest",
    ],
)
def test_bound_field_tampering_invalidates_receipt(field: str) -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    value = "f" * 64 if field.endswith(("digest", "sha")) else "changed"
    tampered = replace(receipt, **{field: value})
    with pytest.raises(Builder2QualificationReceiptError):
        validate_builder2_qualification_receipt(tampered)


def test_changed_cascade_and_attestation_context_are_rejected() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    cascade = observation.cascade_evidence
    changed_cascade = replace(
        cascade,
        provenance=replace(cascade.provenance, artifact_id="different-artifact"),
    )
    with pytest.raises(Builder2QualificationReceiptError):
        validate_builder4_qualification_receipt(
            receipt,
            expected_cascade_evidence=changed_cascade,
        )
    changed_attestation = replace(_attestation(), raw_response_digest="f" * 64)
    with pytest.raises(Builder2QualificationReceiptError):
        validate_builder2_qualification_receipt(
            receipt,
            expected_capture_attestation=changed_attestation,
        )


def test_expected_run_session_and_authorization_bindings_are_checked() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    for expected in (
        replace(_authorization(), controlled_shadow_run_id="run-b"),
        replace(_authorization(), qualification_session_id="session-b"),
        replace(_authorization(), authorization_id="ceo-auth-b"),
    ):
        with pytest.raises(Builder2QualificationReceiptError):
            validate_builder2_qualification_receipt(
                receipt, expected_authorization=expected
            )


def test_unsafe_receipt_state_is_rejected() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    for field in ("accepted", "prediction_input_allowed", "no_bet"):
        tampered = replace(receipt, **{field: False})
        with pytest.raises(Builder2QualificationReceiptError):
            validate_builder2_qualification_receipt(tampered)

    tampered = replace(receipt, publication=True)
    with pytest.raises(Builder2QualificationReceiptError):
        validate_builder2_qualification_receipt(tampered)


def test_digests_are_deterministic_and_mapping_order_independent() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    payload = receipt.as_payload()
    reordered = dict(reversed(tuple(payload.items())))
    restored = Builder2QualificationReceiptV1.from_payload(reordered)
    assert restored.as_payload() == payload
    assert restored.receipt_digest == receipt.receipt_digest

    unsigned = dict(payload)
    unsigned.pop("receipt_digest")
    assert semantic_digest(unsigned) == receipt.receipt_digest
    for field in (
        "provider_event_id",
        "controlled_shadow_run_id",
        "cascade_evidence_digest",
        "capture_attestation_digest",
    ):
        changed = dict(unsigned)
        changed[field] = "f" * 64 if field.endswith("digest") else "changed"
        assert semantic_digest(changed) != receipt.receipt_digest


def test_receipt_contains_no_secrets_or_raw_provider_payload() -> None:
    report, observation, result = _accepted()
    payload = issue_builder2_qualification_receipt(
        report, observation, result
    ).as_payload()
    serialized = str(payload).lower()
    for forbidden in (
        "raw_response",
        "api_key",
        "authorization_header",
        "password",
        "secret",
        "credential",
    ):
        assert forbidden not in serialized
    assert "raw_response_digest" not in payload
    assert "cascade_evidence" not in payload


def test_builder_seams_are_validation_only_and_require_exact_context() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    assert (
        validate_builder1_qualification_receipt(
            receipt,
            expected_observation=observation,
            expected_report=report,
            expected_result=result,
            expected_authorization=_authorization(),
        )
        == receipt
    )
    assert (
        validate_builder4_qualification_receipt(
            receipt,
            expected_observation=observation,
            expected_cascade_evidence=observation.cascade_evidence,
        )
        == receipt
    )
