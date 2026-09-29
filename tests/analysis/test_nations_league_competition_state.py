import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.analysis.nations_league_competition_state import (
    DEFAULT_CONTRACTS,
    DEFAULT_COVERAGE,
    DEFAULT_DATASET,
    DEFAULT_SOURCE,
    build_dataset,
    canonical_digest,
    canonical_fixture_id,
    load_and_validate,
    validate_dataset,
)


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _source(matches):
    payload = {
        "schema_version": "uefa-nl-canonical-result-source-v1",
        "competition": "UEFA Nations League",
        "normalization_version": "sportsbrain-nl-team-aliases-v1",
        "upstream_url": "https://raw.githubusercontent.com/martj42/international_results/master/results.csv",
        "upstream_cache_sha256": "test-only-not-provider-evidence",
        "snapshot_committed_at": "2026-09-29T16:00:59Z",
        "upstream_cache_retrieved_at": None,
        "upstream_retrieval_note": "synthetic unit test only",
        "matches": matches,
    }
    return {**payload, "snapshot_digest": canonical_digest(payload)}


def _match(day, home, away, hs, aws, edition="2020/21", period="2020/21"):
    return {
        "date": day,
        "home_team": home,
        "away_team": away,
        "home_score": hs,
        "away_score": aws,
        "neutral": False,
        "edition": edition,
        "validation_period": period,
    }


def _timeline_for(source):
    records = []
    for match in source["matches"]:
        kickoff = f"{match['date']}T18:45:00Z"
        safe = datetime.fromisoformat(kickoff.replace("Z", "+00:00")) + timedelta(
            hours=6
        )
        records.append(
            {
                "fixture_id": canonical_fixture_id(
                    match["edition"],
                    match["date"],
                    match["home_team"],
                    match["away_team"],
                ),
                "edition": match["edition"],
                "group": None,
                "home_team": match["home_team"],
                "away_team": match["away_team"],
                "kickoff_utc": kickoff,
                "result_safe_available_at": safe.isoformat().replace("+00:00", "Z"),
                "record_digest": "synthetic-timeline-record",
                "source_refs": {"source": "synthetic-test-only"},
            }
        )
    return {
        "schema_version": "uefa-nations-league-fixture-timeline-v1",
        "results_source_digest": source["snapshot_digest"],
        "dataset_digest": "synthetic-timeline-digest",
        "records": records,
    }


def test_committed_dataset_is_deterministic_and_timeline_bound():
    source, contracts = _json(DEFAULT_SOURCE), _json(DEFAULT_CONTRACTS)
    first, first_coverage = build_dataset(
        source, contracts, built_at=source["snapshot_committed_at"]
    )
    second, second_coverage = build_dataset(
        source, contracts, built_at=source["snapshot_committed_at"]
    )
    assert first == second
    assert first_coverage == second_coverage
    assert len(first["records"]) == 512
    assert first_coverage["fixture_coverage_complete"] is True
    assert first_coverage["status"] == "NL_COMPETITION_STATE_READY"
    assert first_coverage["ready_gate"]["safe_consumability_complete"] is True
    assert first_coverage["ready_gate"]["causal_timing_complete"] is True
    assert first_coverage["fields"]["kickoff_timestamp"]["present"] == 512
    assert (
        first_coverage["fields"]["causal_date_cutoff"][
            "strict_kickoff_timestamp_comparison"
        ]
        == 512
    )
    assert first_coverage["fields"]["official_fixture_id"]["missing"] == 512
    assert (
        first_coverage["fields"]["qualification_and_relegation_math"]["computed_exact"]
        == 0
    )
    assert (
        first_coverage["edition_rule_coverage"]["2022/23"]
        == "partially_source_verified"
    )
    assert (
        contracts["editions"]["2022/23"]["tiebreak"]["status"]
        == "official_rule_text_frozen_but_historical_inputs_incomplete"
    )
    assert contracts["editions"]["2022/23"]["tiebreak"]["inputs_available"] == {
        "all_group_results": True,
        "disciplinary_card_totals": False,
        "2022_23_access_list_position": False,
    }
    for record in first["records"]:
        assert record["edition_rule_digest"] == canonical_digest(
            contracts["editions"][record["edition"]]
        )
    validate_dataset(
        first, first_coverage, expected_source_digest=source["snapshot_digest"]
    )


