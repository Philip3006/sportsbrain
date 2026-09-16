"""Cross-contract hardening tests for the Builder-2 authority chain."""

from __future__ import annotations

from dataclasses import replace
from itertools import permutations

import pytest

from src.football.top5_builder2_qualification_receipt import (
    Builder2QualificationReceiptError,
    Builder2QualificationReceiptV1,
    issue_builder2_qualification_receipt,
    semantic_digest,
    validate_builder2_qualification_receipt,
    validate_builder4_qualification_receipt,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    MinimumSamplePolicy,
    ObservationEvidenceKind,
    ProviderQualificationStatus,
    QualificationContractError,
    qualify_provider_observations,
)
from src.football.top5_provider_cascade_validation import evidence_digest
from src.football.top5_qualification_sample_aggregator import (
    Builder2QualificationSampleAggregatorError,
    aggregate_builder2_qualification_samples,
)
from tests.football.test_top5_builder2_qualification_receipt import _accepted
from tests.football.test_top5_controlled_shadow_provider_qualification import (
    READY,
    TIMING,
    _authorization,
    _cascade_for_expected,
    _expected_fixture,
    _observation,
    _observation_for_cascade,
    _qualify,
    _session,
)


def _context_for_fixture(index: int | None = None):
    if index is None:
        report, observation, result = _accepted()
        return report, observation, result, _authorization()

    expected = _expected_fixture(index)
    cascade = _cascade_for_expected(expected, suffix=f"hardening-{index}")
    observation = _observation_for_cascade(
        cascade,
        cascade.attempts[0],
        expected=expected,
        observation_id=f"hardening-observation-{index}",
    )
    authorization = _authorization(fixture_scope=(expected.fixture_key,))
    report = qualify_provider_observations(
        (observation,),
        _session(fixture_scope=(expected.fixture_key,)),
        expected,
        TIMING,
        READY,
        authorization,
    )
    return report, observation, report.results[0], authorization


def _receipt_for_context(index: int | None = None):
    report, observation, result, authorization = _context_for_fixture(index)
    return (
        report,
        observation,
        result,
        authorization,
        issue_builder2_qualification_receipt(report, observation, result),
    )


def _rehash_receipt(receipt: Builder2QualificationReceiptV1, **changes: object):
    changed = replace(receipt, **changes)
    return replace(
        changed,
        receipt_digest=semantic_digest(changed._payload(include_receipt_digest=False)),
    )


def _receipt_variant(
    receipt: Builder2QualificationReceiptV1,
    *,
    result_seed: str,
    observation_id: str | None = None,
    observation_digest: str | None = None,
    provider_event_id: str | None = None,
    provider_request_id: str | None = None,
):
    payload = receipt.as_payload()
    payload["qualification_result_digest"] = result_seed * 64
    payload["qualification_receipt_id"] = f"b2qr-{(result_seed * 64)[:24]}"
    if observation_id is not None:
        payload["observation_id"] = observation_id
    if observation_digest is not None:
        payload["observation_digest"] = observation_digest
    if provider_event_id is not None:
        payload["provider_event_id"] = provider_event_id
    if provider_request_id is not None:
        payload["provider_request_id"] = provider_request_id
    payload.pop("receipt_digest", None)
    payload["receipt_digest"] = semantic_digest(payload)
    return Builder2QualificationReceiptV1.from_payload(payload)


