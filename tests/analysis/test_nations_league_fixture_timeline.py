import json
from datetime import datetime
from pathlib import Path

import pytest

from src.analysis.nations_league_fixture_timeline import (
    DEFAULT_CONTRACTS,
    DEFAULT_COVERAGE,
    DEFAULT_RESULTS,
    DEFAULT_SCHEDULE,
    DEFAULT_TIMELINE,
    build_timeline,
    canonical_digest,
    load_and_validate,
    validate_causal_cutoff_order,
    validate_timeline,
)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _minimal_inputs():
    contracts = {
        "editions": {
            "2020/21": {
                "groups": {"A4": ["Switzerland", "Spain", "Ukraine", "Germany"]}
            },
            "2024/25": {
                "groups": {
                    "A1": ["France", "Italy", "Spain", "Croatia"],
                    "C2": ["Romania", "Kosovo", "Cyprus", "Lithuania"],
                }
            },
        }
    }
    match = {
        "date": "2024-09-06",
        "home_team": "France",
        "away_team": "Italy",
        "home_score": 1,
        "away_score": 0,
        "neutral": False,
        "edition": "2024/25",
        "validation_period": "2024/25",
    }
    results = {
        "normalization_version": "sportsbrain-nl-team-aliases-v1",
        "snapshot_digest": canonical_digest({"matches": [match]}),
        "upstream_url": "https://example.invalid/results",
        "matches": [match],
    }
    schedule_row = {
        "edition": "2024/25",
        "scheduled_date": "2024-09-06",
        "scheduled_local_time": "20:45",
        "timezone": "Europe/Paris",
        "group": "A1",
        "home_team": "France",
        "away_team": "Italy",
        "source_url": "https://example.invalid/schedule",
        "source_pdf_sha256": "a" * 64,
        "schedule_status": "official_fixture_candidate",
    }
    schedule = {"schedule_rows": [schedule_row]}
    return results, contracts, schedule


def test_committed_timeline_has_512_unique_rows_and_explicit_partial_kickoff_coverage():
    timeline, coverage = load_and_validate(DEFAULT_TIMELINE, DEFAULT_COVERAGE)
    assert len(timeline["records"]) == 512
    assert coverage["identity_crosswalk_complete"] is True
    assert coverage["unique_fixture_ids"] == 512
    assert coverage["ambiguous_identity_conflicts"] == 0
    assert coverage["status"] == "NL_FIXTURE_TIMELINE_PARTIAL"
    assert coverage["verified_utc_kickoffs"] < 512
    assert coverage["unresolved_count"] > 0


def test_exact_edition_participant_and_date_match_produces_dst_aware_utc_and_safe_bound():
    results, contracts, schedule = _minimal_inputs()
    timeline, coverage = build_timeline(results, contracts, schedule)
    record = timeline["records"][0]
    assert record["kickoff_utc"] == "2024-09-06T18:45:00Z"
    assert record["local_kickoff"]["timezone"] == "Europe/Paris"
    assert record["result_safe_available_at"] == "2024-09-07T00:45:00Z"
    assert datetime.fromisoformat(
        record["result_safe_available_at"].replace("Z", "+00:00")
    ) > datetime.fromisoformat(record["kickoff_utc"].replace("Z", "+00:00"))
    assert record["source_fixture_id"] is None
    assert coverage["status"] == "NL_FIXTURE_TIMELINE_PARTIAL"


def test_pair_or_date_only_never_crosswalks_and_mismatch_is_explicit():
    results, contracts, schedule = _minimal_inputs()
    schedule["schedule_rows"][0]["scheduled_date"] = "2024-09-07"
    timeline, _ = build_timeline(results, contracts, schedule)
    record = timeline["records"][0]
    assert record["kickoff_utc"] is None
    assert record["provenance_status"] == "date_conflict_unresolved"
    assert record["unresolved_schedule_candidates"][0]["date"] == "2024-09-07"