def test_published_json_reload_and_record_digests_validate():
    dataset, coverage = load_and_validate(DEFAULT_DATASET, DEFAULT_COVERAGE)
    assert len(dataset["records"]) == 512
    assert all(
        record["fixture_id"].startswith("uefa-nl:") for record in dataset["records"]
    )
    assert all(
        record["kickoff"] == record["state_cutoff"] for record in dataset["records"]
    )
    assert all(record["timeline_record_digest"] for record in dataset["records"])
    assert coverage["timeline_join"]["joined_complete"] is True
    assert coverage["fields"]["kickoff_timestamp"]["present"] == 512
    assert coverage["coverage_digest"] == canonical_digest(
        {key: value for key, value in coverage.items() if key != "coverage_digest"}
    )


def test_field_status_contract_is_explicit_and_b5_safe():
    dataset, coverage = load_and_validate(DEFAULT_DATASET, DEFAULT_COVERAGE)
    assert coverage["field_status_contract"]["values"] == [
        "NOT_APPLICABLE",
        "SAFE_BOUND",
        "SAFE_EXACT",
        "UNRESOLVED",
    ]
    safe = {"SAFE_EXACT", "SAFE_BOUND"}
    for record in dataset["records"]:
        statuses = record["field_status"]
        assert statuses["fixture_id"] == "SAFE_EXACT"
        assert statuses["official_fixture_id"] == "UNRESOLVED"
        assert statuses["kickoff"] == "SAFE_EXACT"
        assert statuses["state_cutoff"] == "SAFE_EXACT"
        assert statuses["standings_before"] in safe
        assert statuses["must_win_primitives"] == "UNRESOLVED"
        if statuses["rank"] == "SAFE_EXACT":
            assert all(
                row["rank_status"] != "unresolved_points_tie"
                for table in record["standings_before"]
                for row in table["standing_rows"]
            )


def test_admin_result_safe_exceptions_are_unresolved_not_negative_states():
    dataset, _ = load_and_validate(DEFAULT_DATASET, DEFAULT_COVERAGE)
    admin = [
        record
        for record in dataset["records"]
        if record["result_safe_available_at"] is None
    ]
    assert len(admin) == 2
    assert all(
        record["field_status"]["result_safe_available_at"] == "UNRESOLVED"
        for record in admin
    )
    assert all(
        record["qualification_state"]["status"].startswith("points_bounds")
        for record in admin
        if record["qualification_state"]["participants"]
    )


def test_points_bound_state_never_exposes_unresolved_rank_as_exact():
    dataset, _ = load_and_validate(DEFAULT_DATASET, DEFAULT_COVERAGE)
    unresolved = [
        record
        for record in dataset["records"]
        if record["field_status"]["rank"] == "SAFE_BOUND"
    ]
    assert unresolved
    assert any(
        row["rank_status"] == "unresolved_points_tie"
        for record in unresolved
        for table in record["standings_before"]
        for row in table["standing_rows"]
    )


def test_relegation_contract_exposes_playout_allocation_without_inference():
    dataset, _ = load_and_validate(DEFAULT_DATASET, DEFAULT_COVERAGE)
    c_group = next(
        record
        for record in dataset["records"]
        if record["edition"] == "2022/23" and record["group"] == "C1"
    )
    assert c_group["relegation_state"]["playout_possible"] is None
    assert c_group["relegation_state"]["playout_required"] is None
    assert (
        c_group["relegation_state"]["allocation_status"]
        == "unresolved_edition_specific_relegation_allocation"
    )
    a_group = next(
        record
        for record in dataset["records"]
        if record["edition"] == "2022/23" and record["group"] == "A1"
    )
    assert a_group["relegation_state"]["playout_possible"] is False
    assert a_group["relegation_state"]["playout_required"] is False
    assert a_group["relegation_state"]["allocation_status"] == "direct_relegation_rule"


def test_timeline_join_rejects_a_different_historical_source():
    source, contracts = _json(DEFAULT_SOURCE), _json(DEFAULT_CONTRACTS)
    timeline = _json(Path("results/research/nations_league_fixture_timeline_v1.json"))
    altered = copy.deepcopy(source)
    altered["matches"][0]["home_score"] += 1
    altered["snapshot_digest"] = canonical_digest(
        {key: value for key, value in altered.items() if key != "snapshot_digest"}
    )
    with pytest.raises(ValueError, match="does not bind"):
        build_dataset(
            altered,
            contracts,
            built_at=source["snapshot_committed_at"],
            timeline=timeline,
        )


