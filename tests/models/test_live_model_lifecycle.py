from __future__ import annotations

import json
from copy import deepcopy

import pytest

from src.models.live_model_lifecycle import (
    FileActiveModelPointer,
    ModelLifecycleError,
    model_release,
    retrain_receipt,
    training_snapshot,
    validate_active_pointer,
    validate_release,
    validate_technical,
)


def snapshot(*, watermark="2026-10-01T00:00:00Z"):
    return training_snapshot(
        model_family="test",
        algorithm_version="v1",
        algorithm_digest="a" * 64,
        training_digest="b" * 64,
        training_cutoff="2026-10-01T00:00:00Z",
        training_rows=2,
        result_watermark=watermark,
        feature_schema_digest="c" * 64,
        created_at="2026-10-01T00:01:00Z",
    )


def release(s=None, *, state="d" * 64):
    return model_release(
        s or snapshot(), state_digest=state, created_at="2026-10-01T00:02:00Z"
    )


def test_release_ids_and_retrain_are_deterministic_and_causal():
    first = snapshot()
    assert first == snapshot()
    trained = release(first)
    assert trained == release(first)
    receipt = retrain_receipt(
        first,
        trained,
        triggered_by="RESULT_WATERMARK_ADVANCED",
        created_at="2026-10-01T00:03:00Z",
    )
    assert receipt["release_id"] == trained["release_id"]
    with pytest.raises(ModelLifecycleError):
        snapshot(watermark="2026-10-01T00:01:00Z")


def test_validation_has_no_forward_sample_gate_and_tamper_fails():
    validated = validate_technical(release(), checked_at="2026-10-01T00:03:00Z")
    assert validated["sample_threshold_required"] is False
    forged = deepcopy(release())
    forged["state_digest"] = "e" * 64
    with pytest.raises(ModelLifecycleError):
        validate_release(forged)


def test_result_watermark_state_change_creates_a_fresh_release_and_direct_activation_fails(
    tmp_path,
):
    first = release()
    next_snapshot = training_snapshot(
        model_family="test",
        algorithm_version="v1",
        algorithm_digest="a" * 64,
        training_digest="f" * 64,
        training_cutoff="2026-10-02T00:00:00Z",
        training_rows=3,
        result_watermark="2026-10-02T00:00:00Z",
        feature_schema_digest="c" * 64,
        created_at="2026-10-02T00:01:00Z",
        parent_release_id=first["release_id"],
    )
    next_release = model_release(
        next_snapshot, state_digest="e" * 64, created_at="2026-10-02T00:02:00Z"
    )
    assert next_release["release_id"] != first["release_id"]
    with pytest.raises(ModelLifecycleError):
        FileActiveModelPointer(tmp_path / "active.json").activate(
            first, activated_at="2026-10-01T00:03:00Z", reason="bypass"
        )


def test_atomic_active_pointer_and_rollback_are_bindable(tmp_path):
    store = FileActiveModelPointer(tmp_path / "active.json")
    first, second = release(), release(state="e" * 64)
    first_validated = validate_technical(first, checked_at="2026-10-01T00:03:00Z")[
        "validated_release"
    ]
    second_validated = validate_technical(second, checked_at="2026-10-01T00:03:30Z")[
        "validated_release"
    ]
    first_pointer = store.activate(
        first_validated, activated_at="2026-10-01T00:03:00Z", reason="initial"
    )
    second_pointer = store.activate(
        second_validated,
        activated_at="2026-10-01T00:04:00Z",
        reason="result watermark advanced",
    )
    assert second_pointer["previous_release_id"] == first_pointer["active_release_id"]
    restored = store.activate(
        first_validated, activated_at="2026-10-01T00:05:00Z", reason="rollback"
    )
    assert restored["active_release_id"] == first_pointer["active_release_id"]
    assert (
        validate_active_pointer(json.loads((tmp_path / "active.json").read_text()))
        == restored
    )
