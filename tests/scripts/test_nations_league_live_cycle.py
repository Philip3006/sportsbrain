from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).parents[2]
CYCLE_SCRIPT = ROOT / "scripts/nations_league_live_cycle.py"
PUBLIC_SCRIPT = ROOT / "scripts/build_nations_league_live_public.py"


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_execute_cycle_appends_publicly_materializable_record_and_rerun_is_idempotent(tmp_path):
    manifest = _json("results/audits/nations_league_forward_fixture_manifest.json")
    synthetic_manifest = deepcopy(manifest)
    source = next(
        row
        for row in manifest["fixtures"]
        if row["kickoff_utc"] == "2026-10-01T18:45:00Z"
    )
    synthetic = deepcopy(source)
    synthetic["fixture_id"] = "uefa-nl:synthetic-live-cycle"
    synthetic_manifest["fixtures"] = [synthetic]
    from src.analysis.nations_league_live_runtime import _digest

    synthetic_manifest["manifest_digest"] = _digest(
        {key: value for key, value in synthetic_manifest.items() if key != "manifest_digest"}
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(synthetic_manifest), encoding="utf-8")
    registry = ROOT / "results/audits/continuous_model_lifecycle_registry.json"
    store = tmp_path / "live.jsonl"
    cycle = _module(CYCLE_SCRIPT, "nations_league_live_cycle_test")
    result = cycle.run_cycle(
        manifest=manifest_path,
        input_state=None,
        registry=registry,
        as_of="2026-09-30T20:01:24.572945Z",
        store=store,
        execute_offline=True,
        base_timeline=ROOT / "results/research/nations_league_fixture_timeline_v1.json",
        result_extension=ROOT / "results/research/nations_league_v1_1_result_extension_20260930T200124Z.json",
        source_release_sha="267a5df80ee326cfe41ee6ddc27d3e8c547ab222",
        campaign=ROOT / "results/research/nations_league_v1_1_forward_campaign_20260930T200124Z.json",
    )
    assert result["status"] == "MATERIALIZED"
    assert result["appended_count"] == 1
    assert len(store.read_text(encoding="utf-8").splitlines()) == 1
    again = cycle.run_cycle(
        manifest=manifest_path,
        input_state=None,
        registry=registry,
        as_of="2026-09-30T20:01:24.572945Z",
        store=store,
        execute_offline=True,
        base_timeline=ROOT / "results/research/nations_league_fixture_timeline_v1.json",
        result_extension=ROOT / "results/research/nations_league_v1_1_result_extension_20260930T200124Z.json",
        source_release_sha="267a5df80ee326cfe41ee6ddc27d3e8c547ab222",
        campaign=ROOT / "results/research/nations_league_v1_1_forward_campaign_20260930T200124Z.json",
    )
    assert again["status"] == "ALREADY_CAPTURED"
    assert again["appended_count"] == 0

    public = _module(PUBLIC_SCRIPT, "nations_league_live_public_test")
    output = tmp_path / "signals.json"
    public.materialize(
        campaign=ROOT / "results/research/nations_league_v1_1_forward_campaign_20260930T200124Z.json",
        registry=registry,
        binding=ROOT / "results/audits/nations_league_v1_1_live_evidence_binding.json",
        inputs=[ROOT / "docs/data/signals.json"],
        store=store,
        output=output,
        as_of="2026-10-01T17:15:00Z",
    )
    product = json.loads(output.read_text(encoding="utf-8"))["nations_league"]
    assert product["fixture_count"] == 8
    assert any(row["fixture_id"] == synthetic["fixture_id"] for row in product["fixtures"])


def test_result_refresh_creates_and_activates_one_successor_release_without_io():
    from src.analysis.nations_league_live_runtime import (
        build_fresh_input_state,
        load_active_release,
        refresh_and_activate,
    )
    from src.analysis.nations_league_v1_1 import sha256_json

    manifest = _json("results/audits/nations_league_forward_fixture_manifest.json")
    base = _json("results/research/nations_league_fixture_timeline_v1.json")
    extension = _json(
        "results/research/nations_league_v1_1_result_extension_20260930T200124Z.json"
    )
    changed_extension = deepcopy(extension)
    changed_row = changed_extension["result_rows"][0]
    changed_evidence = deepcopy(changed_row["source_evidence"])
    changed_evidence["home_score"] += 1
    changed_row["source_evidence"] = changed_evidence
    changed_row["home_score"] = changed_evidence["home_score"]
    changed_row["source_digest"] = sha256_json(changed_evidence)
    changed_extension["extension_digest"] = sha256_json(
        {key: value for key, value in changed_extension.items() if key != "extension_digest"}
    )
    state = build_fresh_input_state(
        manifest,
        base,
        changed_extension,
        prediction_cutoff="2026-09-30T20:01:24.572945Z",
    )
    registry = _json("results/audits/continuous_model_lifecycle_registry.json")
    previous = load_active_release(
        ROOT / "results/audits/continuous_model_lifecycle_registry.json"
    )
    updated, active, decision = refresh_and_activate(
        registry,
        state,
        source_release_sha="267a5df80ee326cfe41ee6ddc27d3e8c547ab222",
        activated_at="2026-09-30T20:01:24.572945Z",
    )
    assert decision["status"] == "RETRAINED"
    assert active.release_id != previous.release_id
    assert active.status == "ACTIVE"
    active_rows = [
        row
        for row in updated["lifecycle"]["releases"]
        if row["status"] == "ACTIVE"
    ]
    assert active_rows == [active.to_payload()]
    assert sum(row["status"] == "VALIDATED" for row in updated["lifecycle"]["releases"]) == 1


def test_zero_fixture_public_materialization_is_byte_stable_across_scheduler_ticks(tmp_path):
    public = _module(PUBLIC_SCRIPT, "nations_league_live_public_zero_test")
    registry = ROOT / "results/audits/continuous_model_lifecycle_registry.json"
    campaign = ROOT / "results/research/nations_league_v1_1_forward_campaign_20260930T200124Z.json"
    binding = ROOT / "results/audits/nations_league_v1_1_live_evidence_binding.json"
    output = tmp_path / "signals.json"
    kwargs = {
        "campaign": campaign,
        "registry": registry,
        "binding": binding,
        "inputs": [ROOT / "docs/data/signals.json"],
        "store": None,
        "output": output,
    }
    public.materialize(**kwargs, as_of="2026-10-01T18:45:00Z")
    first_bytes = output.read_bytes()
    first = json.loads(first_bytes)["nations_league"]
    public.materialize(**kwargs, as_of="2026-10-01T19:00:00Z")
    second_bytes = output.read_bytes()
    second = json.loads(second_bytes)["nations_league"]
    public.materialize(**kwargs, as_of="2026-10-01T19:15:00Z")
    third_bytes = output.read_bytes()
    third = json.loads(third_bytes)["nations_league"]
    assert first["fixture_count"] == second["fixture_count"] == third["fixture_count"] == 0
    assert first["public_digest"] == second["public_digest"] == third["public_digest"]
    assert first_bytes == second_bytes == third_bytes
