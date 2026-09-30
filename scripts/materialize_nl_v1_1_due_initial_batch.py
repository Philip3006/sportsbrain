"""Materialize the remaining DUE v1.1 INITIAL shadow subset offline."""

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
    CapturePolicy,
    ForwardCampaignArtifact,
    ForwardEvidenceCampaign,
    append_forward_prediction,
    build_forward_evidence_summary,
    validate_forward_campaign,
)
from src.analysis.nations_league_forward_input import (
    build_input_state,
    predict_from_input_state,
)
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
    validate_shadow_record,
)

MODEL_VERSION = "nations_league_v1_1"
MODEL_DIGEST = "50fc0f9120009b86eda1ebf016c14e93c9bd72c256b849577d5f55217d77f626"
MANIFEST_DIGEST = "5dd24bf5f820e456ec40f9df1ecf45a57f17f13a50e8e1341853c2f42b81091f"
CAMPAIGN_ID = "nl-v1-1-forward-20260930T183441Z"
GERMANY_ID = "uefa-nl:future-3fe70ff0ba848e39b50908d6"
TARGET_IDS = (
    "uefa-nl:future-24e02c77e0dae6970194e816",
    "uefa-nl:future-0f2bd8c1791c064f46230190",
    "uefa-nl:future-3515b2e85084a8451012773f",
    "uefa-nl:future-41e14a9086638f2d4bd278d8",
    "uefa-nl:future-596f9e31eff328f0e49a77b5",
    "uefa-nl:future-392e2f375b6ddb9ac9b6cb14",
)
BASE = ROOT / "results/research/nations_league_fixture_timeline_v1.json"
PREVIOUS_INVENTORY = (
    ROOT
    / "data/research/nations_league/post_base_official_results_20260930T183441Z.json"
)
PREVIOUS_EXTENSION = (
    ROOT / "results/research/nations_league_v1_1_result_extension_20260930T183441Z.json"
)
CAMPAIGN = (
    ROOT / "results/research/nations_league_v1_1_forward_campaign_20260930T183441Z.json"
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
        raise ValueError("timestamp must carry UTC timezone")
    return parsed.astimezone(timezone.utc)


def stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def campaign_from_payload(payload: dict[str, Any]) -> ForwardCampaignArtifact:
    value = payload["campaign"]
    campaign = ForwardEvidenceCampaign(
        campaign_id=value["campaign_id"],
        competition=value["competition"],
        edition=value["edition"],
        model_version=value["model_version"],
        model_digest_value=value["model_digest"],
        campaign_start=value["campaign_start"],
        fixture_manifest=tuple(value["fixture_manifest"]),
        fixture_manifest_digest=value["fixture_manifest_digest"],
        initial_policy=CapturePolicy(**value["initial_policy"]),
        refinement_policy=CapturePolicy(**value["refinement_policy"]),
        promotion_criteria_digest=value["promotion_criteria_digest"],
        no_bet=value["no_bet"],
        signal_status=value["signal_status"],
        evaluation_contract_digest=value["evaluation_contract_digest"],
    )
    artifact = ForwardCampaignArtifact(campaign, tuple(payload["records"]))
    validate_forward_campaign(artifact)
    return artifact


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
        "scope": "post-base result completeness and remaining Oct-1 targets",
        "result_cutoff_basis": "latest listed completed match is 2026-09-29; next scheduled date is 2026-10-01",
    }
    return inventory


def provenance(
    target: dict[str, Any],
    extension: dict[str, Any],
    proof: dict[str, Any],
    capture: str,
) -> dict[str, Any]:
    return {
        "source_digest": target["source_digest"],
        "source_provenance": "fresh official UEFA completeness observation",
        "timeline_digest": TRAINING_TIMELINE_DATASET_DIGEST,
        "base_timeline_digest": TRAINING_TIMELINE_DATASET_DIGEST,
        "result_extension_digest": extension["extension_digest"],
        "completeness_digest": proof["completeness_digest"],
        "capture_cutoff": capture,
    }


