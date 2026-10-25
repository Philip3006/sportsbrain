"""Run the offline, paired causal Nations League context ablation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis.nations_league_context import (
    CONTEXT_CATEGORICAL_FEATURES,
    CONTEXT_NUMERIC_FEATURES,
    render_context_ablation_markdown,
    run_causal_context_ablation,
)
from src.analysis.nations_league_validation import (
    load_local_results,
    predict_dc_event_walk_forward,
    select_historical_matches,
)

EXPECTED_FIXTURES = 512
VALID_FIELD_STATUSES = {
    "SAFE_EXACT",
    "SAFE_BOUND",
    "UNRESOLVED",
    "NOT_APPLICABLE",
}
# The local football-results cache retains the conventional English alias,
# while Builder 4's UEFA fixture timeline uses the official endonym. This is
# an identity-only reconciliation; score, date, venue, and edition must still
# match exactly.
RESULTS_TEAM_IDENTITY_ALIASES = {"Turkey": "Türkiye", "Türkiye": "Türkiye"}


def result_identity_team(value: Any) -> str:
    name = str(value)
    return RESULTS_TEAM_IDENTITY_ALIASES.get(name, name)


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_git_sha(name: str, value: str) -> str:
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value):
        raise ValueError(f"{name} must be a full 40- or 64-character Git SHA")
    return value.lower()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object at {path}")
    return value


def validate_b4_artifact(
    dataset: dict[str, Any],
    coverage: dict[str, Any],
    timeline: dict[str, Any] | None = None,
    *,
    expected_fixtures: int = EXPECTED_FIXTURES,
    expected_dataset_digest: str | None = None,
    expected_coverage_digest: str | None = None,
) -> list[str]:
    """Validate the exact B4 READY artifact and its safe, strictly prior inputs."""
    blockers = []
    if (
        dataset.get("schema_version")
        != "uefa-nations-league-causal-competition-state-v1"
    ):
        blockers.append("unsupported_or_missing_B4_dataset_schema")
    records = dataset.get("records")
    if not isinstance(records, list) or len(records) != expected_fixtures:
        blockers.append("B4_record_count_is_not_exactly_512")
        records = records if isinstance(records, list) else []
    if coverage.get("output_record_count") != len(records):
        blockers.append("B4_coverage_record_count_mismatch")
    if coverage.get("status") != "NL_COMPETITION_STATE_READY":
        blockers.append("B4_competition_state_not_READY")
    ready_gate = coverage.get("ready_gate", {})
    for key in (
        "safe_consumability_complete",
        "causal_timing_complete",
        "all_record_fields_statused",
        "unresolved_optional_fields_explicit",
    ):
        if ready_gate.get(key) is not True:
            blockers.append(f"B4_READY_gate_not_clear:{key}")
    status_contract = coverage.get("field_status_contract", {})
    if set(status_contract.get("values", [])) != VALID_FIELD_STATUSES:
        blockers.append("B4_field_status_contract_invalid")
    if status_contract.get("consumer_rule") != (
        "B5 may select only SAFE_EXACT or SAFE_BOUND fields; "
        "UNRESOLVED and NOT_APPLICABLE are never interpreted as sporting outcomes."
    ):
        blockers.append("B4_field_status_consumer_rule_missing")
    if coverage.get("expected_evaluation_fixture_count") != expected_fixtures:
        blockers.append("B4_coverage_target_is_not_512")
    if coverage.get("fixture_coverage_complete") is not True:
        blockers.append("B4_fixture_coverage_not_complete")
    if coverage.get("official_schedule_match_coverage_verified") is not True:
        blockers.append("B4_official_schedule_coverage_not_verified")
    if coverage.get("dataset_digest") != canonical_digest(dataset):
        blockers.append("B4_dataset_digest_mismatch")
    if not expected_dataset_digest:
        blockers.append("B4_expected_dataset_digest_not_pinned")
    elif coverage.get("dataset_digest") != expected_dataset_digest:
        blockers.append("B4_dataset_digest_not_expected_authoritative_value")
    calculated_coverage_digest = canonical_digest(
        {key: value for key, value in coverage.items() if key != "coverage_digest"}
    )
    if coverage.get("coverage_digest") != calculated_coverage_digest:
        blockers.append("B4_coverage_digest_mismatch")
    if not expected_coverage_digest:
        blockers.append("B4_expected_coverage_digest_not_pinned")
    elif coverage.get("coverage_digest") != expected_coverage_digest:
        blockers.append("B4_coverage_digest_not_expected_authoritative_value")
    fields = coverage.get("fields", {})
    kickoff_coverage = fields.get("kickoff_timestamp", {})
    if (
        kickoff_coverage.get("present") != expected_fixtures
        or kickoff_coverage.get("missing") != 0
        or kickoff_coverage.get("status") != "verified_from_fixture_timeline"
    ):
        blockers.append("B4_full_verified_kickoff_coverage_missing")
    cutoff_coverage = fields.get("causal_date_cutoff", {})
    if cutoff_coverage.get("strict_kickoff_timestamp_comparison") != expected_fixtures:
        blockers.append("B4_exact_causal_kickoff_cutoff_coverage_missing")
    leakage = coverage.get("leakage_checks", {})
    for key in (
        "final_standings_backfilled",
        "final_tables_read",
        "future_match_scores_in_state",
    ):
        if leakage.get(key) is not False:
            blockers.append(f"B4_leakage_check_not_clear:{key}")
    for key in (
        "same_day_results_excluded",
        "uses_only_result_dates_strictly_before_fixture_date",
    ):
        if leakage.get(key) is not True:
            blockers.append(f"B4_leakage_check_not_clear:{key}")

    timeline_records: list[dict[str, Any]] = []
    timeline_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(timeline, dict):
        blockers.append("B4_fixture_timeline_missing")
    else:
        if timeline.get("schema_version") != "uefa-nations-league-fixture-timeline-v1":
            blockers.append("unsupported_or_missing_B4_timeline_schema")
        timeline_records_value = timeline.get("records")
        if (
            not isinstance(timeline_records_value, list)
            or len(timeline_records_value) != expected_fixtures
        ):
            blockers.append("B4_timeline_record_count_is_not_exact")
            timeline_records_value = (
                timeline_records_value
                if isinstance(timeline_records_value, list)
                else []
            )
        timeline_records = timeline_records_value
        timeline_digest = canonical_digest(
            {key: value for key, value in timeline.items() if key != "dataset_digest"}
        )
        if timeline.get("dataset_digest") != timeline_digest:
            blockers.append("B4_timeline_dataset_digest_mismatch")
        timeline_join = coverage.get("timeline_join", {})
        if (
            timeline_join.get("joined_complete") is not True
            or timeline_join.get("joined_records") != expected_fixtures
            or timeline_join.get("missing_fixture_ids") != []
            or timeline_join.get("timeline_dataset_digest") != timeline_digest
        ):
            blockers.append("B4_timeline_join_incomplete_or_digest_mismatch")
        for timeline_record in timeline_records:
            fixture_id = timeline_record.get("fixture_id")
            if not fixture_id or fixture_id in timeline_by_id:
                blockers.append("B4_timeline_fixture_identity_missing_or_duplicate")
                continue
            expected_digest = canonical_digest(
                {
                    key: value
                    for key, value in timeline_record.items()
                    if key != "record_digest"
                }
            )
            if timeline_record.get("record_digest") != expected_digest:
                blockers.append("B4_timeline_record_digest_mismatch")
            timeline_by_id[str(fixture_id)] = timeline_record
        if len(timeline_by_id) != expected_fixtures:
            blockers.append("B4_timeline_fixture_identity_coverage_incomplete")

    ids = set()
    for record in records:
        fixture_id = record.get("fixture_id")
        if not fixture_id or fixture_id in ids:
            blockers.append("B4_fixture_identity_missing_or_duplicate")
            continue
        ids.add(fixture_id)
        expected_record_digest = canonical_digest(
            {key: value for key, value in record.items() if key != "record_digest"}
        )
        if record.get("record_digest") != expected_record_digest:
            blockers.append("B4_record_digest_mismatch")
        if record.get("source_digest") != dataset.get("source_snapshot_digest"):
            blockers.append("B4_record_source_digest_mismatch")
        field_status = record.get("field_status")
        if not isinstance(field_status, dict):
            blockers.append("B4_record_field_status_missing")
        else:
            if not (set(record) - {"record_digest", "field_status"}) <= set(
                field_status
            ):
                blockers.append("B4_record_field_status_coverage_incomplete")
            if any(
                value not in VALID_FIELD_STATUSES for value in field_status.values()
            ):
                blockers.append("B4_record_field_status_value_invalid")
        if not str(fixture_id).startswith("uefa-nl:"):
            blockers.append("B4_fixture_identity_not_canonical")
        try:
            fixture_date = date.fromisoformat(record["fixture_date"])
            cutoff = datetime.fromisoformat(
                str(record["state_cutoff"]).replace("Z", "+00:00")
            )
            kickoff = datetime.fromisoformat(
                str(record["kickoff"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            blockers.append("B4_fixture_date_or_state_cutoff_invalid")
            continue
        if (
            cutoff.tzinfo is None
            or kickoff.tzinfo is None
            or cutoff.utcoffset().total_seconds() != 0
            or kickoff.utcoffset().total_seconds() != 0
        ):
            blockers.append("B4_kickoff_or_cutoff_not_utc")
        if kickoff.date() != fixture_date:
            blockers.append("B4_verified_kickoff_date_mismatch")
        if cutoff != kickoff:
            blockers.append("B4_exact_kickoff_cutoff_mismatch")
        if record.get("kickoff_status") != "verified_from_fixture_timeline":
            blockers.append("B4_kickoff_not_timeline_verified")
        if record.get("state_cutoff_basis") != (
            "strict target kickoff instant; only result_safe_available_at strictly before kickoff is included"
        ):
            blockers.append("B4_strict_result_availability_contract_missing")
        target_timeline = timeline_by_id.get(str(fixture_id))
        if target_timeline is None:
            blockers.append("B4_timeline_identity_missing_for_state_record")
        else:
            for key in (
                "edition",
                "home_team",
                "away_team",
                "home_score",
                "away_score",
            ):
                if record.get(key) != target_timeline.get(key):
                    blockers.append(f"B4_timeline_identity_or_outcome_mismatch:{key}")
            if record.get("fixture_date") != target_timeline.get("date"):
                blockers.append("B4_timeline_fixture_date_mismatch")
            if record.get("kickoff") != target_timeline.get("kickoff_utc"):
                blockers.append("B4_kickoff_does_not_match_timeline")
            if record.get("result_safe_available_at") != target_timeline.get(
                "result_safe_available_at"
            ):
                blockers.append("B4_target_result_safe_time_mismatch")
            if target_timeline.get("status") == "completed_result_recorded":
                try:
                    target_safe_at = datetime.fromisoformat(
                        str(target_timeline["result_safe_available_at"]).replace(
                            "Z", "+00:00"
                        )
                    )
                    if target_safe_at <= kickoff:
                        blockers.append("B4_target_result_safe_time_not_after_kickoff")
                except (KeyError, TypeError, ValueError):
                    blockers.append("B4_target_result_safe_time_invalid")
            elif (
                target_timeline.get("status") != "administratively_awarded"
                or target_timeline.get("result_safe_available_at") is not None
            ):
                blockers.append("B4_target_result_status_or_safe_time_unresolved")
            if record.get("timeline_record_digest") != target_timeline.get(
                "record_digest"
            ):
                blockers.append("B4_timeline_record_digest_binding_mismatch")

        for table in record.get("standings_before", []):
            table_rows = table.get("standing_rows", [])
            row_teams = [row.get("team") for row in table_rows]
            if len(row_teams) != len(set(row_teams)):
                blockers.append("B4_duplicate_standing_team")
                continue
            prior_ids = table.get("prior_result_fixture_ids", [])
            if len(prior_ids) != len(set(prior_ids)):
                blockers.append("B4_duplicate_prior_result_reference")
                continue
            active = {
                row["team"]: row
                for row in table_rows
                if row.get("competition_status") == "active"
            }
            group_records = [
                item
                for item in timeline_records
                if item.get("edition") == record.get("edition")
                and item.get("group") == table.get("group")
            ]
            expected_prior_ids = {
                str(item["fixture_id"])
                for item in group_records
                if item.get("status") == "completed_result_recorded"
                and item.get("result_safe_available_at")
                and item["result_safe_available_at"] < record.get("kickoff", "")
                and item.get("home_team") in active
                and item.get("away_team") in active
            }
            if set(prior_ids) != expected_prior_ids:
                blockers.append("B4_safe_prior_result_identity_set_mismatch")
            stats = {
                team: {
                    "matches": 0,
                    "points": 0,
                    "goals_for": 0,
                    "goals_against": 0,
                    "wins": 0,
                    "draws": 0,
                    "losses": 0,
                }
                for team in active
            }
            for prior_id in prior_ids:
                prior = timeline_by_id.get(str(prior_id))
                if prior is None:
                    blockers.append("B4_prior_result_timeline_identity_missing")
                    continue
                try:
                    prior_safe_at = datetime.fromisoformat(
                        str(prior["result_safe_available_at"]).replace("Z", "+00:00")
                    )
                    prior_kickoff = datetime.fromisoformat(
                        str(prior["kickoff_utc"]).replace("Z", "+00:00")
                    )
                except (KeyError, TypeError, ValueError):
                    blockers.append("B4_prior_result_safe_timestamp_invalid")
                    continue
                if prior_safe_at >= kickoff or prior_kickoff >= kickoff:
                    blockers.append(
                        "B4_prior_result_not_strictly_available_before_kickoff"
                    )
                home, away = prior.get("home_team"), prior.get("away_team")
                if home not in stats or away not in stats:
                    blockers.append("B4_prior_result_not_bound_to_active_table")
                    continue
                home_score, away_score = (
                    prior.get("home_score"),
                    prior.get("away_score"),
                )
                if not isinstance(home_score, int) or not isinstance(away_score, int):
                    blockers.append("B4_prior_result_score_invalid")
                    continue
                for team, goals_for, goals_against in (
                    (home, home_score, away_score),
                    (away, away_score, home_score),
                ):
                    line = stats[team]
                    line["matches"] += 1
                    line["goals_for"] += goals_for
                    line["goals_against"] += goals_against
                if home_score > away_score:
                    stats[home]["points"] += 3
                    stats[home]["wins"] += 1
                    stats[away]["losses"] += 1
                elif home_score < away_score:
                    stats[away]["points"] += 3
                    stats[away]["wins"] += 1
                    stats[home]["losses"] += 1
                else:
                    for team in (home, away):
                        stats[team]["points"] += 1
                        stats[team]["draws"] += 1
            for team, row in active.items():
                counts = stats[team]
                expected_values = {
                    "matches_played_before": counts["matches"],
                    "points_before": counts["points"],
                    "goals_for_before": counts["goals_for"],
                    "goals_against_before": counts["goals_against"],
                    "goal_difference_before": counts["goals_for"]
                    - counts["goals_against"],
                    "wins_before": counts["wins"],
                    "draws_before": counts["draws"],
                    "losses_before": counts["losses"],
                }
                if any(row.get(key) != value for key, value in expected_values.items()):
                    blockers.append(
                        "B4_standings_not_reproducible_from_safe_prior_results"
                    )
                if row.get("goal_difference_before") != (
                    row.get("goals_for_before", 0) - row.get("goals_against_before", 0)
                ):
                    blockers.append("B4_goal_difference_arithmetic_mismatch")
            points = sorted(
                {row["points_before"] for row in active.values()}, reverse=True
            )
            positions = {}
            consumed = 0
            for value in points:
                tied_count = sum(
                    row["points_before"] == value for row in active.values()
                )
                positions[value] = (consumed + 1, consumed + tied_count)
                consumed += tied_count
            for team, row in active.items():
                first, last = positions[row["points_before"]]
                expected_rank = (
                    (first, last, "points_order_unique")
                    if first == last
                    else (None, None, "unresolved_points_tie")
                )
                actual_rank = (
                    row.get("rank_min"),
                    row.get("rank_max"),
                    row.get("rank_status"),
                )
                if actual_rank != expected_rank:
                    blockers.append("B4_points_only_rank_bounds_mismatch")
            future_count = {
                team: sum(
                    1
                    for item in group_records
                    if item.get("kickoff_utc") > record.get("kickoff", "")
                    and team in (item.get("home_team"), item.get("away_team"))
                )
                for team in active
            }
            for team, row in active.items():
                if row.get("remaining_group_matches") != future_count[team]:
                    blockers.append("B4_remaining_group_matches_mismatch")

        remaining = record.get("remaining_schedule", {})
        if isinstance(remaining, dict):
            for item in remaining.get("fixtures", []):
                if "home_score" in item or "away_score" in item:
                    blockers.append("B4_future_schedule_contains_score")
    if timeline_by_id and ids != set(timeline_by_id):
        blockers.append("B4_dataset_timeline_fixture_identity_sets_differ")
    return sorted(set(blockers))


def _standing_for(record: dict[str, Any], side: str) -> dict[str, Any] | None:
    team = record.get(f"{side}_team")
    group = record.get(f"{side}_group")
    if not team or not group:
        return None
    matches = []
    for table in record.get("standings_before", []):
        if table.get("group") != group:
            continue
        matches.extend(
            row for row in table.get("standing_rows", []) if row.get("team") == team
        )
    if len(matches) > 1:
        raise ValueError(
            f"Duplicate B4 standings rows for {record.get('fixture_id')} {side}"
        )
    return matches[0] if matches else None


def _side_state(record: dict[str, Any], kind: str, side: str) -> dict[str, Any]:
    """Read a team-bound participant state, not a fixture-wide outcome flag."""
    direct = record.get(f"{side}_{kind}")
    if isinstance(direct, dict):
        return direct
    nested = record.get(kind, {})
    if isinstance(nested, dict) and isinstance(nested.get(side), dict):
        return nested[side]
    participants = nested.get("participants", {}) if isinstance(nested, dict) else {}
    team_state = participants.get(record.get(f"{side}_team"), {})
    if isinstance(team_state, dict):
        return team_state
    return {}


def derive_context_rows(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Project only supported pre-kickoff table state; unresolved fields stay out."""
    output = []
    for record in records:
        field_status = record.get("field_status", {})

        def field_is_safe(name: str, statuses=field_status) -> bool:
            return statuses.get(name) in {"SAFE_EXACT", "SAFE_BOUND"}

        kickoff = pd.Timestamp(record["kickoff"])
        if kickoff.tzinfo is None:
            kickoff = kickoff.tz_localize("UTC")
        else:
            kickoff = kickoff.tz_convert("UTC")
        side_standings = {
            side: (
                _standing_for(record, side) or {}
                if field_is_safe("standings_before")
                else {}
            )
            for side in ("home", "away")
        }
        supported_bound_values: list[bool] = []
        league_tier = (
            record.get("league_tier") if field_is_safe("league_tier") else None
        )
        if (
            league_tier is None
            and field_is_safe("home_league_tier")
            and field_is_safe("away_league_tier")
        ):
            league_tier = (
                record.get("home_league_tier")
                if record.get("home_league_tier") == record.get("away_league_tier")
                else "cross_tier"
            )
        feature: dict[str, Any] = {
            "fixture_id": record["fixture_id"],
            "record_digest": record["record_digest"],
            "edition": record["edition"],
            "kickoff": kickoff,
            "state_cutoff": pd.Timestamp(record["state_cutoff"]),
            "stage": record.get("stage") if field_is_safe("stage") else None,
            "league_tier": league_tier,
            "group": record.get("group") if field_is_safe("group") else None,
            "outcome": 0
            if record["home_score"] > record["away_score"]
            else 1
            if record["home_score"] == record["away_score"]
            else 2,
        }
        for side in ("home", "away"):
            standing = side_standings[side]
            participant = _side_state(record, "qualification_state", side)
            promotion = participant.get("promotion", {})
            relegation = participant.get("relegation", {})
            matches_played = standing.get("matches_played_before")
            points = standing.get("points_before")
            point_rank_supported = field_is_safe("rank") and standing.get(
                "rank_status"
            ) in {
                "points_order_unique",
                "unresolved_points_tie",
            }
            promotion_possible = None
            if (
                field_is_safe("promotion_state")
                and promotion.get("status")
                == "points_bounds_only_tiebreaks_preserved_as_unresolved"
                and isinstance(promotion.get("can_be_promoted"), bool)
            ):
                promotion_possible = promotion["can_be_promoted"]
            relegation_possible = None
            if (
                field_is_safe("relegation_state")
                and relegation.get("status")
                == "points_bounds_only_tiebreaks_preserved_as_unresolved"
                and isinstance(relegation.get("can_be_relegated"), bool)
            ):
                relegation_possible = relegation["can_be_relegated"]
            supported_bound_values.extend(
                value
                for value in (promotion_possible, relegation_possible)
                if value is not None
            )
            feature.update(
                {
                    f"{side}_points_before": standing.get("points_before"),
                    f"{side}_matches_played_before": standing.get(
                        "matches_played_before"
                    ),
                    f"{side}_goal_difference_before": standing.get(
                        "goal_difference_before"
                    ),
                    f"{side}_goals_for_before": standing.get("goals_for_before"),
                    f"{side}_goals_against_before": standing.get(
                        "goals_against_before"
                    ),
                    f"{side}_points_per_game_before": (
                        points / matches_played
                        if isinstance(points, int)
                        and isinstance(matches_played, int)
                        and matches_played > 0
                        else None
                    ),
                    f"{side}_remaining_games_before": (
                        standing.get("remaining_group_matches")
                        if field_is_safe("remaining_schedule")
                        else None
                    ),
                    f"{side}_table_position_min": (
                        standing.get("rank_min") if point_rank_supported else None
                    ),
                    f"{side}_table_position_max": (
                        standing.get("rank_max") if point_rank_supported else None
                    ),
                    f"{side}_table_position_tied": (
                        standing.get("rank_status") == "unresolved_points_tie"
                        if point_rank_supported
                        else None
                    ),
                    f"{side}_points_bound_promotion_possible": promotion_possible,
                    f"{side}_points_bound_relegation_possible": relegation_possible,
                }
            )
        feature["points_diff_home_minus_away"] = (
            (feature["home_points_before"] - feature["away_points_before"])
            if feature["home_points_before"] is not None
            and feature["away_points_before"] is not None
            else None
        )
        played = [
            standing.get("matches_played_before")
            for standing in side_standings.values()
        ]
        if (
            not field_is_safe("stage")
            or record.get("stage") != "league_phase"
            or not field_is_safe("remaining_schedule")
            or any(not isinstance(value, int) for value in played)
        ):
            feature["group_phase_progress"] = "not_group_stage_or_unavailable"
        elif max(played) <= 2:
            feature["group_phase_progress"] = "early_by_matches_played"
        elif min(played) >= 4:
            feature["group_phase_progress"] = "late_by_matches_played"
        else:
            feature["group_phase_progress"] = "middle_by_matches_played"
        point_values = [
            side_standings[side].get("points_before") for side in ("home", "away")
        ]
        if all(isinstance(value, int) for value in point_values):
            points_gap = abs(point_values[0] - point_values[1])
            feature["points_gap_band"] = (
                "level"
                if points_gap == 0
                else "tight_1_to_3"
                if points_gap <= 3
                else "wide_4_plus"
            )
        else:
            feature["points_gap_band"] = "unavailable"
        feature["points_bound_constraint"] = (
            "constrained_by_supported_points_bound"
            if any(value is False for value in supported_bound_values)
            else "no_closed_supported_points_bound"
            if supported_bound_values
            else "unavailable"
        )
        output.append(feature)
    frame = pd.DataFrame(output)
    # The B4 source uses nullable Boolean objects for points-bound fields.
    # Convert the declared numeric model contract to float/NaN so sklearn's
    # fold-local imputer sees missing values consistently across editions.
    for column in CONTEXT_NUMERIC_FEATURES:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competition-state", required=True, type=Path)
    parser.add_argument("--coverage", required=True, type=Path)
    parser.add_argument("--timeline", required=True, type=Path)
    parser.add_argument("--results-cache", required=True, type=Path)
    parser.add_argument("--source-main-sha", required=True)
    parser.add_argument("--competition-state-source-sha", required=True)
    parser.add_argument("--competition-state-source-pr", type=int)
    parser.add_argument("--expected-dataset-digest", required=True)
    parser.add_argument("--expected-coverage-digest", required=True)
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("results/audits/nations_league_context_ablation_v1.json"),
    )
    parser.add_argument(
        "--output-markdown",
        type=Path,
        default=Path("results/audits/nations_league_context_ablation_v1.md"),
    )
    return parser