def test_state_cutoff_is_exact_kickoff_and_future_results_do_not_leak():
    source, contracts = _json(DEFAULT_SOURCE), _json(DEFAULT_CONTRACTS)
    dataset, _ = build_dataset(
        source,
        contracts,
        built_at=source["snapshot_committed_at"],
        timeline=_timeline_for(source),
    )
    target = next(
        r
        for r in dataset["records"]
        if r["edition"] == "2020/21"
        and r["fixture_date"] == "2020-09-07"
        and r["group"] == "A1"
    )
    assert target["state_cutoff"] == "2020-09-07T18:45:00Z"
    assert target["kickoff"] == target["state_cutoff"]
    table = target["standings_before"][0]
    assert all(
        value.startswith("uefa-nl:") for value in table["prior_result_fixture_ids"]
    )

    changed = copy.deepcopy(source)
    for match in changed["matches"]:
        if (
            match["edition"] == target["edition"]
            and match["date"] >= target["fixture_date"]
        ):
            match["home_score"] += 50
            match["away_score"] += 50
    payload = {key: value for key, value in changed.items() if key != "snapshot_digest"}
    changed["snapshot_digest"] = canonical_digest(payload)
    changed_dataset, _ = build_dataset(
        changed,
        contracts,
        built_at=source["snapshot_committed_at"],
        timeline=_timeline_for(changed),
    )
    changed_target = next(
        r for r in changed_dataset["records"] if r["fixture_id"] == target["fixture_id"]
    )
    assert changed_target["standings_before"] == target["standings_before"]


def test_same_day_result_is_used_only_after_result_safe_time_precedes_target():
    contracts = _json(DEFAULT_CONTRACTS)
    source = _source(
        [
            _match("2020-09-07", "Netherlands", "Italy", 2, 0),
            _match("2020-09-07", "Bosnia and Herzegovina", "Poland", 1, 1),
        ]
    )
    timeline = _timeline_for(source)
    timeline["records"][0]["kickoff_utc"] = "2020-09-07T12:00:00Z"
    timeline["records"][0]["result_safe_available_at"] = "2020-09-07T19:00:00Z"
    timeline["records"][1]["kickoff_utc"] = "2020-09-07T18:45:00Z"
    timeline["records"][1]["result_safe_available_at"] = "2020-09-08T00:45:00Z"
    dataset, _ = build_dataset(
        source,
        contracts,
        built_at=source["snapshot_committed_at"],
        timeline=timeline,
    )
    target = dataset["records"][1]
    assert target["standings_before"][0]["prior_result_fixture_ids"] == []


def test_points_table_arithmetic_and_unresolved_points_tie_are_explicit():
    contracts = _json(DEFAULT_CONTRACTS)
    source = _source(
        [
            _match("2020-09-03", "Netherlands", "Italy", 2, 0),
            _match("2020-09-03", "Bosnia and Herzegovina", "Poland", 1, 1),
            _match("2020-09-07", "Bosnia and Herzegovina", "Netherlands", 1, 1),
        ]
    )
    dataset, _ = build_dataset(
        source,
        contracts,
        built_at=source["snapshot_committed_at"],
        timeline=_timeline_for(source),
    )
    target = dataset["records"][-1]
    rows = {row["team"]: row for row in target["standings_before"][0]["standing_rows"]}
    assert rows["Netherlands"]["points_before"] == 3
    assert rows["Netherlands"]["goals_for_before"] == 2
    assert rows["Netherlands"]["goals_against_before"] == 0
    assert rows["Netherlands"]["goal_difference_before"] == 2
    assert rows["Netherlands"]["wins_before"] == 1
    assert rows["Netherlands"]["draws_before"] == 0
    assert rows["Netherlands"]["losses_before"] == 0
    assert rows["Netherlands"]["matches_played_before"] == 1
    assert rows["Italy"]["rank_status"] == "points_order_unique"
    assert rows["Bosnia and Herzegovina"]["rank_status"] == "unresolved_points_tie"
    assert rows["Poland"]["rank_status"] == "unresolved_points_tie"


