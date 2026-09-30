"""Offline due-state, planning, and idempotency tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.analysis.nations_league_forward_queue import (
    QueueError,
    build_capture_plan,
    classify_fixture,
    execute_plan,
    model_digest,
    render_summary,
    validate_manifest,
)

SOURCE_DIGEST = "b" * 64
TIMELINE_DIGEST = "a" * 64
INPUT_DIGEST = "c" * 64


def _fixture(**overrides):
    fixture = {
        "fixture_id": "uefa-nl:synthetic-001",
        "edition": "2026/27",
        "evaluation_block": "NL_2026_27",
        "home_team": "Austria",
        "away_team": "Belgium",
        "kickoff_utc": "2026-10-02T20:00:00Z",
        "competition": "UEFA Nations League",
        "source_provenance": "synthetic-offline-fixture",
        "source_digest": SOURCE_DIGEST,
        "status": "VERIFIED",
        "predictions": [],
    }
    fixture.update(overrides)
    return fixture


def _manifest(*fixtures):
    return {"fixtures": list(fixtures or (_fixture(),))}


def _provenance():
    return {
        "timeline_digest": TIMELINE_DIGEST,
        "fixture_source_digest": SOURCE_DIGEST,
        "input_snapshot_digest": INPUT_DIGEST,
    }


def _training():
    return [
        {
            "fixture_id": "uefa-nl:training-001",
            "edition": "2024/25",
            "evaluation_block": "NL_2024_25",
            "home_team": "Austria",
            "away_team": "Belgium",
            "kickoff_utc": "2026-08-01T20:00:00Z",
            "competition": "UEFA Nations League",
            "source_provenance": "offline-training",
            "source_digest": "d" * 64,
            "result_safe_available_at": "2026-08-01T23:00:00Z",
            "home_score": 2,
            "away_score": 1,
        }
    ]


@pytest.mark.parametrize(
    ("as_of", "status"),
    [
        ("2026-10-01T18:00:00Z", "INITIAL_DUE"),  # exactly 26h
        ("2026-10-01T22:00:00Z", "INITIAL_DUE"),  # exactly 22h
        ("2026-10-02T18:00:00Z", "REFINEMENT_DUE"),  # exactly 120m
        ("2026-10-02T19:00:00Z", "REFINEMENT_DUE"),  # exactly 60m
        ("2026-10-02T20:00:00Z", "STARTED"),
    ],
)
def test_due_state_boundaries_are_inclusive(as_of, status):
    assert classify_fixture(_fixture(), as_of).status == status


def test_between_windows_too_early_and_too_late_are_distinct():
    fixture = _fixture()
    assert classify_fixture(fixture, "2026-10-01T23:00:00Z").status == "BETWEEN_WINDOWS"
    assert classify_fixture(fixture, "2026-10-01T00:00:00Z").status == "TOO_EARLY"
    assert classify_fixture(fixture, "2026-10-02T19:30:00Z").status == "TOO_LATE"


def test_plan_only_is_deterministic_and_side_effect_free(tmp_path: Path):
    store = tmp_path / "shadow.jsonl"
    plan = build_capture_plan(
        _manifest(), as_of="2026-10-01T20:00:00Z", destination_shadow_store=str(store)
    )
    assert not store.exists()
    assert plan["plans"][0]["lifecycle"] == "INITIAL"
    assert plan["plans"][0]["prediction_cutoff"] == "2026-10-01T20:00:00Z"
    assert plan["plans"][0]["model_digest"] == model_digest()
    assert plan["plans"][0]["no_bet"] is True
    assert plan["plans"][0]["signal_status"] == "SHADOW_ONLY"
    assert "input_snapshot_digest" in plan["plans"][0]["required_input_provenance"]
    assert render_summary(plan).startswith("NEXT INITIAL:")


def test_duplicate_capture_is_already_captured(tmp_path: Path):
    store = tmp_path / "shadow.jsonl"
    first = build_capture_plan(
        _manifest(), as_of="2026-10-01T20:00:00Z", destination_shadow_store=str(store)
    )
    result = execute_plan(
        _manifest(), first, training_records=_training(), input_provenance=_provenance()
    )
    assert result == [
        {
            "fixture_id": _fixture()["fixture_id"],
            "phase": "initial",
            "status": "CAPTURED",
        }
    ]
    second = build_capture_plan(
        _manifest(),
        as_of="2026-10-01T20:00:00Z",
        destination_shadow_store=str(store),
        existing_records=[json.loads(line) for line in store.read_text().splitlines()],
    )
    assert second["plans"][0]["status"] == "ALREADY_CAPTURED"
    assert second["summary"]["captured"] == [_fixture()["fixture_id"] + ":INITIAL"]
    assert (
        execute_plan(
            _manifest(),
            second,
            training_records=_training(),
            input_provenance=_provenance(),
        )[0]["status"]
        == "ALREADY_CAPTURED"
    )
    assert len(store.read_text().splitlines()) == 1


def test_conflicting_duplicate_fails_closed(tmp_path: Path):
    store = tmp_path / "shadow.jsonl"
    plan = build_capture_plan(
        _manifest(), as_of="2026-10-01T20:00:00Z", destination_shadow_store=str(store)
    )
    execute_plan(
        _manifest(), plan, training_records=_training(), input_provenance=_provenance()
    )
    conflicting = json.loads(store.read_text())
    conflicting["model_digest"] = "e" * 64
    store.write_text(json.dumps(conflicting) + "\n")
    with pytest.raises(QueueError, match="conflicting duplicate"):
        execute_plan(
            _manifest(),
            plan,
            training_records=_training(),
            input_provenance=_provenance(),
        )
    with pytest.raises(QueueError, match="conflicting duplicate"):
        build_capture_plan(
            _manifest(),
            as_of="2026-10-01T20:00:00Z",
            destination_shadow_store=str(store),
            existing_records=[conflicting],
        )


def test_invalid_manifest_duplicate_non_utc_started_and_wrong_digest_fail_closed():
    with pytest.raises(QueueError, match="duplicate fixture_id"):
        validate_manifest(_manifest(_fixture(), deepcopy(_fixture())))
    with pytest.raises(QueueError, match="UTC timezone"):
        build_capture_plan(
            _manifest(_fixture(kickoff_utc="2026-10-02T20:00:00")),
            as_of="2026-10-01T20:00:00Z",
        )
    with pytest.raises(QueueError, match="fixture is not VERIFIED"):
        validate_manifest(_manifest(_fixture(status="STARTED")))
    with pytest.raises(QueueError, match="not the frozen"):
        build_capture_plan(
            _manifest(), as_of="2026-10-01T20:00:00Z", model_digest_value="f" * 64
        )


def test_execute_requires_local_provenance_and_preserves_safety(tmp_path: Path):
    store = tmp_path / "shadow.jsonl"
    plan = build_capture_plan(
        _manifest(), as_of="2026-10-01T20:00:00Z", destination_shadow_store=str(store)
    )
    with pytest.raises(QueueError, match="input provenance missing"):
        execute_plan(
            _manifest(), plan, training_records=_training(), input_provenance={}
        )
    execute_plan(
        _manifest(), plan, training_records=_training(), input_provenance=_provenance()
    )
    record = json.loads(store.read_text())
    assert record["shadow"] is True
    assert record["no_bet"] is True
    assert record["publication_enabled"] is False
    assert record["ledger_mutation"] is False
