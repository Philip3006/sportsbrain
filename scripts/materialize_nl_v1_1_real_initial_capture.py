#!/usr/bin/env python3
"""Materialize one explicitly observed, offline NL v1.1 INITIAL capture.

This one-shot evidence materializer reads committed UEFA research artifacts
only. It never contacts a provider, reads credentials, or writes runtime or
public state. The capture cutoff is explicit for reproducibility.
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis.nations_league_forward_campaign import (
    EVIDENCE_REAL,
    append_forward_prediction,
    build_forward_evidence_summary,
    create_forward_campaign,
    serialize_forward_summary,
)
from src.analysis.nations_league_forward_input import build_input_state
from src.analysis.nations_league_result_extension import (
    build_extension,
    completeness,
    validate_append_only_successor,
)
from src.analysis.nations_league_v1_1 import (
    TRAINING_TIMELINE_DATASET_DIGEST,
    model_digest,
    sha256_json,
    training_records_from_timeline,
)

MODEL_VERSION = "nations_league_v1_1"
MODEL_DIGEST = "50fc0f9120009b86eda1ebf016c14e93c9bd72c256b849577d5f55217d77f626"
MANIFEST_DIGEST = "5dd24bf5f820e456ec40f9df1ecf45a57f17f13a50e8e1341853c2f42b81091f"
TARGET_ID = "uefa-nl:future-3fe70ff0ba848e39b50908d6"
BASE_TIMELINE = ROOT / "results/research/nations_league_fixture_timeline_v1.json"
SOURCE_INVENTORY = (
    ROOT / "data/research/nations_league/post_base_official_results_20260930.json"
)
PREVIOUS_EXTENSION = (
    ROOT / "results/research/nations_league_v1_1_result_extension_20260930.json"
)
MANIFEST = ROOT / "results/audits/nations_league_forward_fixture_manifest.json"


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("capture cutoff must be explicit UTC")
    return parsed.astimezone(timezone.utc)


def stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def fresh_inventory(source: dict[str, Any], capture: str) -> dict[str, Any]:
    inventory = deepcopy(source)
    inventory["observed_at"] = capture
    inventory["sources"] = [
        {**row, "observed_at": capture} for row in inventory["sources"]
    ]
    inventory["coverage"] = {
        **inventory["coverage"],
        "verified_through": capture,
        "basis": (
            "Official UEFA result index and match pages observed at the exact "
            "capture cutoff; all post-base results through 2026-09-29 are "
            "accounted for and 2026-10-01 is the next scheduled match date."
        ),
        "observation_method": "ordinary public official UEFA web sources",
    }
    inventory["fresh_observation"] = {
        "observed_at": capture,
        "source_urls": [
            "https://www.uefa.com/uefanationsleague/news/02a2-1fea18abbcbc-456e846509e7-1000--2026-27-uefa-nations-league-all-the-league-phase-fixtures/",
            "https://de.uefa.com/uefanationsleague/match/2047954--germany-vs-serbia/",
        ],
        "scope": "post-base result completeness and Germany-Serbia future identity",
        "result_cutoff_basis": "latest listed completed match is 2026-09-29; next scheduled date is 2026-10-01",
    }
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-at", required=True)
    parser.add_argument("--output-suffix", required=True)
    args = parser.parse_args()
    capture_dt = parse_utc(args.capture_at)
    capture = stamp(capture_dt)
    if not (
        parse_utc("2026-09-30T16:45:00Z")
        <= capture_dt
        <= parse_utc("2026-09-30T20:45:00Z")
    ):
        raise ValueError("Germany-Serbia INITIAL capture cutoff is outside its window")
    if model_digest() != MODEL_DIGEST:
        raise ValueError("frozen model digest mismatch")

    base = read(BASE_TIMELINE)
    previous_extension = read(PREVIOUS_EXTENSION)
    inventory = fresh_inventory(read(SOURCE_INVENTORY), capture)
    extension = build_extension(base, inventory, generated_at=capture)
    validate_append_only_successor(previous_extension, extension)
    proof = completeness(extension, capture)
    if proof["status"] != "READY":
        raise ValueError(f"fresh completeness is not READY: {proof['status']}")

    manifest = read(MANIFEST)
    if manifest["manifest_digest"] != MANIFEST_DIGEST:
        raise ValueError("fixture manifest digest mismatch")
    target = next(row for row in manifest["fixtures"] if row["fixture_id"] == TARGET_ID)
    if target["status"] != "VERIFIED":
        raise ValueError("Germany-Serbia manifest row is not VERIFIED")
    training = training_records_from_timeline(base) + extension["result_rows"]
    provenance = {
        "source_digest": target["source_digest"],
        "source_provenance": "fresh official UEFA completeness observation",
        "timeline_digest": TRAINING_TIMELINE_DATASET_DIGEST,
        "fixture_source_digest": target["source_digest"],
        "base_timeline_digest": TRAINING_TIMELINE_DATASET_DIGEST,
        "result_extension_digest": extension["extension_digest"],
        "completeness_digest": proof["completeness_digest"],
        "capture_cutoff": capture,
    }
    snapshot = build_input_state(
        [deepcopy(target)],
        training,
        prediction_cutoff=capture,
        provenance=provenance,
        base_timeline=base,
        result_extension=extension,
        completeness_artifact=proof,
    )
    if snapshot["team_readiness"] != {"Germany": "READY", "Serbia": "READY"}:
        raise ValueError("Germany and Serbia must both be READY")

    # This is the active gated call used to materialize one local evidence row.
    from src.analysis.nations_league_forward_input import predict_from_input_state

    prediction = predict_from_input_state(snapshot, TARGET_ID, phase="initial")
    if parse_utc(prediction["prediction_timestamp"]) != capture_dt:
        raise ValueError("prediction cutoff is not the exact capture cutoff")

    campaign_rows = []
    for row in manifest["fixtures"]:
        item = deepcopy(row)
        kickoff = parse_utc(row["kickoff_utc"])
        item["initial_eligible"] = (
            row["status"] == "VERIFIED" and kickoff - timedelta(hours=22) >= capture_dt
        )
        item["refinement_eligible"] = (
            row["status"] == "VERIFIED"
            and kickoff - timedelta(minutes=60) >= capture_dt
        )
        campaign_rows.append(item)
    campaign = create_forward_campaign(
        campaign_id=f"nl-v1-1-forward-{args.output_suffix}",
        edition="2026/27",
        campaign_start=capture,
        fixture_manifest=campaign_rows,
        fixture_manifest_digest=MANIFEST_DIGEST,
    )
    campaign = append_forward_prediction(
        campaign, prediction, evidence_class=EVIDENCE_REAL
    )
    summary = build_forward_evidence_summary(campaign, as_of=capture)

    suffix = args.output_suffix
    write(
        ROOT / f"data/research/nations_league/post_base_official_results_{suffix}.json",
        inventory,
    )
    write(
        ROOT / f"results/research/nations_league_v1_1_result_extension_{suffix}.json",
        extension,
    )
    write(
        ROOT / f"results/audits/nations_league_v1_1_result_completeness_{suffix}.json",
        proof,
    )
    write(
        ROOT / f"results/research/nations_league_v1_1_input_state_{suffix}.json",
        snapshot,
    )
    write(
        ROOT / f"results/research/nations_league_v1_1_forward_campaign_{suffix}.json",
        campaign.to_payload(),
    )
    (
        ROOT
        / f"results/research/nations_league_v1_1_forward_shadow_store_{suffix}.jsonl"
    ).write_text(
        json.dumps(prediction, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    audit = {
        "schema": "nations-league-v1-1-real-observed-initial-capture-v1",
        "evidence_class": EVIDENCE_REAL,
        "capture_at": capture,
        "lifecycle": "INITIAL",
        "model_version": MODEL_VERSION,
        "model_digest": MODEL_DIGEST,
        "fixture_manifest_digest": MANIFEST_DIGEST,
        "fixture_source_digest": target["source_digest"],
        "base_timeline_digest": TRAINING_TIMELINE_DATASET_DIGEST,
        "result_extension_digest": extension["extension_digest"],
        "completeness_digest": proof["completeness_digest"],
        "input_snapshot_digest": snapshot["input_snapshot_digest"],
        "fixture_id": TARGET_ID,
        "prediction_record_id": prediction["record_id"],
        "prediction": prediction,
        "no_bet": True,
        "publication": False,
        "ledger_mutation": False,
        "settlement_status": "UNSETTLED",
        "campaign_id": campaign.campaign.campaign_id,
        "campaign_contract_digest": campaign.campaign.evaluation_contract_digest,
        "campaign_summary_digest": summary["summary_digest"],
        "artifact_digest": "0" * 64,
    }
    audit["artifact_digest"] = sha256_json(
        {k: v for k, v in audit.items() if k != "artifact_digest"}
    )
    write(
        ROOT
        / f"results/audits/nations_league_v1_1_real_observed_initial_{suffix}.json",
        audit,
    )
    write(
        ROOT
        / f"results/audits/nations_league_v1_1_forward_campaign_summary_{suffix}.json",
        json.loads(serialize_forward_summary(summary)),
    )
    print(
        json.dumps(
            {
                "capture_at": capture,
                "extension_digest": extension["extension_digest"],
                "completeness_digest": proof["completeness_digest"],
                "input_snapshot_digest": snapshot["input_snapshot_digest"],
                "prediction_record_id": prediction["record_id"],
                "campaign_id": campaign.campaign.campaign_id,
                "campaign_summary_digest": summary["summary_digest"],
                "artifact_digest": audit["artifact_digest"],
                "probabilities": prediction["probabilities"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