def _write_report(audit: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_context_ablation_markdown(audit), encoding="utf-8")


def main() -> int:
    args = build_parser().parse_args()
    for name, value in (
        ("--source-main-sha", args.source_main_sha),
        ("--competition-state-source-sha", args.competition_state_source_sha),
    ):
        try:
            validate_git_sha(name, value)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if (
        args.competition_state_source_pr is not None
        and args.competition_state_source_pr < 1
    ):
        raise SystemExit("--competition-state-source-pr must be positive")
    dataset = read_json(args.competition_state)
    coverage = read_json(args.coverage)
    timeline = read_json(args.timeline)
    blockers = validate_b4_artifact(
        dataset,
        coverage,
        timeline,
        expected_dataset_digest=args.expected_dataset_digest,
        expected_coverage_digest=args.expected_coverage_digest,
    )
    dataset_digest = canonical_digest(dataset)
    audit: dict[str, Any]
    if blockers:
        audit = {
            "schema": "nations-league-safe-context-ablation-audit-v1",
            "status": "NL_SAFE_CONTEXT_BLOCKED",
            "evidence_state": "B4_safe_subset_integrity_validation_failed",
            "competition_state_dataset": "results/research/nations_league_competition_state_v1.json",
            "competition_state_dataset_sha256": dataset_digest,
            "competition_state_artifact_file_sha256": file_digest(
                args.competition_state
            ),
            "competition_state_status": coverage.get("status"),
            "b4_coverage_digest": coverage.get("coverage_digest"),
            "b4_coverage_file_sha256": file_digest(args.coverage),
            "b4_timeline_digest": timeline.get("dataset_digest"),
            "b4_timeline_file_sha256": file_digest(args.timeline),
            "competition_state_source_sha": args.competition_state_source_sha.lower(),
            "competition_state_source_pr": args.competition_state_source_pr,
            "source_main_sha": args.source_main_sha,
            "artifact_source_status": "builder4_ready_or_invalid_safe_subset",
            "b4_ready_integrity_validation_passed": False,
            "eligible_fixtures": len(dataset.get("records", [])),
            "fixtures_evaluated": 0,
            "coverage": 0.0,
            "baseline": None,
            "baseline_plus_context": None,
            "paired_difference_context_minus_baseline": None,
            "paired_date_cluster_bootstrap": None,
            "strata": None,
            "synthetic_evidence_used": False,
            "blockers": blockers,
            "excluded_features": [
                "B4 integrity or strict result-availability validation failed",
                "all unresolved advanced competition-state features are excluded",
                "causal GBT: no certified row-level forecasts supplied",
            ],
            "stacker_recommendation": "Do not include until the safe-subset causal ablation completes.",
            "production_hook": False,
            "activation_changed": False,
            "publication_changed": False,
            "betting_or_ledger_changed": False,
        }
        _write_report(audit, args.output_json, args.output_markdown)
        print(json.dumps({"status": audit["status"], "blockers": blockers}))
        return 2

    records = dataset["records"]
    if len({record["fixture_id"] for record in records}) != EXPECTED_FIXTURES:
        raise ValueError("B4 fixture identity set is not unique and complete")
    timeline_by_id = {row["fixture_id"]: row for row in timeline["records"]}
    administrative_records = [
        record
        for record in records
        if timeline_by_id[record["fixture_id"]].get("status")
        == "administratively_awarded"
    ]
    played_records = [
        record
        for record in records
        if timeline_by_id[record["fixture_id"]].get("status")
        == "completed_result_recorded"
    ]
    if len(played_records) + len(administrative_records) != EXPECTED_FIXTURES:
        raise ValueError("B4 cohort contains unsupported outcome status values")
    results, baseline_source_sha = load_local_results(args.results_cache)
    selected_results = select_historical_matches(results)

    def identity(row: Any) -> tuple[str, str, str, str, str]:
        return (
            str(row.edition),
            str(row.validation_period),
            pd.Timestamp(row.date).date().isoformat(),
            result_identity_team(row.home_team),
            result_identity_team(row.away_team),
        )

    def b4_identity(record: dict[str, Any]) -> tuple[str, str, str, str, str]:
        return (
            str(record["edition"]),
            str(record["validation_period"]),
            str(record["fixture_date"]),
            result_identity_team(record["home_team"]),
            result_identity_team(record["away_team"]),
        )

    result_by_identity = {
        identity(row): row for row in selected_results.itertuples(index=False)
    }
    b4_by_identity = {b4_identity(record): record for record in records}
    if len(result_by_identity) != EXPECTED_FIXTURES or set(result_by_identity) != set(
        b4_by_identity
    ):
        raise ValueError(
            "Local historical results do not exactly match the 512 B4 fixture identities"
        )
    for key, record in b4_by_identity.items():
        result = result_by_identity[key]
        if (
            int(result.home_score) != record["home_score"]
            or int(result.away_score) != record["away_score"]
            or bool(result.neutral) != record["neutral"]
        ):
            raise ValueError(
                f"Local result scores or venue semantics disagree with B4 fixture {record['fixture_id']}"
            )
    local_result_by_fixture = {
        record["fixture_id"]: result_by_identity[b4_identity(record)]
        for record in records
    }
    administrative_result_keys = {
        (
            pd.Timestamp(result.date).date().isoformat(),
            str(result.home_team),
            str(result.away_team),
        )
        for result in (
            local_result_by_fixture[record["fixture_id"]]
            for record in administrative_records
        )
    }
    results_for_model = results.loc[
        ~results.apply(
            lambda row: (
                (
                    str(pd.Timestamp(row["date"]).date()),
                    str(row["home_team"]),
                    str(row["away_team"]),
                )
                in administrative_result_keys
                and str(row["tournament"]) == "UEFA Nations League"
            ),
            axis=1,
        )
    ].copy()
    matches = pd.DataFrame(
        [
            {
                "date": pd.Timestamp(
                    local_result_by_fixture[record["fixture_id"]].date
                ),
                "home_team": local_result_by_fixture[record["fixture_id"]].home_team,
                "away_team": local_result_by_fixture[record["fixture_id"]].away_team,
                "home_score": local_result_by_fixture[record["fixture_id"]].home_score,
                "away_score": local_result_by_fixture[record["fixture_id"]].away_score,
                "neutral": local_result_by_fixture[record["fixture_id"]].neutral,
                "edition": record["edition"],
                "validation_period": record["validation_period"],
            }
            for record in played_records
        ]
    )
    baseline_predictions = predict_dc_event_walk_forward(results_for_model, matches)
    examples = derive_context_rows(records)
    examples["causal_information_verified"] = True
    probability_rows = []
    expected_keys = {
        (row.date, row.home_team, row.away_team)
        for row in matches.itertuples(index=False)
    }
    if len(expected_keys) != len(played_records) or len(baseline_predictions) != len(
        expected_keys
    ):
        raise ValueError(
            "Causal DC baseline did not cover every played fixture; no partial ablation is permitted"
        )
    prediction_by_fixture: dict[str, dict[str, Any]] = {}
    for record in played_records:
        local_result = local_result_by_fixture[record["fixture_id"]]
        prediction = baseline_predictions[
            (
                pd.Timestamp(local_result.date),
                local_result.home_team,
                local_result.away_team,
            )
        ]
        if (
            not prediction.get("training_max_date")
            or prediction["training_max_date"] >= record["fixture_date"]
            or prediction.get("training_cutoff_exclusive", "") > record["fixture_date"]
        ):
            raise ValueError(
                f"Causal DC forecast cutoff is not strictly before fixture {record['fixture_id']}"
            )
        prediction_by_fixture[record["fixture_id"]] = prediction
    for _, row in examples.iterrows():
        if row["fixture_id"] not in prediction_by_fixture:
            continue
        baseline_probability = prediction_by_fixture[row["fixture_id"]]["probabilities"]
        probability_rows.append(
            {
                **row.to_dict(),
                "base_p_home": baseline_probability[0],
                "base_p_draw": baseline_probability[1],
                "base_p_away": baseline_probability[2],
            }
        )
    evaluation_examples = pd.DataFrame(probability_rows)
    audit = run_causal_context_ablation(
        evaluation_examples,
        [record["fixture_id"] for record in played_records],
        numeric_context=CONTEXT_NUMERIC_FEATURES,
        categorical_context=CONTEXT_CATEGORICAL_FEATURES,
        provenance={
            "competition_state_dataset_sha256": dataset_digest,
            "competition_state_artifact_path": "results/research/nations_league_competition_state_v1.json",
            "competition_state_artifact_file_sha256": file_digest(
                args.competition_state
            ),
            "b4_coverage_digest": coverage["coverage_digest"],
            "b4_coverage_file_sha256": file_digest(args.coverage),
            "b4_timeline_dataset_sha256": timeline["dataset_digest"],
            "b4_timeline_artifact_file_sha256": file_digest(args.timeline),
            "b4_timeline_artifact_path": "results/research/nations_league_fixture_timeline_v1.json",
            "competition_state_source_sha": args.competition_state_source_sha.lower(),
            "competition_state_source_pr": args.competition_state_source_pr,
            "baseline_source_sha256": baseline_source_sha,
            "baseline_method": "existing causal event-level Dixon-Coles walk-forward",
            "source_main_sha": args.source_main_sha,
            "historical_fixture_identity_coverage": len(result_by_identity),
            "identity_only_team_aliases": {"Turkey": "Türkiye"},
            "identity_only_alias_matches": sum(
                1
                for row in selected_results.itertuples(index=False)
                if "Turkey" in (str(row.home_team), str(row.away_team))
            ),
            "baseline_forecast_coverage": len(baseline_predictions),
            "b4_artifact_status": coverage.get("status"),
        },
    )
    audit["b4_ready_integrity_validation_passed"] = True
    audit["b4_field_status_allowlist"] = ["SAFE_EXACT", "SAFE_BOUND"]
    audit["fixture_coverage"] = {
        "canonical_b4_identities": len(records),
        "verified_kickoffs": sum(
            record.get("kickoff_status") == "verified_from_fixture_timeline"
            for record in records
        ),
        "exact_causal_kickoff_cutoffs": sum(
            record.get("state_cutoff") == record.get("kickoff") for record in records
        ),
        "local_result_identity_matches": len(result_by_identity),
        "played_fixtures_in_primary_evaluation": len(played_records),
        "administratively_decided_outcomes_excluded": len(administrative_records),
        "administratively_decided_fixture_ids_excluded": sorted(
            record["fixture_id"] for record in administrative_records
        ),
        "raw_causal_baseline_forecasts": len(baseline_predictions),
        "context_available_fixtures": int(
            examples.loc[
                examples["fixture_id"].isin(
                    [record["fixture_id"] for record in played_records]
                ),
                ["home_points_before", "away_points_before"],
            ]
            .notna()
            .all(axis=1)
            .sum()
        ),
        "edition_counts_source": dict(
            sorted(Counter(r["edition"] for r in records).items())
        ),
        "edition_counts_evaluated": dict(
            sorted(Counter(r["edition"] for r in played_records).items())
        ),
    }
    audit["included_features"] = {
        "numeric": list(CONTEXT_NUMERIC_FEATURES),
        "categorical": list(CONTEXT_CATEGORICAL_FEATURES),
    }
    audit["context_feature_non_null_counts"] = {
        feature: int(examples[feature].notna().sum())
        for feature in (*CONTEXT_NUMERIC_FEATURES, *CONTEXT_CATEGORICAL_FEATURES)
    }
    audit["excluded_features"] = {
        "inferred_matchday": "not present in #215 timeline evidence; never inferred",
        "official_uefa_fixture_ids": "unavailable and not a predictive feature",
        "article_15_tiebreak_outputs": "unresolved or missing source inputs",
        "disciplinary_and_access_list_tiebreaks": "no source data in the B4 artifact",
        "c_league_relegation_allocation": "edition-specific allocation unresolved",
        "exact_must_win_and_qualification_labels": "UNRESOLVED under the final B4 field-status contract",
        "NOT_APPLICABLE_fields": "not encoded as predictive categories or outcomes; treated as unavailable",
        "UNRESOLVED_fields": "not encoded as predictive categories or outcomes; treated as unavailable",
        "subjective_motivation": "not defined or used",
        "causal_gbt": "no certified row-level causal GBT forecast artifact supplied on current main or #216",
    }
    audit["causal_gbt_comparison"] = "not_run_no_certified_row_level_forecasts"
    audit["outcome_label_policy"] = {
        "primary_evaluation": "played/completed-result fixtures only",
        "administrative_outcomes": "excluded from scoring and DC training because no played-match result-safe timestamp exists",
        "excluded_fixture_ids": sorted(
            record["fixture_id"] for record in administrative_records
        ),
    }
    audit["stacker_recommendation"] = (
        "Safe context may enter a later research-only stacker test; this ablation is not production evidence."
        if audit["status"] == "NL_SAFE_CONTEXT_GAIN"
        else "Do not include as a stacker candidate based on this result; no supported safe-context gain was established."
    )
    audit["production_hook"] = False
    audit["activation_changed"] = False
    audit["publication_changed"] = False
    audit["betting_or_ledger_changed"] = False
    _write_report(audit, args.output_json, args.output_markdown)
    print(
        json.dumps(
            {
                "status": audit["status"],
                "fixtures_evaluated": audit["fixtures_evaluated"],
                "eligible_fixtures": audit["eligible_fixtures"],
                "competition_state_dataset_sha256": dataset_digest,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
