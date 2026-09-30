from __future__ import annotations

from dataclasses import replace

import pytest

from src.models.lifecycle import (
    ACTIVE,
    REJECTED,
    LifecycleError,
    ModelLifecycle,
    TrainingSnapshot,
    canonical_digest,
    validate_causal_training_rows,
)


def _rows() -> list[dict[str, str]]:
    return [
        {
            "fixture_id": "fixture-1",
            "result_safe_available_at": "2026-09-01T10:00:00Z",
        },
        {
            "fixture_id": "fixture-2",
            "result_safe_available_at": "2026-09-02T10:00:00Z",
        },
    ]


def _snapshot(
    *,
    captured_at: str = "2026-09-03T12:00:00Z",
    training_cutoff: str = "2026-09-03T00:00:00Z",
) -> TrainingSnapshot:
    rows = _rows()
    return TrainingSnapshot(
        model_family="test_family",
        sport="football",
        scope="test",
        algorithm_version="v1",
        algorithm_digest="a" * 64,
        training_data_digest=canonical_digest(rows),
        training_cutoff=training_cutoff,
        training_row_count=len(rows),
        result_safe_watermark="2026-09-02T10:00:00Z",
        feature_schema_digest=canonical_digest(["fixture_id"]),
        trained_state_digest=canonical_digest({"rating": 1500.0}),
        source_release_sha="source-sha",
        captured_at=captured_at,
    )


def test_result_availability_must_strictly_precede_training_cutoff():
    with pytest.raises(LifecycleError, match="strictly before"):
        validate_causal_training_rows(
            [
                {
                    "fixture_id": "same-time",
                    "result_safe_available_at": "2026-09-03T00:00:00Z",
                }
            ],
            "2026-09-03T00:00:00Z",
        )


def test_serialized_lifecycle_restores_with_one_active_release_and_history():
    lifecycle = ModelLifecycle()
    first, _ = lifecycle.create_or_noop(
        _snapshot(),
        parameter_digest=canonical_digest({"k": 1}),
        created_at="2026-09-03T12:00:00Z",
    )
    lifecycle.validate(
        first.release_id,
        training_rows=_rows(),
        trained_state={"rating": 1500.0},
        parameter_payload={"k": 1},
        prediction_smoke={"home": 0.4, "draw": 0.2, "away": 0.4},
    )
    lifecycle.activate(first.release_id, activated_at="2026-09-03T12:01:00Z")
    restored = ModelLifecycle.from_payload(lifecycle.to_payload())
    assert restored.to_payload() == lifecycle.to_payload()
    assert restored.pointers["test_family"].release_id == first.release_id
    assert sum(item.status == ACTIVE for item in restored.releases.values()) == 1


def test_release_identity_is_deterministic_and_repeat_is_noop_after_activation():
    lifecycle = ModelLifecycle()
    first, receipt = lifecycle.create_or_noop(
        _snapshot(), parameter_digest=canonical_digest({"k": 1}), created_at="2026-09-03T12:00:00Z"
    )
    assert receipt.outcome == "CREATED"
    assert lifecycle.validate(
        first.release_id,
        training_rows=_rows(),
        trained_state={"rating": 1500.0},
        parameter_payload={"k": 1},
        prediction_smoke={"home": 0.4, "draw": 0.2, "away": 0.4},
    ).status == "VALIDATED"
    lifecycle.activate(first.release_id, activated_at="2026-09-03T12:01:00Z")

    repeated, noop = lifecycle.create_or_noop(
        _snapshot(
            captured_at="2026-09-04T13:00:00Z",
            training_cutoff="2026-09-04T00:00:00Z",
        ),
        parameter_digest=canonical_digest({"k": 1}),
        created_at="2026-09-04T13:00:00Z",
    )
    assert repeated.release_id == first.release_id
    assert noop.outcome == "NO_OP"
    assert lifecycle.pointers["test_family"].release_id == first.release_id
    health = lifecycle.health("test_family", updated_at="2026-09-04T13:01:00Z")
    assert health.status == "ACTIVE"
    assert health.active_training_cutoff == "2026-09-03T00:00:00Z"
    assert health.training_row_count == 2
    assert health.last_successful_retrain == "2026-09-04T13:00:00Z"
    assert health.last_retrain_outcome == "NO_OP"
    assert health.last_failure_reason is None


