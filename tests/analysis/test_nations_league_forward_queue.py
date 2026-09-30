"""Offline queue boundaries, canonical handoff and append-only execution."""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.nations_league_forward_queue import due_state, execute, plan, read_store
from src.analysis.nations_league_forward_input import build_input_state
from src.analysis.nations_league_result_extension import completeness
from src.analysis.nations_league_v1_1 import (
    model_digest,
    training_records_from_timeline,
)

ROOT = Path(__file__).resolve().parents[2]
BASE = json.loads(
    (ROOT / "results/research/nations_league_fixture_timeline_v1.json").read_text()
)
EXTENSION = json.loads(
    (ROOT / "results/research/nations_league_v1_1_result_extension_20260930.json").read_text()
)
READY_CUTOFF = EXTENSION["results_verified_through"]


def manifest(kickoff="2026-10-02T20:00:00Z"):
    return {
        "schema": "nations-league-future-fixture-manifest-v1",
        "forward_shadow_model": "nations_league_v1_1",
        "forward_shadow_model_digest": model_digest(),
        "observed_at_utc": "2026-09-30T00:00:00Z",
        "fixtures": [
            {
                "fixture_id": "synthetic:queue-test",
                "competition": "UEFA Nations League",
                "edition": "2026/27",
                "evaluation_block": "NL_2026_27",
                "home_team": "Austria",
                "away_team": "Belgium",
                "kickoff_utc": kickoff,
                "status": "VERIFIED",
                "source_provenance": "synthetic offline test only",
                "source_digest": "b" * 64,
                "source_provenance_records": [
                    {"kind": "synthetic", "url": "offline:test"}
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "timestamp,state",
    [
        ("2026-10-01T17:59:59Z", "TOO_EARLY"),
        ("2026-10-01T18:00:00Z", "INITIAL_DUE"),
        ("2026-10-01T22:00:00Z", "INITIAL_DUE"),
        ("2026-10-01T22:00:01Z", "BETWEEN_WINDOWS"),
        ("2026-10-02T18:00:00Z", "REFINEMENT_DUE"),
        ("2026-10-02T19:00:00Z", "REFINEMENT_DUE"),
        ("2026-10-02T19:00:01Z", "TOO_LATE"),
        ("2026-10-02T20:00:00Z", "STARTED"),
    ],
)
def test_boundaries(timestamp, state):
    assert due_state("2026-10-02T20:00:00Z", timestamp) == state


@pytest.mark.parametrize(
    "timestamp", ["2026-10-01T20:00:00", "2026-10-01T20:00:00+02:00"]
)
def test_non_utc_rejected(timestamp):
    with pytest.raises(ValueError, match="UTC"):
        plan(manifest(), timestamp, Path("unused"))


def test_default_cli_plan_has_zero_side_effects(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest()))
    store = tmp_path / "missing" / "shadow.jsonl"
    command = [
        sys.executable,
        "scripts/nations_league_forward_queue.py",
        "--manifest",
        str(path),
        "--as-of",
        "2026-10-01T20:00:00Z",
        "--store",
        str(store),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    output = json.loads(result.stdout)
    assert output["mode"] == "PLAN_ONLY"
    assert output["summary"]["DUE NOW"] == ["synthetic:queue-test:initial"]
    assert not store.parent.exists()


def test_invalid_manifest_and_model(tmp_path):
    with pytest.raises(ValueError, match="schema"):
        plan({}, "2026-10-01T20:00:00Z", tmp_path / "store")
    old = manifest()
    old["forward_shadow_model"] = "nations_league_v1"
    with pytest.raises(ValueError, match="model"):
        plan(old, "2026-10-01T20:00:00Z", tmp_path / "store")
    with pytest.raises(ValueError, match="model digest"):
        plan(
            manifest(),
            "2026-10-01T20:00:00Z",
            tmp_path / "store",
            expected_model_digest="a" * 64,
        )
    value = manifest()
    value["fixtures"].append(deepcopy(value["fixtures"][0]))
    with pytest.raises(ValueError, match="duplicate manifest"):
        plan(value, "2026-10-01T20:00:00Z", tmp_path / "store")


def input_state(
    cutoff=READY_CUTOFF,
    fixtures=None,
    history=None,
    proof=None,
    fixture_kickoff="2026-10-01T16:00:00Z",
):
    fixture = manifest(fixture_kickoff)["fixtures"][0]
    training = training_records_from_timeline(BASE) + EXTENSION["result_rows"]
    provenance = {
        "source_digest": BASE["dataset_digest"],
        "source_provenance": "synthetic completeness proof",
    }
    return build_input_state(
        [fixture] if fixtures is None else fixtures,
        training if history is None else history,
        prediction_cutoff=cutoff,
        provenance=provenance if proof is None else proof,
        base_timeline=BASE if proof is None else None,
        result_extension=EXTENSION if proof is None else None,
        completeness_artifact=(
            completeness(EXTENSION, cutoff) if proof is None else None
        ),
    )


def test_execution_reuses_gated_runner_and_skips_duplicate(tmp_path):
    store = tmp_path / "shadow.jsonl"
    first_manifest = manifest("2026-10-01T16:00:00Z")
    first = execute(
        first_manifest,
        READY_CUTOFF,
        store,
        input_state(fixture_kickoff="2026-10-01T16:00:00Z"),
    )
    before = store.read_bytes()
    assert len(first["appended_record_ids"]) == 1
    record = read_store(store)[0]
    assert record["model_digest"] == model_digest()
    assert record["no_bet"] and not record["publication_enabled"]
    second = execute(
        first_manifest,
        READY_CUTOFF,
        store,
        input_state(fixture_kickoff="2026-10-01T16:00:00Z"),
    )
    assert second["appended_record_ids"] == []
    assert second["fixtures"][0]["status"] == "ALREADY_CAPTURED"
    assert store.read_bytes() == before
    conflict = deepcopy(record)
    conflict["source_digest"] = "c" * 64
    with pytest.raises(ValueError, match="conflicting capture"):
        plan(first_manifest, READY_CUTOFF, store, [conflict])
    with pytest.raises(ValueError, match="duplicate capture"):
        plan(first_manifest, READY_CUTOFF, store, [record, record])


def test_started_has_no_due_capture(tmp_path):
    result = plan(manifest(), "2026-10-02T20:00:00Z", tmp_path / "store")
    assert result["fixtures"][0]["state"] == "STARTED"
    assert result["summary"]["DUE NOW"] == []


def test_bad_provenance_commits_no_predictions(tmp_path):
    store = tmp_path / "shadow.jsonl"
    with pytest.raises(KeyError):
        execute(manifest(), "2026-10-01T20:00:00Z", store, {})
    assert read_store(store) == []


def test_manual_refinement_is_offline_and_preserves_initial(tmp_path, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    store = tmp_path / "shadow.jsonl"
    first_manifest = manifest("2026-10-01T16:00:00Z")
    execute(
        first_manifest,
        READY_CUTOFF,
        store,
        input_state(fixture_kickoff="2026-10-01T16:00:00Z"),
    )
    initial_line = store.read_text().splitlines()[0]
    refinement = plan(first_manifest, "2026-10-01T14:30:00Z", store, read_store(store))
    assert refinement["fixtures"][0]["state"] == "REFINEMENT_DUE"
    assert store.read_text().splitlines()[0] == initial_line
    assert [r["phase"] for r in read_store(store)] == ["initial"]
    tampered = read_store(store)
    tampered[0]["input_provenance"]["timeline_digest"] = "c" * 64
    with pytest.raises(ValueError, match="provenance digest"):
        plan(first_manifest, "2026-10-01T14:30:00Z", store, tampered)


@pytest.mark.parametrize(
    "failure",
    [
        "LIVE_RESULT_REFRESH_REQUIRED",
        "STALE_INPUT",
        "MISSING_TEAM",
        "AMBIGUOUS_IDENTITY",
    ],
)
def test_non_ready_state_cannot_append(failure, tmp_path):
    fixtures = manifest("2026-10-01T16:00:00Z")["fixtures"]
    proof = {"source_digest": "a" * 64, "source_provenance": "synthetic"}
    kwargs = {}
    run_manifest = manifest("2026-10-01T16:00:00Z")
    if failure == "LIVE_RESULT_REFRESH_REQUIRED":
        kwargs["proof"] = proof
        kwargs["fixture_kickoff"] = "2026-10-01T16:00:00Z"
    elif failure == "STALE_INPUT":
        kwargs["cutoff"] = "2026-10-01T20:00:00Z"
        kwargs["fixture_kickoff"] = "2026-10-02T20:00:00Z"
        run_manifest = manifest("2026-10-02T20:00:00Z")
    elif failure == "MISSING_TEAM":
        fixtures[0]["away_team"] = "Atlantis"
        kwargs["fixtures"] = fixtures
        run_manifest["fixtures"] = deepcopy(fixtures)
    else:
        fixtures[0]["away_team"] = "Turkey"
        kwargs["fixtures"] = fixtures
        run_manifest["fixtures"] = deepcopy(fixtures)
    snapshot = input_state(**kwargs)
    assert failure in snapshot["team_readiness"].values()
    store = tmp_path / "shadow.jsonl"
    with pytest.raises(ValueError, match="not READY"):
        execute(run_manifest, kwargs.get("cutoff", READY_CUTOFF), store, snapshot)
    assert not store.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_digest", "c" * 64),
        ("fixture_id", "synthetic:other"),
        ("kickoff_utc", "2026-10-02T21:00:00Z"),
    ],
)
def test_input_state_manifest_binding_rejected(field, value, tmp_path):
    fixtures = manifest()["fixtures"]
    fixtures[0][field] = value
    snapshot = input_state(fixtures=fixtures)
    store = tmp_path / "shadow.jsonl"
    with pytest.raises(ValueError, match="fixture/source binding"):
        execute(manifest(), READY_CUTOFF, store, snapshot)
    assert not store.exists()


def test_cutoff_and_tampered_ready_state_rejected(tmp_path):
    store = tmp_path / "shadow.jsonl"
    with pytest.raises(ValueError, match="cutoff"):
        execute(manifest(), "2026-10-01T21:00:00Z", store, input_state())
    snapshot = input_state(
        proof={"source_digest": BASE["dataset_digest"], "source_provenance": "raw"}
    )
    snapshot["team_readiness"] = {"Austria": "READY", "Belgium": "READY"}
    with pytest.raises(ValueError, match="snapshot mismatch"):
        execute(manifest(), READY_CUTOFF, store, snapshot)
    assert not store.exists()


def test_legacy_raw_cli_flags_rejected(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "scripts/nations_league_forward_queue.py",
            "--manifest",
            "unused",
            "--as-of",
            "2026-10-01T20:00:00Z",
            "--execute-offline",
            "--training",
            "raw.json",
            "--input-provenance",
            "raw-proof.json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_cli_requires_validated_input_state(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest("2026-10-01T16:00:00Z"))
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(input_state(fixture_kickoff="2026-10-01T16:00:00Z"))
    )
    store = tmp_path / "shadow.jsonl"
    command = [
        sys.executable,
        "scripts/nations_league_forward_queue.py",
        "--manifest",
        str(manifest_path),
        "--as-of",
        READY_CUTOFF,
        "--store",
        str(store),
        "--execute-offline",
    ]
    missing = subprocess.run(command, capture_output=True, text=True, check=False)
    assert missing.returncode == 2
    assert not store.exists()
    subprocess.run(
        command + ["--input-state", str(state_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert len(read_store(store)) == 1


def test_no_partial_append_if_later_due_fixture_missing_from_state(tmp_path):
    value = manifest("2026-10-01T16:00:00Z")
    second = deepcopy(value["fixtures"][0])
    second["fixture_id"] = "synthetic:second"
    value["fixtures"].append(second)
    store = tmp_path / "shadow.jsonl"
    with pytest.raises(ValueError, match="missing from input state"):
        execute(value, READY_CUTOFF, store, input_state())
    assert read_store(store) == []


def test_merged_builder1_manifest_handoff(tmp_path):
    from src.analysis.nations_league_future_fixture_intake import build_manifest

    value = build_manifest()
    assert (
        len(plan(value, "2026-09-30T18:00:00Z", tmp_path / "store")["fixtures"]) == 104
    )
