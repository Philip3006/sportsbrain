"""Tests for the offline Nations League future-fixture intake."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

from src.analysis.nations_league_future_fixture_intake import (
    OBSERVED_AT_UTC,
    build_manifest,
    canonical_future_fixture_id,
    kickoff_to_utc,
    parse_utc,
    validate_manifest,
)
from src.analysis.nations_league_v1_1 import validate_target_fixture, model_digest


def test_manifest_contains_only_unstarted_verified_public_schedule_rows():
    manifest = build_manifest()
    assert len(manifest["fixtures"]) == 104
    assert {fixture["status"] for fixture in manifest["fixtures"]} == {"VERIFIED"}
    assert manifest["predictions"] == []
    assert manifest["provider_ids_used"] == []
    assert manifest["forward_shadow_model"] == "nations_league_v1_1"
    assert manifest["forward_shadow_model_digest"] == model_digest()
    assert all(fixture["source_provenance_records"] for fixture in manifest["fixtures"])


def test_verified_manifest_record_is_accepted_by_frozen_forward_target_contract():
    fixture = build_manifest()["fixtures"][0]
    validate_target_fixture(fixture)


def test_identity_is_deterministic_and_provider_independent():
    values = {
        "competition": "UEFA Nations League",
        "edition": "2026/27",
        "stage": "league_phase",
        "group": "A1",
        "home_team": "France",
        "away_team": "Italy",
        "kickoff_utc": "2026-11-12T19:45:00Z",
    }
    first = canonical_future_fixture_id(**values)
    second = canonical_future_fixture_id(**values)
    assert first == second
    assert "provider" not in first
    assert canonical_future_fixture_id(**{**values, "away_team": "Belgium"}) != first


def test_cet_source_kickoff_is_normalized_to_utc():
    assert kickoff_to_utc("2026-10-01", "20:45") == "2026-10-01T19:45:00Z"
    assert kickoff_to_utc("2026-10-02", "16:00") == "2026-10-02T15:00:00Z"


def test_capture_windows_match_frozen_forward_shadow_windows():
    fixture = next(
        item
        for item in build_manifest()["fixtures"]
        if item["home_team"] == "Germany" and item["away_team"] == "Serbia"
    )
    kickoff = parse_utc(fixture["kickoff_utc"])
    initial = fixture["capture_windows"]["initial"]
    refinement = fixture["capture_windows"]["refinement"]
    assert parse_utc(initial["start_utc"]) == kickoff - timedelta(hours=26)
    assert parse_utc(initial["end_utc"]) == kickoff - timedelta(hours=22)
    assert parse_utc(refinement["start_utc"]) == kickoff - timedelta(minutes=120)
    assert parse_utc(refinement["end_utc"]) == kickoff - timedelta(minutes=60)
    assert parse_utc(refinement["target_utc"]) == kickoff - timedelta(minutes=90)


def test_duplicate_fixture_identity_fails_closed():
    manifest = build_manifest()
    manifest["fixtures"].append(deepcopy(manifest["fixtures"][0]))
    with pytest.raises(ValueError, match="duplicate fixture identity"):
        validate_manifest(manifest)


def test_unresolved_kickoff_cannot_be_marked_verified():
    manifest = build_manifest()
    fixture = manifest["fixtures"][0]
    fixture["kickoff_utc"] = None
    with pytest.raises((TypeError, ValueError), match="kickoff_utc is required"):
        validate_manifest(manifest)


def test_started_fixture_cannot_feed_verified_future_campaign():
    manifest = build_manifest(observed_at_utc="2026-10-01T18:00:00Z")
    fixture = manifest["fixtures"][0]
    assert fixture["status"] == "STARTED"
    fixture["status"] = "VERIFIED"
    with pytest.raises(ValueError, match="started fixture cannot be VERIFIED"):
        validate_manifest(manifest)


def test_source_provenance_is_required():
    manifest = build_manifest()
    manifest["fixtures"][0]["source_provenance_records"] = []
    with pytest.raises(ValueError, match="source provenance records required"):
        validate_manifest(manifest)


def test_manifest_observation_clock_is_explicit_utc():
    manifest = build_manifest()
    assert manifest["observed_at_utc"] == OBSERVED_AT_UTC
    assert parse_utc(manifest["observed_at_utc"]).tzinfo is not None