def test_rejected_candidate_cannot_replace_active_pointer_and_rollback_is_deterministic():
    lifecycle = ModelLifecycle()
    first, _ = lifecycle.create_or_noop(
        _snapshot(), parameter_digest=canonical_digest({"k": 1}), created_at="2026-09-03T12:00:00Z"
    )
    lifecycle.validate(
        first.release_id,
        training_rows=_rows(),
        trained_state={"rating": 1500.0},
        parameter_payload={"k": 1},
    )
    lifecycle.activate(first.release_id, activated_at="2026-09-03T12:01:00Z")
    assert sum(
        release.status == ACTIVE for release in lifecycle.releases.values()
    ) == 1

    invalid_snapshot = replace(
        _snapshot(),
        training_data_digest=canonical_digest([{"fixture_id": "different", "result_safe_available_at": "2026-09-01T10:00:00Z"}]),
        trained_state_digest=canonical_digest({"rating": 1510.0}),
    )
    invalid, _ = lifecycle.create_or_noop(
        invalid_snapshot,
        parameter_digest=canonical_digest({"k": 2}),
        created_at="2026-09-04T12:00:00Z",
    )
    rejected = lifecycle.validate(
        invalid.release_id,
        training_rows=_rows(),
        trained_state={"rating": float("nan")},
        parameter_payload={"k": 2},
    )
    assert rejected.status == REJECTED
    assert lifecycle.pointers["test_family"].release_id == first.release_id
    with pytest.raises(LifecycleError, match="VALIDATED"):
        lifecycle.activate(invalid.release_id, activated_at="2026-09-04T12:01:00Z")

    changed_rows = [
        *_rows(),
        {"fixture_id": "fixture-3", "result_safe_available_at": "2026-09-03T10:00:00Z"},
    ]
    next_snapshot = replace(
        _snapshot(),
        training_data_digest=canonical_digest(changed_rows),
        training_row_count=3,
        result_safe_watermark="2026-09-03T10:00:00Z",
        trained_state_digest=canonical_digest({"rating": 1510.0}),
        training_cutoff="2026-09-04T00:00:00Z",
    )
    next_release, _ = lifecycle.create_or_noop(
        next_snapshot,
        parameter_digest=canonical_digest({"k": 2}),
        created_at="2026-09-04T12:00:00Z",
    )
    lifecycle.validate(
        next_release.release_id,
        training_rows=changed_rows,
        trained_state={"rating": 1510.0},
        parameter_payload={"k": 2},
    )
    lifecycle.activate(next_release.release_id, activated_at="2026-09-04T12:01:00Z")
    rolled_back = lifecycle.rollback("test_family", activated_at="2026-09-04T12:02:00Z")
    assert rolled_back.release_id == first.release_id
    assert lifecycle.pointers["test_family"].release_id == first.release_id
    assert lifecycle.releases[first.release_id].status == ACTIVE


def test_sequential_activation_and_two_rollbacks_keep_one_active_release():
    lifecycle = ModelLifecycle()
    first, _ = lifecycle.create_or_noop(
        _snapshot(), parameter_digest=canonical_digest({"k": 1}), created_at="2026-09-03T12:00:00Z"
    )
    lifecycle.validate(
        first.release_id,
        training_rows=_rows(),
        trained_state={"rating": 1500.0},
        parameter_payload={"k": 1},
    )
    lifecycle.activate(first.release_id, activated_at="2026-09-03T12:01:00Z")
    rows_2 = [
        *_rows(),
        {"fixture_id": "fixture-3", "result_safe_available_at": "2026-09-03T10:00:00Z"},
    ]
    second_snapshot = replace(
        _snapshot(),
        training_data_digest=canonical_digest(rows_2),
        training_row_count=3,
        result_safe_watermark="2026-09-03T10:00:00Z",
        trained_state_digest=canonical_digest({"rating": 1510.0}),
        training_cutoff="2026-09-04T00:00:00Z",
    )
    second, _ = lifecycle.create_or_noop(
        second_snapshot,
        parameter_digest=canonical_digest({"k": 2}),
        created_at="2026-09-04T12:00:00Z",
    )
    lifecycle.validate(
        second.release_id,
        training_rows=rows_2,
        trained_state={"rating": 1510.0},
        parameter_payload={"k": 2},
    )
    lifecycle.activate(second.release_id, activated_at="2026-09-04T12:01:00Z")
    assert lifecycle.releases[first.release_id].status == "VALIDATED"
    assert lifecycle.releases[second.release_id].status == ACTIVE
    rows_3 = [
        *rows_2,
        {"fixture_id": "fixture-4", "result_safe_available_at": "2026-09-04T10:00:00Z"},
    ]
    third_snapshot = replace(
        second_snapshot,
        training_data_digest=canonical_digest(rows_3),
        training_row_count=4,
        result_safe_watermark="2026-09-04T10:00:00Z",
        trained_state_digest=canonical_digest({"rating": 1520.0}),
        training_cutoff="2026-09-05T00:00:00Z",
    )
    third, _ = lifecycle.create_or_noop(
        third_snapshot,
        parameter_digest=canonical_digest({"k": 3}),
        created_at="2026-09-05T12:00:00Z",
    )
    lifecycle.validate(
        third.release_id,
        training_rows=rows_3,
        trained_state={"rating": 1520.0},
        parameter_payload={"k": 3},
    )
    lifecycle.activate(third.release_id, activated_at="2026-09-05T12:01:00Z")
    assert lifecycle.pointers["test_family"].release_id == third.release_id
    assert [
        release.status
        for release in lifecycle.releases.values()
        if release.snapshot.model_family == "test_family"
        and release.status == ACTIVE
    ] == [ACTIVE]
    assert lifecycle.releases[first.release_id].status == "VALIDATED"
    assert lifecycle.releases[second.release_id].status == "VALIDATED"

    rolled_to_second = lifecycle.rollback(
        "test_family", activated_at="2026-09-05T12:02:00Z"
    )
    assert rolled_to_second.release_id == second.release_id
    assert lifecycle.releases[third.release_id].status == "VALIDATED"
    assert lifecycle.releases[second.release_id].status == ACTIVE
    lifecycle.rollback("test_family", activated_at="2026-09-05T12:03:00Z")
    assert lifecycle.pointers["test_family"].release_id == third.release_id
    assert lifecycle.releases[third.release_id].status == ACTIVE
    assert lifecycle.releases[second.release_id].status == "VALIDATED"
    assert sum(
        release.status == ACTIVE
        for release in lifecycle.releases.values()
        if release.snapshot.model_family == "test_family"
    ) == 1


