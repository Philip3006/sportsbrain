from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/build_continuous_model_lifecycle_registry.py"
SOURCE_SHA = "267a5df80ee326cfe41ee6ddc27d3e8c547ab222"


def _module():
    spec = importlib.util.spec_from_file_location("lifecycle_registry_builder", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_committed_inventory_and_registry_are_reproducible_from_sealed_inputs():
    inventory, binding, registry = _module().build_artifacts(
        ROOT, source_release_sha=SOURCE_SHA
    )
    assert inventory == _read("results/audits/continuous_model_lifecycle_inventory.json")
    assert binding == _read("results/audits/nations_league_v1_1_live_evidence_binding.json")
    assert registry == _read("results/audits/continuous_model_lifecycle_registry.json")


def test_inventory_is_explicit_about_legacy_disabled_and_candidate_boundaries():
    inventory = _read("results/audits/continuous_model_lifecycle_inventory.json")
    families = {row["model_family"]: row for row in inventory["families"]}
    required = {
        "model_family",
        "sport",
        "scope",
        "algorithm",
        "training_source",
        "result_source",
        "training_script",
        "current_artifact",
        "retrain_cadence",
        "settlement_cadence",
        "current_activation",
        "rollback",
        "publication_consumer",
        "continuous_retraining_state",
    }
    assert all(required <= set(row) for row in families.values())
    assert families["tennis_lgbm"]["retrain_cadence"].startswith("daily 05:00")
    assert families["bundesliga2_dixon_coles"]["rollback"] == "NOT_IMPLEMENTED"
    assert families["world_cup_global_dc_lgbm_stacker"]["status"] == "DISABLED"
    assert families["top5_football"]["status"] == "CANDIDATE"


def test_committed_nl_health_is_pointer_authoritative_and_operational():
    registry = _read("results/audits/continuous_model_lifecycle_registry.json")
    health = next(
        item
        for item in registry["health"]
        if item["model_family"] == "nations_league_v1_1"
    )
    assert health["status"] == "ACTIVE"
    assert health["active_release_id"] == (
        "78161c4097c06e596efa95721aa62db0ed72a057a3fd664cbecd32f0c0be29bb"
    )
    assert health["active_training_cutoff"] == "2026-09-30T20:01:24.572945+00:00"
    assert health["last_result_watermark"] == "2026-09-30T17:04:37.634354+00:00"
    assert health["training_row_count"] == 566
    assert health["last_successful_retrain"] == "2026-09-30T20:01:24.572945Z"
    assert health["last_failure_reason"] is None

    binding = _read("results/audits/nations_league_v1_1_live_evidence_binding.json")
    assert binding["binding_digest"] == (
        "6f3cca76f598862d0595616416cecd73b2cdd8213d4b1aecd493eb03261a12a8"
    )
    assert len(binding["source_records"]) == 7
