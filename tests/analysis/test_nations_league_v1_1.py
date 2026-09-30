"""Offline contract tests for the corrected Nations League v1.1 successor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analysis.nations_league_v1 import model_digest as legacy_model_digest
from src.analysis.nations_league_v1 import validate_lifecycle_timestamp
from src.analysis.nations_league_v1_1 import (
    ADMINISTRATIVE_EXCEPTION_COUNT,
    COMPETITION,
    OLD_FROZEN_MODEL_DIGEST,
    TRAINING_RESULT_COUNT,
    append_shadow_record,
    append_shadow_settlement,
    build_forward_shadow_prediction,
    build_shadow_settlement,
    build_supersession_artifact,
    calculate_forward_metrics,
    deterministic_record_id,
    model_digest,
    sha256_json,
    training_records_from_timeline,
    validate_legacy_contract_unchanged,
    validate_point_in_time_training,
    validate_target_fixture,
    validate_training_timeline,
)

TIMELINE_PATH = Path("results/research/nations_league_fixture_timeline_v1.json")
SUPERCESSION_PATH = Path("results/audits/nations_league_v1_supersession_v1_1.json")


def _timeline() -> dict:
    return json.loads(TIMELINE_PATH.read_text())


def _fixture(**overrides):
    record = {
        "fixture_id": "uefa-nl:v11-test-001",
        "edition": "2024/25",
        "evaluation_block": "NL_2024_25",
        "home_team": "Austria",
        "away_team": "Belgium",
        "kickoff_utc": "2026-10-02T20:00:00Z",
        "competition": COMPETITION,
        "source_provenance": "canonical-timeline:test",
        "source_digest": "a" * 64,
        "result_safe_available_at": "2026-10-01T20:00:00Z",
        "home_score": 2,
        "away_score": 1,
    }
    record.update(overrides)
    return record


def test_legacy_digest_is_unchanged_and_successor_is_distinct():
    validate_legacy_contract_unchanged()
    assert legacy_model_digest() == OLD_FROZEN_MODEL_DIGEST
    assert model_digest() == model_digest()
    assert model_digest() != OLD_FROZEN_MODEL_DIGEST


def test_materialized_v11_model_spec_is_digest_bound():
    artifact = json.loads(
        Path("results/research/nations_league_v1_1_model_spec.json").read_text()
    )
    body = {key: value for key, value in artifact.items() if key != "artifact_digest"}
    assert artifact["artifact_digest"] == sha256_json(body)
    assert artifact["model_digest"] == model_digest()
    assert artifact["model"]["training_universe"]["result_safe_training_count"] == 510
    assert artifact["promotion_state"] == "CANDIDATE_ONLY"


def test_exact_retained_timeline_adapts_to_510_nl_training_rows():
    timeline = _timeline()
    validate_training_timeline(timeline)
    rows = training_records_from_timeline(timeline)
    assert len(rows) == TRAINING_RESULT_COUNT == 510
    assert all(row["competition"] == COMPETITION for row in rows)
    assert all("UEFA competitive" not in row.values() for row in rows)
    assert {row["fixture_id"] for row in rows} == {
        row["fixture_id"]
        for row in timeline["records"]
        if row.get("administrative_exception") is None
    }
    assert (
        sum(
            row.get("administrative_exception") is not None
            for row in timeline["records"]
        )
        == ADMINISTRATIVE_EXCEPTION_COUNT
    )


def test_timeline_rejects_digest_or_non_nl_mutation():
    timeline = _timeline()
    timeline["records"][0]["competition"] = "UEFA competitive"
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_training_timeline(timeline)


def test_training_cutoff_is_strict_and_target_validator_is_separate():
    with pytest.raises(ValueError, match="strictly before"):
        validate_point_in_time_training([_fixture()], "2026-10-01T20:00:00Z")
    validate_point_in_time_training([_fixture()], "2026-10-01T20:00:01Z")
    validate_target_fixture(_fixture())
    with pytest.raises(ValueError, match="not UEFA Nations League"):
        validate_target_fixture(_fixture(competition="UEFA Euro"))


def test_administrative_rows_are_audited_but_never_training_or_target_rows():
    exception = _fixture(
        result_safe_available_at=None,
        administrative_exception=True,
    )
    with pytest.raises(ValueError, match="administrative"):
        validate_target_fixture(exception)
    assert validate_point_in_time_training([exception], "2026-10-01T20:00:01Z") == []


def test_lifecycle_boundaries_are_preserved():
    validate_lifecycle_timestamp(
        "initial", "2026-10-01T18:00:00Z", "2026-10-02T20:00:00Z"
    )
    validate_lifecycle_timestamp(
        "refinement", "2026-10-02T18:00:00Z", "2026-10-02T20:00:00Z"
    )
    with pytest.raises(ValueError, match="initial"):
        validate_lifecycle_timestamp(
            "initial", "2026-10-01T17:59:59Z", "2026-10-02T20:00:00Z"
        )
    with pytest.raises(ValueError, match="refinement"):
        validate_lifecycle_timestamp(
            "refinement", "2026-10-02T17:59:59Z", "2026-10-02T20:00:00Z"
        )


def test_v11_prediction_settlement_metrics_and_append_only_safety():
    fixture = _fixture()
    prediction = build_forward_shadow_prediction(
        fixture,
        phase="initial",
        prediction_timestamp="2026-10-01T18:00:00Z",
        training_records=[
            _fixture(
                fixture_id="uefa-nl:training-001",
                result_safe_available_at="2026-09-30T20:00:00Z",
            )
        ],
        input_provenance={
            "timeline_digest": "b" * 64,
            "fixture_source_digest": "a" * 64,
        },
    )
    assert prediction["model_digest"] == model_digest()
    assert prediction["shadow"] and prediction["no_bet"]
    assert prediction["publication_enabled"] is False
    assert prediction["ledger_mutation"] is False
    assert prediction["record_id"] == deterministic_record_id(
        fixture["fixture_id"], "initial"
    )
    history = append_shadow_record([], prediction)
    settlement = build_shadow_settlement(
        prediction,
        home_score=2,
        away_score=1,
        result_safe_available_at="2026-10-02T22:00:00Z",
        settled_at="2026-10-02T23:00:00Z",
        result_provenance="canonical timeline result",
    )
    combined = append_shadow_settlement(history, settlement)
    metrics = calculate_forward_metrics(combined)
    assert metrics["sample_count"] == 1
    assert set(metrics["lifecycle_stage_breakdown"]) == {"initial"}
    with pytest.raises(ValueError, match="append-only"):
        append_shadow_record(history, prediction)
    assert prediction["record_id"] != sha256_json(
        {"fixture_id": fixture["fixture_id"], "phase": "initial"}
    )


def test_supersession_artifact_is_materialized_and_deterministic():
    expected = json.loads(SUPERCESSION_PATH.read_text())
    assert build_supersession_artifact() == expected
    assert expected["old_status"] == "SUPERSEDED_BEFORE_REAL_FORWARD_EVIDENCE"
    assert (
        expected["historical_evidence"]["old_v1_real_forward_prediction_generated"]
        is False
    )
