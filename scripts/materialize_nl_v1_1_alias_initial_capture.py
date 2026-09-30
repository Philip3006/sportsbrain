"""Materialize the one newly-ready Republic of Ireland INITIAL capture offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.materialize_nl_v1_1_due_initial_batch import (
    BASE,
    CAMPAIGN_ID,
    MANIFEST,
    MODEL_DIGEST,
    MODEL_VERSION,
    PREVIOUS_EXTENSION,
    campaign_from_payload,
    fresh_inventory,
    parse_utc,
    preflight,
    stamp,
    write,
)
from src.analysis.nations_league_forward_campaign import (
    EVIDENCE_REAL,
    append_forward_prediction,
    build_forward_evidence_summary,
)
from src.analysis.nations_league_forward_input import predict_from_input_state
from src.analysis.nations_league_result_extension import (
    build_extension,
    completeness,
    validate_append_only_successor,
)
from src.analysis.nations_league_v1_1 import (
    TRAINING_TIMELINE_DATASET_DIGEST,
    sha256_json,
    training_records_from_timeline,
    validate_shadow_record,
)

TARGET_ID = "uefa-nl:future-596f9e31eff328f0e49a77b5"
PREVIOUS_SUFFIX = "20260930T193447Z"
PREVIOUS_CAMPAIGN = (
    ROOT
    / f"results/research/nations_league_v1_1_forward_campaign_{PREVIOUS_SUFFIX}.json"
)
PREVIOUS_INVENTORY = (
    ROOT
    / f"data/research/nations_league/post_base_official_results_{PREVIOUS_SUFFIX}.json"
)
PREVIOUS_CAMPAIGN_RECORD_ID = (
    "2fab2e3afb12d51f554f883d786720e3c4306ef14ff66b329f26c608c19f676e"
)


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-at", required=True)
    parser.add_argument("--output-suffix", required=True)
    args = parser.parse_args()
    capture_dt = parse_utc(args.capture_at)
    capture = stamp(capture_dt)
    if (
        not parse_utc("2026-09-30T16:45:00Z")
        <= capture_dt
        <= parse_utc("2026-09-30T20:45:00Z")
    ):
        raise ValueError("capture is outside the remaining INITIAL window")

    base = read(BASE)
    manifest = read(MANIFEST)
    previous_inventory = read(PREVIOUS_INVENTORY)
    previous_extension = read(PREVIOUS_EXTENSION)
    target = next(row for row in manifest["fixtures"] if row["fixture_id"] == TARGET_ID)
    inventory = fresh_inventory(previous_inventory, capture)
    extension = build_extension(base, inventory, generated_at=capture)
    validate_append_only_successor(previous_extension, extension)
    proof = completeness(extension, capture)
    if (
        proof["status"],
        proof["expected_count"],
        proof["verified_count"],
        proof["unresolved_count"],
    ) != ("READY", 56, 56, 0):
        raise ValueError("fresh completeness is not READY 56/56/0")

    training = training_records_from_timeline(base) + extension["result_rows"]
    if len(training) != 566:
        raise ValueError("unexpected causal training row count")
    state, readiness, snapshot, blocker = preflight(
        target,
        training,
        base,
        extension,
        proof,
        capture,
    )
    if state != "READY" or readiness != {
        "Republic of Ireland": "READY",
        "Austria": "READY",
    }:
        raise ValueError(f"target input is not READY: {state}, {readiness}, {blocker}")
    if snapshot is None:
        raise ValueError("READY target did not produce an input snapshot")
    if snapshot["fixtures"] != [target]:
        raise ValueError("input snapshot changed the source fixture")
    if (
        snapshot["identity_bindings"]["Republic of Ireland"]["canonical_team"]
        != "Ireland"
    ):
        raise ValueError("existing Republic of Ireland alias did not bind to Ireland")

    campaign = campaign_from_payload(read(PREVIOUS_CAMPAIGN))
    if campaign.campaign.campaign_id != CAMPAIGN_ID or len(campaign.records) != 6:
        raise ValueError("unexpected #237 successor campaign")
    prior_germany = next(
        row
        for row in campaign.records
        if row["fixture_id"] == "uefa-nl:future-3fe70ff0ba848e39b50908d6"
    )
    if prior_germany["record_id"] != PREVIOUS_CAMPAIGN_RECORD_ID:
        raise ValueError("Germany evidence identity changed")
    if any(row["fixture_id"] == TARGET_ID for row in campaign.records):
        raise ValueError("duplicate Ireland campaign record")

    prediction = predict_from_input_state(snapshot, TARGET_ID, phase="initial")
    validate_shadow_record(prediction)
    if prediction["fixture_id"] != TARGET_ID:
        raise ValueError("prediction fixture binding changed")
    if (
        prediction["home_team"] != "Republic of Ireland"
        or prediction["away_team"] != "Austria"
    ):
        raise ValueError("prediction source identity changed")
    if prediction["model_identity"]["home_team"] != "Ireland":
        raise ValueError("prediction model identity changed")
    if (
        prediction["model_digest"] != MODEL_DIGEST
        or prediction["model_version"] != MODEL_VERSION
    ):
        raise ValueError("prediction model binding changed")
    if parse_utc(prediction["prediction_timestamp"]) != capture_dt:
        raise ValueError("prediction timestamp differs from capture")
    successor = append_forward_prediction(
        campaign, prediction, evidence_class=EVIDENCE_REAL
    )
    summary = build_forward_evidence_summary(successor, as_of=capture)
    if summary["completeness"]["initial_captured"] != 7:
        raise ValueError("successor did not contain seven INITIAL captures")
    if (
        summary["completeness"]["initial_due"] != 0
        or summary["completeness"]["initial_missed"] != 0
    ):
        raise ValueError("INITIAL lifecycle accounting is not closed")

    audit = {
        "schema": "nations-league-v1-1-deterministic-alias-initial-capture-v1",
        "capture_at": capture,
        "fixture_id": TARGET_ID,
        "source_digest": target["source_digest"],
        "kickoff_utc": target["kickoff_utc"],
        "edition": target["edition"],
        "group": target["group"],
        "evaluation_block": target["evaluation_block"],
        "source_home_team": "Republic of Ireland",
        "source_away_team": "Austria",
        "canonical_model_home_team": "Ireland",
        "canonical_model_away_team": "Austria",
        "normalization_version": snapshot["identity_bindings"]["Republic of Ireland"][
            "normalization_version"
        ],
        "identity_binding_digest": snapshot["identity_binding_digest"],
        "fixture_manifest_digest": manifest["manifest_digest"],
        "model_digest": MODEL_DIGEST,
        "base_timeline_digest": TRAINING_TIMELINE_DATASET_DIGEST,
        "result_extension_digest": extension["extension_digest"],
        "completeness_digest": proof["completeness_digest"],
        "input_snapshot_digest": snapshot["input_snapshot_digest"],
        "record_id": prediction["record_id"],
        "previous_campaign_id": campaign.campaign.campaign_id,
        "previous_record_count": len(campaign.records),
        "result_count": len(training),
        "canonical_elo_identity_count": {
            "Ireland": 1,
            "Austria": 1,
        },
        "conflicting_identity_count": 0,
        "evidence_class": EVIDENCE_REAL,
        "no_bet": True,
        "publication": False,
        "ledger_mutation": False,
        "provider_calls": 0,
        "artifact_digest": "0" * 64,
    }
    audit["artifact_digest"] = sha256_json(
        {key: value for key, value in audit.items() if key != "artifact_digest"}
    )
    suffix = args.output_suffix
    output = {
        "inventory": ROOT
        / f"data/research/nations_league/post_base_official_results_{suffix}.json",
        "extension": ROOT
        / f"results/research/nations_league_v1_1_result_extension_{suffix}.json",
        "completeness": ROOT
        / f"results/audits/nations_league_v1_1_result_completeness_{suffix}.json",
        "input": ROOT
        / f"results/research/nations_league_v1_1_input_state_{suffix}.json",
        "store": ROOT
        / f"results/research/nations_league_v1_1_forward_shadow_store_alias_{suffix}.jsonl",
        "campaign": ROOT
        / f"results/research/nations_league_v1_1_forward_campaign_{suffix}.json",
        "summary": ROOT
        / f"results/audits/nations_league_v1_1_forward_campaign_summary_{suffix}.json",
        "audit": ROOT
        / f"results/audits/nations_league_v1_1_alias_initial_capture_{suffix}.json",
    }
    # No artifact is written until all input, prediction, append, and audit checks pass.
    write(output["inventory"], inventory)
    write(output["extension"], extension)
    write(output["completeness"], proof)
    write(output["input"], snapshot)
    output["store"].write_text(
        json.dumps(prediction, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write(output["campaign"], successor.to_payload())
    write(output["summary"], summary)
    write(output["audit"], audit)
    print(
        json.dumps(
            {
                "capture_at": capture,
                "record_id": prediction["record_id"],
                "probabilities": prediction["probabilities"],
                "identity_binding_digest": snapshot["identity_binding_digest"],
                "input_snapshot_digest": snapshot["input_snapshot_digest"],
                "extension_digest": extension["extension_digest"],
                "completeness_digest": proof["completeness_digest"],
                "summary_digest": summary["summary_digest"],
                "audit_digest": audit["artifact_digest"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
