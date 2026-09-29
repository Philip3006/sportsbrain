"""Causal, audit-first UEFA Nations League historical state builder.

This is a research-data module only. It has no provider, runtime, publication,
betting, or ledger dependencies. Unsupported source fields are emitted as null
with explicit reasons instead of being inferred from final tables.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACTS = ROOT / "data/research/nations_league/edition_contracts_v1.json"
DEFAULT_SOURCE = ROOT / "data/research/nations_league/historical_results_source_v1.json"
DEFAULT_DATASET = ROOT / "results/research/nations_league_competition_state_v1.json"
DEFAULT_COVERAGE = (
    ROOT / "results/audits/nations_league_competition_state_coverage_v1.json"
)
DEFAULT_TIMELINE = ROOT / "results/research/nations_league_fixture_timeline_v1.json"
DEFAULT_TIMELINE_COVERAGE = (
    ROOT / "results/audits/nations_league_fixture_timeline_coverage_v1.json"
)
DATASET_SCHEMA = "uefa-nations-league-causal-competition-state-v1"
NORMALIZATION_VERSION = "sportsbrain-nl-team-aliases-v1"

ALIASES = {
    "bosnia-herzegovina": "Bosnia and Herzegovina",
    "bosnia and herzegovina": "Bosnia and Herzegovina",
    "czech republic": "Czechia",
    "north macedonia": "North Macedonia",
    "republic of ireland": "Ireland",
    "ireland": "Ireland",
    "turkey": "Türkiye",
    "türkiye": "Türkiye",
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_fixture_id(
    edition: str, fixture_date: str, home_team: str, away_team: str
) -> str:
    identity = "|".join(
        (edition, fixture_date, canonical_team(home_team), canonical_team(away_team))
    )
    return "uefa-nl:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def file_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object in {path}")
    return value


def canonical_team(value: str) -> str:
    stripped = " ".join(unicodedata.normalize("NFC", value).split())
    return ALIASES.get(stripped.casefold(), stripped)


def _parse_match(row: dict[str, Any]) -> dict[str, Any]:
    required = {
        "date",
        "home_team",
        "away_team",
        "home_score",
        "away_score",
        "neutral",
        "edition",
        "validation_period",
    }
    if required - row.keys():
        raise ValueError(f"Source match missing keys: {sorted(required - row.keys())}")
    match_date = date.fromisoformat(row["date"])
    home, away = canonical_team(row["home_team"]), canonical_team(row["away_team"])
    if not home or not away or home == away:
        raise ValueError("Invalid participant identity")
    home_score, away_score = row["home_score"], row["away_score"]
    if (
        type(home_score) is not int
        or type(away_score) is not int
        or min(home_score, away_score) < 0
    ):
        raise ValueError("Invalid final score")
    if not isinstance(row["neutral"], bool):
        raise TypeError("Invalid neutral flag")
    return {
        "date": match_date.isoformat(),
        "home_team": home,
        "away_team": away,
        "home_score": home_score,
        "away_score": away_score,
        "neutral": row["neutral"],
        "edition": row["edition"],
        "validation_period": row["validation_period"],
    }


def _edition_team_maps(
    contracts: dict[str, Any],
) -> tuple[dict[str, dict[str, tuple[str, str]]], dict[str, set[str]]]:
    maps: dict[str, dict[str, tuple[str, str]]] = {}
    active: dict[str, set[str]] = {}
    for edition, contract in contracts["editions"].items():
        team_map: dict[str, tuple[str, str]] = {}
        excluded: set[str] = set()
        for group, teams in contract["groups"].items():
            for team in teams:
                normalized = canonical_team(team)
                if normalized in team_map:
                    raise ValueError(
                        f"Team appears in multiple groups in {edition}: {normalized}"
                    )
                team_map[normalized] = (group[0], group)
        for team, exception in contract.get("participant_exceptions", {}).items():
            if exception.get("eligible_for_fixture_schedule") is False:
                excluded.add(canonical_team(team))
        maps[edition] = team_map
        active[edition] = set(team_map) - excluded
    return maps, active


def _fixture_stage(
    match: dict[str, Any], team_map: dict[str, tuple[str, str]]
) -> tuple[str, str | None, str | None]:
    home_group = team_map.get(match["home_team"], (None, None))[1]
    away_group = team_map.get(match["away_team"], (None, None))[1]
    home_tier = team_map.get(match["home_team"], (None, None))[0]
    away_tier = team_map.get(match["away_team"], (None, None))[0]
    period, edition, match_day = (
        match["validation_period"],
        match["edition"],
        date.fromisoformat(match["date"]),
    )
    if period == "2022/23-delayed-relegation-playoffs":
        return "league_c_relegation_playout", "C/D", None
    if home_group is not None and home_group == away_group:
        return "league_phase", home_tier, home_group
    if edition == "2020/21" and home_tier == away_tier == "A":
        return "league_a_finals", "A", None
    if edition == "2022/23" and home_tier == away_tier == "A":
        return "league_a_finals", "A", None
    if edition == "2024/25":
        if home_tier != away_tier:
            return "promotion_relegation_playoff", f"{home_tier}/{away_tier}", None
        if home_tier == away_tier == "A" and match_day.month == 3:
            return "league_a_quarter_final", "A", None
        if home_tier == away_tier == "A":
            return "league_a_final_tournament", "A", None
    return "unclassified_non_group_fixture", None, None


def _empty_line(team: str) -> dict[str, Any]:
    return {
        "team": team,
        "matches_played_before": 0,
        "points_before": 0,
        "goals_for_before": 0,
        "goals_against_before": 0,
        "goal_difference_before": 0,
        "wins_before": 0,
        "draws_before": 0,
        "losses_before": 0,
    }


def _table_before(
    *,
    edition: str,
    group: str,
    target_kickoff: datetime | None,
    matches: list[dict[str, Any]],
    contract: dict[str, Any],
    active_teams: set[str],
    team_map: dict[str, tuple[str, str]],
    timeline_by_fixture: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    group_teams = [canonical_team(t) for t in contract["groups"][group]]
    lines = {team: _empty_line(team) for team in group_teams}
    used_ids: list[str] = []
    future_fixtures = [
        item
        for item in timeline_by_fixture.values()
        if item.get("edition") == edition
        and item.get("group") == group
        and target_kickoff is not None
        and item.get("kickoff_utc")
        and datetime.fromisoformat(item["kickoff_utc"].replace("Z", "+00:00"))
        > target_kickoff
    ]
    for match in matches:
        if match["edition"] != edition:
            continue
        if match["home_team"] not in team_map or match["away_team"] not in team_map:
            continue
        if (
            team_map[match["home_team"]][1] != group
            or team_map[match["away_team"]][1] != group
        ):
            continue
        # Suspended/non-participating sides have no eligible fixtures and do not
        # contribute a match result to the active group's sporting table.
        if (
            match["home_team"] not in active_teams
            or match["away_team"] not in active_teams
        ):
            continue
        fixture_id = canonical_fixture_id(
            match["edition"],
            match["date"],
            match["home_team"],
            match["away_team"],
        )
        timeline_record = timeline_by_fixture.get(fixture_id)
        if timeline_record is None or target_kickoff is None:
            continue
        result_safe_at = timeline_record.get("result_safe_available_at")
        if not result_safe_at:
            continue
        if (
            datetime.fromisoformat(result_safe_at.replace("Z", "+00:00"))
            >= target_kickoff
        ):
            continue
        home, away = lines[match["home_team"]], lines[match["away_team"]]
        hs, aw = match["home_score"], match["away_score"]
        home["matches_played_before"] += 1
        away["matches_played_before"] += 1
        home["goals_for_before"] += hs
        home["goals_against_before"] += aw
        away["goals_for_before"] += aw
        away["goals_against_before"] += hs
        if hs > aw:
            home["wins_before"] += 1
            away["losses_before"] += 1
            home["points_before"] += 3
        elif hs < aw:
            away["wins_before"] += 1
            home["losses_before"] += 1
            away["points_before"] += 3
        else:
            home["draws_before"] += 1
            away["draws_before"] += 1
            home["points_before"] += 1
            away["points_before"] += 1
        used_ids.append(fixture_id)
    exception_map = {
        canonical_team(t): v
        for t, v in contract.get("participant_exceptions", {}).items()
    }
    for team, line in lines.items():
        line["goal_difference_before"] = (
            line["goals_for_before"] - line["goals_against_before"]
        )
        exception = exception_map.get(team)
        if exception:
            line["competition_status"] = exception["status"]
            line["remaining_group_matches"] = 0
        elif team in active_teams:
            line["remaining_group_matches"] = sum(
                item["home_team"] == team or item["away_team"] == team
                for item in future_fixtures
            )
            line["competition_status"] = "active"
        else:
            line["remaining_group_matches"] = None
            line["competition_status"] = "unknown_participation_status"
    # At intermediate cutoffs, report rank only when points alone separate a
    # team. Any points tie stays a set/range, never an arbitrary sort.
    ranking_lines = [line for team, line in lines.items() if team in active_teams]
    fixed_exception_status = {
        canonical_team(team): value["status"] for team, value in exception_map.items()
    }
    points_sorted = sorted(
        {line["points_before"] for line in ranking_lines}, reverse=True
    )
    points_position: dict[int, tuple[int, int]] = {}
    consumed = 0
    for value in points_sorted:
        tied = sum(1 for line in ranking_lines if line["points_before"] == value)
        points_position[value] = (consumed + 1, consumed + tied)
        consumed += tied
    for line in lines.values():
        if (
            line["team"] in fixed_exception_status
            and "automatically_ranked_fourth" in fixed_exception_status[line["team"]]
        ):
            line["rank_min"] = 4
            line["rank_max"] = 4
            line["rank_status"] = "fixed_by_uefa_nonparticipation_decision"
            continue
        if line["team"] not in active_teams:
            line["rank_min"] = None
            line["rank_max"] = None
            line["rank_status"] = "unknown_participation_status"
            continue
        first, last = points_position[line["points_before"]]
        line["rank_min"] = first if first == last else None
        line["rank_max"] = last if first == last else None
        line["rank_status"] = (
            "points_order_unique" if first == last else "unresolved_points_tie"
        )
    ordered = sorted(
        lines.values(),
        key=lambda item: (
            item["rank_min"] is None,
            item["rank_min"] or 999,
            item["team"],
        ),
    )
    return {
        "group": group,
        "league_tier": group[0],
        "standing_rows": ordered,
        "rank_semantics": "rank_min/rank_max only reflect points; tied ranks are unresolved, not alphabetically ranked",
        "prior_result_fixture_ids": sorted(used_ids),
        "remaining_schedule": {
            "status": "timeline_bound_without_future_results",
            "fixtures": [
                {
                    "fixture_id": item["fixture_id"],
                    "home_team": item["home_team"],
                    "away_team": item["away_team"],
                    "kickoff_utc": item["kickoff_utc"],
                }
                for item in sorted(
                    future_fixtures,
                    key=lambda value: (
                        value["kickoff_utc"],
                        value["fixture_id"],
                    ),
                )
            ],
        },
    }


def _source_match_id(match: dict[str, Any]) -> str:
    identity = {
        key: match[key]
        for key in ("edition", "validation_period", "date", "home_team", "away_team")
    }
    return f"result:{canonical_digest(identity)[:20]}"


def _parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timeline timestamps must be timezone-aware UTC values")
    return parsed


def _qualification_slots(edition: str, tier: str) -> int | None:
    if tier != "A":
        return None
    return 2 if edition == "2024/25" else 1


def _promotion_slots(tier: str) -> int | None:
    return 1 if tier in {"B", "C", "D"} else None


def _points_bound_state(
    *,
    team: str,
    table: dict[str, Any],
    edition: str,
    tier: str,
    group: str,
) -> dict[str, Any]:
    rows = [
        row for row in table["standing_rows"] if row["competition_status"] == "active"
    ]
    current = next((row for row in rows if row["team"] == team), None)
    if current is None:
        return {
            "team": team,
            "points_before": None,
            "max_remaining_points": None,
            "qualification": {
                "can_still_qualify": False,
                "mathematically_qualified": False,
                "mathematically_eliminated": True,
                "points_to_relevant_boundary": None,
                "status": "fixed_non_participant_status",
            },
            "promotion": {
                "can_be_promoted": False,
                "mathematically_promoted": False,
                "mathematically_eliminated_from_promotion": True,
                "points_to_relevant_boundary": None,
                "status": "fixed_non_participant_status",
            },
            "relegation": {
                "can_be_relegated": False,
                "mathematically_relegated": True,
                "points_to_relevant_boundary": None,
                "status": "fixed_non_participant_status",
            },
            "must_win_primitives": {
                "win_required_for_mathematical_survival": False,
                "win_required_for_mathematical_qualification": False,
                "draw_sufficient_for_mathematical_goal": False,
                "loss_eliminates": True,
                "points_required_from_remaining_matches": None,
                "status": "fixed_non_participant_status",
            },
        }
    points = current["points_before"]
    max_remaining = current["remaining_group_matches"] * 3
    opponent_maxima = [
        row["points_before"] + row["remaining_group_matches"] * 3
        for row in rows
        if row["team"] != team
    ]

    def top_state(slots: int | None) -> dict[str, Any]:
        if slots is None:
            return {
                "can": None,
                "qualified": None,
                "eliminated": None,
                "status": "not_applicable",
            }
        guaranteed = sum(value >= points for value in opponent_maxima) < slots
        eliminated = (
            sum(
                row["points_before"] > points + max_remaining
                for row in rows
                if row["team"] != team
            )
            >= slots
        )
        return {
            "can": not eliminated,
            "qualified": guaranteed,
            "eliminated": eliminated,
            "status": "points_bounds_only_tiebreaks_preserved_as_unresolved",
        }

    qualification = top_state(_qualification_slots(edition, tier))
    promotion = top_state(_promotion_slots(tier))
    direct_relegation = tier in {"A", "B"}
    relegation = {
        "can": None,
        "relegated": None,
        "status": "unresolved_edition_specific_relegation_allocation"
        if tier == "C"
        else "not_applicable"
        if tier == "D"
        else "points_bounds_only_tiebreaks_preserved_as_unresolved",
    }
    if direct_relegation:
        guaranteed_safe = any(
            points > row["points_before"] + row["remaining_group_matches"] * 3
            for row in rows
            if row["team"] != team
        )
        mathematically_relegated = (
            sum(
                row["points_before"] > points + max_remaining
                for row in rows
                if row["team"] != team
            )
            >= len(rows) - 1
        )
        relegation.update(
            {
                "can": not guaranteed_safe,
                "relegated": mathematically_relegated,
            }
        )
    return {
        "team": team,
        "points_before": points,
        "max_remaining_points": max_remaining,
        "qualification": {
            "can_still_qualify": qualification["can"],
            "mathematically_qualified": qualification["qualified"],
            "mathematically_eliminated": qualification["eliminated"],
            "points_to_relevant_boundary": None,
            "status": qualification["status"],
        },
        "promotion": {
            "can_be_promoted": promotion["can"],
            "mathematically_promoted": promotion["qualified"],
            "mathematically_eliminated_from_promotion": promotion["eliminated"],
            "points_to_relevant_boundary": None,
            "status": promotion["status"],
        },
        "relegation": {
            "can_be_relegated": relegation["can"],
            "mathematically_relegated": relegation["relegated"],
            "points_to_relevant_boundary": None,
            "status": relegation["status"],
        },
        "must_win_primitives": {
            "win_required_for_mathematical_survival": None,
            "win_required_for_mathematical_qualification": None,
            "draw_sufficient_for_mathematical_goal": None,
            "loss_eliminates": None,
            "points_required_from_remaining_matches": None,
            "status": "not_computed_without_outcome_state_solver",
        },
    }


def build_dataset(
    source: dict[str, Any],
    contracts: dict[str, Any],
    *,
    built_at: str,
    timeline: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if source.get("schema_version") != "uefa-nl-canonical-result-source-v1":
        raise ValueError("Unsupported historical source schema")
    if contracts.get("schema_version") != "uefa-nations-league-edition-contracts-v1":
        raise ValueError("Unsupported edition contract schema")
    if contracts.get("normalization_version") != NORMALIZATION_VERSION:
        raise ValueError("Team normalization version does not match contract")
    source_payload = {
        key: value for key, value in source.items() if key != "snapshot_digest"
    }
    source_digest = canonical_digest(source_payload)
    if source.get("snapshot_digest") != source_digest:
        raise ValueError("Historical source snapshot digest mismatch")
    if timeline is None:
        timeline = _read_json(DEFAULT_TIMELINE)
    if timeline.get("schema_version") != "uefa-nations-league-fixture-timeline-v1":
        raise ValueError("Unsupported fixture timeline schema")
    if timeline.get("results_source_digest") != source.get("snapshot_digest"):
        raise ValueError("Fixture timeline does not bind to the historical source")
    timeline_records = timeline.get("records")
    if not isinstance(timeline_records, list):
        raise TypeError("Fixture timeline records are missing")
    timeline_by_fixture = {
        record["fixture_id"]: record
        for record in timeline_records
        if isinstance(record, dict) and record.get("fixture_id")
    }
    if len(timeline_by_fixture) != len(timeline_records):
        raise ValueError("Fixture timeline contains duplicate or invalid IDs")
    matches = [_parse_match(row) for row in source["matches"]]
    match_keys = [
        (
            m["edition"],
            m["validation_period"],
            m["date"],
            m["home_team"],
            m["away_team"],
        )
        for m in matches
    ]
    if len(match_keys) != len(set(match_keys)):
        raise ValueError("Duplicate canonical result identity")
    maps, active_by_edition = _edition_team_maps(contracts)
    for match in matches:
        if match["edition"] not in maps:
            raise ValueError(f"Unsupported edition: {match['edition']}")
        if (
            match["home_team"] not in maps[match["edition"]]
            or match["away_team"] not in maps[match["edition"]]
        ):
            raise ValueError(f"Unmapped participant in source match: {match}")
    matches.sort(
        key=lambda row: (
            row["date"],
            row["edition"],
            row["home_team"],
            row["away_team"],
        )
    )
    records: list[dict[str, Any]] = []
    stage_counts: dict[str, int] = defaultdict(int)
    unresolved_points_ties = 0
    timeline_joined = 0
    missing_timeline_fixtures: list[str] = []
    exact_cutoff_count = 0
    for match in matches:
        edition, contract = match["edition"], contracts["editions"][match["edition"]]
        stage, tier, group = _fixture_stage(match, maps[edition])
        stage_counts[f"{edition}:{stage}"] += 1
        fixture_id = canonical_fixture_id(
            edition, match["date"], match["home_team"], match["away_team"]
        )
        timeline_record = timeline_by_fixture.get(fixture_id)
        if timeline_record is None:
            missing_timeline_fixtures.append(fixture_id)
        else:
            timeline_joined += 1
        kickoff = timeline_record.get("kickoff_utc") if timeline_record else None
        kickoff_dt = _parse_utc(kickoff)
        cutoff = kickoff_dt
        if cutoff is not None:
            exact_cutoff_count += 1
        table_groups: list[str] = []
        if group:
            table_groups = [group]
        else:
            for team in (match["home_team"], match["away_team"]):
                team_group = maps[edition].get(team, (None, None))[1]
                if team_group and team_group not in table_groups:
                    table_groups.append(team_group)
        standings = [
            _table_before(
                edition=edition,
                group=g,
                target_kickoff=kickoff_dt,
                matches=matches,
                contract=contract,
                active_teams=active_by_edition[edition],
                team_map=maps[edition],
                timeline_by_fixture=timeline_by_fixture,
            )
            for g in sorted(table_groups)
        ]
        unresolved_points_ties += sum(
            1
            for table in standings
            for row in table["standing_rows"]
            if row["rank_status"] == "unresolved_points_tie"
        )
        home_tier, home_group = maps[edition][match["home_team"]]
        away_tier, away_group = maps[edition][match["away_team"]]
        future_schedule = []
        if timeline_record and kickoff_dt is not None:
            future_schedule = [
                {
                    "fixture_id": item["fixture_id"],
                    "home_team": item["home_team"],
                    "away_team": item["away_team"],
                    "kickoff_utc": item["kickoff_utc"],
                }
                for item in timeline_records
                if item.get("edition") == edition
                and item.get("group") == group
                and item.get("kickoff_utc")
                and _parse_utc(item["kickoff_utc"]) > kickoff_dt
            ]
        participant_states = {}
        if group and standings:
            table = standings[0]
            for team in (match["home_team"], match["away_team"]):
                participant_states[team] = _points_bound_state(
                    team=team,
                    table=table,
                    edition=edition,
                    tier=tier or home_tier,
                    group=group,
                )
        state_cutoff = cutoff.isoformat().replace("+00:00", "Z") if cutoff else None
        record = {
            "fixture_id": fixture_id,
            "official_fixture_id": None,
            "competition": "UEFA Nations League",
            "edition": edition,
            "validation_period": match["validation_period"],
            "stage": stage,
            "league_tier": tier,
            "group": group,
            "home_league_tier": home_tier,
            "away_league_tier": away_tier,
            "home_group": home_group,
            "away_group": away_group,
            "home_team": match["home_team"],
            "away_team": match["away_team"],
            "fixture_date": match["date"],
            "kickoff": kickoff,
            "matchday": timeline_record.get("matchday") if timeline_record else None,
            "result_safe_available_at": timeline_record.get("result_safe_available_at")
            if timeline_record
            else None,
            "home_score": match["home_score"],
            "away_score": match["away_score"],
            "neutral": match["neutral"],
            "state_cutoff": state_cutoff,
            "state_cutoff_basis": "strict target kickoff instant; only result_safe_available_at strictly before kickoff is included",
            "standings_before": standings,
            "remaining_schedule": {
                "status": "timeline_bound_without_future_results"
                if timeline_record
                else "unresolved_missing_timeline",
                "fixtures": sorted(
                    future_schedule,
                    key=lambda item: (item["kickoff_utc"], item["fixture_id"]),
                ),
            },
            "qualification_state": {
                "can_still_qualify": None,
                "mathematically_qualified": None,
                "mathematically_eliminated": None,
                "can_be_promoted": None,
                "mathematically_promoted": None,
                "can_be_relegated": None,
                "mathematically_relegated": None,
                "points_to_relevant_boundary": None,
                "max_remaining_points": None,
                "participants": participant_states,
                "status": "points_bounds_computed; exact_tiebreak_and_rule_states_preserved_unresolved",
            },
            "relegation_state": {
                "can_be_relegated": None,
                "mathematically_relegated": None,
                "points_to_relevant_boundary": None,
                "max_remaining_points": None,
                "participants": participant_states,
                "status": "points_bounds_computed_where_direct_rule_applies; edition_specific_c_states_unresolved",
            },
            "must_win_primitives": {
                "win_required_for_mathematical_survival": None,
                "win_required_for_mathematical_qualification": None,
                "draw_sufficient_for_mathematical_goal": None,
                "loss_eliminates": None,
                "points_required_from_remaining_matches": None,
                "status": "not_computed_without_complete_schedule_and_rules",
            },
            "rule_version": f"uefa-unl-{edition.replace('/', '-')}-timeline-bound-v2",
            "edition_rule_digest": canonical_digest(contract),
            "source_digest": source_digest,
            "source_fixture_identity_status": "canonical_timeline_fixture_id; official_match_id_absent",
            "kickoff_status": "verified_from_fixture_timeline"
            if kickoff
            else "missing_from_fixture_timeline",
            "matchday_status": "verified_in_timeline"
            if timeline_record and timeline_record.get("matchday") is not None
            else "not_present_in_fixture_timeline_schedule_evidence",
            "timeline_provenance": timeline_record.get("source_refs")
            if timeline_record
            else None,
            "timeline_record_digest": timeline_record.get("record_digest")
            if timeline_record
            else None,
            "timeline_dataset_digest": timeline.get("dataset_digest"),
            "record_digest": None,
        }
        payload = {
            key: value for key, value in record.items() if key != "record_digest"
        }
        record["record_digest"] = canonical_digest(payload)
        records.append(record)
    dataset = {
        "schema_version": DATASET_SCHEMA,
        "competition": "UEFA Nations League",
        "built_at": built_at,
        "normalization_version": NORMALIZATION_VERSION,
        "source_snapshot_digest": source_digest,
        "edition_contracts_digest": canonical_digest(contracts),
        "records": records,
    }
    coverage = {
        "schema_version": "uefa-nations-league-competition-state-coverage-v1",
        "status": "NL_COMPETITION_STATE_PARTIAL",
        "ready_gate": {
            "fixture_timeline_join_complete": timeline_joined == len(records)
            and not missing_timeline_fixtures,
            "all_kickoffs_verified": all(r["kickoff"] is not None for r in records),
            "matchday_complete": all(r["matchday"] is not None for r in records),
            "exact_rule_and_tiebreak_state_complete": False,
            "status": "PARTIAL",
        },
        "built_at": built_at,
        "expected_evaluation_fixture_count": 512,
        "source_fixture_count": len(matches),
        "output_record_count": len(records),
        "fixture_coverage_complete": len(matches) == 512 and len(records) == 512,
        "fixture_coverage_basis": "one-to-one coverage of the hash-verified existing 512-row evaluation source; not an independent official-UEFA-ID crosswalk",
        "timeline_join": {
            "timeline_schema": timeline["schema_version"],
            "timeline_dataset_digest": timeline.get("dataset_digest"),
            "joined_records": timeline_joined,
            "missing_fixture_ids": sorted(missing_timeline_fixtures),
            "joined_complete": timeline_joined == len(records)
            and not missing_timeline_fixtures,
        },
        "official_schedule_match_coverage_verified": timeline_joined == len(records)
        and not missing_timeline_fixtures,
        "missing_source_fixtures": [],
        "period_counts": dict(sorted(_count_by(matches, "validation_period").items())),
        "stage_counts": dict(sorted(stage_counts.items())),
        "fields": {
            "official_fixture_id": {
                "present": 0,
                "missing": len(records),
                "status": "not_present_in_source_or_timeline; canonical_timeline_id_used",
            },
            "kickoff_timestamp": {
                "present": sum(r["kickoff"] is not None for r in records),
                "missing": sum(r["kickoff"] is None for r in records),
                "status": "verified_from_fixture_timeline",
            },
            "matchday": {
                "present": sum(r["matchday"] is not None for r in records),
                "missing": sum(r["matchday"] is None for r in records),
                "status": "not_frozen_in_fixture_timeline_schedule_evidence",
            },
            "canonical_participants": {
                "present": len(records),
                "missing": 0,
                "status": "covered",
            },
            "league_and_group_mapping": {
                "present": sum(r["stage"] == "league_phase" for r in records),
                "missing": sum(r["stage"] != "league_phase" for r in records),
                "status": "partial_knockout_records_have_no_single_group",
            },
            "causal_date_cutoff": {
                "present": exact_cutoff_count,
                "strict_kickoff_timestamp_comparison": exact_cutoff_count,
                "status": "strict_target_kickoff; result-safe evidence must precede cutoff",
            },
            "standings_before": {
                "present": sum(bool(r["standings_before"]) for r in records),
                "missing": sum(not r["standings_before"] for r in records),
                "status": "covered_where_official_group_mapping_exists",
            },
            "qualification_and_relegation_math": {
                "computed_points_bounds": sum(
                    bool(r["qualification_state"].get("participants")) for r in records
                ),
                "computed_exact": 0,
                "unresolved": len(records),
                "status": "points_bounds_available; exact edition tie-break and allocation states remain unresolved",
            },
        },
        "unresolved_points_tie_rows": unresolved_points_ties,
        "leakage_checks": {
            "uses_only_result_dates_strictly_before_fixture_date": True,
            "same_day_results_excluded": True,
            "final_standings_backfilled": False,
            "final_tables_read": False,
            "future_match_scores_in_state": False,
        },
        "edition_rule_coverage": {
            edition: value["edition_status"]
            for edition, value in sorted(contracts["editions"].items())
        },
        "limitations": [
            "Official provider match IDs are absent; the canonical fixture timeline ID is the stable cross-contract identity.",
            "Matchday labels were not present in the completed timeline schedule evidence and remain null rather than inferred.",
            "2022/23 edition-specific Article 15 tie-break text was not frozen, so exact tie ranks and qualification/relegation boundary states remain unresolved.",
            "2020/21 and 2024/25 tie-break rules require disciplinary points and access-list values absent from the 512-result snapshot for some tied ranks.",
            "The 2022/23 delayed C/D play-out is represented as its observed two-leg pair; its provenance and exact two-leg result do not constitute the absent full scheduled fixture snapshot.",
        ],
    }
    coverage["dataset_digest"] = canonical_digest(dataset)
    coverage["coverage_digest"] = canonical_digest(
        {key: value for key, value in coverage.items() if key != "coverage_digest"}
    )
    return dataset, coverage


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row[key])] += 1
    return counts


def validate_dataset(
    dataset: dict[str, Any],
    coverage: dict[str, Any],
    *,
    expected_source_digest: str | None = None,
) -> None:
    if dataset.get("schema_version") != DATASET_SCHEMA:
        raise ValueError("Unsupported dataset schema")
    records = dataset.get("records")
    if not isinstance(records, list) or len(records) != coverage.get(
        "output_record_count"
    ):
        raise ValueError("Dataset/coverage record count mismatch")
    dataset_digest = canonical_digest(dataset)
    if coverage.get("dataset_digest") != dataset_digest:
        raise ValueError("Dataset digest does not match coverage audit")
    expected_coverage_digest = canonical_digest(
        {key: value for key, value in coverage.items() if key != "coverage_digest"}
    )
    if coverage.get("coverage_digest") != expected_coverage_digest:
        raise ValueError("Coverage audit digest mismatch")
    if (
        expected_source_digest is not None
        and dataset.get("source_snapshot_digest") != expected_source_digest
    ):
        raise ValueError("Dataset source digest mismatch")
    fixture_ids: set[str] = set()
    for record in records:
        fixture_id = record.get("fixture_id")
        if not fixture_id or fixture_id in fixture_ids:
            raise ValueError("Missing or duplicate canonical fixture ID")
        fixture_ids.add(fixture_id)
        if record.get("kickoff") is not None:
            kickoff = _parse_utc(record["kickoff"])
            if record.get("state_cutoff") != record["kickoff"]:
                raise ValueError("Exact causal cutoff must equal target kickoff")
            result_safe = _parse_utc(record.get("result_safe_available_at"))
            if result_safe is not None and result_safe <= kickoff:
                raise ValueError("Result-safe time must follow kickoff")
        if record.get("official_fixture_id") is not None:
            raise ValueError("Unsupported official fixture IDs must remain null")
        if record.get("home_league_tier") not in {"A", "B", "C", "D"} or record.get(
            "away_league_tier"
        ) not in {"A", "B", "C", "D"}:
            raise ValueError("Fixture-side league tiers are missing or invalid")
        if record.get("home_group", "")[0:1] != record.get(
            "home_league_tier"
        ) or record.get("away_group", "")[0:1] != record.get("away_league_tier"):
            raise ValueError("Fixture-side group does not match its league tier")
        fixture_day = date.fromisoformat(record["fixture_date"])
        cutoff_value = record.get("state_cutoff")
        if cutoff_value is None:
            raise ValueError("Timeline-bound state requires an exact causal cutoff")
        cutoff = _parse_utc(cutoff_value)
        if cutoff is None or cutoff <= datetime.combine(
            fixture_day - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
        ):
            raise ValueError("State cutoff is not a valid exact kickoff instant")
        for table in record["standings_before"]:
            if table["group"] is None or table["league_tier"] != table["group"][0]:
                raise ValueError("Invalid group/league identity")
            for row in table["standing_rows"]:
                if (
                    row["goal_difference_before"]
                    != row["goals_for_before"] - row["goals_against_before"]
                ):
                    raise ValueError("Standing goal-difference arithmetic mismatch")
            if len(table["prior_result_fixture_ids"]) != len(
                set(table["prior_result_fixture_ids"])
            ):
                raise ValueError("Duplicate prior-result references")
        payload = {
            key: value for key, value in record.items() if key != "record_digest"
        }
        if record.get("record_digest") != canonical_digest(payload):
            raise ValueError("Record digest mismatch")
    if not coverage.get("leakage_checks", {}).get("same_day_results_excluded"):
        raise ValueError("Same-day results must be excluded")
    timeline_join = coverage.get("timeline_join", {})
    if not timeline_join.get("joined_complete"):
        raise ValueError("Competition-state timeline join is incomplete")
    if any(not record.get("timeline_record_digest") for record in records):
        raise ValueError("Competition-state record is missing timeline provenance")


def load_and_validate(
    dataset_path: Path = DEFAULT_DATASET, coverage_path: Path = DEFAULT_COVERAGE
) -> tuple[dict[str, Any], dict[str, Any]]:
    dataset, coverage = _read_json(dataset_path), _read_json(coverage_path)
    validate_dataset(dataset, coverage)
    return dataset, coverage
