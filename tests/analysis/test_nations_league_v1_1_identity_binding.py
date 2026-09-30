"""Deterministic source-to-canonical identity binding tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.analysis.nations_league_forward_input import (
    build_input_state,
    predict_from_input_state,
)
from src.analysis.nations_league_v1_1 import (
    MODEL_SPEC,
    model_digest,
    training_records_from_timeline,
)

ROOT = Path(__file__).resolve().parents[2]
SUFFIX = "20260930T193447Z"
TARGET_ID = "uefa-nl:future-596f9e31eff328f0e49a77b5"
MODEL_DIGEST = "50fc0f9120009b86eda1ebf016c14e93c9bd72c256b849577d5f55217d77f626"


def load(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def alias_context():
    base = load("results/research/nations_league_fixture_timeline_v1.json")
    extension = load(
        f"results/research/nations_league_v1_1_result_extension_{SUFFIX}.json"
    )
    proof = load(
        f"results/audits/nations_league_v1_1_result_completeness_{SUFFIX}.json"
    )
    manifest = load("results/audits/nations_league_forward_fixture_manifest.json")
    target = next(row for row in manifest["fixtures"] if row["fixture_id"] == TARGET_ID)
    training = training_records_from_timeline(base) + extension["result_rows"]
    provenance = {
        "source_digest": target["source_digest"],
        "source_provenance": "fresh official UEFA completeness observation",
    }
    state = build_input_state(
        [target],
        training,
        prediction_cutoff=proof["prediction_cutoff"],
        provenance=provenance,
        base_timeline=base,
        result_extension=extension,
        completeness_artifact=proof,
    )
    return base, extension, proof, manifest, target, training, state


def test_existing_alias_is_unique_and_sealed_in_the_model_state():
    _, _, _, manifest, target, training, state = alias_context()
    assert target["home_team"] == "Republic of Ireland"
    assert target["away_team"] == "Austria"
    assert target["fixture_id"] == TARGET_ID
    assert target["kickoff_utc"] == "2026-10-01T18:45:00Z"
    assert (
        manifest["manifest_digest"]
        == "5dd24bf5f820e456ec40f9df1ecf45a57f17f13a50e8e1341853c2f42b81091f"
    )
    assert len(training) == 566
    assert all(
        "Republic of Ireland" not in (row["home_team"], row["away_team"])
        for row in training
    )
    assert [team for team in state["elo_state"] if team == "Ireland"] == ["Ireland"]
    assert state["team_readiness"] == {
        "Republic of Ireland": "READY",
        "Austria": "READY",
    }
    assert state["identity_bindings"] == {
        "Republic of Ireland": {
            "source_team": "Republic of Ireland",
            "canonical_team": "Ireland",
            "resolution": "EXPLICIT_EXISTING_ALIAS",
            "normalization_version": "sportsbrain-nl-team-aliases-v1",
        },
        "Austria": {
            "source_team": "Austria",
            "canonical_team": "Austria",
            "resolution": "IDENTITY",
            "normalization_version": "sportsbrain-nl-team-aliases-v1",
        },
    }
    assert state["identity_binding_digest"]


def test_alias_prediction_preserves_source_identity_and_binds_model_identity():
    _, _, _, _, target, _, state = alias_context()
    record = predict_from_input_state(state, TARGET_ID, phase="initial")
    assert record["home_team"] == "Republic of Ireland"
    assert record["away_team"] == "Austria"
    assert record["model_identity"] == {
        "home_team": "Ireland",
        "away_team": "Austria",
        "normalization_version": "sportsbrain-nl-team-aliases-v1",
        "identity_binding_digest": state["identity_binding_digest"],
    }
    assert record["fixture_id"] == target["fixture_id"]
    assert record["model_digest"] == MODEL_DIGEST
    assert "sealed_identity_binding" in record["source_evidence"]


def test_tampered_identity_binding_invalidates_snapshot():
    *_, state = alias_context()
    tampered = deepcopy(state)
    tampered["identity_bindings"]["Republic of Ireland"]["canonical_team"] = "Austria"
    with pytest.raises(ValueError, match="input snapshot mismatch"):
        predict_from_input_state(tampered, TARGET_ID, phase="initial")


def test_missing_canonical_elo_is_not_ready():
    _, _, proof, _, target, training, _ = alias_context()
    without_ireland = [
        row for row in training if "Ireland" not in (row["home_team"], row["away_team"])
    ]
    state = build_input_state(
        [target],
        without_ireland,
        prediction_cutoff=proof["prediction_cutoff"],
        provenance={
            "source_digest": target["source_digest"],
            "source_provenance": "identity test",
        },
    )
    assert state["team_readiness"]["Republic of Ireland"] == "MISSING_TEAM"


def test_prior_germany_and_one_pr237_prediction_replay_unchanged():
    germany_snapshot = load(
        "results/research/nations_league_v1_1_input_state_20260930T183441Z.json"
    )
    germany_audit = load(
        "results/audits/nations_league_v1_1_real_observed_initial_20260930T183441Z.json"
    )
    assert (
        predict_from_input_state(
            germany_snapshot,
            "uefa-nl:future-3fe70ff0ba848e39b50908d6",
            phase="initial",
        )
        == germany_audit["prediction"]
    )

    batch_snapshot = load(
        f"results/research/nations_league_v1_1_input_state_batch_{SUFFIX}.json"
    )
    first = json.loads(
        (
            ROOT
            / f"results/research/nations_league_v1_1_forward_shadow_store_batch_{SUFFIX}.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert (
        predict_from_input_state(batch_snapshot, first["fixture_id"], phase="initial")
        == first
    )


def test_frozen_model_digest_remains_unchanged():
    assert model_digest() == MODEL_DIGEST
    assert MODEL_SPEC["model_version"] == "nations_league_v1_1"