def preflight(
    target: dict[str, Any],
    training: list[dict[str, Any]],
    base: dict[str, Any],
    extension: dict[str, Any],
    proof: dict[str, Any],
    capture: str,
) -> tuple[str, dict[str, Any], dict[str, Any] | None, str | None]:
    snapshot = build_input_state(
        [deepcopy(target)],
        training,
        prediction_cutoff=capture,
        provenance=provenance(target, extension, proof, capture),
        base_timeline=base,
        result_extension=extension,
        completeness_artifact=proof,
    )
    readiness = snapshot["team_readiness"]
    states = set(readiness.values())
    if states == {"READY"}:
        return "READY", readiness, snapshot, None
    state = next(iter(states - {"READY"}), "BLOCKED")
    return state, readiness, snapshot, f"{state}: target input is not READY"


def validate_batch_predictions(
    predictions: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    snapshot: dict[str, Any],
    existing: ForwardCampaignArtifact,
    capture: str,
) -> None:
    if len(predictions) != len(targets):
        raise ValueError("prediction batch size mismatch")
    expected_ids = {row["fixture_id"] for row in targets}
    existing_keys = {
        (row["fixture_id"], row["phase"])
        for row in existing.records
        if row.get("record_type", "prediction") == "prediction"
    }
    record_ids = set()
    for prediction in predictions:
        validate_shadow_record(prediction)
        if prediction["fixture_id"] not in expected_ids:
            raise ValueError("prediction target escaped READY batch")
        if prediction["phase"] != "initial" or parse_utc(
            prediction["prediction_timestamp"]
        ) != parse_utc(snapshot["prediction_cutoff"]):
            raise ValueError("prediction lifecycle/cutoff mismatch")
        if (prediction["fixture_id"], prediction["phase"]) in existing_keys:
            raise ValueError("duplicate campaign fixture/phase")
        if prediction["record_id"] in record_ids:
            raise ValueError("duplicate batch record id")
        record_ids.add(prediction["record_id"])
        if (
            prediction["model_version"] != MODEL_VERSION
            or prediction["model_digest"] != MODEL_DIGEST
        ):
            raise ValueError("prediction model binding mismatch")
        if prediction["eventual_result"] is not None:
            raise ValueError("prediction is already settled")
        if (
            prediction["shadow"] is not True
            or prediction["no_bet"] is not True
            or prediction["publication_enabled"] is not False
            or prediction["ledger_mutation"] is not False
        ):
            raise ValueError("prediction safety binding mismatch")
        if parse_utc(prediction["prediction_timestamp"]) != parse_utc(capture):
            raise ValueError("prediction timestamp differs from capture")


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
    if model_digest() != MODEL_DIGEST:
        raise ValueError("frozen model digest mismatch")

    base = read(BASE)
    manifest = read(MANIFEST)
    if manifest["manifest_digest"] != MANIFEST_DIGEST:
        raise ValueError("fixture manifest digest mismatch")
    previous_inventory = read(PREVIOUS_INVENTORY)
    previous_extension = read(PREVIOUS_EXTENSION)
    inventory = fresh_inventory(previous_inventory, capture)
    extension = build_extension(base, inventory, generated_at=capture)
    validate_append_only_successor(previous_extension, extension)
    proof = completeness(extension, capture)
    if (
        proof["status"] != "READY"
        or proof["expected_count"] != 56
        or proof["verified_count"] != 56
        or proof["unresolved_count"] != 0
    ):
        raise ValueError("fresh completeness is not the required READY 56/56/0")

    manifest_by_id = {row["fixture_id"]: row for row in manifest["fixtures"]}
    targets = [manifest_by_id[fixture_id] for fixture_id in TARGET_IDS]
    training = training_records_from_timeline(base) + extension["result_rows"]
    if len(training) != 566:
        raise ValueError("unexpected causal training row count")
    preflight_rows = []
    ready_targets = []
    for target in targets:
        state, readiness, _snapshot, blocker = preflight(
            target, training, base, extension, proof, capture
        )
        preflight_rows.append(
            {
                "fixture_id": target["fixture_id"],
                "home_team": target["home_team"],
                "away_team": target["away_team"],
                "home_readiness": readiness.get(target["home_team"]),
                "away_readiness": readiness.get(target["away_team"]),
                "state": state,
                "captured": False,
                "blocker": blocker,
            }
        )
        if state == "READY":
            ready_targets.append(target)

    if not ready_targets:
        raise ValueError("no READY targets remain after preflight")

    ready_snapshot = build_input_state(
        [deepcopy(target) for target in ready_targets],
        training,
        prediction_cutoff=capture,
        provenance=provenance(ready_targets[0], extension, proof, capture),
        base_timeline=base,
        result_extension=extension,
        completeness_artifact=proof,
    )
    if any(value != "READY" for value in ready_snapshot["team_readiness"].values()):
        raise ValueError("shared READY batch contains a blocked team")
    predictions = [
        predict_from_input_state(ready_snapshot, target["fixture_id"], phase="initial")
        for target in ready_targets
    ]
    campaign = campaign_from_payload(read(CAMPAIGN))
    if campaign.campaign.campaign_id != CAMPAIGN_ID:
        raise ValueError("unexpected prior campaign")
    prior_germany = next(
        row for row in campaign.records if row.get("fixture_id") == GERMANY_ID
    )
    if (
        prior_germany["record_id"]
        != "2fab2e3afb12d51f554f883d786720e3c4306ef14ff66b329f26c608c19f676e"
    ):
        raise ValueError("Germany evidence identity changed")
    validate_batch_predictions(
        predictions, ready_targets, ready_snapshot, campaign, capture
    )
    successor = campaign
    for prediction in predictions:
        successor = append_forward_prediction(
            successor, prediction, evidence_class=EVIDENCE_REAL
        )
    summary = build_forward_evidence_summary(successor, as_of=capture)
    for row in preflight_rows:
        if row["state"] == "READY":
            row["captured"] = True
    audit = {
        "schema": "nations-league-v1-1-due-initial-batch-v1",
        "capture_at": capture,
        "model_version": MODEL_VERSION,
        "model_digest": MODEL_DIGEST,
        "base_timeline_digest": TRAINING_TIMELINE_DATASET_DIGEST,
        "result_extension_digest": extension["extension_digest"],
        "completeness_digest": proof["completeness_digest"],
        "input_snapshot_digest": ready_snapshot["input_snapshot_digest"],
        "fixture_manifest_digest": MANIFEST_DIGEST,
        "prior_campaign_id": campaign.campaign.campaign_id,
        "prior_campaign_contract_digest": campaign.campaign.evaluation_contract_digest,
        "prior_germany_record_id": prior_germany["record_id"],
        "attempted_fixtures": preflight_rows,
        "ready_count": len(ready_targets),
        "blocked_count": len(targets) - len(ready_targets),
        "new_prediction_record_ids": [row["record_id"] for row in predictions],
        "evidence_class": EVIDENCE_REAL,
        "signal_status": "SHADOW_ONLY",
        "no_bet": True,
        "publication": False,
        "ledger_mutation": False,
        "artifact_digest": "0" * 64,
    }
    audit["artifact_digest"] = sha256_json(
        {key: value for key, value in audit.items() if key != "artifact_digest"}
    )
    suffix = args.output_suffix
    # All validation is complete before any new file is written.
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
        ROOT / f"results/research/nations_league_v1_1_input_state_batch_{suffix}.json",
        ready_snapshot,
    )
    (
        ROOT
        / f"results/research/nations_league_v1_1_forward_shadow_store_batch_{suffix}.jsonl"
    ).write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in predictions
        ),
        encoding="utf-8",
    )
    write(
        ROOT / f"results/research/nations_league_v1_1_forward_campaign_{suffix}.json",
        successor.to_payload(),
    )
    write(
        ROOT
        / f"results/audits/nations_league_v1_1_forward_campaign_summary_{suffix}.json",
        summary,
    )
    write(
        ROOT / f"results/audits/nations_league_v1_1_due_initial_batch_{suffix}.json",
        audit,
    )
    print(
        json.dumps(
            {
                "capture_at": capture,
                "ready_count": len(ready_targets),
                "blocked_count": len(targets) - len(ready_targets),
                "extension_digest": extension["extension_digest"],
                "completeness_digest": proof["completeness_digest"],
                "input_snapshot_digest": ready_snapshot["input_snapshot_digest"],
                "record_ids": [row["record_id"] for row in predictions],
                "campaign_summary_digest": summary["summary_digest"],
                "audit_digest": audit["artifact_digest"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
