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
