"""Replay checks for the immutable first real v1.1 INITIAL capture."""

import json
from pathlib import Path

from src.analysis.nations_league_forward_campaign import (
    CapturePolicy,
    ForwardCampaignArtifact,
    ForwardEvidenceCampaign,
    build_forward_evidence_summary,
    validate_forward_campaign,
)
from src.analysis.nations_league_forward_input import predict_from_input_state
from src.analysis.nations_league_result_extension import (
    build_extension,
    completeness,
    validate_append_only_successor,
)

ROOT = Path(__file__).resolve().parents[2]
SUFFIX = "20260930T183441Z"
TARGET_ID = "uefa-nl:future-3fe70ff0ba848e39b50908d6"


def load(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def campaign_artifact(payload):
    campaign = payload["campaign"]
    value = ForwardEvidenceCampaign(
        campaign_id=campaign["campaign_id"],
        competition=campaign["competition"],
        edition=campaign["edition"],
        model_version=campaign["model_version"],
        model_digest_value=campaign["model_digest"],
        campaign_start=campaign["campaign_start"],
        fixture_manifest=tuple(campaign["fixture_manifest"]),
        fixture_manifest_digest=campaign["fixture_manifest_digest"],
        initial_policy=CapturePolicy(**campaign["initial_policy"]),
        refinement_policy=CapturePolicy(**campaign["refinement_policy"]),
        promotion_criteria_digest=campaign["promotion_criteria_digest"],
        no_bet=campaign["no_bet"],
        signal_status=campaign["signal_status"],
        evaluation_contract_digest=campaign["evaluation_contract_digest"],
    )
    return ForwardCampaignArtifact(value, tuple(payload["records"]))


def test_fresh_successor_and_ready_proof_replay_exactly():
    base = load("results/research/nations_league_fixture_timeline_v1.json")
    source = load(
        f"data/research/nations_league/post_base_official_results_{SUFFIX}.json"
    )
    previous = load("results/research/nations_league_v1_1_result_extension_20260930.json")
    saved = load(f"results/research/nations_league_v1_1_result_extension_{SUFFIX}.json")
    rebuilt = build_extension(base, source, generated_at=source["observed_at"])
    assert rebuilt == saved
    validate_append_only_successor(previous, saved)

    proof = load(f"results/audits/nations_league_v1_1_result_completeness_{SUFFIX}.json")
    assert completeness(saved, proof["prediction_cutoff"]) == proof
    assert proof["status"] == "READY"
    assert (proof["expected_count"], proof["verified_count"], proof["unresolved_count"]) == (56, 56, 0)


def test_snapshot_and_prediction_bind_target_and_all_fresh_digests():
    snapshot = load(f"results/research/nations_league_v1_1_input_state_{SUFFIX}.json")
    audit = load(f"results/audits/nations_league_v1_1_real_observed_initial_{SUFFIX}.json")
    target = next(row for row in load("results/audits/nations_league_forward_fixture_manifest.json")["fixtures"] if row["fixture_id"] == TARGET_ID)
    state_target = snapshot["fixtures"][0]
    assert {field: state_target[field] for field in ("fixture_id", "competition", "edition", "stage", "group", "home_team", "away_team", "kickoff_utc", "source_digest", "evaluation_block")} == {field: target[field] for field in ("fixture_id", "competition", "edition", "stage", "group", "home_team", "away_team", "kickoff_utc", "source_digest", "evaluation_block")}
    assert snapshot["team_readiness"] == {"Germany": "READY", "Serbia": "READY"}
    prediction = predict_from_input_state(snapshot, TARGET_ID, phase="initial")
    assert prediction == audit["prediction"]
    assert prediction["record_id"] == audit["prediction_record_id"]
    assert prediction["eventual_result"] is None
    assert prediction["shadow"] is True
    assert prediction["no_bet"] is True
    assert prediction["publication_enabled"] is False
    assert prediction["ledger_mutation"] is False


def test_campaign_is_one_real_unsettled_initial_sample_and_shadow_only():
    payload = load(f"results/research/nations_league_v1_1_forward_campaign_{SUFFIX}.json")
    artifact = campaign_artifact(payload)
    validate_forward_campaign(artifact)
    summary = build_forward_evidence_summary(artifact)
    saved_summary = load(f"results/audits/nations_league_v1_1_forward_campaign_summary_{SUFFIX}.json")
    assert summary == saved_summary
    assert summary["completeness"]["initial_captured"] == 1
    assert summary["completeness"]["settled"] == 0
    assert summary["completeness"]["unsettled"] == 1
    assert summary["evidence_state"] == "FORWARD_EVIDENCE_ACCUMULATING"
    assert summary["safety"] == {
        "no_bet": True,
        "signal_status": "SHADOW_ONLY",
        "automatic_promotion": False,
        "provider_authority_changed": False,
        "publication": False,
        "ledger_mutation": False,
    }


def test_audit_digest_and_store_are_append_only_evidence():
    audit = load(f"results/audits/nations_league_v1_1_real_observed_initial_{SUFFIX}.json")
    from src.analysis.nations_league_v1_1 import sha256_json

    assert audit["artifact_digest"] == sha256_json({k: v for k, v in audit.items() if k != "artifact_digest"})
    store = (ROOT / f"results/research/nations_league_v1_1_forward_shadow_store_{SUFFIX}.jsonl").read_text().splitlines()
    assert len(store) == 1
    assert json.loads(store[0]) == audit["prediction"]
