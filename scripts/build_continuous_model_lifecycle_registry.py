#!/usr/bin/env python3
"""Build deterministic offline lifecycle inventory and Nations League registry.

This utility reads only committed repository artifacts.  It does not train a
model, contact a provider, inspect credentials, write runtime state, or publish
anything.  Its three JSON outputs are reviewable source artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis.nations_league_model_lifecycle import (
    FROZEN_ALGORITHM_DIGEST,
    establish_initial_active_release,
    release_binding_payload,
)
from src.models.lifecycle import canonical_digest

SNAPSHOTS = (
    "results/research/nations_league_v1_1_input_state_20260930T183441Z.json",
    "results/research/nations_league_v1_1_input_state_batch_20260930T193447Z.json",
    "results/research/nations_league_v1_1_input_state_20260930T200124Z.json",
)
CAMPAIGNS = (
    "results/research/nations_league_v1_1_forward_campaign_20260930T183441Z.json",
    "results/research/nations_league_v1_1_forward_campaign_20260930T193447Z.json",
    "results/research/nations_league_v1_1_forward_campaign_20260930T200124Z.json",
)


def _read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError(f"{path} is not a JSON object")
    return value


def _inventory() -> list[dict[str, Any]]:
    """Observed current workflows and artifacts; unknowns stay explicit."""

    return [
        {
            "model_family": "nations_league_v1_1",
            "status": "ACTIVE",
            "sport": "football",
            "scope": "UEFA Nations League",
            "algorithm": "causal_elo",
            "training_source": "canonical timeline + sealed result extension",
            "result_source": "result_safe_available_at in sealed extension",
            "training_script": "src.analysis.nations_league_forward_input.build_input_state",
            "current_artifact": "results/research/nations_league_v1_1_input_state_20260930T200124Z.json",
            "retrain_cadence": "result-driven; no scheduler",
            "settlement_cadence": "manual/offline forward-shadow settlement",
            "current_activation": "continuous-model-lifecycle-registry-v1 active pointer",
            "rollback": "generic previous active pointer",
            "publication_consumer": "nations-league-live-public-v1 adapter",
            "continuous_retraining_state": "READY_RESULT_DRIVEN",
            "evidence": [
                "src/analysis/nations_league_v1_1.py",
                "src/analysis/nations_league_forward_input.py",
            ],
        },
        {
            "model_family": "tennis_lgbm",
            "status": "LEGACY",
            "sport": "tennis",
            "scope": "tennis-data full tour",
            "algorithm": "LightGBM + calibrator",
            "training_source": "tennis-data.co.uk XLSX via fetch_full_tour_odds",
            "result_source": "tennis settlement workflow / ledger outcomes",
            "training_script": "scripts/tennis_train.py",
            "current_artifact": "models/tennis_lgbm/{model.pkl,calibrator.pkl,metadata.json}",
            "retrain_cadence": "daily 05:00 UTC (.github/workflows/tennis_lgbm_retrain.yml)",
            "settlement_cadence": "Cloudflare dispatch plus slow GitHub cron fallback",
            "current_activation": "legacy file-path consumer; no atomic pointer",
            "rollback": "NOT_IMPLEMENTED",
            "publication_consumer": "src/tennis/ensemble.py",
            "continuous_retraining_state": "MAPPING_READY_POINTER_MIGRATION_REQUIRED",
            "evidence": [".github/workflows/tennis_lgbm_retrain.yml", "scripts/tennis_train.py"],
        },
        {
            "model_family": "tennis_calibration",
            "status": "LEGACY",
            "sport": "tennis",
            "scope": "tennis signals",
            "algorithm": "signal recalibrator",
            "training_source": "settled signal/ledger history",
            "result_source": "tennis settlement workflow",
            "training_script": "scripts/tennis_recalibrate.py",
            "current_artifact": "models/tennis_calibrators/{hard.pkl,recalibrate_meta.json}",
            "retrain_cadence": "weekly Sunday 05:00 UTC (.github/workflows/tennis_recalibrate.yml)",
            "settlement_cadence": "Cloudflare dispatch plus slow GitHub cron fallback",
            "current_activation": "legacy artifact consumer; no atomic pointer",
            "rollback": "NOT_IMPLEMENTED",
            "publication_consumer": "UNKNOWN",
            "continuous_retraining_state": "MAPPING_READY_POINTER_MIGRATION_REQUIRED",
            "evidence": [".github/workflows/tennis_recalibrate.yml", "scripts/tennis_recalibrate.py"],
        },
        {
            "model_family": "bundesliga2_elo",
            "status": "LEGACY",
            "sport": "football",
            "scope": "2. Bundesliga",
            "algorithm": "Elo",
            "training_source": "data/cache/bundesliga2_matches.pkl",
            "result_source": "bundesliga2 settlement/cache workflow",
            "training_script": "scripts/train_elo_bundesliga2.py",
            "current_artifact": "data/cache/elo_ratings_bl2.json",
            "retrain_cadence": "daily 05:00 UTC (.github/workflows/bundesliga2_retrain.yml)",
            "settlement_cadence": "scheduled bundesliga2_settle workflow",
            "current_activation": "legacy shared cache overwrite; no atomic pointer",
            "rollback": "NOT_IMPLEMENTED",
            "publication_consumer": "scripts/bundesliga2_scan.py",
            "continuous_retraining_state": "MAPPING_READY_POINTER_MISSING",
            "evidence": [".github/workflows/bundesliga2_retrain.yml", "scripts/train_elo_bundesliga2.py"],
        },
        {
            "model_family": "bundesliga2_dixon_coles",
            "status": "LEGACY",
            "sport": "football",
            "scope": "2. Bundesliga",
            "algorithm": "Dixon-Coles",
            "training_source": "data/cache/bundesliga2_matches.pkl (last five seasons)",
            "result_source": "bundesliga2 settlement/cache workflow",
            "training_script": "scripts/train_dc_bundesliga2.py",
            "current_artifact": "models/dc_bundesliga2/params_latest.pkl",
            "retrain_cadence": "daily 05:00 UTC (.github/workflows/bundesliga2_retrain.yml)",
            "settlement_cadence": "scheduled bundesliga2_settle workflow",
            "current_activation": "params_latest file/symlink; no atomic pointer",
            "rollback": "NOT_IMPLEMENTED",
            "publication_consumer": "scripts/bundesliga2_scan.py",
            "continuous_retraining_state": "MAPPING_READY_POINTER_MISSING",
            "evidence": [".github/workflows/bundesliga2_retrain.yml", "scripts/train_dc_bundesliga2.py"],
        },
        {
            "model_family": "bundesliga2_lgbm",
            "status": "LEGACY",
            "sport": "football",
            "scope": "2. Bundesliga",
            "algorithm": "LightGBM",
            "training_source": "bundesliga2 cached match universe",
            "result_source": "bundesliga2 settlement/cache workflow",
            "training_script": "scripts/train_lgbm_bundesliga2.py",
            "current_artifact": "models/lgbm_bundesliga2/{model.pkl,gate.json}",
            "retrain_cadence": "NOT_VERIFIED_IN_ACTIVE_WORKFLOW",
            "settlement_cadence": "scheduled bundesliga2_settle workflow",
            "current_activation": "legacy file-path consumer; no atomic pointer",
            "rollback": "NOT_IMPLEMENTED",
            "publication_consumer": "UNKNOWN",
            "continuous_retraining_state": "MAPPING_REQUIRED",
            "evidence": ["scripts/train_lgbm_bundesliga2.py", "models/lgbm_bundesliga2/gate.json"],
        },
        {
            "model_family": "world_cup_global_dc_lgbm_stacker",
            "status": "DISABLED",
            "sport": "football",
            "scope": "World Cup / legacy international",
            "algorithm": "Dixon-Coles + LightGBM + stacker",
            "training_source": "international result history",
            "result_source": "international result history",
            "training_script": "scripts/auto_retrain.py",
            "current_artifact": "models/{dixon_coles,lgbm}",
            "retrain_cadence": "disabled (.github/workflows/auto_retrain.yml.disabled)",
            "settlement_cadence": "UNKNOWN",
            "current_activation": "legacy artifacts; auto activation is disabled",
            "rollback": "legacy snapshots only; no generic pointer",
            "publication_consumer": "UNKNOWN",
            "continuous_retraining_state": "DISABLED_DO_NOT_REENABLE",
            "evidence": [".github/workflows/auto_retrain.yml.disabled", "scripts/auto_retrain.py"],
        },
        {
            "model_family": "top5_football",
            "status": "CANDIDATE",
            "sport": "football",
            "scope": "EPL/BL1/LL/SA/L1",
            "algorithm": "M5 governed Top-5 model",
            "training_source": "governed B4 evidence",
            "result_source": "governed B4 reconciliation/qualification",
            "training_script": "NOT_APPLICABLE_UNTIL_B4_ACCEPTANCE",
            "current_artifact": "provider-neutral acceptance artifacts",
            "retrain_cadence": "NOT_APPROVED",
            "settlement_cadence": "NOT_APPROVED",
            "current_activation": "explicit authorization only",
            "rollback": "governed activation seam",
            "publication_consumer": "B1/B3 prepublication contracts",
            "continuous_retraining_state": "BLOCKED_BY_GOVERNED_EVIDENCE_AND_AUTHORITY",
            "evidence": ["src/football/top5_final_acceptance.py"],
        },
    ]


def build_artifacts(root: Path, *, source_release_sha: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    snapshots = [_read(root / relative) for relative in SNAPSHOTS]
    if len({item.get("input_snapshot_digest") for item in snapshots}) != len(snapshots):
        raise ValueError("sealed input-state identities are not unique")
    latest = snapshots[-1]
    lifecycle, active = establish_initial_active_release(
        latest, source_release_sha=source_release_sha
    )
    binding_by_record_id: dict[str, dict[str, Any]] = {}
    by_snapshot = {item["input_snapshot_digest"]: item for item in snapshots}
    for relative in CAMPAIGNS:
        campaign = _read(root / relative)
        for record in campaign.get("records", []):
            provenance = record.get("input_provenance")
            if not isinstance(provenance, dict):
                raise TypeError("campaign record has no input provenance")
            input_digest = provenance.get("input_snapshot_digest")
            state = by_snapshot.get(input_digest)
            if state is None:
                raise ValueError("campaign record references an unknown input-state")
            if record.get("model_digest") != FROZEN_ALGORITHM_DIGEST:
                raise ValueError("campaign record uses a non-frozen model")
            training_data_digest = canonical_digest(state["training_records"])
            trained_state_digest = canonical_digest(state["elo_state"])
            if (
                training_data_digest != active.snapshot.training_data_digest
                or trained_state_digest != active.snapshot.trained_state_digest
            ):
                raise ValueError("source record does not bind to the active state")
            binding_row = {
                "source_prediction_record_id": record["record_id"],
                "fixture_id": record["fixture_id"],
                "phase": record["phase"],
                "input_snapshot_digest": input_digest,
                "training_data_digest": training_data_digest,
                "trained_state_digest": trained_state_digest,
                "algorithm_digest": FROZEN_ALGORITHM_DIGEST,
            }
            prior = binding_by_record_id.get(record["record_id"])
            if prior is not None and prior != binding_row:
                raise ValueError("repeated campaign record has conflicting binding")
            binding_by_record_id[record["record_id"]] = binding_row
    binding_rows = list(binding_by_record_id.values())
    if len(binding_rows) != 7:
        raise ValueError("expected exactly seven immutable real INITIAL source records")
    binding = {
        "schema": "nations-league-live-evidence-binding-v1",
        "active_model_release": release_binding_payload(active),
        "source_records": sorted(binding_rows, key=lambda row: row["source_prediction_record_id"]),
    }
    binding["binding_digest"] = canonical_digest(binding)
    inventory = {
        "schema": "continuous-model-lifecycle-inventory-v1",
        "source_release_sha": source_release_sha,
        "families": _inventory(),
    }
    inventory["inventory_digest"] = canonical_digest(inventory)
    registry = {
        "schema": "continuous-model-lifecycle-registry-v1",
        "source_release_sha": source_release_sha,
        "inventory_digest": inventory["inventory_digest"],
        "lifecycle": lifecycle.to_payload(),
        "health": [active_health.to_payload() for active_health in [lifecycle.health("nations_league_v1_1", updated_at=latest["observed_at"])]],
        "nations_league_live_evidence_binding_digest": binding["binding_digest"],
    }
    registry["registry_digest"] = canonical_digest(registry)
    return inventory, binding, registry


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-release-sha", required=True)
    parser.add_argument("--inventory-output", type=Path, default=ROOT / "results/audits/continuous_model_lifecycle_inventory.json")
    parser.add_argument("--binding-output", type=Path, default=ROOT / "results/audits/nations_league_v1_1_live_evidence_binding.json")
    parser.add_argument("--registry-output", type=Path, default=ROOT / "results/audits/continuous_model_lifecycle_registry.json")
    args = parser.parse_args()
    inventory, binding, registry = build_artifacts(ROOT, source_release_sha=args.source_release_sha)
    _write(args.inventory_output, inventory)
    _write(args.binding_output, binding)
    _write(args.registry_output, registry)
    print(json.dumps({"inventory_digest": inventory["inventory_digest"], "binding_digest": binding["binding_digest"], "registry_digest": registry["registry_digest"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
