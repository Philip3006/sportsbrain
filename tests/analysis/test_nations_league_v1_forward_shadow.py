"""Offline forward-shadow runner tests for frozen Nations League v1."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from src.analysis.nations_league_v1 import (
    COMPETITION,
    append_shadow_record,
    append_shadow_settlement,
    build_forward_shadow_prediction,
    build_shadow_settlement,
    calculate_forward_metrics,
    model_digest,
)

TIMELINE_DIGEST = "a" * 64
SOURCE_DIGEST = "b" * 64


def _fixture(**overrides):
    fixture = {
        "fixture_id": "uefa-nl:forward-001",
        "edition": "2024/25",
        "evaluation_block": "NL_2024_25",
        "home_team": "Austria",
        "away_team": "Belgium",
        "kickoff_utc": "2026-10-02T20:00:00Z",
        "competition": COMPETITION,
        "source_provenance": "PR215:nations-league-fixture-timeline-v1",
        "source_digest": SOURCE_DIGEST,
        "neutral": False,
    }
    fixture.update(overrides)
    return fixture


def _training():
    return [
        {
            "fixture_id": "uefa-nl:training-001",
            "edition": "2022/23",
            "evaluation_block": "NL_2022_23",
            "home_team": "Austria",
            "away_team": "Belgium",
            "kickoff_utc": "2026-08-01T20:00:00Z",
            "competition": COMPETITION,
            "source_provenance": "frozen-training",
            "source_digest": "c" * 64,
            "result_safe_available_at": "2026-08-01T23:00:00Z",
            "home_score": 2,
            "away_score": 1,
            "neutral": False,
        }
    ]


def _provenance(**overrides):
    value = {
        "timeline_digest": TIMELINE_DIGEST,
        "fixture_source_digest": SOURCE_DIGEST,
        "training_cutoff": "2026-10-01T20:00:00Z",
    }
    value.update(overrides)
    return value


def _prediction(
    phase="initial",
    timestamp="2026-10-01T20:00:00Z",
    input_provenance=None,
    **kwargs,
):
    return build_forward_shadow_prediction(
        _fixture(**kwargs),
        phase=phase,
        prediction_timestamp=timestamp,
        training_records=_training(),
        input_provenance=input_provenance or _provenance(),
    )


def test_prediction_binds_frozen_model_fixture_and_input_provenance():
    record = _prediction()
    assert record["model_version"] == "nations_league_v1"
    assert record["model_digest"] == model_digest()
    assert record["record_id"]
    assert record["input_provenance_digest"]
    assert record["shadow"] is True
    assert record["no_bet"] is True
    assert record["publication_enabled"] is False
    assert record["ledger_mutation"] is False
    assert record["is_actionable_value_signal"] is False
    assert sum(record["probabilities"].values()) == pytest.approx(1.0)


def test_initial_and_refinement_are_separate_and_deterministic():
    initial = _prediction()
    refinement = _prediction(phase="refinement", timestamp="2026-10-02T18:30:00Z")
    assert initial["phase"] == "initial"
    assert refinement["phase"] == "refinement"
    assert initial["record_id"] != refinement["record_id"]
    assert initial["probabilities"] == _prediction()["probabilities"]


@pytest.mark.parametrize(
    ("phase", "timestamp"),
    [
        ("initial", "2026-10-01T17:59:59Z"),
        ("initial", "2026-10-02T00:00:01Z"),
        ("refinement", "2026-10-02T17:59:59Z"),
        ("refinement", "2026-10-02T19:01:00Z"),
    ],
)
def test_invalid_lifecycle_window_is_rejected(phase, timestamp):
    with pytest.raises(ValueError, match="window"):
        _prediction(phase=phase, timestamp=timestamp)


def test_post_kickoff_and_missing_identity_or_market_provenance_are_rejected():
    with pytest.raises(ValueError, match="before target kickoff"):
        _prediction(timestamp="2026-10-02T20:00:00Z")
    with pytest.raises(ValueError, match="non-empty string"):
        _prediction(fixture_id=None)
    with pytest.raises(ValueError, match="forbidden field"):
        _prediction(input_provenance={**_provenance(), "market_odds": {}})


def test_duplicate_prediction_is_rejected_without_replacing_original():
    first = _prediction()
    original = deepcopy(first)
    records = append_shadow_record([], first)
    conflicting = deepcopy(first)
    conflicting["probabilities"] = {"home": 0.2, "draw": 0.3, "away": 0.5}
    with pytest.raises(ValueError, match="append-only"):
        append_shadow_record(records, conflicting)
    assert records == [original]


def test_settlement_is_append_only_and_requires_result_safe_time():
    prediction = _prediction()
    with pytest.raises(ValueError, match="after result-safe"):
        build_shadow_settlement(
            prediction,
            home_score=1,
            away_score=0,
            result_safe_available_at="2026-10-02T21:00:00Z",
            settled_at="2026-10-02T20:30:00Z",
            result_provenance="canonical-result-source",
        )
    settlement = build_shadow_settlement(
        prediction,
        home_score=1,
        away_score=0,
        result_safe_available_at="2026-10-02T23:00:00Z",
        settled_at="2026-10-03T00:00:00Z",
        result_provenance="canonical-result-source",
    )
    records = append_shadow_record([], prediction)
    settled = append_shadow_settlement(records, settlement)
    assert settled[0] == prediction
    assert settled[1]["eventual_result"]["outcome"] == "home"
    with pytest.raises(ValueError, match="append-only"):
        append_shadow_settlement(settled, settlement)


def test_metrics_are_finite_and_stage_separated():
    initial = _prediction()
    refinement = _prediction(phase="refinement", timestamp="2026-10-02T18:30:00Z")
    records = [initial, refinement]
    for prediction, scores in zip((initial, refinement), ((1, 0), (1, 1))):
        records = append_shadow_settlement(
            records,
            build_shadow_settlement(
                prediction,
                home_score=scores[0],
                away_score=scores[1],
                result_safe_available_at="2026-10-02T23:00:00Z",
                settled_at="2026-10-03T00:00:00Z",
                result_provenance="canonical-result-source",
            ),
        )
    metrics = calculate_forward_metrics(records)
    assert metrics["sample_count"] == 2
    assert set(metrics["lifecycle_stage_breakdown"]) == {"initial", "refinement"}
    assert metrics["overall"]["brier_score"] >= 0
    assert metrics["overall"]["log_loss"] >= 0
    assert metrics["overall"]["calibration"]["ece"] >= 0
    assert metrics["lifecycle_stage_breakdown"]["initial"]["sample_count"] == 1
    assert metrics["lifecycle_stage_breakdown"]["refinement"]["sample_count"] == 1


def test_settlement_digest_mismatch_is_rejected_by_metrics():
    prediction = _prediction()
    settlement = build_shadow_settlement(
        prediction,
        home_score=1,
        away_score=0,
        result_safe_available_at="2026-10-02T23:00:00Z",
        settled_at="2026-10-03T00:00:00Z",
        result_provenance="canonical-result-source",
    )
    settlement["model_digest"] = "d" * 64
    with pytest.raises(ValueError, match="model-digest mismatch"):
        calculate_forward_metrics([prediction, settlement])


def test_manual_cli_predict_settle_and_metrics_are_offline(tmp_path: Path):
    fixture_path = tmp_path / "fixture.json"
    training_path = tmp_path / "training.json"
    provenance_path = tmp_path / "provenance.json"
    store_path = tmp_path / "forward-shadow.jsonl"
    fixture_path.write_text(json.dumps(_fixture()), encoding="utf-8")
    training_path.write_text(json.dumps(_training()), encoding="utf-8")
    provenance_path.write_text(json.dumps(_provenance()), encoding="utf-8")
    script = Path(__file__).parents[2] / "scripts/nations_league_v1_forward_shadow.py"

    predict = subprocess.run(
        [
            sys.executable,
            str(script),
            "predict",
            "--fixture",
            str(fixture_path),
            "--training",
            str(training_path),
            "--input-provenance",
            str(provenance_path),
            "--phase",
            "initial",
            "--prediction-timestamp",
            "2026-10-01T20:00:00Z",
            "--store",
            str(store_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    record_id = json.loads(predict.stdout)["record_id"]
    subprocess.run(
        [
            sys.executable,
            str(script),
            "settle",
            "--store",
            str(store_path),
            "--record-id",
            record_id,
            "--home-score",
            "1",
            "--away-score",
            "0",
            "--result-safe-available-at",
            "2026-10-02T23:00:00Z",
            "--settled-at",
            "2026-10-03T00:00:00Z",
            "--result-provenance",
            "canonical-result-source",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    metrics = subprocess.run(
        [sys.executable, str(script), "metrics", "--store", str(store_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(metrics.stdout)["sample_count"] == 1
    assert len(store_path.read_text(encoding="utf-8").splitlines()) == 2