def test_report_projects_to_exact_receipt_and_sample_chain() -> None:
    report, observation, result, authorization, receipt = _receipt_for_context()

    assert receipt.qualification_session_id == report.session.qualification_session_id
    assert receipt.qualification_report_identity == (
        f"{report.session.qualification_session_id}:"
        f"{ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED.value}"
    )
    assert receipt.fixture_key == observation.fixture_key
    assert receipt.provider_identity == observation.provider_identity
    assert receipt.provider_event_id == observation.provider_event_id
    assert receipt.provider_request_id == observation.provider_request_id
    assert receipt.observation_id == observation.observation_id
    assert receipt.observation_digest == semantic_digest(
        {
            key: value
            for key, value in observation.as_payload().items()
            if key != "capture_attestation"
        }
    )
    assert receipt.cascade_evidence_digest == evidence_digest(
        observation.cascade_evidence
    )
    assert receipt.ceo_authorization_id == authorization.authorization_id
    assert (
        validate_builder2_qualification_receipt(
            receipt,
            expected_observation=observation,
            expected_report=report,
            expected_result=result,
            expected_authorization=authorization,
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

    sample = aggregate_builder2_qualification_samples(
        [receipt], minimum_sample_policy=MinimumSamplePolicy(1, 1)
    )
    assert sample.sample_sufficient is True
    assert sample.production_activation_authorized is False
    assert sample.receipt_provenance[0].receipt_digest == receipt.receipt_digest


def test_receipt_and_sample_serialization_round_trip_is_exact() -> None:
    _, _, _, _, receipt = _receipt_for_context()
    restored_receipt = Builder2QualificationReceiptV1.from_payload(
        dict(reversed(tuple(receipt.as_payload().items())))
    )
    assert restored_receipt == receipt
    assert restored_receipt.as_payload() == receipt.as_payload()

    sample = aggregate_builder2_qualification_samples([receipt])
    restored_sample = type(sample).from_payload(sample.as_payload())
    assert restored_sample == sample
    assert restored_sample.as_payload() == sample.as_payload()


def test_report_receipt_mismatch_and_cross_observation_substitution_fail_closed() -> (
    None
):
    report_a, observation_a, result_a, authorization_a, receipt_a = (
        _receipt_for_context()
    )
    report_b, observation_b, result_b, authorization_b, receipt_b = (
        _receipt_for_context(7)
    )

    with pytest.raises(Builder2QualificationReceiptError):
        validate_builder2_qualification_receipt(receipt_a, expected_report=report_b)
    with pytest.raises(Builder2QualificationReceiptError):
        validate_builder2_qualification_receipt(
            receipt_a, expected_observation=observation_b
        )
    with pytest.raises(Builder2QualificationReceiptError):
        validate_builder2_qualification_receipt(
            receipt_a,
            expected_observation=observation_b,
            expected_report=report_b,
            expected_result=result_b,
            expected_authorization=authorization_b,
        )
    with pytest.raises(Builder2QualificationReceiptError):
        validate_builder2_qualification_receipt(
            receipt_b,
            expected_observation=observation_a,
            expected_report=report_a,
            expected_result=result_a,
            expected_authorization=authorization_a,
        )


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("provider_identity", "odds_api_io"),
        ("fixture_key", "EPL|Different|Fixture|2026-09-16T14:00:00+00:00"),
        ("provider_event_id", "different-event"),
        ("provider_request_id", "different-request"),
        ("controlled_shadow_run_id", "different-run"),
        ("qualification_session_id", "different-session"),
        ("ceo_authorization_id", "different-authorization"),
        ("capture_attestation_digest", "f" * 64),
        ("cascade_evidence_digest", "f" * 64),
        ("adapter_version", "different-adapter"),
        ("adapter_source_sha", "f" * 64),
    ],
)
def test_expected_binding_substitution_cannot_unlock_another_identity(
    field: str, replacement: str
) -> None:
    _, _, _, _, receipt = _receipt_for_context()
    with pytest.raises(Builder2QualificationReceiptError):
        validate_builder2_qualification_receipt(
            receipt, expected_bindings={field: replacement}
        )


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("provider_identity", "odds_api_io"),
        ("fixture_key", "EPL|Different|Fixture|2026-09-16T14:00:00+00:00"),
        ("provider_event_id", "different-event"),
        ("provider_request_id", "different-request"),
        ("controlled_shadow_run_id", "different-run"),
        ("ceo_authorization_id", "different-authorization"),
        ("capture_attestation_digest", "f" * 64),
        ("cascade_evidence_digest", "f" * 64),
        ("adapter_version", "different-adapter"),
        ("adapter_source_sha", "f" * 64),
    ],
)
def test_rehashed_tampering_still_fails_against_exact_observation_context(
    field: str, replacement: str
) -> None:
    report, observation, result, authorization, receipt = _receipt_for_context()
    tampered = _rehash_receipt(receipt, **{field: replacement})
    with pytest.raises(Builder2QualificationReceiptError):
        validate_builder2_qualification_receipt(
            tampered,
            expected_observation=observation,
            expected_report=report,
            expected_result=result,
            expected_authorization=authorization,
        )


