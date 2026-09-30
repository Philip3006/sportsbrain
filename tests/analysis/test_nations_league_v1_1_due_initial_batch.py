"""Regression checks for the remaining real v1.1 INITIAL batch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.materialize_nl_v1_1_due_initial_batch import (
    CAMPAIGN_ID,
    GERMANY_ID,
    MANIFEST_DIGEST,
    TARGET_IDS,
    campaign_from_payload,
    preflight,
    validate_batch_predictions,
)
from src.analysis.nations_league_competition_state import canonical_team
from src.analysis.nations_league_forward_campaign import build_forward_evidence_summary
from src.analysis.nations_league_result_extension import (
    build_extension,
    completeness,
    validate_append_only_successor,
)
from src.analysis.nations_league_v1_1 import (
    TRAINING_TIMELINE_DATASET_DIGEST,
    sha256_json,
    training_records_from_timeline,
)

ROOT = Path(__file__).resolve().parents[2]
SUFFIX = "20260930T193447Z"
CAPTURE = "2026-09-30T19:34:47.774518Z"
PREVIOUS_SUFFIX = "20260930T183441Z"
BLOCKED_ID = "uefa-nl:future-596f9e31eff328f0e49a77b5"


def load(relative: str):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _campaign(relative: str):
    return campaign_from_payload(load(relative))


def _batch_context():
    base = load("results/research/nations_league_fixture_timeline_v1.json")
    inventory = load(
        f"data/research/nations_league/post_base_official_results_{SUFFIX}.json"
    )
    extension = load(
        f"results/research/nations_league_v1_1_result_extension_{SUFFIX}.json"
    )
    proof = load(
        f"results/audits/nations_league_v1_1_result_completeness_{SUFFIX}.json"
    )
    manifest = load("results/audits/nations_league_forward_fixture_manifest.json")
    targets = {row["fixture_id"]: row for row in manifest["fixtures"]}
    training = training_records_from_timeline(base) + extension["result_rows"]
    return base, inventory, extension, proof, targets, training


def test_fresh_proof_and_extension_are_sealed_and_append_only():
    base, inventory, extension, proof, _, _ = _batch_context()
    previous_extension = load(
        f"results/research/nations_league_v1_1_result_extension_{PREVIOUS_SUFFIX}.json"
    )
    assert build_extension(base, inventory, generated_at=CAPTURE) == extension
    assert validate_append_only_successor(previous_extension, extension) is None
    assert completeness(extension, proof["prediction_cutoff"]) == proof
    assert (proof["status"], proof["verified_count"], proof["unresolved_count"]) == (
        "READY",
        56,
        0,
    )
    assert proof["base_timeline_digest"] == TRAINING_TIMELINE_DATASET_DIGEST


def test_identity_block_is_explicit_and_does_not_bypass_the_manifest():
    base, _, extension, proof, targets, training = _batch_context()
    target = targets[BLOCKED_ID]
    state, readiness, _, blocker = preflight(
        target, training, base, extension, proof, CAPTURE
    )
    assert canonical_team("Republic of Ireland") == "Ireland"
    assert state == "AMBIGUOUS_IDENTITY"
    assert readiness["Republic of Ireland"] == "AMBIGUOUS_IDENTITY"
    assert readiness["Austria"] == "READY"
    assert blocker == "AMBIGUOUS_IDENTITY: target input is not READY"
    assert target["fixture_id"] == BLOCKED_ID


def test_ready_batch_has_five_independent_targets_and_shared_causal_state():
    snapshot = load(
        f"results/research/nations_league_v1_1_input_state_batch_{SUFFIX}.json"
    )
    audit = load(f"results/audits/nations_league_v1_1_due_initial_batch_{SUFFIX}.json")
    records = [
        json.loads(line)
        for line in (
            ROOT
            / f"results/research/nations_league_v1_1_forward_shadow_store_batch_{SUFFIX}.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(snapshot["training_records"]) == 566
    assert len(snapshot["fixtures"]) == 5
    assert set(snapshot["team_readiness"].values()) == {"READY"}
    assert {row["fixture_id"] for row in records} == set(TARGET_IDS) - {BLOCKED_ID}
    assert all(
        row["prediction_timestamp"] == snapshot["prediction_cutoff"] for row in records
    )
    assert all(row["phase"] == "initial" for row in records)
    assert all(row["no_bet"] and row["shadow"] for row in records)
    assert audit["ready_count"] == 5
    assert audit["blocked_count"] == 1
    assert audit["fixture_manifest_digest"] == MANIFEST_DIGEST
    assert audit["base_timeline_digest"] == TRAINING_TIMELINE_DATASET_DIGEST
    assert audit["artifact_digest"] == sha256_json(
        {key: value for key, value in audit.items() if key != "artifact_digest"}
    )


def test_successor_preserves_germany_and_summary_counts():
    previous = _campaign(
        f"results/research/nations_league_v1_1_forward_campaign_{PREVIOUS_SUFFIX}.json"
    )
    successor = _campaign(
        f"results/research/nations_league_v1_1_forward_campaign_{SUFFIX}.json"
    )
    validate_append_only_successor(
        load(
            f"results/research/nations_league_v1_1_result_extension_{PREVIOUS_SUFFIX}.json"
        ),
        load(f"results/research/nations_league_v1_1_result_extension_{SUFFIX}.json"),
    )
    old_germany = next(
        row for row in previous.records if row["fixture_id"] == GERMANY_ID
    )
    new_germany = next(
        row for row in successor.records if row["fixture_id"] == GERMANY_ID
    )
    assert new_germany == old_germany
    assert successor.campaign.campaign_id == CAMPAIGN_ID
    assert len(successor.records) == 6
    summary = build_forward_evidence_summary(successor, as_of=CAPTURE)
    assert summary == load(
        f"results/audits/nations_league_v1_1_forward_campaign_summary_{SUFFIX}.json"
    )
    assert summary["completeness"] == {
        "eligible_fixtures": 104,
        "initial_eligible": 103,
        "initial_captured": 6,
        "initial_pending": 96,
        "initial_due": 1,
        "initial_missed": 0,
        "refinement_eligible": 104,
        "refinement_captured": 0,
        "refinement_pending": 104,
        "refinement_due": 0,
        "refinement_missed": 0,
        "settled": 0,
        "unsettled": 6,
        "administrative_exceptions": 0,
        "cancelled_exceptions": 0,
        "synthetic_predictions_excluded": 0,
    }


def test_invalid_ready_batch_is_rejected_before_any_append():
    snapshot = load(
        f"results/research/nations_league_v1_1_input_state_batch_{SUFFIX}.json"
    )
    predictions = [
        json.loads(line)
        for line in (
            ROOT
            / f"results/research/nations_league_v1_1_forward_shadow_store_batch_{SUFFIX}.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    targets = snapshot["fixtures"]
    existing = _campaign(
        f"results/research/nations_league_v1_1_forward_campaign_{PREVIOUS_SUFFIX}.json"
    )
    tampered = [dict(row) for row in predictions]
    tampered[0]["model_digest"] = "0" * 64
    with pytest.raises(ValueError, match="frozen v1.1 model"):
        validate_batch_predictions(tampered, targets, snapshot, existing, CAPTURE)
    assert len(existing.records) == 1


def test_prior_campaign_file_is_unchanged_by_successor_materialization():
    artifact = _campaign(
        f"results/research/nations_league_v1_1_forward_campaign_{PREVIOUS_SUFFIX}.json"
    )
    assert len(artifact.records) == 1
    assert artifact.records[0]["fixture_id"] == GERMANY_ID