def test_2022_russia_is_fixed_fourth_and_not_counted_as_an_active_match_opponent():
    contracts = _json(DEFAULT_CONTRACTS)
    source = _source(
        [
            _match(
                "2022-06-02",
                "Israel",
                "Iceland",
                2,
                2,
                edition="2022/23",
                period="2022/23",
            ),
            _match(
                "2022-06-06",
                "Iceland",
                "Albania",
                1,
                1,
                edition="2022/23",
                period="2022/23",
            ),
            _match(
                "2022-06-06",
                "Israel",
                "Russia",
                3,
                0,
                edition="2022/23",
                period="2022/23",
            ),
        ]
    )
    dataset, _ = build_dataset(
        source,
        contracts,
        built_at=source["snapshot_committed_at"],
        timeline=_timeline_for(source),
    )
    table = next(
        t
        for r in dataset["records"]
        if r["fixture_date"] == "2022-06-06"
        for t in r["standings_before"]
        if t["group"] == "B2"
    )
    russia = next(row for row in table["standing_rows"] if row["team"] == "Russia")
    assert (
        russia["competition_status"]
        == "suspended_non_participant_automatically_ranked_fourth_and_relegated"
    )
    assert russia["remaining_group_matches"] == 0
    assert russia["rank_min"] == russia["rank_max"] == 4
    assert russia["rank_status"] == "fixed_by_uefa_nonparticipation_decision"
    assert "Russia" not in table["prior_result_fixture_ids"]


def test_2022_delayed_playout_is_separate_and_2024_knockouts_are_not_group_fixtures():
    dataset, _ = load_and_validate(DEFAULT_DATASET, DEFAULT_COVERAGE)
    delayed = [
        r
        for r in dataset["records"]
        if r["validation_period"] == "2022/23-delayed-relegation-playoffs"
    ]
    assert len(delayed) == 2
    assert {r["stage"] for r in delayed} == {"league_c_relegation_playout"}
    assert {r["league_tier"] for r in delayed} == {"C/D"}
    assert all(r["home_group"] and r["away_group"] for r in delayed)
    assert all(r["group"] is None for r in delayed)
    final_records = [
        r
        for r in dataset["records"]
        if r["edition"] == "2024/25" and r["stage"] != "league_phase"
    ]
    assert final_records
    assert all(r["group"] is None for r in final_records)


def test_unsupported_fixture_or_kickoff_ids_cannot_be_filled_without_source():
    dataset, coverage = load_and_validate(DEFAULT_DATASET, DEFAULT_COVERAGE)
    altered = copy.deepcopy(dataset)
    altered["records"][0]["kickoff"] = "2020-09-03T19:45:00Z"
    with pytest.raises(ValueError, match="Dataset digest"):
        validate_dataset(altered, coverage)


def test_dataset_and_coverage_digest_tampering_fail_closed():
    dataset, coverage = load_and_validate(DEFAULT_DATASET, DEFAULT_COVERAGE)
    altered_coverage = copy.deepcopy(coverage)
    altered_coverage["tampered_note"] = "not covered by the frozen audit digest"
    with pytest.raises(ValueError, match="Coverage audit digest"):
        validate_dataset(dataset, altered_coverage)


def test_cross_tier_fixtures_keep_both_participant_tiers_and_groups():
    dataset, _ = load_and_validate(DEFAULT_DATASET, DEFAULT_COVERAGE)
    cross_tier = next(
        row
        for row in dataset["records"]
        if row["edition"] == "2024/25"
        and row["stage"] == "promotion_relegation_playoff"
    )
    assert (
        cross_tier["league_tier"]
        == f"{cross_tier['home_league_tier']}/{cross_tier['away_league_tier']}"
    )
    assert cross_tier["group"] is None
    assert cross_tier["home_group"][0] == cross_tier["home_league_tier"]
    assert cross_tier["away_group"][0] == cross_tier["away_league_tier"]


def test_team_alias_mapping_is_canonical_and_source_has_no_duplicate_fixture_ids():
    dataset, _ = load_and_validate(DEFAULT_DATASET, DEFAULT_COVERAGE)
    assert len({row["fixture_id"] for row in dataset["records"]}) == 512
    assert any(
        row["home_team"] == "Türkiye" or row["away_team"] == "Türkiye"
        for row in dataset["records"]
    )