def test_rehashed_session_tampering_fails_exact_report_binding() -> None:
    report, observation, result, authorization, receipt = _receipt_for_context()
    tampered = _rehash_receipt(
        receipt,
        qualification_session_id="different-session",
        qualification_report_identity="different-session:REAL_OBSERVATION_VALIDATED",
    )
    with pytest.raises(Builder2QualificationReceiptError):
        validate_builder2_qualification_receipt(
            tampered,
            expected_observation=observation,
            expected_report=report,
            expected_result=result,
            expected_authorization=authorization,
        )


@pytest.mark.parametrize(
    "kind",
    [
        ObservationEvidenceKind.TEST_FIXTURE,
        ObservationEvidenceKind.MOCK,
        ObservationEvidenceKind.OFFLINE_REPLAY,
    ],
)
def test_rejected_non_real_and_offline_evidence_cannot_issue_receipts(
    kind: ObservationEvidenceKind,
) -> None:
    report = _qualify((_observation(kind),), authorization=None)
    assert report.results[0].real_observed is False
    with pytest.raises(Builder2QualificationReceiptError):
        issue_builder2_qualification_receipt(
            report, _observation(kind), report.results[0]
        )

    rejected_observation = replace(_observation(), market_phase="IN_PLAY")
    rejected_report = _qualify((rejected_observation,))
    with pytest.raises(Builder2QualificationReceiptError):
        issue_builder2_qualification_receipt(
            rejected_report, rejected_observation, rejected_report.results[0]
        )


def test_sample_evidence_is_conservative_across_duplicates_conflicts_and_unattributed() -> (
    None
):
    _, _, _, _, receipt_a = _receipt_for_context()
    _, _, _, _, receipt_b = _receipt_for_context(1)
    exact_duplicate = aggregate_builder2_qualification_samples(
        [receipt_a, receipt_a], minimum_sample_policy=MinimumSamplePolicy(1, 1)
    )
    assert exact_duplicate.sample_sufficient is True
    assert exact_duplicate.eligible_distinct_observation_count == 1
    assert exact_duplicate.eligible_distinct_fixture_count == 1

    divergent = _rehash_receipt(receipt_a, provider_request_id="divergent-request")
    divergent_report = aggregate_builder2_qualification_samples(
        [receipt_a, divergent], minimum_sample_policy=MinimumSamplePolicy(1, 1)
    )
    assert divergent_report.sample_sufficient is False
    assert divergent_report.eligible_distinct_observation_count == 0
    assert divergent_report.eligible_distinct_fixture_count == 0

    identity_conflict = _receipt_variant(
        receipt_a,
        result_seed="a",
        observation_digest="b" * 64,
    )
    identity_report = aggregate_builder2_qualification_samples(
        [receipt_a, identity_conflict], minimum_sample_policy=MinimumSamplePolicy(1, 1)
    )
    assert identity_report.sample_sufficient is False
    assert identity_report.eligible_distinct_observation_count == 0
    assert identity_report.eligible_distinct_fixture_count == 0

    fixture_conflict = _receipt_variant(
        receipt_a,
        result_seed="c",
        provider_event_id="different-event",
    )
    fixture_report = aggregate_builder2_qualification_samples(
        [receipt_a, fixture_conflict], minimum_sample_policy=MinimumSamplePolicy(1, 1)
    )
    assert fixture_report.sample_sufficient is False
    assert fixture_report.eligible_distinct_observation_count == 0
    assert fixture_report.eligible_distinct_fixture_count == 0

    unattributed_payload = receipt_b.as_payload()
    unattributed_payload["fixture_key"] = "fixture-without-league"
    unattributed_payload.pop("receipt_digest")
    unattributed_payload["receipt_digest"] = semantic_digest(unattributed_payload)
    unattributed = Builder2QualificationReceiptV1.from_payload(unattributed_payload)
    mixed = aggregate_builder2_qualification_samples(
        [receipt_a, unattributed], minimum_sample_policy=MinimumSamplePolicy(2, 2)
    )
    assert mixed.sample_sufficient is False
    assert mixed.distinct_fixture_count == 2
    assert mixed.eligible_distinct_fixture_count == 1
    assert mixed.eligible_distinct_observation_count == 1


