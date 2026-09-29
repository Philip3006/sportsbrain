import copy
import json
from pathlib import Path

import pytest

from src.analysis.nations_league_competition_state import (
    DEFAULT_CONTRACTS,
    DEFAULT_COVERAGE,
    DEFAULT_DATASET,
    DEFAULT_SOURCE,
    build_dataset,
    canonical_digest,
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


def test_committed_dataset_is_deterministic_full_512_result_coverage_and_explicitly_partial():
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
    assert first_coverage["fields"]["kickoff_timestamp"]["present"] == 0
    assert first_coverage["fields"]["official_fixture_id"]["missing"] == 512
    assert (
        first_coverage["fields"]["qualification_and_relegation_math"]["computed_exact"]
        == 0
    )
    assert (
        first_coverage["edition_rule_coverage"]["2022/23"]
        == "partially_source_verified"
    )
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
    assert coverage["coverage_digest"] == canonical_digest(
        {key: value for key, value in coverage.items() if key != "coverage_digest"}
    )


def test_state_cutoff_is_conservative_day_before_and_source_same_day_results_do_not_leak():
    source, contracts = _json(DEFAULT_SOURCE), _json(DEFAULT_CONTRACTS)
    dataset, _ = build_dataset(
        source, contracts, built_at=source["snapshot_committed_at"]
    )
    target = next(
        r
        for r in dataset["records"]
        if r["edition"] == "2020/21"
        and r["fixture_date"] == "2020-09-07"
        and r["group"] == "A1"
    )
    assert target["state_cutoff"] == "2020-09-06T00:00:00Z"
    assert target["kickoff"] is None
    table = target["standings_before"][0]
    assert "result:" in " ".join(table["prior_result_fixture_ids"])

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
        changed, contracts, built_at=source["snapshot_committed_at"]
    )
    changed_target = next(
        r for r in changed_dataset["records"] if r["fixture_id"] == target["fixture_id"]
    )
    assert changed_target["standings_before"] == target["standings_before"]


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
        source, contracts, built_at=source["snapshot_committed_at"]
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
        source, contracts, built_at=source["snapshot_committed_at"]
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
    altered["records"][0]["kickoff"] = "2020-09-03T18:45:00Z"
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