def test_rejected_retrain_is_visible_as_active_with_failure_and_recovery_clears_it():
    lifecycle = ModelLifecycle()
    first, _ = lifecycle.create_or_noop(
        _snapshot(), parameter_digest=canonical_digest({"k": 1}), created_at="2026-09-03T12:00:00Z"
    )
    lifecycle.validate(
        first.release_id,
        training_rows=_rows(),
        trained_state={"rating": 1500.0},
        parameter_payload={"k": 1},
    )
    lifecycle.activate(first.release_id, activated_at="2026-09-03T12:01:00Z")
    rejected_snapshot = replace(
        _snapshot(), trained_state_digest=canonical_digest({"rating": 1510.0})
    )
    rejected, _ = lifecycle.create_or_noop(
        rejected_snapshot,
        parameter_digest=canonical_digest({"k": 1}),
        created_at="2026-09-04T12:00:00Z",
    )
    result = lifecycle.validate(
        rejected.release_id,
        training_rows=_rows(),
        trained_state={"rating": 1500.0},
        parameter_payload={"k": 1},
        validated_at="2026-09-04T12:01:00Z",
    )
    assert result.status == REJECTED
    failed_health = lifecycle.health("test_family", updated_at="2026-09-04T12:02:00Z")
    assert failed_health.status == "ACTIVE_WITH_RETRAIN_FAILURE"
    assert failed_health.active_release_id == first.release_id
    assert failed_health.last_retrain_outcome == REJECTED
    assert failed_health.last_failure_reason == "trained state digest does not match snapshot"
    assert lifecycle.pointers["test_family"].release_id == first.release_id

    rows_2 = [
        *_rows(),
        {"fixture_id": "fixture-3", "result_safe_available_at": "2026-09-03T10:00:00Z"},
    ]
    recovery_snapshot = replace(
        _snapshot(),
        training_data_digest=canonical_digest(rows_2),
        training_row_count=3,
        result_safe_watermark="2026-09-03T10:00:00Z",
        trained_state_digest=canonical_digest({"rating": 1510.0}),
        training_cutoff="2026-09-04T00:00:00Z",
    )
    recovery, _ = lifecycle.create_or_noop(
        recovery_snapshot,
        parameter_digest=canonical_digest({"k": 2}),
        created_at="2026-09-04T13:00:00Z",
    )
    lifecycle.validate(
        recovery.release_id,
        training_rows=rows_2,
        trained_state={"rating": 1510.0},
        parameter_payload={"k": 2},
        validated_at="2026-09-04T13:01:00Z",
    )
    lifecycle.activate(recovery.release_id, activated_at="2026-09-04T13:02:00Z")
    recovered = lifecycle.health("test_family", updated_at="2026-09-04T13:03:00Z")
    assert recovered.status == "ACTIVE"
    assert recovered.active_release_id == recovery.release_id
    assert recovered.last_failure_reason is None


def test_serialization_rejects_corrupt_active_state_instead_of_repairing_it():
    lifecycle = ModelLifecycle()
    first, _ = lifecycle.create_or_noop(
        _snapshot(), parameter_digest=canonical_digest({"k": 1}), created_at="2026-09-03T12:00:00Z"
    )
    lifecycle.validate(
        first.release_id,
        training_rows=_rows(),
        trained_state={"rating": 1500.0},
        parameter_payload={"k": 1},
    )
    lifecycle.activate(first.release_id, activated_at="2026-09-03T12:01:00Z")
    second_snapshot = replace(
        _snapshot(),
        training_data_digest=canonical_digest(
            [* _rows(), {"fixture_id": "fixture-3", "result_safe_available_at": "2026-09-03T10:00:00Z"}]
        ),
        training_row_count=3,
        result_safe_watermark="2026-09-03T10:00:00Z",
        training_cutoff="2026-09-04T00:00:00Z",
        trained_state_digest=canonical_digest({"rating": 1510.0}),
    )
    second, _ = lifecycle.create_or_noop(
        second_snapshot,
        parameter_digest=canonical_digest({"k": 2}),
        created_at="2026-09-04T12:00:00Z",
    )
    lifecycle._releases[second.release_id] = replace(
        second, status=ACTIVE
    )
    with pytest.raises(LifecycleError, match="active release set|ACTIVE release"):
        lifecycle.to_payload()
