from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.analysis.nations_league_model_lifecycle import establish_initial_active_release
from src.notifications.nations_league_live_public import (
    NationsLeagueLivePublicError,
    build_live_public_nations_league,
    validate_live_public_nations_league,
)

ROOT = Path(__file__).parents[2]
SOURCE_SHA = "e4d7c579ce347e385e08817c5083527faf6bcc87"


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
        _records(), active_release=_active_release(), evidence_binding=_binding()
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
        _records(), active_release=_active_release(), evidence_binding=_binding()
    )
    ireland = next(
        item
        for item in output["fixtures"]
        if item["source_identity"]["home_team"] == "Republic of Ireland"
    )
    assert ireland["canonical_identity"]["home_team"] == "Ireland"


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
        [*records, refined], active_release=_active_release(), evidence_binding=binding
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
            _records(), active_release=_active_release(), evidence_binding=binding
        )