def test_policy_is_caller_supplied_and_sample_sufficiency_is_not_authority() -> None:
    _, _, _, _, receipt = _receipt_for_context()
    without_policy = aggregate_builder2_qualification_samples([receipt])
    assert without_policy.sample_sufficient is None
    assert without_policy.production_activation_authorized is False

    policy = MinimumSamplePolicy(1, 1)
    report = aggregate_builder2_qualification_samples(
        [receipt], minimum_sample_policy=policy
    )
    assert report.minimum_sample_policy == policy
    assert report.sample_sufficient is True
    assert report.no_bet is True
    assert report.publication is False
    assert report.production_activation_authorized is False


def test_mapping_and_input_ordering_are_deterministic() -> None:
    receipts = tuple(_receipt_for_context(index)[4] for index in (None, 1, 2))
    expected = aggregate_builder2_qualification_samples(
        receipts, minimum_sample_policy=MinimumSamplePolicy(3, 3)
    ).as_payload()
    for ordering in permutations(receipts):
        assert (
            aggregate_builder2_qualification_samples(
                ordering, minimum_sample_policy=MinimumSamplePolicy(3, 3)
            ).as_payload()
            == expected
        )


@pytest.mark.parametrize(
    "missing",
    [
        "schema_version",
        "qualification_receipt_id",
        "qualification_report_identity",
        "qualification_report_digest",
        "qualification_result_digest",
        "qualification_session_id",
        "controlled_shadow_run_id",
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
        "qualification_status",
        "accepted",
        "prediction_input_allowed",
        "no_bet",
        "publication",
        "production_activation",
        "monetary_spend_authorized",
        "receipt_digest",
    ],
)
def test_missing_receipt_fields_are_rejected(missing: str) -> None:
    _, _, _, _, receipt = _receipt_for_context()
    payload = receipt.as_payload()
    payload.pop(missing)
    with pytest.raises(Builder2QualificationReceiptError):
        Builder2QualificationReceiptV1.from_payload(payload).validate()


def test_invalid_enum_and_unknown_extra_state_are_fail_closed() -> None:
    _, _, _, _, receipt = _receipt_for_context()
    payload = receipt.as_payload()
    payload["qualification_status"] = "APPROVED_FOR_CONTROLLED_ACTIVATION"
    with pytest.raises(Builder2QualificationReceiptError):
        Builder2QualificationReceiptV1.from_payload(payload).validate()

    extra = receipt.as_payload()
    extra["production_authorization"] = "granted"
    restored = Builder2QualificationReceiptV1.from_payload(extra)
    assert restored == receipt
    assert restored.as_payload() == receipt.as_payload()

    report, _, _, _, _ = _receipt_for_context()
    with pytest.raises(QualificationContractError):
        replace(report, qualification_status="UNKNOWN_STATUS").validate()


def test_malformed_sample_report_payloads_are_rejected() -> None:
    _, _, _, _, receipt = _receipt_for_context()
    sample = aggregate_builder2_qualification_samples([receipt])
    malformed = sample.as_payload()
    malformed["eligible_distinct_fixture_count"] = "many"
    with pytest.raises(Builder2QualificationSampleAggregatorError):
        type(sample).from_payload(malformed).validate()

    missing_provenance = sample.as_payload()
    missing_provenance["receipt_provenance"] = []
    with pytest.raises(Builder2QualificationSampleAggregatorError):
        type(sample).from_payload(missing_provenance).validate()


def test_provider_report_status_and_all_merged_safety_invariants_are_explicit() -> None:
    report, observation, result, _, receipt = _receipt_for_context()
    assert report.production_activation_authorized is False
    assert report.signal_time_note == "NO PRODUCTION SIGNAL-TIME VALUES APPROVED"
    assert receipt.accepted is True
    assert receipt.prediction_input_allowed is True
    assert receipt.no_bet is True
    assert receipt.publication is False
    assert receipt.production_activation is False
    assert receipt.monetary_spend_authorized is False
    assert result.status is ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
    assert observation.capture_attestation is not None
