"""Deterministic offline input-state tests; no results refresh or provider access."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.analysis.nations_league_forward_input import (
    FROZEN_DIGEST,
    build_input_state,
    predict_from_input_state,
    timeline_training,
)
from src.analysis.nations_league_v1 import model_digest

CUTOFF = "2026-10-01T20:00:00+00:00"


def inputs():
    common = {
        "edition": "2026/27",
        "evaluation_block": "NL_2026_27",
        "competition": "UEFA Nations League",
        "home_team": "Austria",
        "away_team": "Belgium",
        "source_digest": "a" * 64,
        "source_provenance": "deterministic synthetic test",
        "neutral": False,
    }
    fixture = dict(
        common, fixture_id="uefa-nl:future", kickoff_utc="2026-10-02T20:00:00Z"
    )
    history = [
        dict(
            common,
            fixture_id="uefa-nl:past",
            kickoff_utc="2026-10-01T10:00:00Z",
            result_safe_available_at="2026-10-01T19:59:59Z",
            home_score=1,
            away_score=0,
        )
    ]
    proof = {
        "source_digest": "b" * 64,
        "source_provenance": "synthetic completeness proof",
        "results_verified_through": CUTOFF,
        "observed_at": CUTOFF,
    }
    return [fixture], history, proof


def state(fixtures=None, history=None, proof=None):
    f, h, p = inputs()
    return build_input_state(
        f if fixtures is None else fixtures,
        h if history is None else history,
        prediction_cutoff=CUTOFF,
        provenance=p if proof is None else proof,
    )


def test_prior_second_and_binding():
    snapshot = state()
    assert set(snapshot["team_readiness"].values()) == {"READY"}
    record = predict_from_input_state(snapshot, "uefa-nl:future", phase="initial")
    assert (
        record["input_provenance"]["input_snapshot_digest"]
        == snapshot["input_snapshot_digest"]
    )
    assert record["model_digest"] == model_digest() == FROZEN_DIGEST
    assert record["no_bet"] and not record["publication_enabled"]


@pytest.mark.parametrize("safe", ["2026-10-01T20:00:00Z", "2026-10-01T20:00:01Z"])
def test_equal_or_future_result_rejected(safe):
    _, history, _ = inputs()
    history[0]["result_safe_available_at"] = safe
    with pytest.raises(ValueError, match="strictly before"):
        state(history=history)


def test_duplicate_result():
    _, history, _ = inputs()
    with pytest.raises(ValueError, match="duplicate"):
        state(history=history + deepcopy(history))


def test_ambiguous_alias():
    fixtures, _, _ = inputs()
    fixtures[0]["away_team"] = "Turkey"
    assert state(fixtures=fixtures)["team_readiness"]["Turkey"] == "AMBIGUOUS_IDENTITY"


def test_missing_team():
    fixtures, _, _ = inputs()
    fixtures[0]["away_team"] = "France"
    assert state(fixtures=fixtures)["team_readiness"]["France"] == "MISSING_TEAM"


@pytest.mark.parametrize(
    "proof,status",
    [
        (
            {"source_digest": "b" * 64, "source_provenance": "test"},
            "LIVE_RESULT_REFRESH_REQUIRED",
        ),
        (
            {
                "source_digest": "b" * 64,
                "source_provenance": "test",
                "observed_at": CUTOFF,
                "results_verified_through": "2026-09-30T20:00:00Z",
            },
            "STALE_INPUT",
        ),
    ],
)
def test_freshness_fail_closed(proof, status):
    snapshot = state(proof=proof)
    assert set(snapshot["team_readiness"].values()) == {status}
    with pytest.raises(ValueError, match="not READY"):
        predict_from_input_state(snapshot, "uefa-nl:future", phase="initial")


def test_digest_reproducible_and_tamper_rejected():
    assert state() == state()
    snapshot = state()
    snapshot["elo_state"]["Austria"] += 1
    with pytest.raises(ValueError, match="mismatch"):
        predict_from_input_state(snapshot, "uefa-nl:future", phase="initial")


def test_non_future_target():
    fixtures, _, _ = inputs()
    fixtures[0]["kickoff_utc"] = CUTOFF
    with pytest.raises(ValueError, match="future"):
        state(fixtures=fixtures)


def test_noncausal_proof():
    _, _, proof = inputs()
    proof["observed_at"] = "2026-10-01T20:00:01Z"
    with pytest.raises(ValueError, match="noncausal"):
        state(proof=proof)


def test_repository_timeline():
    path = (
        Path(__file__).resolve().parents[2]
        / "results/research/nations_league_fixture_timeline_v1.json"
    )
    timeline = json.loads(path.read_text())
    rows = timeline_training(timeline)
    assert len(rows) == 510
    assert max(row["kickoff_utc"] for row in rows) == "2025-06-08T19:00:00Z"
    fixtures, _, _ = inputs()
    snapshot = build_input_state(
        fixtures,
        rows,
        prediction_cutoff=CUTOFF,
        provenance={
            "source_digest": timeline["dataset_digest"],
            "source_provenance": str(path.name),
        },
    )
    assert set(snapshot["team_readiness"].values()) == {"LIVE_RESULT_REFRESH_REQUIRED"}
    timeline["records"][0]["home_score"] += 1
    with pytest.raises(ValueError, match="digest"):
        timeline_training(timeline)
