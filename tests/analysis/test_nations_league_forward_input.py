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
from src.analysis.nations_league_result_extension import completeness
from src.analysis.nations_league_v1_1 import (
    model_digest,
    training_records_from_timeline,
)

ROOT = Path(__file__).resolve().parents[2]
BASE = json.loads(
    (ROOT / "results/research/nations_league_fixture_timeline_v1.json").read_text()
)
EXTENSION = json.loads(
    (
        ROOT / "results/research/nations_league_v1_1_result_extension_20260930.json"
    ).read_text()
)
CUTOFF = EXTENSION["results_verified_through"]


def inputs():
    common = {
        "edition": "2026/27",
        "evaluation_block": "NL_2026_27",
        "competition": "UEFA Nations League",
        "home_team": "Austria",
        "away_team": "Belgium",
        "source_digest": BASE["dataset_digest"],
        "source_provenance": "deterministic synthetic test",
        "neutral": False,
    }
    fixture = dict(
        common, fixture_id="uefa-nl:future", kickoff_utc="2026-10-01T16:00:00Z"
    )
    history = training_records_from_timeline(BASE) + EXTENSION["result_rows"]
    proof = {
        "source_digest": BASE["dataset_digest"],
        "source_provenance": "synthetic completeness proof",
    }
    return [fixture], history, proof


def state(fixtures=None, history=None, proof=None):
    f, h, p = inputs()
    return build_input_state(
        f if fixtures is None else fixtures,
        h if history is None else history,
        prediction_cutoff=CUTOFF,
        provenance=p if proof is None else proof,
        base_timeline=BASE if proof is None else None,
        result_extension=EXTENSION if proof is None else None,
        completeness_artifact=(
            completeness(EXTENSION, CUTOFF) if proof is None else None
        ),
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


@pytest.mark.parametrize("safe", [CUTOFF, "2026-09-30T17:04:37.635373Z"])
def test_equal_or_future_result_rejected(safe):
    _, history, _ = inputs()
    history[0]["result_safe_available_at"] = safe
    with pytest.raises(ValueError, match="strictly before"):
        state(history=history)


def test_duplicate_result():
    _, history, _ = inputs()
    with pytest.raises(ValueError, match="duplicate"):
        state(history=history + deepcopy(history))


def test_unknown_alias_is_fail_closed():
    fixtures, _, _ = inputs()
    fixtures[0]["away_team"] = "Ireland Republic"
    assert (
        state(fixtures=fixtures)["team_readiness"]["Ireland Republic"] == "MISSING_TEAM"
    )


def test_existing_explicit_alias_is_ready():
    fixtures, _, _ = inputs()
    fixtures[0]["away_team"] = "Turkey"
    assert state(fixtures=fixtures)["team_readiness"]["Turkey"] == "READY"


def test_missing_team():
    fixtures, _, _ = inputs()
    fixtures[0]["away_team"] = "Atlantis"
    assert state(fixtures=fixtures)["team_readiness"]["Atlantis"] == "MISSING_TEAM"


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
                "results_verified_through": "2026-09-30T16:00:00Z",
            },
            "LIVE_RESULT_REFRESH_REQUIRED",
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
    snapshot = state(proof=proof)
    assert set(snapshot["team_readiness"].values()) == {"LIVE_RESULT_REFRESH_REQUIRED"}


def test_valid_stale_completeness_artifact_cannot_become_ready():
    stale_cutoff = "2026-10-01T20:00:00Z"
    fixtures, history, proof = inputs()
    fixtures[0]["kickoff_utc"] = "2026-10-02T20:00:00Z"
    snapshot = build_input_state(
        fixtures,
        history,
        prediction_cutoff=stale_cutoff,
        provenance=proof,
        base_timeline=BASE,
        result_extension=EXTENSION,
        completeness_artifact=completeness(EXTENSION, stale_cutoff),
    )
    assert set(snapshot["team_readiness"].values()) == {"STALE_INPUT"}


@pytest.mark.parametrize(
    "field",
    ["base_timeline", "result_extension", "completeness"],
)
def test_sealed_completeness_bindings_reject_tampering(field):
    snapshot = state()
    if field == "base_timeline":
        snapshot[field]["dataset_digest"] = "a" * 64
    elif field == "result_extension":
        snapshot[field]["extension_digest"] = "a" * 64
    else:
        snapshot[field]["completeness_digest"] = "a" * 64
    with pytest.raises(ValueError):
        predict_from_input_state(snapshot, "uefa-nl:future", phase="initial")


def test_forged_current_watermark_never_produces_ready():
    fixtures, history, proof = inputs()
    proof["results_verified_through"] = CUTOFF
    proof["observed_at"] = CUTOFF
    snapshot = build_input_state(
        fixtures,
        history,
        prediction_cutoff=CUTOFF,
        provenance=proof,
    )
    assert set(snapshot["team_readiness"].values()) == {"LIVE_RESULT_REFRESH_REQUIRED"}


def test_frozen_digest_mismatch(monkeypatch):
    monkeypatch.setattr(
        "src.analysis.nations_league_forward_input.model_digest", lambda: "f" * 64
    )
    with pytest.raises(ValueError, match="frozen model digest"):
        state()


def test_historical_v1_digest_is_not_accepted_as_active_input():
    snapshot = state()
    snapshot["model_digest"] = (
        "f55549e7225f55deac23c7a31b757acf509ad0b4810b93ba8244301d3395a8ee"
    )
    with pytest.raises(ValueError, match="mismatch"):
        predict_from_input_state(snapshot, "uefa-nl:future", phase="initial")


def test_equivalent_utc_input_same_digest():
    _, history, _ = inputs()
    history[0]["result_safe_available_at"] = history[0][
        "result_safe_available_at"
    ].replace("Z", "+00:00")
    assert state(history=history) == state()


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
