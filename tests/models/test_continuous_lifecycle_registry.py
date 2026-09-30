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