def test_ambiguous_exact_schedule_crosswalk_fails_closed():
    results, contracts, schedule = _minimal_inputs()
    schedule["schedule_rows"].append(dict(schedule["schedule_rows"][0]))
    timeline, coverage = build_timeline(results, contracts, schedule)
    assert timeline["records"][0]["kickoff_utc"] is None
    assert timeline["records"][0]["unresolved_reason"] == "multiple_exact_schedule_rows"
    assert coverage["status"] == "NL_FIXTURE_TIMELINE_PARTIAL"


@pytest.mark.parametrize(
    "match,expected_status",
    [
        (
            {
                "date": "2020-11-17",
                "home_team": "Switzerland",
                "away_team": "Ukraine",
                "edition": "2020/21",
            },
            "unresolved_award_availability",
        ),
        (
            {
                "date": "2024-11-15",
                "home_team": "Romania",
                "away_team": "Kosovo",
                "edition": "2024/25",
            },
            "unresolved_award_availability",
        ),
    ],
)
def test_awarded_fixtures_have_no_fabricated_result_safe_time(match, expected_status):
    results, contracts, schedule = _minimal_inputs()
    row = dict(results["matches"][0])
    row.update(
        match,
        home_score=3,
        away_score=0,
        validation_period=match["edition"],
        neutral=False,
    )
    results["matches"] = [row]
    results["snapshot_digest"] = canonical_digest({"matches": results["matches"]})
    timeline, _ = build_timeline(results, contracts, schedule)
    record = timeline["records"][0]
    assert record["status"] == "administratively_awarded"
    assert record["result_safe_available_at"] is None
    assert record["result_safe_status"] == expected_status


def test_reference_projection_keeps_closing_benchmark_out_of_prediction_inputs():
    timeline, _ = build_timeline(*_minimal_inputs())
    record = timeline["records"][0]
    projection = record["reference_projection"]
    assert projection["initial_t_minus_24h_at"] == "2024-09-05T18:45:00Z"
    assert projection["refinement_t_minus_90m_at"] == "2024-09-06T17:15:00Z"
    assert projection["closing_benchmark_capture_at"] is None
    assert (
        projection["closing_benchmark_status"]
        == "kickoff_boundary_only_not_an_observed_capture"
    )
    assert projection["closing_odds_prediction_input"] is False


def test_2022_06_11_reference_cutoff_is_classified_per_fixture():
    _, coverage = load_and_validate(DEFAULT_TIMELINE, DEFAULT_COVERAGE)
    cutoff = coverage["reference_cutoff"]
    assert cutoff["at"] == "2022-06-11T00:25:00Z"
    assert cutoff["fixtures_on_date"] == 10
    assert all(row["kickoff_utc"] is not None for row in cutoff["fixtures"])
    assert all(
        row["relative_to_2022_06_11T00_25Z"] == "future" for row in cutoff["fixtures"]
    )


def test_b1_strict_causal_cutoff_order_and_equality_rejection():
    validate_causal_cutoff_order(
        "2022-06-10T20:00:00Z",
        "2022-06-10T21:00:00Z",
        "2022-06-10T23:00:00Z",
        "2022-06-11T00:25:00Z",
    )
    with pytest.raises(ValueError, match="required ordering"):
        validate_causal_cutoff_order(
            "2022-06-10T21:00:00Z",
            "2022-06-10T21:00:00Z",
            "2022-06-10T23:00:00Z",
            "2022-06-11T00:25:00Z",
        )


def test_validator_rejects_future_or_non_postkickoff_result_safe_bound():
    timeline, coverage = build_timeline(*_minimal_inputs())
    record = timeline["records"][0]
    record["result_safe_available_at"] = record["kickoff_utc"]
    record["record_digest"] = canonical_digest(
        {k: v for k, v in record.items() if k != "record_digest"}
    )
    timeline["dataset_digest"] = canonical_digest(
        {k: v for k, v in timeline.items() if k != "dataset_digest"}
    )
    coverage["dataset_digest"] = timeline["dataset_digest"]
    coverage["coverage_digest"] = canonical_digest(
        {k: v for k, v in coverage.items() if k != "coverage_digest"}
    )
    with pytest.raises(ValueError, match="after kickoff"):
        validate_timeline(timeline, coverage)
