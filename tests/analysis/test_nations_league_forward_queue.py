"""Offline queue boundaries, canonical handoff and append-only execution."""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.nations_league_forward_queue import due_state, execute, plan, read_store
from src.analysis.nations_league_v1 import model_digest


def manifest():
    return {
        "schema": "nations-league-future-fixture-manifest-v1",
        "forward_shadow_model": "nations_league_v1",
        "observed_at_utc": "2026-10-01T00:00:00Z",
        "fixtures": [
            {
                "fixture_id": "synthetic:queue-test",
                "competition": "UEFA Nations League",
                "edition": "2026/27",
                "evaluation_block": "NL_2026_27",
                "home_team": "Austria",
                "away_team": "Belgium",
                "kickoff_utc": "2026-10-02T20:00:00Z",
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


def test_execution_reuses_frozen_runner_and_skips_duplicate(tmp_path):
    store = tmp_path / "shadow.jsonl"
    provenance = {
        "synthetic:queue-test": {
            "timeline_digest": "a" * 64,
            "fixture_source_digest": "b" * 64,
            "training_cutoff": "2026-10-01T20:00:00Z",
        }
    }
    first = execute(manifest(), "2026-10-01T20:00:00Z", store, [], provenance)
    before = store.read_bytes()
    assert len(first["appended_record_ids"]) == 1
    record = read_store(store)[0]
    assert record["model_digest"] == model_digest()
    assert record["no_bet"] and not record["publication_enabled"]
    second = execute(manifest(), "2026-10-01T21:00:00Z", store, [], {})
    assert second["appended_record_ids"] == []
    assert second["fixtures"][0]["status"] == "ALREADY_CAPTURED"
    assert store.read_bytes() == before
    conflict = deepcopy(record)
    conflict["source_digest"] = "c" * 64
    with pytest.raises(ValueError, match="conflicting capture"):
        plan(manifest(), "2026-10-01T20:00:00Z", store, [conflict])
    with pytest.raises(ValueError, match="duplicate capture"):
        plan(manifest(), "2026-10-01T20:00:00Z", store, [record, record])


def test_started_has_no_due_capture(tmp_path):
    result = plan(manifest(), "2026-10-02T20:00:00Z", tmp_path / "store")
    assert result["fixtures"][0]["state"] == "STARTED"
    assert result["summary"]["DUE NOW"] == []


def test_bad_provenance_commits_no_predictions(tmp_path):
    store = tmp_path / "shadow.jsonl"
    with pytest.raises(KeyError):
        execute(manifest(), "2026-10-01T20:00:00Z", store, [], {})
    assert read_store(store) == []


def test_manual_refinement_is_offline_and_preserves_initial(tmp_path, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    store = tmp_path / "shadow.jsonl"
    inputs = {
        "synthetic:queue-test": {
            "timeline_digest": "a" * 64,
            "fixture_source_digest": "b" * 64,
            "training_cutoff": "2026-10-01T20:00:00Z",
        }
    }
    execute(manifest(), "2026-10-01T20:00:00Z", store, [], inputs)
    initial_line = store.read_text().splitlines()[0]
    inputs["synthetic:queue-test"]["training_cutoff"] = "2026-10-02T18:30:00Z"
    execute(manifest(), "2026-10-02T18:30:00Z", store, [], inputs)
    assert store.read_text().splitlines()[0] == initial_line
    assert [r["phase"] for r in read_store(store)] == ["initial", "refinement"]
    tampered = read_store(store)
    tampered[0]["input_provenance"]["timeline_digest"] = "c" * 64
    with pytest.raises(ValueError, match="provenance digest"):
        plan(manifest(), "2026-10-02T18:30:00Z", store, tampered)
