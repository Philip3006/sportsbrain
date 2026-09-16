"""No-network tests for the Builder-2 receipt-only sample aggregator."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.football.top5_builder2_qualification_receipt import (
    issue_builder2_qualification_receipt,
    semantic_digest,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    MinimumSamplePolicy,
    qualify_provider_observations,
)
from src.football.top5_qualification_sample_aggregator import (
    FAILURE_DIVERGENT_RECEIPT_ID_CONFLICT,
    FAILURE_DUPLICATE_OBSERVATION,
    FAILURE_DUPLICATE_RECEIPT,
    FAILURE_FIXTURE_PROVIDER_CONFLICT,
    FAILURE_OBSERVATION_DIGEST_CONFLICT,
    FAILURE_OBSERVATION_ID_CONFLICT,
    FAILURE_UNATTRIBUTED_LEAGUE,
    QUALIFICATION_SAMPLE_AGGREGATOR_SCHEMA_VERSION,
    Builder2QualificationSampleAggregatorError,
    aggregate_builder2_qualification_samples,
)
from tests.football.test_top5_builder2_qualification_receipt import _accepted
from tests.football.test_top5_controlled_shadow_provider_qualification import (
    EXPECTED,
    READY,
    TIMING,
    _authorization,
    _cascade_for_expected,
    _expected_fixture,
    _observation_for_cascade,
    _qualify,
    _session,
)


def _receipt_for_fixture(index: int):
    expected = _expected_fixture(index)
    cascade = _cascade_for_expected(expected, suffix=str(index))
    observation = _observation_for_cascade(
        cascade,
        cascade.attempts[0],
        expected=expected,
        observation_id=f"observation-fixture-{index}",
    )
    report = qualify_provider_observations(
        (observation,),
        _session(fixture_scope=(expected.fixture_key,)),
        expected,
        TIMING,
        READY,
        _authorization(fixture_scope=(expected.fixture_key,)),
    )
    return issue_builder2_qualification_receipt(report, observation, report.results[0])


def _receipt_for_same_observation_with_changed_result():
    report, observation, result = _accepted()
    changed_result = replace(result, source_age_seconds=1.0)
    changed_report = replace(report, results=(changed_result,))
    return issue_builder2_qualification_receipt(
        changed_report, observation, changed_result
    )


def _receipt_for_same_fixture_with_changed_provider_identity():
    cascade = _cascade_for_expected(EXPECTED, suffix="2")
    observation = _observation_for_cascade(
        cascade,
        cascade.attempts[0],
        observation_id="observation-2",
    )
    report = _qualify((observation,))
    return issue_builder2_qualification_receipt(report, observation, report.results[0])


def _receipt_variant(receipt, **changes):
    payload = receipt.as_payload()
    payload.update(changes)
    if "qualification_result_digest" in changes:
        payload["qualification_receipt_id"] = (
            f"b2qr-{payload['qualification_result_digest'][:24]}"
        )
    payload.pop("receipt_digest", None)
    payload["receipt_digest"] = semantic_digest(payload)
    return type(receipt).from_payload(payload)


def _rehashed_report(report, **changes):
    changed = replace(report, **changes)
    return replace(
        changed,
        report_digest=semantic_digest(changed._payload(include_report_digest=False)),
    )


def test_valid_receipts_aggregate_counts_identities_provenance_and_safety() -> None:
    first_report, first_observation, first_result = _accepted()
    first = issue_builder2_qualification_receipt(
        first_report, first_observation, first_result
    )
    report = aggregate_builder2_qualification_samples(
        [
            first,
            _receipt_for_fixture(1),
        ]
    )

    assert report.schema_version == QUALIFICATION_SAMPLE_AGGREGATOR_SCHEMA_VERSION
    assert report.input_receipt_count == 2
    assert report.total_valid_receipts == 2
    assert report.distinct_observation_count == 2
    assert report.distinct_fixture_count == 2
    assert report.eligible_receipt_count == 2
    assert report.eligible_distinct_observation_count == 2
    assert report.eligible_distinct_fixture_count == 2
    assert report.per_league_counts == {"EPL": 2}
    assert report.per_provider_counts == {"the_odds_api": 2}
    assert report.eligible_per_league_counts == {"EPL": 2}
    assert report.eligible_per_provider_counts == {"the_odds_api": 2}
    assert report.controlled_shadow_run_ids == ("controlled-run-1",)
    assert report.qualification_session_ids == ("qualification-session-1",)
    assert report.ceo_authorization_ids == ("ceo-auth-1",)
    assert len(report.receipt_provenance) == 2
    assert report.duplicate_receipt_ids == ()
    assert report.duplicate_observation_ids == ()
    assert report.fixture_provider_conflicts == ()
    assert report.failure_taxonomy == {}
    assert report.no_bet is True
    assert report.publication is False
    assert report.production_activation_authorized is False
    assert report.freshness.supported is False
    assert report.freshness.denominator is None
    assert report.observation_coverage.supported is False
    assert report.observation_coverage.rate is None


def test_accepts_only_canonical_receipts_and_rejects_invalid_or_report_inputs() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    with pytest.raises(Builder2QualificationSampleAggregatorError):
        aggregate_builder2_qualification_samples([report])

    with pytest.raises(Builder2QualificationSampleAggregatorError):
        aggregate_builder2_qualification_samples([replace(receipt, no_bet=False)])

    report = aggregate_builder2_qualification_samples([receipt.as_payload()])
    assert report.total_valid_receipts == 1
    assert report.eligible_receipt_count == 1


def test_policy_is_caller_supplied_only_and_never_authorizes_production() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    without_policy = aggregate_builder2_qualification_samples([receipt])
    assert without_policy.sample_sufficient is None

    policy = MinimumSamplePolicy(2, 1)
    sufficient = aggregate_builder2_qualification_samples(
        [receipt, receipt], minimum_sample_policy=policy
    )
    assert sufficient.sample_sufficient is False
    assert sufficient.minimum_sample_policy == policy
    assert sufficient.production_activation_authorized is False

    sufficient = aggregate_builder2_qualification_samples(
        [receipt, _receipt_for_fixture(1)],
        minimum_sample_policy=MinimumSamplePolicy(2, 2),
    )
    assert sufficient.sample_sufficient is True
    assert sufficient.eligible_distinct_observation_count == 2
    assert sufficient.eligible_distinct_fixture_count == 2
    assert sufficient.production_activation_authorized is False

    with pytest.raises(Builder2QualificationSampleAggregatorError):
        aggregate_builder2_qualification_samples(
            [receipt], minimum_sample_policy={"minimum_real_observations": 1}
        )


def test_exact_duplicate_receipts_are_detected_and_not_counted_twice() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    report = aggregate_builder2_qualification_samples([receipt, receipt])

    assert report.input_receipt_count == 2
    assert report.total_valid_receipts == 1
    assert report.distinct_observation_count == 1
    assert report.eligible_distinct_observation_count == 1
    assert report.eligible_distinct_fixture_count == 1
    assert report.duplicate_receipt_ids == (receipt.qualification_receipt_id,)
    assert report.failure_taxonomy == {FAILURE_DUPLICATE_RECEIPT: 1}


def test_duplicate_observation_is_detected_even_when_receipt_ids_differ() -> None:
    first_report, first_observation, first_result = _accepted()
    first = issue_builder2_qualification_receipt(
        first_report, first_observation, first_result
    )
    second = _receipt_for_same_observation_with_changed_result()
    assert first.qualification_receipt_id != second.qualification_receipt_id
    assert first.observation_id == second.observation_id
    assert first.observation_digest == second.observation_digest

    report = aggregate_builder2_qualification_samples([first, second])
    assert report.total_valid_receipts == 2
    assert report.distinct_observation_count == 1
    assert report.duplicate_observation_ids == (first.observation_id,)
    assert report.duplicate_observation_digests == (first.observation_digest,)
    assert report.eligible_receipt_count == 1
    assert report.eligible_distinct_observation_count == 1
    assert report.eligible_distinct_fixture_count == 1
    assert report.failure_taxonomy == {FAILURE_DUPLICATE_OBSERVATION: 2}


def test_divergent_receipt_id_variants_are_reported_and_never_eligible() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    divergent = _receipt_variant(
        receipt,
        observation_id="divergent-observation",
        observation_digest="d" * 64,
    )
    assert divergent.qualification_receipt_id == receipt.qualification_receipt_id
    assert divergent.receipt_digest != receipt.receipt_digest

    aggregated = aggregate_builder2_qualification_samples(
        [receipt, divergent], minimum_sample_policy=MinimumSamplePolicy(1, 1)
    )

    assert aggregated.total_valid_receipts == 2
    assert aggregated.distinct_observation_count == 2
    assert aggregated.distinct_fixture_count == 1
    assert aggregated.eligible_receipt_count == 0
    assert aggregated.eligible_distinct_observation_count == 0
    assert aggregated.eligible_distinct_fixture_count == 0
    assert aggregated.divergent_receipt_ids == (receipt.qualification_receipt_id,)
    assert aggregated.sample_sufficient is False
    assert aggregated.failure_taxonomy == {
        FAILURE_DUPLICATE_RECEIPT: 1,
        FAILURE_DIVERGENT_RECEIPT_ID_CONFLICT: 1,
    }


def test_observation_identity_conflicts_are_excluded_from_eligible_evidence() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
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

    aggregated = aggregate_builder2_qualification_samples(
        [receipt, same_id_new_digest, same_digest_new_id],
        minimum_sample_policy=MinimumSamplePolicy(1, 1),
    )

    assert aggregated.distinct_observation_count == 3
    assert aggregated.eligible_distinct_observation_count == 0
    assert aggregated.eligible_distinct_fixture_count == 0
    assert aggregated.observation_identity_conflict_ids == (receipt.observation_id,)
    assert aggregated.observation_identity_conflict_digests == (
        receipt.observation_digest,
    )
    assert aggregated.sample_sufficient is False
    assert aggregated.failure_taxonomy == {
        FAILURE_DUPLICATE_OBSERVATION: 2,
        FAILURE_OBSERVATION_DIGEST_CONFLICT: 1,
        FAILURE_OBSERVATION_ID_CONFLICT: 1,
    }


def test_fixture_provider_identity_conflict_is_reported() -> None:
    first_report, first_observation, first_result = _accepted()
    first = issue_builder2_qualification_receipt(
        first_report, first_observation, first_result
    )
    second = _receipt_for_same_fixture_with_changed_provider_identity()
    assert first.fixture_key == second.fixture_key
    assert first.provider_identity == second.provider_identity
    assert first.provider_event_id != second.provider_event_id

    report = aggregate_builder2_qualification_samples([first, second])
    assert len(report.fixture_provider_conflicts) == 1
    conflict = report.fixture_provider_conflicts[0]
    assert conflict.fixture_key == first.fixture_key
    assert conflict.provider_identity == first.provider_identity
    assert set(conflict.provider_event_ids) == {
        first.provider_event_id,
        second.provider_event_id,
    }
    assert report.distinct_fixture_count == 1
    assert report.eligible_distinct_observation_count == 0
    assert report.eligible_distinct_fixture_count == 0
    assert report.failure_taxonomy == {FAILURE_FIXTURE_PROVIDER_CONFLICT: 1}


def test_unattributed_fixture_has_no_invented_league_or_coverage_rate() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    payload = receipt.as_payload()
    unsigned = dict(payload)
    unsigned["fixture_key"] = "fixture-without-canonical-league"
    unsigned.pop("receipt_digest")
    unsigned["receipt_digest"] = semantic_digest(unsigned)
    changed = type(receipt).from_payload(unsigned)

    report = aggregate_builder2_qualification_samples(
        [changed], minimum_sample_policy=MinimumSamplePolicy(1, 1)
    )
    assert report.per_league_counts == {}
    assert report.distinct_fixture_count == 1
    assert report.eligible_distinct_fixture_count == 0
    assert report.eligible_distinct_observation_count == 0
    assert report.unattributed_fixture_keys == ("fixture-without-canonical-league",)
    assert report.failure_taxonomy == {FAILURE_UNATTRIBUTED_LEAGUE: 1}
    assert report.observation_coverage.supported is False
    assert report.observation_coverage.denominator is None
    assert report.sample_sufficient is False


def test_report_validation_rederives_counts_and_sufficiency_from_provenance() -> None:
    first_report, first_observation, first_result = _accepted()
    first = issue_builder2_qualification_receipt(
        first_report, first_observation, first_result
    )
    second = _receipt_for_fixture(1)
    report = aggregate_builder2_qualification_samples(
        [first, second], minimum_sample_policy=MinimumSamplePolicy(2, 2)
    )

    tampered_fields = {
        "input_receipt_count": report.input_receipt_count + 1,
        "total_valid_receipts": report.total_valid_receipts + 1,
        "distinct_observation_count": report.distinct_observation_count + 1,
        "distinct_fixture_count": report.distinct_fixture_count + 1,
        "eligible_receipt_count": report.eligible_receipt_count + 1,
        "eligible_distinct_observation_count": (
            report.eligible_distinct_observation_count + 1
        ),
        "eligible_distinct_fixture_count": report.eligible_distinct_fixture_count + 1,
        "per_league_counts": {"EPL": 3},
        "per_provider_counts": {"the_odds_api": 3},
        "eligible_per_league_counts": {"EPL": 3},
        "eligible_per_provider_counts": {"the_odds_api": 3},
    }
    for field_name, value in tampered_fields.items():
        with pytest.raises(Builder2QualificationSampleAggregatorError):
            _rehashed_report(report, **{field_name: value}).validate()

    insufficient = aggregate_builder2_qualification_samples(
        [first], minimum_sample_policy=MinimumSamplePolicy(2, 2)
    )
    with pytest.raises(Builder2QualificationSampleAggregatorError):
        _rehashed_report(insufficient, sample_sufficient=True).validate()

    tampered_occurrence = replace(
        report.receipt_provenance[0],
        input_occurrence_count=report.receipt_provenance[0].input_occurrence_count + 1,
    )
    with pytest.raises(Builder2QualificationSampleAggregatorError):
        _rehashed_report(
            report,
            receipt_provenance=(tampered_occurrence, *report.receipt_provenance[1:]),
        ).validate()


def test_aggregation_is_deterministic_across_input_order_and_round_trips() -> None:
    first_report, first_observation, first_result = _accepted()
    first = issue_builder2_qualification_receipt(
        first_report, first_observation, first_result
    )
    second = _receipt_for_fixture(1)
    left = aggregate_builder2_qualification_samples([first, second])
    right = aggregate_builder2_qualification_samples([second, first])

    assert left.as_payload() == right.as_payload()
    assert left.report_digest == right.report_digest

    restored = type(left).from_payload(left.as_payload())
    assert restored == left
    assert restored.as_payload() == left.as_payload()


def test_provenance_contains_bound_authority_and_evidence_digests_only() -> None:
    report, observation, result = _accepted()
    receipt = issue_builder2_qualification_receipt(report, observation, result)
    provenance = aggregate_builder2_qualification_samples([receipt]).receipt_provenance[
        0
    ]
    payload = provenance.as_payload()

    assert payload["qualification_receipt_id"] == receipt.qualification_receipt_id
    assert payload["controlled_shadow_run_id"] == receipt.controlled_shadow_run_id
    assert payload["ceo_authorization_id"] == receipt.ceo_authorization_id
    assert payload["provider_event_id"] == receipt.provider_event_id
    assert payload["provider_request_id"] == receipt.provider_request_id
    assert payload["observation_digest"] == receipt.observation_digest
    assert payload["cascade_evidence_digest"] == receipt.cascade_evidence_digest
    serialized = str(payload).lower()
    for forbidden in ("raw_response", "api_key", "secret", "credential"):
        assert forbidden not in serialized
