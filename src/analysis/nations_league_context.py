"""Causal, offline UEFA Nations League standings and match-context features.

This module consumes an explicitly supplied fixture schedule, edition-specific
group rules, and fixture-keyed results. It does not fetch data or connect to a
scanner, signal detector, model runtime, publisher, or betting/ledger path.
Every feature for a target fixture is computed from fixtures strictly earlier
than its kickoff. When timestamps have only date precision, all fixtures on the
same date are conservatively excluded from one another's pre-match standings.

Scenario flags use points-only reachability. Ties are treated optimistically
for "still possible" and conservatively for "clinched/sufficient"; this avoids
pretending to know scorelines or unresolved future tie-breakers.
"Near must win" and goal-relative draw utility are emitted only when a caller
provides an explicit competition-defined mathematical goal; no subjective
threshold is inferred.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import pairwise, product
from typing import Any

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

SCHEDULE_COLUMNS = {
    "fixture_id",
    "edition",
    "league",
    "group",
    "matchday",
    "kickoff",
    "home_team",
    "away_team",
}
RESULT_COLUMNS = {"fixture_id", "home_score", "away_score"}
RULE_KEYS = {
    "qualification_slots",
    "promotion_slots",
    "relegation_slots",
    "relegation_playoff_slots",
    "expected_fixtures_per_team",
    "table_tiebreakers",
}
TIEBREAKERS = {
    "points",
    "goal_difference",
    "goals_for",
    "goals_against",
    "head_to_head_points",
    "head_to_head_goal_difference",
    "head_to_head_goals_for",
}
MATHEMATICAL_GOALS = {
    "qualification",
    "promotion",
    "avoid_relegation",
}
MAX_SCENARIO_COMBINATIONS = 1_000_000
OUTCOMES = (0, 1, 2)  # scheduled home win, draw, scheduled away win
CONTEXT_NUMERIC_FEATURES = (
    "matchday",
    "home_matches_played_before",
    "away_matches_played_before",
    "home_points_before",
    "away_points_before",
    "points_diff_home_minus_away",
    "home_goal_difference_before",
    "away_goal_difference_before",
    "home_remaining_games_before",
    "away_remaining_games_before",
    "home_table_position_min",
    "home_table_position_max",
    "away_table_position_min",
    "away_table_position_max",
    "home_table_position_tied",
    "away_table_position_tied",
    "home_points_to_qualification_boundary",
    "away_points_to_qualification_boundary",
    "home_points_gap_to_promotion",
    "away_points_gap_to_promotion",
    "home_points_to_relegation_boundary",
    "away_points_to_relegation_boundary",
    "home_points_gap_to_relegation_playoff",
    "away_points_gap_to_relegation_playoff",
    "home_qualification_still_possible",
    "away_qualification_still_possible",
    "home_mathematically_qualified",
    "away_mathematically_qualified",
    "home_mathematically_eliminated",
    "away_mathematically_eliminated",
    "home_mathematically_promoted",
    "away_mathematically_promoted",
    "home_mathematically_relegated",
    "away_mathematically_relegated",
    "home_mathematically_safe_from_relegation",
    "away_mathematically_safe_from_relegation",
    "home_promotion_still_possible",
    "away_promotion_still_possible",
    "home_relegation_still_possible",
    "away_relegation_still_possible",
    "home_win_required_for_mathematical_goal",
    "away_win_required_for_mathematical_goal",
    "home_draw_sufficient_for_mathematical_goal",
    "away_draw_sufficient_for_mathematical_goal",
    "home_loss_eliminates",
    "away_loss_eliminates",
    "mathematically_consequential",
)
CONTEXT_CATEGORICAL_FEATURES = ("league_tier", "group", "mathematical_goal")


def _required_columns(frame: pd.DataFrame, required: set[str], label: str) -> None:
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{label} missing required columns: {sorted(missing)}")


def _normalize_inputs(
    schedule: pd.DataFrame,
    results: pd.DataFrame,
    group_rules: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    _required_columns(schedule, SCHEDULE_COLUMNS, "Schedule")
    _required_columns(results, RESULT_COLUMNS, "Results")
    fixtures = schedule.copy().reset_index(drop=True)
    scores = results.copy().reset_index(drop=True)

    for column in (
        "fixture_id",
        "edition",
        "league",
        "group",
        "home_team",
        "away_team",
    ):
        fixtures[column] = fixtures[column].astype("string").str.strip()
        if fixtures[column].isna().any() or fixtures[column].eq("").any():
            raise ValueError(f"Schedule contains an empty {column}")
    fixtures["league"] = fixtures["league"].str.upper()
    if not fixtures["league"].isin({"A", "B", "C", "D"}).all():
        raise ValueError("Schedule league must be one of A/B/C/D")
    if fixtures["fixture_id"].duplicated().any():
        raise ValueError("Schedule fixture_id values must be unique")
    if fixtures["home_team"].eq(fixtures["away_team"]).any():
        raise ValueError("A fixture cannot contain the same home and away team")
    fixtures["kickoff"] = pd.to_datetime(fixtures["kickoff"], errors="raise", utc=True)
    fixtures["matchday"] = pd.to_numeric(fixtures["matchday"], errors="raise")
    if (fixtures["matchday"] < 1).any() or not np.equal(
        fixtures["matchday"] % 1, 0
    ).all():
        raise ValueError("Schedule matchday values must be positive integers")
    fixtures["matchday"] = fixtures["matchday"].astype(int)

    scores["fixture_id"] = scores["fixture_id"].astype("string").str.strip()
    if scores["fixture_id"].isna().any() or scores["fixture_id"].eq("").any():
        raise ValueError("Results contain an empty fixture_id")
    if scores["fixture_id"].duplicated().any():
        raise ValueError("Results contain duplicate fixture_id values")
    unknown_results = set(scores["fixture_id"]) - set(fixtures["fixture_id"])
    if unknown_results:
        raise ValueError(
            "Results contain fixture IDs absent from the supplied schedule"
        )
    for column in ("home_score", "away_score"):
        numeric = pd.to_numeric(scores[column], errors="coerce")
        if (
            numeric.isna().any()
            or (numeric < 0).any()
            or not np.equal(numeric % 1, 0).all()
        ):
            raise ValueError(
                f"Results {column} must contain non-negative integer scores"
            )
        scores[column] = numeric.astype(int)

    group_keys = set(zip(fixtures["edition"], fixtures["league"], fixtures["group"]))
    for key in group_keys:
        rule = group_rules.get(tuple(key))
        if rule is None:
            raise ValueError(
                f"Missing explicit group rules for edition/league/group {tuple(key)}"
            )
        if not RULE_KEYS.issubset(rule):
            raise ValueError(
                f"Group rules for {tuple(key)} must define {sorted(RULE_KEYS)}"
            )
        tiebreakers = rule["table_tiebreakers"]
        if not isinstance(tiebreakers, (tuple, list)) or not tiebreakers:
            raise ValueError(
                f"Group rules for {tuple(key)} need ordered table_tiebreakers"
            )
        if any(item not in TIEBREAKERS for item in tiebreakers):
            raise ValueError(
                f"Unsupported table tiebreaker in group rules for {tuple(key)}"
            )
        team_count = len(
            set(
                fixtures.loc[
                    fixtures["edition"].eq(key[0])
                    & fixtures["league"].eq(key[1])
                    & fixtures["group"].eq(key[2]),
                    ["home_team", "away_team"],
                ]
                .to_numpy()
                .ravel()
            )
        )
        if (
            isinstance(rule["expected_fixtures_per_team"], bool)
            or not isinstance(rule["expected_fixtures_per_team"], (int, np.integer))
            or rule["expected_fixtures_per_team"] < 1
        ):
            raise ValueError(
                f"expected_fixtures_per_team must be a positive integer for {tuple(key)}"
            )
        group_rows = fixtures.loc[
            fixtures["edition"].eq(key[0])
            & fixtures["league"].eq(key[1])
            & fixtures["group"].eq(key[2])
        ]
        appearances = pd.concat(
            [group_rows["home_team"], group_rows["away_team"]]
        ).value_counts()
        if (
            len(appearances) != team_count
            or not appearances.eq(rule["expected_fixtures_per_team"]).all()
        ):
            raise ValueError(
                f"Schedule is incomplete/contradictory for group {tuple(key)}"
            )
        repeated_at_kickoff = group_rows.assign(
            pair=group_rows.apply(
                lambda row: "|".join(sorted((row["home_team"], row["away_team"]))),
                axis=1,
            )
        ).duplicated(["kickoff", "pair"])
        if repeated_at_kickoff.any():
            raise ValueError(
                f"Schedule duplicates a group fixture at one kickoff for {tuple(key)}"
            )
        for field in (
            "qualification_slots",
            "promotion_slots",
            "relegation_slots",
            "relegation_playoff_slots",
        ):
            value = rule[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, np.integer))
                or not 0 <= value <= team_count
            ):
                raise ValueError(
                    f"{field} must be an integer from zero through group size"
                )
        if rule["relegation_slots"] + rule["relegation_playoff_slots"] > team_count:
            raise ValueError(
                f"Relegation and playoff slots exceed group size for {tuple(key)}"
            )
        objective = rule.get("mathematical_goal")
        if objective is not None and objective not in MATHEMATICAL_GOALS:
            raise ValueError(f"Unsupported mathematical_goal for group {tuple(key)}")

    return fixtures, scores


def _empty_table(teams: Sequence[str]) -> dict[str, dict[str, int]]:
    return {
        team: {"points": 0, "goals_for": 0, "goals_against": 0, "goal_difference": 0}
        for team in teams
    }


def _add_match(
    table: dict[str, dict[str, int]], home: str, away: str, hg: int, ag: int
) -> None:
    table[home]["goals_for"] += hg
    table[home]["goals_against"] += ag
    table[away]["goals_for"] += ag
    table[away]["goals_against"] += hg
    table[home]["goal_difference"] += hg - ag
    table[away]["goal_difference"] += ag - hg
    table[home]["points"] += 3 if hg > ag else (1 if hg == ag else 0)
    table[away]["points"] += 3 if ag > hg else (1 if hg == ag else 0)


def _table_order(
    teams: Sequence[str],
    prior: pd.DataFrame,
    tiebreakers: Sequence[str],
) -> tuple[list[str], dict[str, tuple[int, int]], dict[str, dict[str, int]]]:
    """Rank a pre-match table with caller-declared rules; unresolved ties stay ties."""
    stats = _empty_table(teams)
    for row in prior.itertuples(index=False):
        _add_match(
            stats,
            row.home_team,
            row.away_team,
            int(row.home_score),
            int(row.away_score),
        )

    groups: list[list[str]] = [sorted(teams)]
    for criterion in tiebreakers:
        refined: list[list[str]] = []
        for tied in groups:
            if len(tied) < 2:
                refined.append(tied)
                continue
            values: dict[str, int] = {}
            if criterion.startswith("head_to_head_"):
                metric = criterion.removeprefix("head_to_head_")
                mini = _empty_table(tied)
                for row in prior.itertuples(index=False):
                    if row.home_team in mini and row.away_team in mini:
                        _add_match(
                            mini,
                            row.home_team,
                            row.away_team,
                            int(row.home_score),
                            int(row.away_score),
                        )
                if metric == "points":
                    values = {team: mini[team]["points"] for team in tied}
                elif metric == "goal_difference":
                    values = {
                        team: mini[team]["goals_for"] - mini[team]["goals_against"]
                        for team in tied
                    }
                elif metric == "goals_for":
                    values = {team: mini[team]["goals_for"] for team in tied}
            else:
                values = {team: stats[team][criterion] for team in tied}
            for value in sorted(set(values.values()), reverse=True):
                refined.append(sorted(team for team in tied if values[team] == value))
        groups = refined

    order: list[str] = []
    positions: dict[str, tuple[int, int]] = {}
    next_position = 1
    for tied in groups:
        low, high = next_position, next_position + len(tied) - 1
        for team in tied:
            positions[team] = (low, high)
        order.extend(tied)
        next_position = high + 1
    for team, values in stats.items():
        values["goal_difference"] = values["goals_for"] - values["goals_against"]
    return order, positions, stats


def _slots_margin(
    team: str,
    order: Sequence[str],
    positions: Mapping[str, tuple[int, int]],
    stats: Mapping[str, Mapping[str, int]],
    slots: int,
    field: str,
) -> int | None:
    if slots == 0:
        return None
    if positions[team][0] <= slots:
        return 0
    cutoff_team = order[slots - 1]
    return max(0, int(stats[cutoff_team][field] - stats[team][field]))


def _gap_to_bottom_zone(
    team: str,
    order: Sequence[str],
    positions: Mapping[str, tuple[int, int]],
    stats: Mapping[str, Mapping[str, int]],
    relegation_slots: int,
    playoff_slots: int,
) -> int | None:
    if relegation_slots + playoff_slots == 0:
        return None
    zone_start = len(order) - relegation_slots - playoff_slots + 1
    if positions[team][1] >= zone_start:
        return 0
    cutoff_team = order[zone_start - 1]
    return max(0, int(stats[cutoff_team]["points"] - stats[team]["points"]))


def _scenario_summary(
    team: str,
    target_fixture_id: str,
    remaining: pd.DataFrame,
    starting_points: Mapping[str, int],
    qualification_slots: int,
    promotion_slots: int,
    relegation_slots: int,
) -> dict[str, bool | None]:
    fixture_rows = list(remaining.itertuples(index=False))
    if not any(row.fixture_id == target_fixture_id for row in fixture_rows):
        raise ValueError(
            "Target fixture is missing from its pre-match remaining schedule"
        )
    combination_count = 3 ** len(fixture_rows)
    if combination_count > MAX_SCENARIO_COMBINATIONS:
        raise ValueError(
            "Group schedule exceeds safe points-scenario enumeration limit"
        )
    team_count = len(starting_points)
    target_row = next(
        row for row in fixture_rows if row.fixture_id == target_fixture_id
    )
    target_team_side = "home" if target_row.home_team == team else "away"

    def final_points(outcomes: Sequence[int]) -> dict[str, int]:
        points = dict(starting_points)
        for row, outcome in zip(fixture_rows, outcomes, strict=True):
            if outcome == 0:
                points[row.home_team] += 3
            elif outcome == 2:
                points[row.away_team] += 3
            else:
                points[row.home_team] += 1
                points[row.away_team] += 1
        return points

    def target_result(outcome: int) -> str:
        if outcome == 1:
            return "draw"
        return (
            "win"
            if (outcome == 0 and target_team_side == "home")
            or (outcome == 2 and target_team_side == "away")
            else "loss"
        )

    def possible_for_slots(points: Mapping[str, int], slots: int) -> bool:
        return (
            slots > 0
            and sum(
                value > points[team] for other, value in points.items() if other != team
            )
            < slots
        )

    def guaranteed_in_slots(points: Mapping[str, int], slots: int) -> bool:
        return (
            slots > 0
            and sum(
                value >= points[team]
                for other, value in points.items()
                if other != team
            )
            < slots
        )

    safe_slots = team_count - relegation_slots
    any_qual_possible = False
    every_scenario_qual_guaranteed = qualification_slots > 0
    every_scenario_relegated = relegation_slots > 0
    draw_scenarios = 0
    every_draw_qual_guaranteed = qualification_slots > 0
    nonwin_qual_possible = False
    win_qual_possible = False
    loss_qual_possible = False
    any_promotion_possible = False
    every_scenario_promotion_guaranteed = promotion_slots > 0
    nonwin_promotion_possible = False
    win_promotion_possible = False
    loss_promotion_possible = False
    every_draw_promotion_guaranteed = promotion_slots > 0
    draw_scenarios_for_promotion = 0
    any_relegation_possible = False
    any_safe_possible = False
    every_scenario_safe = safe_slots > 0
    every_draw_safe = safe_slots > 0
    draw_scenarios_for_safety = 0
    nonwin_safe_possible = False
    win_safe_possible = False
    loss_safe_possible = False
    goal_possibility_by_result = {
        "qualification": {"win": False, "draw": False, "loss": False},
        "promotion": {"win": False, "draw": False, "loss": False},
        "relegation_safety": {"win": False, "draw": False, "loss": False},
    }
    goal_guarantee_by_result = {
        "qualification": {"win": True, "draw": True, "loss": True},
        "promotion": {"win": True, "draw": True, "loss": True},
        "relegation_safety": {"win": True, "draw": True, "loss": True},
    }

    for outcomes in product(OUTCOMES, repeat=len(fixture_rows)):
        points = final_points(outcomes)
        target_outcome = target_result(
            outcomes[
                next(
                    i
                    for i, row in enumerate(fixture_rows)
                    if row.fixture_id == target_fixture_id
                )
            ]
        )
        qual_possible = possible_for_slots(points, qualification_slots)
        qual_guaranteed = guaranteed_in_slots(points, qualification_slots)
        promotion_guaranteed = guaranteed_in_slots(points, promotion_slots)
        every_scenario_promotion_guaranteed &= promotion_guaranteed
        any_qual_possible |= qual_possible
        promotion_possible = possible_for_slots(points, promotion_slots)
        any_promotion_possible |= promotion_possible
        goal_possibility_by_result["qualification"][target_outcome] |= qual_possible
        goal_possibility_by_result["promotion"][target_outcome] |= promotion_possible
        goal_guarantee_by_result["qualification"][target_outcome] &= qual_guaranteed
        goal_guarantee_by_result["promotion"][target_outcome] &= promotion_guaranteed
        if target_outcome == "win":
            win_qual_possible |= qual_possible
            win_promotion_possible |= promotion_possible
        elif target_outcome == "loss":
            loss_qual_possible |= qual_possible
            loss_promotion_possible |= promotion_possible
        every_scenario_qual_guaranteed &= qual_guaranteed
        safe_guaranteed = (
            safe_slots > 0
            and sum(
                value >= points[team]
                for other, value in points.items()
                if other != team
            )
            < safe_slots
        )
        relegated_guaranteed = (
            relegation_slots > 0
            and sum(
                value > points[team] for other, value in points.items() if other != team
            )
            >= safe_slots
        )
        relegated_possible = (
            relegation_slots > 0
            and sum(
                value >= points[team]
                for other, value in points.items()
                if other != team
            )
            >= safe_slots
        )
        any_relegation_possible |= relegated_possible
        every_scenario_safe &= safe_guaranteed
        every_scenario_relegated &= relegated_guaranteed
        if target_outcome == "draw":
            draw_scenarios += 1
            every_draw_qual_guaranteed &= qual_guaranteed
            draw_scenarios_for_promotion += 1
            every_draw_promotion_guaranteed &= promotion_guaranteed
            draw_scenarios_for_safety += 1
            every_draw_safe &= safe_guaranteed
        if target_outcome != "win":
            nonwin_qual_possible |= qual_possible
            nonwin_promotion_possible |= promotion_possible
        safe_possible = (
            safe_slots > 0
            and sum(
                value > points[team] for other, value in points.items() if other != team
            )
            < safe_slots
        )
        any_safe_possible |= safe_possible
        goal_possibility_by_result["relegation_safety"][target_outcome] |= safe_possible
        goal_guarantee_by_result["relegation_safety"][target_outcome] &= safe_guaranteed
        if target_outcome == "win":
            win_safe_possible |= safe_possible
        elif target_outcome == "loss":
            loss_safe_possible |= safe_possible
        else:
            nonwin_safe_possible |= safe_possible

    applicable_goals = []
    if qualification_slots:
        applicable_goals.append("qualification")
    if promotion_slots:
        applicable_goals.append("promotion")
    if relegation_slots:
        applicable_goals.append("relegation_safety")
    goal_changes_by_target_result = any(
        len(
            {
                (
                    goal_possibility_by_result[goal][result],
                    goal_guarantee_by_result[goal][result],
                )
                for result in ("win", "draw", "loss")
            }
        )
        > 1
        for goal in applicable_goals
    )

    return {
        "qualification_still_possible": any_qual_possible
        if qualification_slots
        else None,
        "qualification_clinched": every_scenario_qual_guaranteed
        if qualification_slots
        else None,
        "mathematically_qualified": every_scenario_qual_guaranteed
        if qualification_slots
        else None,
        "must_win_for_qualification": (win_qual_possible and not nonwin_qual_possible)
        if qualification_slots
        else None,
        "loss_eliminates_qualification": (any_qual_possible and not loss_qual_possible)
        if qualification_slots
        else None,
        "draw_sufficient_for_qualification": (
            every_draw_qual_guaranteed
            if qualification_slots and draw_scenarios
            else None
        ),
        "promotion_still_possible": any_promotion_possible if promotion_slots else None,
        "mathematically_promoted": every_scenario_promotion_guaranteed
        if promotion_slots
        else None,
        "must_win_for_promotion": (
            win_promotion_possible and not nonwin_promotion_possible
        )
        if promotion_slots
        else None,
        "loss_eliminates_promotion": (
            any_promotion_possible and not loss_promotion_possible
        )
        if promotion_slots
        else None,
        "draw_sufficient_for_promotion": (
            every_draw_promotion_guaranteed
            if promotion_slots and draw_scenarios_for_promotion
            else None
        ),
        "qualification_elimination_risk": (not every_scenario_qual_guaranteed)
        if qualification_slots
        else None,
        "mathematically_eliminated": (not any_qual_possible)
        if qualification_slots
        else None,
        "relegation_still_possible": any_relegation_possible
        if relegation_slots
        else None,
        "mathematically_relegated": every_scenario_relegated
        if relegation_slots
        else None,
        "mathematically_safe_from_relegation": every_scenario_safe
        if relegation_slots
        else None,
        "relegation_risk": any_relegation_possible if relegation_slots else None,
        "must_win_to_avoid_relegation": (win_safe_possible and not nonwin_safe_possible)
        if relegation_slots
        else None,
        "loss_eliminates_relegation_safety": (
            any_safe_possible and not loss_safe_possible
        )
        if relegation_slots
        else None,
        "draw_sufficient_for_relegation_safety": (
            every_draw_safe if relegation_slots and draw_scenarios_for_safety else None
        ),
        "goal_possibility_by_target_result": goal_possibility_by_result,
        "mathematically_consequential": goal_changes_by_target_result,
    }


def build_context_features(
    schedule: pd.DataFrame,
    results: pd.DataFrame,
    group_rules: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> pd.DataFrame:
    """Build one pre-match context row per fixture, failing on ambiguous inputs.

    ``group_rules`` is keyed by ``(edition, league, group)``. Each value must
    include qualification/promotion/relegation slot counts and an explicit
    ordered ``table_tiebreakers`` sequence. Optional ``mathematical_goal``
    may be ``qualification``, ``promotion``, or ``avoid_relegation``; without
    it, goal-relative win/draw/loss labels remain unknown.
    """
    fixtures, scores = _normalize_inputs(schedule, results, group_rules)
    score_lookup = scores.set_index("fixture_id")
    output: list[dict[str, Any]] = []

    for fixture in fixtures.itertuples(index=False):
        key = (fixture.edition, fixture.league, fixture.group)
        group_fixtures = fixtures.loc[
            fixtures["edition"].eq(fixture.edition)
            & fixtures["league"].eq(fixture.league)
            & fixtures["group"].eq(fixture.group)
        ].copy()
        teams = sorted(
            set(group_fixtures["home_team"]) | set(group_fixtures["away_team"])
        )
        prior_fixtures = group_fixtures.loc[group_fixtures["kickoff"] < fixture.kickoff]
        missing_prior = [
            fixture_id
            for fixture_id in prior_fixtures["fixture_id"]
            if fixture_id not in score_lookup.index
        ]
        if missing_prior:
            raise ValueError(
                f"Prior group fixtures lack final results before {fixture.fixture_id}: {sorted(missing_prior)}"
            )
        prior = prior_fixtures.merge(
            scores, on="fixture_id", how="inner", validate="one_to_one"
        )
        order, positions, stats = _table_order(
            teams, prior, group_rules[key]["table_tiebreakers"]
        )
        remaining = group_fixtures.loc[
            group_fixtures["kickoff"] >= fixture.kickoff
        ].copy()
        if fixture.fixture_id not in set(remaining["fixture_id"]):
            raise ValueError("Target fixture absent from remaining group schedule")
        if not any(
            fixture.home_team in (row.home_team, row.away_team)
            for row in remaining.itertuples(index=False)
        ) or not any(
            fixture.away_team in (row.home_team, row.away_team)
            for row in remaining.itertuples(index=False)
        ):
            raise ValueError("Remaining schedule does not include both target teams")

        rules = group_rules[key]
        starting_points = {team: int(stats[team]["points"]) for team in teams}
        home_scenarios = _scenario_summary(
            fixture.home_team,
            fixture.fixture_id,
            remaining,
            starting_points,
            int(rules["qualification_slots"]),
            int(rules["promotion_slots"]),
            int(rules["relegation_slots"]),
        )
        away_scenarios = _scenario_summary(
            fixture.away_team,
            fixture.fixture_id,
            remaining,
            starting_points,
            int(rules["qualification_slots"]),
            int(rules["promotion_slots"]),
            int(rules["relegation_slots"]),
        )

        row: dict[str, Any] = {
            "fixture_id": fixture.fixture_id,
            "edition": fixture.edition,
            "league": fixture.league,
            "league_tier": fixture.league,
            "group": fixture.group,
            "matchday": int(fixture.matchday),
            "kickoff": fixture.kickoff,
            "mathematical_goal": rules.get("mathematical_goal"),
            "group_stage_max_matchday": int(group_fixtures["matchday"].max()),
            "scenario_basis": "points_only_ties_optimistic_for_possible_conservative_for_clinched",
            "table_tiebreakers": list(rules["table_tiebreakers"]),
        }
        for side, team in (("home", fixture.home_team), ("away", fixture.away_team)):
            rank_min, rank_max = positions[team]
            remaining_games = int(
                remaining["home_team"].eq(team).sum()
                + remaining["away_team"].eq(team).sum()
            )
            matches_played = int(rules["expected_fixtures_per_team"]) - remaining_games
            if matches_played < 0:
                raise ValueError(
                    f"Remaining schedule exceeds the declared format for {team}"
                )
            values = stats[team]
            row.update(
                {
                    f"{side}_team": team,
                    f"{side}_points_before": int(values["points"]),
                    f"{side}_matches_played_before": matches_played,
                    f"{side}_table_position_before": rank_min
                    if rank_min == rank_max
                    else None,
                    f"{side}_table_position_min": rank_min,
                    f"{side}_table_position_max": rank_max,
                    f"{side}_table_position_tied": rank_min != rank_max,
                    f"{side}_goal_difference_before": int(values["goal_difference"]),
                    f"{side}_remaining_games_before": remaining_games,
                    f"{side}_points_gap_to_qualification": _slots_margin(
                        team,
                        order,
                        positions,
                        stats,
                        int(rules["qualification_slots"]),
                        "points",
                    ),
                    f"{side}_points_to_qualification_boundary": _slots_margin(
                        team,
                        order,
                        positions,
                        stats,
                        int(rules["qualification_slots"]),
                        "points",
                    ),
                    f"{side}_points_gap_to_promotion": _slots_margin(
                        team,
                        order,
                        positions,
                        stats,
                        int(rules["promotion_slots"]),
                        "points",
                    ),
                    f"{side}_points_gap_to_relegation_playoff": _gap_to_bottom_zone(
                        team,
                        order,
                        positions,
                        stats,
                        int(rules["relegation_slots"]),
                        int(rules["relegation_playoff_slots"]),
                    ),
                    f"{side}_points_to_relegation_boundary": _gap_to_bottom_zone(
                        team,
                        order,
                        positions,
                        stats,
                        int(rules["relegation_slots"]),
                        int(rules["relegation_playoff_slots"]),
                    ),
                    f"{side}_points_margin_to_relegation_safety": (
                        int(
                            values["points"]
                            - stats[
                                order[len(teams) - int(rules["relegation_slots"]) - 1]
                            ]["points"]
                        )
                        if int(rules["relegation_slots"])
                        and len(teams) > int(rules["relegation_slots"])
                        else None
                    ),
                }
            )
            for name, value in (
                home_scenarios if side == "home" else away_scenarios
            ).items():
                row[f"{side}_{name}"] = value
            goal_field_names = {
                "qualification": (
                    "must_win_for_qualification",
                    "draw_sufficient_for_qualification",
                    "loss_eliminates_qualification",
                ),
                "promotion": (
                    "must_win_for_promotion",
                    "draw_sufficient_for_promotion",
                    "loss_eliminates_promotion",
                ),
                "avoid_relegation": (
                    "must_win_to_avoid_relegation",
                    "draw_sufficient_for_relegation_safety",
                    "loss_eliminates_relegation_safety",
                ),
            }
            selected_goal_fields = goal_field_names.get(rules.get("mathematical_goal"))
            row[f"{side}_win_required_for_mathematical_goal"] = (
                row[f"{side}_{selected_goal_fields[0]}"]
                if selected_goal_fields
                else None
            )
            row[f"{side}_draw_sufficient_for_mathematical_goal"] = (
                row[f"{side}_{selected_goal_fields[1]}"]
                if selected_goal_fields
                else None
            )
            row[f"{side}_loss_eliminates"] = (
                row[f"{side}_{selected_goal_fields[2]}"]
                if selected_goal_fields
                else None
            )
            row[f"{side}_near_must_win"] = None
            row[f"{side}_near_must_win_reason"] = (
                "requires an externally declared probability/utility threshold"
            )
        row["points_diff_home_minus_away"] = int(
            row["home_points_before"] - row["away_points_before"]
        )
        row["goal_difference_diff_home_minus_away"] = int(
            row["home_goal_difference_before"] - row["away_goal_difference_before"]
        )
        row["mathematically_consequential"] = bool(
            home_scenarios["mathematically_consequential"]
            or away_scenarios["mathematically_consequential"]
        )
        output.append(row)

    return (
        pd.DataFrame(output)
        .sort_values(["kickoff", "fixture_id"])
        .reset_index(drop=True)
    )


def _probability_array(frame: pd.DataFrame) -> np.ndarray:
    columns = ["base_p_home", "base_p_draw", "base_p_away"]
    values = frame[columns].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("Baseline probabilities must be finite and non-negative")
    totals = values.sum(axis=1)
    if not np.allclose(totals, 1.0, atol=1e-6):
        raise ValueError("Baseline probabilities must sum to one")
    return np.log(np.clip(values, 1e-9, 1.0))


def _full_class_probabilities(model: Pipeline, features: pd.DataFrame) -> np.ndarray:
    raw = model.predict_proba(features)
    result = np.zeros((len(features), 3), dtype=float)
    for index, label in enumerate(model.named_steps["classifier"].classes_):
        result[:, int(label)] = raw[:, index]
    return result


def _make_model(
    numeric_columns: Sequence[str],
    categorical_columns: Sequence[str],
    c_value: float,
) -> Pipeline:
    transforms: list[tuple[str, Any, Sequence[str]]] = []
    if numeric_columns:
        transforms.append(
            (
                "numeric",
                Pipeline(
                    [
                        (
                            "imputer",
                            SimpleImputer(
                                strategy="constant", fill_value=0, add_indicator=True
                            ),
                        ),
                        ("scaler", StandardScaler()),
                    ]
                ),
                list(numeric_columns),
            )
        )
    if categorical_columns:
        transforms.append(
            (
                "categorical",
                Pipeline(
                    [
                        (
                            "imputer",
                            SimpleImputer(
                                strategy="constant", fill_value="__MISSING__"
                            ),
                        ),
                        ("onehot", OneHotEncoder(handle_unknown="ignore")),
                    ]
                ),
                list(categorical_columns),
            )
        )
    transformer = ColumnTransformer(transforms, remainder="drop")
    return Pipeline(
        [
            ("features", transformer),
            ("classifier", LogisticRegression(C=c_value, max_iter=2000)),
        ]
    )


def run_paired_ablation(
    examples: pd.DataFrame,
    numeric_context: Sequence[str],
    categorical_context: Sequence[str] = (),
    minimum_training_rows: int = 30,
    c_value: float = 1.0,
) -> dict[str, Any]:
    """Run a causal expanding-window baseline vs baseline+context ablation.

    The caller supplies already point-in-time baseline probabilities, actual
    outcomes (0=home, 1=draw, 2=away), and context values from
    :func:`build_context_features`. Both variants share identical dates/rows;
    same-kickoff observations are never allowed into one another's training set.
    """
    required = {
        "kickoff",
        "outcome",
        "base_p_home",
        "base_p_draw",
        "base_p_away",
        *numeric_context,
        *categorical_context,
    }
    _required_columns(examples, required, "Ablation examples")
    if minimum_training_rows < 3:
        raise ValueError("minimum_training_rows must be at least 3")
    frame = examples.copy().reset_index(drop=True)
    frame["kickoff"] = pd.to_datetime(frame["kickoff"], errors="raise", utc=True)
    frame["outcome"] = pd.to_numeric(frame["outcome"], errors="raise").astype(int)
    if not frame["outcome"].isin((0, 1, 2)).all():
        raise ValueError("outcome must use 0=home, 1=draw, 2=away")
    frame["_source_index"] = np.arange(len(frame))
    frame = frame.sort_values(["kickoff", "_source_index"]).reset_index(drop=True)
    probabilities = _probability_array(frame)
    frame[["_base_log_home", "_base_log_draw", "_base_log_away"]] = probabilities
    numeric_context = list(numeric_context)
    categorical_context = list(categorical_context)

    base_columns = ["_base_log_home", "_base_log_draw", "_base_log_away"]
    base_model = _make_model(base_columns, (), c_value)
    augmented_model = _make_model(
        [*base_columns, *numeric_context], categorical_context, c_value
    )
    records: list[dict[str, Any]] = []
    for kickoff in frame["kickoff"].drop_duplicates().sort_values():
        train = frame.loc[frame["kickoff"] < kickoff]
        test = frame.loc[frame["kickoff"].eq(kickoff)]
        if len(train) < minimum_training_rows or train["outcome"].nunique() < 2:
            continue
        base_model.fit(train, train["outcome"])
        augmented_model.fit(train, train["outcome"])
        base_probs = _full_class_probabilities(base_model, test)
        augmented_probs = _full_class_probabilities(augmented_model, test)
        for row_index, base, augmented in zip(
            test.index, base_probs, augmented_probs, strict=True
        ):
            row = frame.loc[row_index]
            records.append(
                {
                    "source_index": int(row["_source_index"]),
                    "fixture_id": str(row["fixture_id"])
                    if "fixture_id" in frame
                    else None,
                    "kickoff": kickoff.isoformat(),
                    "outcome": int(row["outcome"]),
                    "training_rows": len(train),
                    "training_max_kickoff": train["kickoff"].max().isoformat(),
                    "baseline_probabilities": base.tolist(),
                    "context_probabilities": augmented.tolist(),
                }
            )

    if not records:
        raise ValueError("Insufficient causal history for paired ablation predictions")

    y = np.asarray([record["outcome"] for record in records], dtype=int)
    baseline = np.asarray(
        [record["baseline_probabilities"] for record in records], dtype=float
    )
    augmented = np.asarray(
        [record["context_probabilities"] for record in records], dtype=float
    )
    one_hot = np.eye(3)[y]

    def metrics(values: np.ndarray) -> dict[str, float]:
        clipped = np.clip(values, 1e-15, 1.0)
        return {
            "multiclass_brier": float(np.mean(np.sum((values - one_hot) ** 2, axis=1))),
            "log_loss": float(-np.mean(np.log(clipped[np.arange(len(y)), y]))),
        }

    return {
        "schema": "nations-league-context-ablation-v1",
        "evaluation": "paired_expanding_window_causal",
        "no_lookahead": all(
            pd.Timestamp(record["training_max_kickoff"])
            < pd.Timestamp(record["kickoff"])
            for record in records
        ),
        "feature_rows": len(frame),
        "predicted_rows": len(records),
        "coverage": len(records) / len(frame),
        "baseline": metrics(baseline),
        "baseline_plus_context": metrics(augmented),
        "delta_context_minus_baseline": {
            "multiclass_brier": metrics(augmented)["multiclass_brier"]
            - metrics(baseline)["multiclass_brier"],
            "log_loss": metrics(augmented)["log_loss"] - metrics(baseline)["log_loss"],
        },
        "predictions": records,
        "context_features": {
            "numeric": numeric_context,
            "categorical": categorical_context,
        },
        "interpretation": "diagnostic_only_no_production_hook",
    }


def _metric_summary(outcomes: np.ndarray, probabilities: np.ndarray) -> dict[str, Any]:
    clipped = np.clip(probabilities, 1e-15, 1.0)
    one_hot = np.eye(3)[outcomes]
    class_names = ("home", "draw", "away")
    class_calibration = {}
    for index, name in enumerate(class_names):
        predicted = probabilities[:, index]
        observed = one_hot[:, index]
        class_calibration[name] = {
            "mean_probability": float(predicted.mean()),
            "observed_frequency": float(observed.mean()),
            "absolute_calibration_gap": float(abs(predicted.mean() - observed.mean())),
            "brier_one_vs_rest": float(np.mean((predicted - observed) ** 2)),
        }
    edges = np.linspace(0.0, 1.0, 11)
    class_ece = []
    for index in range(3):
        ece = 0.0
        for bin_index, (low, high) in enumerate(pairwise(edges)):
            mask = (probabilities[:, index] >= low) & (probabilities[:, index] < high)
            if bin_index == 9:
                mask |= probabilities[:, index] == 1.0
            if mask.any():
                ece += float(mask.mean()) * abs(
                    float(probabilities[mask, index].mean())
                    - float(one_hot[mask, index].mean())
                )
        class_ece.append(ece)
    return {
        "multiclass_brier": float(
            np.mean(np.sum((probabilities - one_hot) ** 2, axis=1))
        ),
        "multiclass_log_loss": float(
            -np.mean(np.log(clipped[np.arange(len(outcomes)), outcomes]))
        ),
        "ece_10_bin_macro_ovr": float(np.mean(class_ece)),
        "home_calibration": class_calibration["home"],
        "draw_calibration": class_calibration["draw"],
        "away_calibration": class_calibration["away"],
        "coverage_count": len(outcomes),
        "sharpness_mean_max_probability": float(probabilities.max(axis=1).mean()),
        "accuracy_argmax": float((probabilities.argmax(axis=1) == outcomes).mean()),
    }


def _paired_cluster_bootstrap(
    rows: Sequence[Mapping[str, Any]],
    n_bootstrap: int,
    seed: int,
) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        cluster = (str(row.get("edition", "unknown")), str(row["kickoff"])[:10])
        grouped.setdefault(cluster, []).append(row)
    by_edition: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for (edition, date), cluster_rows in grouped.items():
        by_edition.setdefault(edition, {})[date] = cluster_rows
    if not grouped:
        return {"status": "not_available", "reason": "No paired dates"}

    rng = np.random.default_rng(seed)
    brier_differences: list[float] = []
    log_loss_differences: list[float] = []
    for _ in range(n_bootstrap):
        sample: list[Mapping[str, Any]] = []
        for date_groups in by_edition.values():
            dates = sorted(date_groups)
            for selected in rng.choice(dates, size=len(dates), replace=True):
                sample.extend(date_groups[str(selected)])
        outcomes = np.asarray([int(row["outcome"]) for row in sample], dtype=int)
        baseline = np.asarray(
            [row["baseline_probabilities"] for row in sample], dtype=float
        )
        context = np.asarray(
            [row["context_probabilities"] for row in sample], dtype=float
        )
        baseline_metrics = _metric_summary(outcomes, baseline)
        context_metrics = _metric_summary(outcomes, context)
        brier_differences.append(
            context_metrics["multiclass_brier"] - baseline_metrics["multiclass_brier"]
        )
        log_loss_differences.append(
            context_metrics["multiclass_log_loss"]
            - baseline_metrics["multiclass_log_loss"]
        )

    def interval(values: Sequence[float]) -> dict[str, float | int]:
        return {
            "lower_95": float(np.quantile(values, 0.025)),
            "upper_95": float(np.quantile(values, 0.975)),
            "bootstrap_replicates": len(values),
        }

    return {
        "status": "computed",
        "method": "paired date-cluster bootstrap, resampled within edition",
        "seed": seed,
        "confidence_level": 0.95,
        "brier_context_minus_baseline": interval(brier_differences),
        "log_loss_context_minus_baseline": interval(log_loss_differences),
    }


def _strata_for_rows(
    rows: Sequence[Mapping[str, Any]], minimum_sample: int
) -> dict[str, Any]:
    if not rows:
        return {}

    def optional_int(value: Any) -> int | None:
        if value is None or pd.isna(value):
            return None
        return int(value)

    max_matchday_by_group: dict[tuple[str, str, str], int] = {}
    for row in rows:
        matchday = optional_int(
            row.get("group_stage_max_matchday", row.get("matchday"))
        )
        if matchday is None:
            continue
        group_key = (
            str(row.get("edition", "unknown")),
            str(row.get("league_tier", "unknown")),
            str(row.get("group", "unknown")),
        )
        max_matchday_by_group[group_key] = max(
            max_matchday_by_group.get(group_key, 0),
            matchday,
        )

    partitions: dict[str, dict[str, list[Mapping[str, Any]]]] = {
        "group_stage": {"early": [], "late": [], "not_group_stage": []},
        "mathematical_constraint": {
            "consequential": [],
            "non_consequential_low_constraint": [],
            "unavailable": [],
        },
        "league_tier": {},
        "observed_outcome": {"home": [], "draw": [], "away": []},
    }
    for row in rows:
        outcome_name = ("home", "draw", "away")[int(row["outcome"])]
        partitions["observed_outcome"][outcome_name].append(row)
        tier = str(row.get("league_tier", "unknown"))
        partitions["league_tier"].setdefault(tier, []).append(row)
        group_key = (
            str(row.get("edition", "unknown")),
            tier,
            str(row.get("group", "unknown")),
        )
        matchday = optional_int(row.get("matchday"))
        maximum_matchday = optional_int(row.get("group_stage_max_matchday"))
        if matchday is None or maximum_matchday is None:
            partitions["group_stage"]["not_group_stage"].append(row)
        else:
            max_matchday_by_group[group_key] = max(
                max_matchday_by_group.get(group_key, 0), maximum_matchday
            )
            midpoint = (max_matchday_by_group[group_key] + 1) // 2
            partitions["group_stage"][
                "early" if matchday <= midpoint else "late"
            ].append(row)
        consequence = row.get("mathematically_consequential")
        if consequence is None or pd.isna(consequence):
            label = "unavailable"
        else:
            label = (
                "consequential"
                if bool(consequence)
                else "non_consequential_low_constraint"
            )
        partitions["mathematical_constraint"][label].append(row)

    result: dict[str, Any] = {}
    for partition, groups in partitions.items():
        result[partition] = {}
        for label, subset in groups.items():
            sample_count = len(subset)
            baseline = (
                _metric_summary(
                    np.asarray([row["outcome"] for row in subset], dtype=int),
                    np.asarray(
                        [row["baseline_probabilities"] for row in subset], dtype=float
                    ),
                )
                if sample_count
                else None
            )
            context = (
                _metric_summary(
                    np.asarray([row["outcome"] for row in subset], dtype=int),
                    np.asarray(
                        [row["context_probabilities"] for row in subset], dtype=float
                    ),
                )
                if sample_count
                else None
            )
            result[partition][label] = {
                "sample_count": sample_count,
                "status": (
                    "unavailable"
                    if sample_count == 0
                    else "reported"
                    if sample_count >= minimum_sample
                    else "insufficient_sample"
                ),
                "baseline": baseline,
                "baseline_plus_context": context,
                "paired_difference_context_minus_baseline": (
                    {
                        key: context[key] - baseline[key]
                        for key in (
                            "multiclass_brier",
                            "multiclass_log_loss",
                            "ece_10_bin_macro_ovr",
                        )
                    }
                    if sample_count
                    else None
                ),
            }
    return result


def _small_stratum_sensitivity(
    rows: Sequence[Mapping[str, Any]],
    minimum_sample: int,
) -> dict[str, Any]:
    """Check whether dropping small categories reverses the global Brier gain."""

    def optional_int(value: Any) -> int | None:
        if value is None or pd.isna(value):
            return None
        return int(value)

    max_matchday_by_group: dict[tuple[str, str, str], int] = {}
    for row in rows:
        matchday = optional_int(
            row.get("group_stage_max_matchday", row.get("matchday"))
        )
        if matchday is None:
            continue
        group_key = (
            str(row.get("edition", "unknown")),
            str(row.get("league_tier", "unknown")),
            str(row.get("group", "unknown")),
        )
        max_matchday_by_group[group_key] = max(
            max_matchday_by_group.get(group_key, 0),
            matchday,
        )

    def group_stage(row: Mapping[str, Any]) -> str:
        matchday = optional_int(row.get("matchday"))
        if matchday is None:
            return "not_group_stage"
        group_key = (
            str(row.get("edition", "unknown")),
            str(row.get("league_tier", "unknown")),
            str(row.get("group", "unknown")),
        )
        halfway = (max_matchday_by_group.get(group_key, 0) + 1) // 2
        return "early" if matchday <= halfway else "late"

    def constraint(row: Mapping[str, Any]) -> str:
        value = row.get("mathematically_consequential")
        if value is None or pd.isna(value):
            return "unavailable"
        return "consequential" if bool(value) else "low_constraint"

    categories = {
        "league_tier": lambda row: str(row.get("league_tier", "unknown")),
        "group_stage": group_stage,
        "mathematical_constraint": constraint,
        "observed_outcome": lambda row: ("home", "draw", "away")[int(row["outcome"])],
    }
    sensitivity = []
    reversal = False
    for partition, key_fn in categories.items():
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            label = key_fn(row)
            groups.setdefault(label, []).append(row)
        small_labels = [
            label for label, subset in groups.items() if len(subset) < minimum_sample
        ]
        if not small_labels:
            continue
        remaining = [row for row in rows if key_fn(row) not in small_labels]
        if not remaining:
            continues_gain = False
        else:
            y = np.asarray([int(row["outcome"]) for row in remaining], dtype=int)
            b = np.asarray(
                [row["baseline_probabilities"] for row in remaining], dtype=float
            )
            c = np.asarray(
                [row["context_probabilities"] for row in remaining], dtype=float
            )
            continues_gain = (
                _metric_summary(y, c)["multiclass_brier"]
                < _metric_summary(y, b)["multiclass_brier"]
            )
        reversal |= not continues_gain
        sensitivity.append(
            {
                "partition": partition,
                "small_categories_removed": small_labels,
                "remaining_fixtures": len(remaining),
                "brier_gain_remains": continues_gain,
            }
        )
    return {
        "small_stratum_only_uplift": reversal,
        "method": "remove all below-minimum categories per partition; verify full-cohort Brier direction remains",
        "checks": sensitivity,
    }


def evaluate_paired_probabilities(
    rows: Sequence[Mapping[str, Any]],
    eligible_count: int,
    *,
    n_bootstrap: int = 2000,
    seed: int = 20260929,
    minimum_stratum_sample: int = 20,
    minimum_evaluation_count: int = 100,
    log_loss_regression_tolerance: float = 0.01,
    ece_regression_tolerance: float = 0.02,
    class_calibration_regression_tolerance: float = 0.03,
) -> dict[str, Any]:
    """Score paired walk-forward predictions and classify global evidence.

    Rows must be real matched fixtures from the caller's verified B4 dataset.
    This function does not create or accept synthetic rows on behalf of the
    runner; synthetic fixtures belong only in unit tests.
    """
    if eligible_count < 0 or n_bootstrap < 100:
        raise ValueError(
            "eligible_count must be non-negative and n_bootstrap at least 100"
        )
    if not rows:
        return {
            "status": "NL_CONTEXT_BLOCKED",
            "reason": "No paired out-of-sample predictions",
            "fixtures_evaluated": 0,
            "eligible_fixtures": eligible_count,
            "coverage": 0.0,
        }
    required = {
        "fixture_id",
        "kickoff",
        "edition",
        "league_tier",
        "group",
        "matchday",
        "outcome",
        "baseline_probabilities",
        "context_probabilities",
    }
    for index, row in enumerate(rows):
        missing = required - set(row)
        if missing:
            raise ValueError(f"Paired prediction row {index} missing {sorted(missing)}")
        if (
            len(row["baseline_probabilities"]) != 3
            or len(row["context_probabilities"]) != 3
        ):
            raise ValueError(
                f"Paired prediction row {index} does not contain complete 1X2 vectors"
            )
        if not 0 <= int(row["outcome"]) <= 2:
            raise ValueError(f"Paired prediction row {index} has invalid outcome")
        if not pd.Timestamp(row["kickoff"]).tzinfo:
            raise ValueError(
                f"Paired prediction row {index} kickoff must be timezone-aware"
            )
        for name in ("baseline_probabilities", "context_probabilities"):
            values = np.asarray(row[name], dtype=float)
            if (
                not np.isfinite(values).all()
                or (values < 0).any()
                or not np.isclose(values.sum(), 1, atol=1e-6)
            ):
                raise ValueError(f"Paired prediction row {index} has invalid {name}")
    if len({str(row["fixture_id"]) for row in rows}) != len(rows):
        raise ValueError("Paired predictions contain duplicate fixture IDs")

    outcomes = np.asarray([int(row["outcome"]) for row in rows], dtype=int)
    baseline_probs = np.asarray(
        [row["baseline_probabilities"] for row in rows], dtype=float
    )
    context_probs = np.asarray(
        [row["context_probabilities"] for row in rows], dtype=float
    )
    baseline_metrics = _metric_summary(outcomes, baseline_probs)
    context_metrics = _metric_summary(outcomes, context_probs)
    differences = {
        key: context_metrics[key] - baseline_metrics[key]
        for key in (
            "multiclass_brier",
            "multiclass_log_loss",
            "ece_10_bin_macro_ovr",
            "sharpness_mean_max_probability",
        )
    }
    for class_name in ("home", "draw", "away"):
        differences[f"{class_name}_absolute_calibration_gap"] = (
            context_metrics[f"{class_name}_calibration"]["absolute_calibration_gap"]
            - baseline_metrics[f"{class_name}_calibration"]["absolute_calibration_gap"]
        )

    bootstrap = _paired_cluster_bootstrap(rows, n_bootstrap, seed)
    coverage = len(rows) / eligible_count if eligible_count else 0.0
    strata = _strata_for_rows(rows, minimum_stratum_sample)
    concentration = _small_stratum_sensitivity(rows, minimum_stratum_sample)
    gates = {
        "brier_improved": differences["multiclass_brier"] < 0,
        "log_loss_not_relevantly_worse": differences["multiclass_log_loss"]
        <= log_loss_regression_tolerance,
        "ece_not_relevantly_worse": differences["ece_10_bin_macro_ovr"]
        <= ece_regression_tolerance,
        "class_calibration_not_relevantly_worse": all(
            differences[f"{name}_absolute_calibration_gap"]
            <= class_calibration_regression_tolerance
            for name in ("home", "draw", "away")
        ),
        "minimum_evaluation_coverage": len(rows) >= minimum_evaluation_count
        and coverage >= 0.8,
        "paired_full_cohort": True,
        "gain_not_confined_to_small_stratum": not concentration[
            "small_stratum_only_uplift"
        ],
    }
    brier_ci = bootstrap.get("brier_context_minus_baseline", {})
    if not gates["minimum_evaluation_coverage"]:
        status = "NL_CONTEXT_BLOCKED"
        classification_reason = "minimum_out_of_sample_count_or_coverage_not_met"
    elif (
        not gates["log_loss_not_relevantly_worse"]
        or not gates["ece_not_relevantly_worse"]
        or not gates["class_calibration_not_relevantly_worse"]
    ):
        status = "NL_CONTEXT_REGRESSION"
        classification_reason = "material_log_loss_or_calibration_regression"
    elif (
        differences["multiclass_brier"] >= 0
        or concentration["small_stratum_only_uplift"]
    ):
        status = "NL_CONTEXT_NO_GAIN"
        classification_reason = (
            "no_full_cohort_brier_improvement"
            if differences["multiclass_brier"] >= 0
            else "apparent_gain_does_not_survive_small_stratum_sensitivity"
        )
    elif (
        brier_ci.get("upper_95", 0.0) < 0
        and gates["gain_not_confined_to_small_stratum"]
    ):
        status = "NL_CONTEXT_UPLIFT_SUPPORTED"
        classification_reason = (
            "paired_95_percent_brier_interval_below_zero_and_gates_pass"
        )
    else:
        status = "NL_CONTEXT_PROMISING_NOT_CONFIRMED"
        classification_reason = "full_cohort_brier_point_estimate_improves_but_bootstrap_support_is_inconclusive"

    return {
        "schema": "nations-league-context-ablation-audit-v1",
        "status": status,
        "classification_reason": classification_reason,
        "fixtures_evaluated": len(rows),
        "eligible_fixtures": eligible_count,
        "coverage": coverage,
        "baseline": baseline_metrics,
        "baseline_plus_context": context_metrics,
        "paired_difference_context_minus_baseline": differences,
        "paired_date_cluster_bootstrap": bootstrap,
        "gates": gates,
        "thresholds": {
            "minimum_evaluation_count": minimum_evaluation_count,
            "minimum_coverage": 0.8,
            "minimum_stratum_sample": minimum_stratum_sample,
            "log_loss_regression_tolerance": log_loss_regression_tolerance,
            "ece_regression_tolerance": ece_regression_tolerance,
            "class_calibration_regression_tolerance": class_calibration_regression_tolerance,
        },
        "strata": strata,
        "small_stratum_sensitivity": concentration,
        "global_claim_basis": "full_paired_out_of_sample_fixture_cohort_only",
        "interpretation": "research_only_no_production_hook",
    }


def run_causal_context_ablation(
    examples: pd.DataFrame,
    eligible_fixture_ids: Sequence[str],
    numeric_context: Sequence[str],
    categorical_context: Sequence[str],
    provenance: Mapping[str, Any],
    *,
    minimum_training_rows: int = 30,
    n_bootstrap: int = 2000,
    seed: int = 20260929,
) -> dict[str, Any]:
    """Run the complete paired OOS ablation on an exact B4-derived fixture set.

    ``examples`` must be the exact join of canonical competition-state records,
    observed outcomes, and point-in-time baseline predictions. The function
    rejects incomplete joins and any state cut at/after kickoff. The first
    expanding-window warm-up rows are listed as excluded rather than silently
    removed from the eligible cohort.
    """
    required = {
        "fixture_id",
        "kickoff",
        "state_cutoff",
        "record_digest",
        "edition",
        "league_tier",
        "group",
        "matchday",
        "outcome",
        "base_p_home",
        "base_p_draw",
        "base_p_away",
        *numeric_context,
        *categorical_context,
    }
    _required_columns(examples, required, "Verified causal ablation input")
    targets = [str(value) for value in eligible_fixture_ids]
    if not targets or len(targets) > 512 or len(set(targets)) != len(targets):
        raise ValueError("Eligible fixture IDs must be unique and contain 1..512 rows")
    frame = examples.copy().reset_index(drop=True)
    frame["fixture_id"] = frame["fixture_id"].astype(str)
    if frame["fixture_id"].duplicated().any():
        raise ValueError("Joined examples contain duplicate fixture IDs")
    if set(frame["fixture_id"]) != set(targets) or len(frame) != len(targets):
        raise ValueError(
            "Joined examples do not exactly match the eligible B4 fixture set"
        )
    frame["kickoff"] = pd.to_datetime(frame["kickoff"], errors="raise", utc=True)
    frame["state_cutoff"] = pd.to_datetime(
        frame["state_cutoff"], errors="raise", utc=True
    )
    if not (frame["state_cutoff"] < frame["kickoff"]).all():
        raise ValueError("Competition state cutoff must be strictly before kickoff")
    record_digests = frame["record_digest"].astype(str).str.lower()
    if (
        frame["record_digest"].isna().any()
        or not record_digests.map(
            lambda value: (
                len(value) == 64
                and all(character in "0123456789abcdef" for character in value)
            )
        ).all()
    ):
        raise ValueError("Every joined row must retain its B4 record digest")
    for field in ("competition_state_dataset_sha256", "baseline_source_sha256"):
        value = provenance.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value.lower())
        ):
            raise ValueError(f"Provenance requires a valid {field}")
    source_main_sha = provenance.get("source_main_sha")
    if (
        not isinstance(source_main_sha, str)
        or len(source_main_sha) not in (40, 64)
        or any(
            character not in "0123456789abcdef" for character in source_main_sha.lower()
        )
    ):
        raise ValueError("Provenance requires a valid source_main_sha")

    walk_forward = run_paired_ablation(
        frame,
        numeric_context=numeric_context,
        categorical_context=categorical_context,
        minimum_training_rows=minimum_training_rows,
    )
    source_rows = frame.to_dict(orient="records")
    paired_rows = []
    predicted_source_indexes: set[int] = set()
    for prediction in walk_forward["predictions"]:
        index = int(prediction["source_index"])
        source = source_rows[index]
        predicted_source_indexes.add(index)
        paired_rows.append(
            {
                "fixture_id": str(source["fixture_id"]),
                "kickoff": source["kickoff"],
                "edition": str(source["edition"]),
                "league_tier": str(source["league_tier"]),
                "group": str(source["group"]),
                "matchday": (
                    None if pd.isna(source["matchday"]) else int(source["matchday"])
                ),
                "group_stage_max_matchday": (
                    None
                    if pd.isna(source.get("group_stage_max_matchday"))
                    else int(source["group_stage_max_matchday"])
                ),
                "mathematically_consequential": (
                    None
                    if source.get("mathematically_consequential") is None
                    or pd.isna(source.get("mathematically_consequential"))
                    else bool(source["mathematically_consequential"])
                ),
                "outcome": int(prediction["outcome"]),
                "baseline_probabilities": prediction["baseline_probabilities"],
                "context_probabilities": prediction["context_probabilities"],
            }
        )
    audit = evaluate_paired_probabilities(
        paired_rows,
        eligible_count=len(targets),
        n_bootstrap=n_bootstrap,
        seed=seed,
    )
    oos = frame.iloc[sorted(predicted_source_indexes)]
    raw_baseline_probabilities = oos[
        ["base_p_home", "base_p_draw", "base_p_away"]
    ].to_numpy(dtype=float)
    audit["raw_causal_baseline_forecast_metrics_oos"] = _metric_summary(
        oos["outcome"].to_numpy(dtype=int), raw_baseline_probabilities
    )
    audit["walk_forward"] = {
        "method": "expanding window; same-kickoff fixtures share a fold",
        "minimum_training_rows": minimum_training_rows,
        "no_lookahead": bool(walk_forward["no_lookahead"]),
        "predicted_fixture_ids": [row["fixture_id"] for row in paired_rows],
        "warmup_exclusions": [
            str(frame.iloc[index]["fixture_id"])
            for index in range(len(frame))
            if index not in predicted_source_indexes
        ],
    }
    audit["provenance"] = dict(provenance)
    audit["context_features"] = {
        "numeric": list(numeric_context),
        "categorical": list(categorical_context),
    }
    audit["synthetic_evidence_used"] = False
    audit["production_hook"] = False
    return audit


def render_context_ablation_markdown(audit: Mapping[str, Any]) -> str:
    """Render a concise auditable report without inventing unavailable metrics."""
    status = str(audit.get("status", "NL_CONTEXT_BLOCKED"))
    provenance = audit.get("provenance", {})
    lines = [
        "# UEFA Nations League Competition-Context Ablation",
        "",
        f"- Status: `{status}`",
        f"- Competition-state SHA-256: `{provenance.get('competition_state_dataset_sha256', audit.get('competition_state_dataset_sha256', 'unavailable'))}`",
        f"- B4 source commit: `{provenance.get('competition_state_source_sha', audit.get('competition_state_source_sha', 'unavailable'))}`",
        f"- B4 source PR: `{provenance.get('competition_state_source_pr', audit.get('competition_state_source_pr', 'not supplied'))}`",
        f"- Baseline source SHA-256: `{provenance.get('baseline_source_sha256', audit.get('baseline_source_sha256', 'unavailable'))}`",
        f"- Source main SHA: `{provenance.get('source_main_sha', audit.get('source_main_sha', 'unavailable'))}`",
        f"- Eligible fixtures: {audit.get('eligible_fixtures', 'unavailable')}",
        f"- Paired OOS fixtures: {audit.get('fixtures_evaluated', 0)}",
        f"- OOS coverage: {audit.get('coverage', 0.0):.3f}",
        f"- Synthetic evidence used: {str(bool(audit.get('synthetic_evidence_used', False))).lower()}",
        "",
    ]
    if audit.get("artifact_source_status"):
        lines.extend([f"- B4 artifact state: `{audit['artifact_source_status']}`", ""])
    if "b4_readiness_validation_passed" in audit:
        lines.extend(
            [
                (
                    "- B4 readiness validation passed: "
                    f"{str(bool(audit['b4_readiness_validation_passed'])).lower()}"
                ),
                "",
            ]
        )
    if audit.get("blockers"):
        lines.extend(["## Blockers", ""])
        lines.extend(f"- `{blocker}`" for blocker in audit["blockers"])
        lines.append("")
    if audit.get("excluded_features"):
        lines.extend(["## Excluded / unavailable", ""])
        lines.extend(f"- {item}" for item in audit["excluded_features"])
        lines.append("")
    if (
        audit.get("baseline") is not None
        and audit.get("baseline_plus_context") is not None
    ):
        lines.extend(
            [
                "## Primary paired metrics",
                "",
                "| Metric | Baseline | Baseline + context | Context − baseline |",
                "|---|---:|---:|---:|",
            ]
        )
        difference = audit.get("paired_difference_context_minus_baseline", {})
        for key, label in (
            ("multiclass_brier", "Multiclass Brier"),
            ("multiclass_log_loss", "Multiclass log loss"),
            ("ece_10_bin_macro_ovr", "ECE (10-bin macro OVR)"),
            ("sharpness_mean_max_probability", "Sharpness (mean max p)"),
        ):
            lines.append(
                f"| {label} | {audit['baseline'][key]:.6f} | "
                f"{audit['baseline_plus_context'][key]:.6f} | "
                f"{difference.get(key, 0.0):+.6f} |"
            )
        lines.extend(
            [
                "",
                "## Outcome calibration",
                "",
                "| Outcome | Baseline mean p / observed / abs gap | Context mean p / observed / abs gap |",
                "|---|---:|---:|",
            ]
        )
        for name in ("home", "draw", "away"):
            baseline_cal = audit["baseline"][f"{name}_calibration"]
            context_cal = audit["baseline_plus_context"][f"{name}_calibration"]
            lines.append(
                f"| {name.title()} | {baseline_cal['mean_probability']:.4f} / "
                f"{baseline_cal['observed_frequency']:.4f} / "
                f"{baseline_cal['absolute_calibration_gap']:.4f} | "
                f"{context_cal['mean_probability']:.4f} / "
                f"{context_cal['observed_frequency']:.4f} / "
                f"{context_cal['absolute_calibration_gap']:.4f} |"
            )
        lines.extend(["", "## Paired date-cluster bootstrap (95% CI)", ""])
        bootstrap = audit.get("paired_date_cluster_bootstrap", {})
        for key, label in (
            ("brier_context_minus_baseline", "Brier difference"),
            ("log_loss_context_minus_baseline", "Log-loss difference"),
        ):
            interval = bootstrap.get(key)
            if interval:
                lines.append(
                    f"- {label}: [{interval['lower_95']:.6f}, "
                    f"{interval['upper_95']:.6f}]"
                )
        lines.extend(
            [
                "",
                "## Strata",
                "",
                "| Stratum | Level | n | Status | Δ Brier | Δ log loss |",
                "|---|---|---:|---|---:|---:|",
            ]
        )
        for partition, levels in audit.get("strata", {}).items():
            for level, values in levels.items():
                delta = values.get("paired_difference_context_minus_baseline")
                lines.append(
                    f"| {partition} | {level} | {values['sample_count']} | "
                    f"{values['status']} | "
                    f"{delta['multiclass_brier']:+.6f} | "
                    f"{delta['multiclass_log_loss']:+.6f} |"
                    if delta
                    else f"| {partition} | {level} | {values['sample_count']} | {values['status']} | n/a | n/a |"
                )
    else:
        lines.extend(
            [
                (
                    "Empirical metrics are unavailable. The canonical Builder 4 "
                    "competition-state artifact and a complete causal baseline join "
                    "are required; no synthetic result is substituted."
                ),
            ]
        )
    stacker_recommendation = audit.get("stacker_recommendation") or (
        "Include only as an experimental candidate; not a production signal."
        if status == "NL_CONTEXT_UPLIFT_SUPPORTED"
        else "Do not include until stronger independent evidence is available."
    )
    lines.extend(
        [
            "",
            f"Stacker-test recommendation: {stacker_recommendation}",
            "",
            (
                "Research-only result. No production hook, activation, publication, "
                "betting, or ledger path is changed."
            ),
            "",
        ]
    )
    return "\n".join(lines)
