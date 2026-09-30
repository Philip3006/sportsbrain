from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.analysis.nations_league_live_runtime import (
    build_fresh_input_state,
)
from src.analysis.nations_league_model_lifecycle import establish_initial_active_release
from src.notifications.nations_league_live_public import (
    NationsLeagueLivePublicError,
    build_live_public_nations_league,
    validate_live_public_nations_league,
)

ROOT = Path(__file__).parents[2]
SOURCE_SHA = "267a5df80ee326cfe41ee6ddc27d3e8c547ab222"


def _json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _active_release():
    _, release = establish_initial_active_release(
        _json("results/research/nations_league_v1_1_input_state_20260930T200124Z.json"),
        source_release_sha=SOURCE_SHA,
    )
    return release


def _records() -> list[dict]:
    return _json(
        "results/research/nations_league_v1_1_forward_campaign_20260930T200124Z.json"
    )["records"]


def _binding() -> dict:
    return _json("results/audits/nations_league_v1_1_live_evidence_binding.json")


def test_seven_immutable_real_records_are_live_serializable_without_sample_gate():
    output = build_live_public_nations_league(
        _records(), active_release=_active_release(), evidence_binding=_binding(),
        as_of="2026-09-30T20:01:24Z"
    )
    assert output["status"] == "LIVE"
    assert output["publication_enabled"] is True
    assert output["no_bet"] is True
    assert output["betting_enabled"] is False
    assert output["ledger_mutation"] is False
    assert output["fixture_count"] == 7
    assert output["model_release"]["release_id"] == _active_release().release_id
    source = _records()[0]
    projected = next(
        item for item in output["fixtures"] if item["fixture_id"] == source["fixture_id"]
    )
    assert projected["probabilities"] == source["probabilities"]
    assert validate_live_public_nations_league(output) == output


def test_source_and_canonical_team_identity_are_both_preserved_for_alias_binding():
    output = build_live_public_nations_league(
        _records(), active_release=_active_release(), evidence_binding=_binding(),
        as_of="2026-09-30T20:01:24Z"
    )
    ireland = next(
        item
        for item in output["fixtures"]
        if item["source_identity"]["home_team"] == "Republic of Ireland"
    )
    assert ireland["canonical_identity"]["home_team"] == "Ireland"


def test_zero_fixture_view_transitions_once_and_is_stable_after_expiry():
    records = _records()
    release = _active_release()
    binding = _binding()
    before = build_live_public_nations_league(
        records,
        active_release=release,
        evidence_binding=binding,
        as_of="2026-10-01T18:44:59Z",
    )
    first = build_live_public_nations_league(
        records,
        active_release=release,
        evidence_binding=binding,
        as_of="2026-10-01T18:45:00Z",
    )
    later = [
        build_live_public_nations_league(
            records,
            active_release=release,
            evidence_binding=binding,
            as_of=cutoff,
        )
        for cutoff in ("2026-10-01T19:00:00Z", "2026-10-01T19:15:00Z")
    ]
    assert before["fixture_count"] == 7
    assert first["fixture_count"] == 0
    assert first["fixtures"] == []
    assert first["updated_at"] == "2026-10-01T18:45:00Z"
    assert len(first["audit_history"]) == 7
    assert validate_live_public_nations_league(first) == first
    assert all(bundle == first for bundle in later)
    assert all(bundle["public_digest"] == first["public_digest"] for bundle in later)
    assert [
        row["source_prediction_record_ids"]
        for row in first["audit_history"]
    ] == [[record["record_id"]] for record in sorted(records, key=lambda item: item["fixture_id"])]

    changed = deepcopy(records)
    changed[0]["probabilities"]["home"] += 0.001
    changed[0]["probabilities"]["away"] -= 0.001
    changed_bundle = build_live_public_nations_league(
        changed,
        active_release=release,
        evidence_binding=binding,
        as_of="2026-10-01T19:15:00Z",
    )
    assert changed_bundle["public_digest"] != first["public_digest"]


