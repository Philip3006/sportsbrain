import json
from pathlib import Path

from src.analysis.nations_league_historical_odds_plan import (
    HISTORICAL_COVERAGE_BOUNDARY,
    build_plan,
    classify_fixtures,
)


def _record(fixture_id, kickoff, *, admin=False):
    return {
        "fixture_id": fixture_id,
        "edition": "2024/25",
        "home_team": "Home",
        "away_team": "Away",
        "kickoff_utc": kickoff,
        "record_digest": f"digest-{fixture_id}",
        "administrative_exception": {"reason": "test"} if admin else None,
        "result_safe_status": None if admin else "bounded_from_verified_kickoff",
        "result_safe_available_at": None if admin else "2024-01-01T06:00:00Z",
    }


def _test_plan(rows):
    from src.analysis import nations_league_historical_odds_plan as module

    return build_plan(
        {"schema_version": module.TIMELINE_SCHEMA, "records": rows},
        timeline_head_sha="test",
        timeline_digest="test",
    )


def test_classification_keeps_post_coverage_admin_eligible_for_odds():
    rows = [_record("before", "2022-06-10T18:45:00Z"), _record("after", "2022-06-11T18:45:00Z"), _record("admin", "2022-06-12T18:45:00Z", admin=True)]
    result = classify_fixtures(rows, coverage_boundary=HISTORICAL_COVERAGE_BOUNDARY)
    assert result[0]["classification"] == "BEFORE_PROVIDER_COVERAGE"
    assert result[1]["classification"] == "AFTER_PROVIDER_COVERAGE"
    assert result[2]["classification"] == "AFTER_PROVIDER_COVERAGE_ADMINISTRATIVE_EXCEPTION"
    assert result[2]["historical_odds_eligible"] is True


def test_bulk_snapshot_deduplication_groups_identical_timestamps():
    plan = _test_plan([_record("one", "2022-06-12T18:45:00Z"), _record("two", "2022-06-12T18:45:00Z")])
    initial = plan["plans"]["PREDICTION_ONLY"]["INITIAL"]
    assert initial["fixtures_covered"] == 2
    assert initial["unique_http_requests"] == 1
    assert initial["deduplication_savings"] == {"requests": 1, "credits": 10}
    assert initial["requests"][0]["fixture_ids"] == ["one", "two"]


def test_initial_phase_excludes_snapshot_before_provider_boundary():
    plan = _test_plan([_record("one", "2022-06-11T18:45:00Z")])
    initial = plan["plans"]["PREDICTION_ONLY"]["INITIAL"]
    assert initial["fixtures_covered"] == 0
    assert initial["unique_http_requests"] == 0
    assert initial["phase_exclusions"][0]["fixture_id"] == "one"
    refinement = plan["plans"]["PREDICTION_ONLY"]["REFINEMENT"]
    assert refinement["fixtures_covered"] == 1


def test_request_plan_is_deterministic():
    rows = [_record("one", "2022-06-11T18:45:00Z")]
    assert _test_plan(rows)["request_plan_digest"] == _test_plan(rows)["request_plan_digest"]


def test_full_plan_reuses_identical_timestamp_across_phases():
    plan = _test_plan(
        [
            _record("one", "2022-06-11T18:45:00Z"),
            _record("two", "2022-06-12T18:45:00Z"),
        ]
    )
    total = plan["plan_totals"]["FULL_RESEARCH"]
    assert total["unique_http_requests"] == 4
    assert total["estimated_credits"] == 40
    assert total["shared_bulk_requests"][1]["phases"] == ["CLOSING", "INITIAL"]


def test_committed_final_plan_binds_pr215_and_has_no_unresolved_fixture():
    root = Path(__file__).resolve().parents[2]
    artifact = json.loads(
        (root / "results/audits/nations_league_historical_odds_request_plan_20260929.json").read_text()
    )
    assert artifact["status_marker"] == "NL_HISTORICAL_ODDS_FINAL_PLAN_READY"
    assert artifact["timeline"]["head_sha"] == "065c6b40eb9911df3703d2e3079730a556136ee3"
    assert artifact["timeline"]["dataset_digest"] == "2c60c6b823b0cae948947fffe2a0e3456495c1fc5d510379ae95690e1c3fa6ef"
    assert artifact["fixture_universe"] == {
        "total": 512,
        "before_provider_coverage": 233,
        "after_provider_coverage": 279,
        "administrative_exceptions": 2,
        "genuinely_unusable": 0,
        "historical_odds_eligible": 279,
    }
    assert artifact["plan_totals"]["PREDICTION_ONLY"]["estimated_credits"] == 1470
    assert artifact["plan_totals"]["FULL_RESEARCH"]["estimated_credits"] == 1820
    assert all(item["classification"] != "UNUSABLE" for item in artifact["fixture_classifications"])
