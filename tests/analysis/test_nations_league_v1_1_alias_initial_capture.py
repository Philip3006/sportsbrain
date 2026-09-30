"""Replay checks for the deterministic alias-bound INITIAL capture."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.materialize_nl_v1_1_due_initial_batch import campaign_from_payload
from src.analysis.nations_league_forward_campaign import (
    build_forward_evidence_summary,
    validate_forward_campaign,
)
from src.analysis.nations_league_v1_1 import model_digest, sha256_json

ROOT = Path(__file__).resolve().parents[2]
SUFFIX = "20260930T200124Z"
PREVIOUS_SUFFIX = "20260930T193447Z"
TARGET_ID = "uefa-nl:future-596f9e31eff328f0e49a77b5"
RECORD_ID = "102e452a5be15d4d2ab87fbd322dbc9c253fffa980048feecfec8ad5b6517a9a"


def load(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_fresh_completeness_and_identity_artifacts_are_exact():
    proof = load(
        f"results/audits/nations_league_v1_1_result_completeness_{SUFFIX}.json"
    )
    audit = load(
        f"results/audits/nations_league_v1_1_alias_initial_capture_{SUFFIX}.json"
    )
    assert (
        proof["status"],
        proof["expected_count"],
        proof["verified_count"],
        proof["unresolved_count"],
    ) == ("READY", 56, 56, 0)
    assert proof["prediction_cutoff"] == "2026-09-30T20:01:24.572945+00:00"
    assert proof["results_verified_through"] == "2026-09-30T20:01:24.572945Z"
    assert audit["source_home_team"] == "Republic of Ireland"
    assert audit["canonical_model_home_team"] == "Ireland"
    assert audit["normalization_version"] == "sportsbrain-nl-team-aliases-v1"
    assert audit["fixture_id"] == TARGET_ID
    manifest = load("results/audits/nations_league_forward_fixture_manifest.json")
    target = next(row for row in manifest["fixtures"] if row["fixture_id"] == TARGET_ID)
    assert audit["source_digest"] == target["source_digest"]
    assert audit["kickoff_utc"] == "2026-10-01T18:45:00Z"
    assert audit["edition"] == "2026/27"
    assert audit["group"] == "B3"
    assert audit["evaluation_block"] == "NL_2026_27"
    assert audit["canonical_elo_identity_count"] == {"Ireland": 1, "Austria": 1}
    assert audit["conflicting_identity_count"] == 0
    assert (
        audit["identity_binding_digest"]
        == "61fe12469156cd6d47de34bec1b3b6b97973144019be586695ba1f854dd77e02"
    )
    assert audit["record_id"] == RECORD_ID
    assert audit["model_digest"] == model_digest()
    assert audit["provider_calls"] == 0
    assert audit["artifact_digest"] == sha256_json(
        {key: value for key, value in audit.items() if key != "artifact_digest"}
    )


def test_one_source_facing_prediction_uses_canonical_model_identity():
    snapshot = load(f"results/research/nations_league_v1_1_input_state_{SUFFIX}.json")
    lines = (
        (
            ROOT
            / f"results/research/nations_league_v1_1_forward_shadow_store_alias_{SUFFIX}.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["record_id"] == RECORD_ID
    assert record["fixture_id"] == TARGET_ID
    assert record["home_team"] == "Republic of Ireland"
    assert record["away_team"] == "Austria"
    assert record["model_identity"]["home_team"] == "Ireland"
    assert record["model_identity"]["away_team"] == "Austria"
    assert (
        record["model_identity"]["identity_binding_digest"]
        == snapshot["identity_binding_digest"]
    )
    assert record["prediction_timestamp"] == "2026-09-30T20:01:24.572945+00:00"
    assert record["no_bet"] is True
    assert record["publication_enabled"] is False
    assert record["ledger_mutation"] is False


def test_campaign_successor_preserves_six_and_closes_initial_due_state():
    previous = campaign_from_payload(
        load(
            f"results/research/nations_league_v1_1_forward_campaign_{PREVIOUS_SUFFIX}.json"
        )
    )
    successor = campaign_from_payload(
        load(f"results/research/nations_league_v1_1_forward_campaign_{SUFFIX}.json")
    )
    validate_forward_campaign(successor)
    previous_by_id = {row["record_id"]: row for row in previous.records}
    successor_by_id = {row["record_id"]: row for row in successor.records}
    assert len(previous.records) == 6
    assert len(successor.records) == 7
    assert set(previous_by_id).issubset(successor_by_id)
    for record_id, record in previous_by_id.items():
        assert successor_by_id[record_id] == record
    assert len(successor_by_id) == 7
    assert all(
        row.get("evidence_class") == "REAL_OBSERVED" for row in successor.records
    )
    summary = build_forward_evidence_summary(
        successor, as_of="2026-09-30T20:01:24.572945Z"
    )
    assert summary == load(
        f"results/audits/nations_league_v1_1_forward_campaign_summary_{SUFFIX}.json"
    )
    assert summary["completeness"]["initial_captured"] == 7
    assert summary["completeness"]["initial_due"] == 0
    assert summary["completeness"]["initial_missed"] == 0
    assert summary["completeness"]["settled"] == 0
    assert summary["completeness"]["unsettled"] == 7
    assert summary["completeness"]["synthetic_predictions_excluded"] == 0
