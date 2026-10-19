import pytest

from src.research.nations_league_web_odds_pilot import (
    EVIDENCE_STATUSES,
    classify_timing,
    normalize_odds,
    phase_record,
    target_timestamp,
)


FIXTURE = {
    "fixture_id": "fixture-1",
    "edition": "2024/25",
    "group": "A1",
    "tier": "A",
    "home_team": "Home",
    "away_team": "Away",
    "kickoff_utc": "2024-10-10T18:45:00Z",
}


def test_targets_are_utc_and_phase_specific():
    assert target_timestamp(FIXTURE["kickoff_utc"], "INITIAL") == "2024-10-09T18:45:00Z"
    assert target_timestamp(FIXTURE["kickoff_utc"], "REFINEMENT") == "2024-10-10T17:15:00Z"
    assert target_timestamp(FIXTURE["kickoff_utc"], "CLOSING_BENCHMARK") == FIXTURE["kickoff_utc"]


def test_timing_requires_explicit_observation_timestamp():
    status, offset = classify_timing("INITIAL", "2024-10-09T18:45:00Z", None)
    assert status == "UNTIMESTAMPED_HISTORICAL"
    assert offset is None


def test_timing_accepts_only_the_requested_window():
    assert classify_timing("REFINEMENT", "2024-10-10T17:15:00Z", "2024-10-10T17:00:00Z")[0] == "VERIFIED_NEAR_TARGET"
    assert classify_timing("REFINEMENT", "2024-10-10T17:15:00Z", "2024-10-10T15:00:00Z")[0] == "OPENING_ONLY"
    assert classify_timing("REFINEMENT", "2024-10-10T17:15:00Z", "2024-10-10T18:00:00Z")[0] == "UNAVAILABLE"


def test_odds_normalization_is_explicit_and_deterministic():
    result = normalize_odds([2.0, 3.0, 4.0])
    assert result["raw_implied"]["home"] == pytest.approx(0.5)
    assert sum(result["normalized_no_vig"].values()) == pytest.approx(1.0)


def test_invalid_odds_are_rejected():
    with pytest.raises(ValueError):
        normalize_odds([2.0, 1.0, 4.0])


def test_untimestamped_odds_are_never_promoted_to_target_evidence():
    row = phase_record(FIXTURE, "INITIAL", {"odds_decimal": [2.0, 3.0, 4.0], "evidence_status": "UNTIMESTAMPED_HISTORICAL"})
    assert row["evidence_status"] == "UNTIMESTAMPED_HISTORICAL"
    assert row["raw_implied_probabilities"] is not None
    assert row["timing_relation"] == "not_demonstrated"


def test_status_vocabulary_is_closed():
    assert "UNAVAILABLE" in EVIDENCE_STATUSES
    assert "IDENTITY_AMBIGUOUS" in EVIDENCE_STATUSES