def test_refinement_replaces_initial_only_in_public_view_and_keeps_audit_history():
    records = _records()
    refined = deepcopy(records[0])
    refined["record_id"] = "a" * 64
    refined["phase"] = "refinement"
    refined["prediction_timestamp"] = "2026-10-01T17:15:00Z"
    binding = _binding()
    original = next(
        row
        for row in binding["source_records"]
        if row["source_prediction_record_id"] == records[0]["record_id"]
    )
    refinement_binding = deepcopy(original)
    refinement_binding["source_prediction_record_id"] = refined["record_id"]
    refinement_binding["phase"] = "refinement"
    binding["source_records"].append(refinement_binding)
    body = {key: value for key, value in binding.items() if key != "binding_digest"}
    import hashlib

    binding["binding_digest"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    output = build_live_public_nations_league(
        [*records, refined], active_release=_active_release(), evidence_binding=binding,
        as_of="2026-09-30T20:01:24Z"
    )
    fixture = next(item for item in output["fixtures"] if item["fixture_id"] == refined["fixture_id"])
    assert fixture["phase"] == "refinement"
    audit = next(item for item in output["audit_history"] if item["fixture_id"] == refined["fixture_id"])
    assert audit["source_prediction_record_ids"] == [records[0]["record_id"], refined["record_id"]]


def test_state_or_source_substitution_fails_closed():
    binding = _binding()
    binding["source_records"][0]["trained_state_digest"] = "b" * 64
    with pytest.raises(NationsLeagueLivePublicError, match="binding digest"):
        build_live_public_nations_league(
            _records(), active_release=_active_release(), evidence_binding=binding,
            as_of="2026-09-30T20:01:24Z"
        )


def test_future_live_refinement_carries_its_own_release_and_keeps_initial_audit():
    manifest = _json("results/audits/nations_league_forward_fixture_manifest.json")
    base = _json("results/research/nations_league_fixture_timeline_v1.json")
    extension = _json(
        "results/research/nations_league_v1_1_result_extension_20260930T200124Z.json"
    )
    state = build_fresh_input_state(
        manifest,
        base,
        extension,
        prediction_cutoff="2026-09-30T20:01:24.572945Z",
    )
    release = _active_release()
    fixture_id = _records()[0]["fixture_id"]
    refinement = deepcopy(_records()[0])
    refinement.update(
        {
            "status": "LIVE",
            "phase": "refinement",
            "prediction_timestamp": "2026-09-30T17:15:00Z",
            "betting_enabled": False,
            "publication_enabled": True,
            "ledger_mutation": False,
            "model_release_id": release.release_id,
            "algorithm_digest": release.snapshot.algorithm_digest,
            "training_data_digest": release.snapshot.training_data_digest,
            "trained_state_digest": release.snapshot.trained_state_digest,
            "training_cutoff": release.snapshot.training_cutoff,
            "input_snapshot_digest": state["input_snapshot_digest"],
        }
    )
    import hashlib

    identity = {
        "fixture_id": fixture_id,
        "phase": "refinement",
        "release_id": release.release_id,
        "input_snapshot_digest": state["input_snapshot_digest"],
    }
    refinement["record_id"] = hashlib.sha256(
        json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    output = build_live_public_nations_league(
        [*_records(), refinement], active_release=release, evidence_binding=_binding(),
        as_of="2026-09-30T20:01:24Z"
    )
    fixture = next(row for row in output["fixtures"] if row["fixture_id"] == fixture_id)
    assert fixture["phase"] == "refinement"
    assert fixture["model_release"]["release_id"] == release.release_id
    audit = next(row for row in output["audit_history"] if row["fixture_id"] == fixture_id)
    assert len(audit["source_prediction_record_ids"]) == 2
    assert validate_live_public_nations_league(output) == output
