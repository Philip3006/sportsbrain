"""Contract tests for the research-only Nations League v1 freeze."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analysis.nations_league_v1 import (
    COMPETITION,
    INITIAL_WINDOW,
    MODEL_VERSION,
    append_shadow_record,
    deterministic_record_id,
    fit_causal_elo,
    model_digest,
    predict_1x2,
    sha256_json,
    validate_fixture_record,
    validate_lifecycle_timestamp,
    validate_point_in_time_training,
    validate_shadow_record,
    validate_target_cutoff,
)


def _fixture(**overrides):
    record = {
        "fixture_id": "uefa-nl:test-001",
        "edition": "2024/25",
        "evaluation_block": "NL_2024_25",
        "home_team": "Austria",
        "away_team": "Belgium",
        "kickoff_utc": "2026-09-02T20:00:00Z",
        "competition": COMPETITION,
        "source_provenance": "PR215:nations-league-fixture-timeline-v1",
        "source_digest": "a" * 64,
        "result_safe_available_at": "2026-08-01T20:00:00Z",
        "neutral": False,
        "home_score": 2,
        "away_score": 1,
    }
    record.update(overrides)
    return record


def _shadow_record(**overrides):
    record = {
        "record_id": deterministic_record_id("uefa-nl:test-001", "initial"),
        "fixture_id": "uefa-nl:test-001",
        "kickoff_utc": "2026-09-02T20:00:00Z",
        "phase": "initial",
        "prediction_timestamp": "2026-09-01T20:00:00Z",
        "model_version": MODEL_VERSION,
        "model_digest": model_digest(),
        "probabilities": {"home": 0.45, "draw": 0.25, "away": 0.30},
        "source_evidence": ["timeline-digest", "fixture-source-digest"],
        "eventual_result": None,
        "brier_score": None,
        "log_loss": None,
        "calibration_bucket": "0.4-0.5",
        "shadow": True,
        "signal_status": "SHADOW_ONLY",
        "no_bet": True,
        "publication_enabled": False,
        "ledger_mutation": False,
        "is_actionable_value_signal": False,
    }
    record.update(overrides)
    return record


def test_model_spec_digest_is_deterministic_and_matches_materialized_artifact():
    path = Path("results/research/nations_league_v1_model_spec_20260930.json")
    artifact = json.loads(path.read_text())
    body = {key: value for key, value in artifact.items() if key != "artifact_digest"}
    assert artifact["artifact_digest"] == sha256_json(body)
    assert artifact["model_digest"] == model_digest()
    assert artifact["model"]["family"] == "causal_elo"


def test_identical_causal_inputs_produce_identical_probabilities():
    training = [_fixture()]
    first = predict_1x2(
        fit_causal_elo(training, "2026-09-01T00:00:00Z"), "Austria", "Belgium"
    )
    second = predict_1x2(
        fit_causal_elo(training, "2026-09-01T00:00:00Z"), "Austria", "Belgium"
    )
    assert first == second
    assert sum(first.values()) == pytest.approx(1.0)


def test_point_in_time_cutoff_rejects_future_result():
    with pytest.raises(ValueError, match="strictly before"):
        validate_point_in_time_training(
            [_fixture(result_safe_available_at="2026-09-02T00:00:00Z")],
            "2026-09-01T00:00:00Z",
        )
    with pytest.raises(ValueError, match="strictly before"):
        validate_target_cutoff("2026-09-02T20:00:00Z", "2026-09-03T00:00:00Z")


def test_administrative_exception_is_explicit_and_excluded_from_training():
    exception = _fixture(
        result_safe_available_at=None,
        administrative_exception=True,
        administrative_exception_reason="missing_result_safe_available_at",
    )
    validate_fixture_record(exception)
    assert validate_point_in_time_training([exception], "2026-09-01T00:00:00Z") == []


def test_neutral_site_removes_home_advantage():
    neutral = predict_1x2({}, "Austria", "Belgium", neutral=True)
    home = predict_1x2({}, "Austria", "Belgium", neutral=False)
    assert home["home"] > neutral["home"]


def test_lifecycle_windows_and_legacy_window_gate():
    validate_lifecycle_timestamp(
        "initial", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"
    )
    validate_lifecycle_timestamp(
        "refinement", "2026-09-01T22:30:00Z", "2026-09-02T00:00:00Z"
    )
    validate_lifecycle_timestamp(
        "closing_benchmark", "2026-09-01T23:59:00Z", "2026-09-02T00:00:00Z"
    )
    assert INITIAL_WINDOW[0].total_seconds() == 22 * 3600
    with pytest.raises(ValueError, match="initial"):
        validate_lifecycle_timestamp(
            "initial", "2026-09-01T22:30:00Z", "2026-09-02T00:00:00Z"
        )
    with pytest.raises(ValueError, match="refinement"):
        validate_lifecycle_timestamp(
            "refinement", "2026-09-01T23:30:00Z", "2026-09-02T00:00:00Z"
        )


def test_forward_shadow_append_is_immutable_and_non_actionable():
    record = _shadow_record()
    validate_shadow_record(record)
    original = []
    appended = append_shadow_record(original, record)
    assert original == []
    assert appended[0]["record_id"] == deterministic_record_id(
        "uefa-nl:test-001", "initial"
    )
    with pytest.raises(ValueError, match="append-only"):
        append_shadow_record(appended, record)
    with pytest.raises(ValueError, match="excluded inputs"):
        validate_shadow_record(_shadow_record(context_features={"rest_days": 4}))


def test_forward_shadow_record_rejects_actionability_and_wrong_digest():
    with pytest.raises(ValueError, match="SHADOW_ONLY"):
        validate_shadow_record(_shadow_record(signal_status="ACTIVE"))
    with pytest.raises(ValueError, match="model digest"):
        validate_shadow_record(_shadow_record(model_digest="b" * 64))
