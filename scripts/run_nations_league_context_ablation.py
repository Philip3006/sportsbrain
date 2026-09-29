"""Run the offline, paired causal Nations League context ablation."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
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
)

EXPECTED_FIXTURES = 512


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object at {path}")
    return value


def validate_b4_artifact(
    dataset: dict[str, Any], coverage: dict[str, Any]
) -> list[str]:
    """Validate B4 integrity and readiness without weakening its source contract."""
    blockers = []
    if (
        dataset.get("schema_version")
        != "uefa-nations-league-causal-competition-state-v1"
    ):
        blockers.append("unsupported_or_missing_B4_dataset_schema")
    records = dataset.get("records")
    if not isinstance(records, list) or len(records) != EXPECTED_FIXTURES:
        blockers.append("B4_record_count_is_not_exactly_512")
        records = records if isinstance(records, list) else []
    if coverage.get("output_record_count") != len(records):
        blockers.append("B4_coverage_record_count_mismatch")
    if coverage.get("expected_evaluation_fixture_count") != EXPECTED_FIXTURES:
        blockers.append("B4_coverage_target_is_not_512")
    if coverage.get("fixture_coverage_complete") is not True:
        blockers.append("B4_fixture_coverage_not_complete")
    if coverage.get("official_schedule_match_coverage_verified") is not True:
        blockers.append("B4_official_schedule_coverage_not_verified")
    if coverage.get("dataset_digest") != canonical_digest(dataset):
        blockers.append("B4_dataset_digest_mismatch")
    expected_coverage_digest = canonical_digest(
        {key: value for key, value in coverage.items() if key != "coverage_digest"}
    )
    if coverage.get("coverage_digest") != expected_coverage_digest:
        blockers.append("B4_coverage_digest_mismatch")

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
        try:
            fixture_date = date.fromisoformat(record["fixture_date"])
            cutoff = datetime.fromisoformat(
                record["state_cutoff"].replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            blockers.append("B4_fixture_date_or_state_cutoff_invalid")
            continue
        if (
            cutoff.date() >= fixture_date
            or cutoff.tzinfo is None
            or cutoff.utcoffset().total_seconds() != 0
        ):
            blockers.append("B4_state_cutoff_not_strictly_pre_fixture")
        if record.get("kickoff") is None:
            blockers.append("B4_kickoff_timestamp_missing")
        else:
            try:
                kickoff = datetime.fromisoformat(
                    str(record["kickoff"]).replace("Z", "+00:00")
                )
                if kickoff.tzinfo is None or cutoff >= kickoff:
                    blockers.append("B4_state_cutoff_not_strictly_pre_kickoff")
            except ValueError:
                blockers.append("B4_kickoff_timestamp_invalid")
        if record.get("stage") == "league_phase" and record.get("matchday") is None:
            blockers.append("B4_league_phase_matchday_missing")
        remaining = record.get("remaining_schedule", {})
        if record.get("stage") == "league_phase" and (
            not isinstance(remaining, dict)
            or remaining.get("status") in {None, "not_frozen_in_source"}
        ):
            blockers.append("B4_complete_remaining_group_schedule_missing")
        for state_name in ("qualification_state", "relegation_state"):
            state = record.get(state_name, {})
            if (
                isinstance(state, dict)
                and "unresolved" in str(state.get("status", "")).lower()
            ):
                blockers.append(f"B4_{state_name}_unresolved")
        if record.get("stage") == "league_phase":
            for side in ("home", "away"):
                if not _side_state(record, "qualification_state", side):
                    blockers.append(f"B4_{side}_qualification_state_not_team_bound")
                if not _side_state(record, "relegation_state", side):
                    blockers.append(f"B4_{side}_relegation_state_not_team_bound")
                if not _side_state(record, "must_win_primitives", side):
                    blockers.append(f"B4_{side}_mathematical_goal_primitives_missing")
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
    """Read only explicitly side-keyed state; never apply a fixture-wide flag to both teams."""
    direct = record.get(f"{side}_{kind}")
    if isinstance(direct, dict):
        return direct
    nested = record.get(kind, {})
    if isinstance(nested, dict) and isinstance(nested.get(side), dict):
        return nested[side]
    return {}


def derive_context_rows(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Project only point-in-time B4 state into the fixed B5 feature schema."""
    max_matchday: dict[tuple[str, str], int] = {}
    for record in records:
        if (
            record.get("stage") == "league_phase"
            and record.get("group")
            and record.get("matchday")
        ):
            key = (str(record["edition"]), str(record["group"]))
            max_matchday[key] = max(max_matchday.get(key, 0), int(record["matchday"]))

    output = []
    for record in records:
        kickoff = pd.Timestamp(record["kickoff"])
        if kickoff.tzinfo is None:
            kickoff = kickoff.tz_localize("UTC")
        else:
            kickoff = kickoff.tz_convert("UTC")
        home_state_flags = []
        for side in ("home", "away"):
            primitives = _side_state(record, "must_win_primitives", side)
            home_state_flags.extend(
                primitives.get(key)
                for key in (
                    "win_required_for_mathematical_goal",
                    "draw_sufficient_for_mathematical_goal",
                    "loss_eliminates",
                )
            )
        consequential_values = [
            value for value in home_state_flags if value is not None
        ]
        feature: dict[str, Any] = {
            "fixture_id": record["fixture_id"],
            "record_digest": record["record_digest"],
            "edition": record["edition"],
            "kickoff": kickoff,
            "state_cutoff": pd.Timestamp(record["state_cutoff"]),
            "league_tier": record.get("league_tier")
            or f"{record.get('home_league_tier')}/{record.get('away_league_tier')}",
            "group": record.get("group"),
            "matchday": record.get("matchday"),
            "group_stage_max_matchday": max_matchday.get(
                (str(record["edition"]), str(record.get("group"))),
                int(record.get("matchday") or 0),
            ),
            "mathematical_goal": record.get("mathematical_goal"),
            "outcome": 0
            if record["home_score"] > record["away_score"]
            else 1
            if record["home_score"] == record["away_score"]
            else 2,
        }
        for side in ("home", "away"):
            standing = _standing_for(record, side) or {}
            qualification = _side_state(record, "qualification_state", side)
            relegation = _side_state(record, "relegation_state", side)
            primitives = _side_state(record, "must_win_primitives", side)
            feature.update(
                {
                    f"{side}_points_before": standing.get("points_before"),
                    f"{side}_matches_played_before": standing.get(
                        "matches_played_before"
                    ),
                    f"{side}_goal_difference_before": standing.get(
                        "goal_difference_before"
                    ),
                    f"{side}_remaining_games_before": standing.get(
                        "remaining_group_matches"
                    ),
                    f"{side}_table_position_min": standing.get("rank_min"),
                    f"{side}_table_position_max": standing.get("rank_max"),
                    f"{side}_table_position_tied": (
                        standing.get("rank_status") == "unresolved_points_tie"
                        if standing.get("rank_status") is not None
                        else None
                    ),
                    f"{side}_points_to_qualification_boundary": qualification.get(
                        "points_to_relevant_boundary"
                    ),
                    f"{side}_points_gap_to_promotion": qualification.get(
                        "points_to_promotion_boundary"
                    ),
                    f"{side}_points_to_relegation_boundary": relegation.get(
                        "points_to_relevant_boundary"
                    ),
                    f"{side}_points_gap_to_relegation_playoff": relegation.get(
                        "points_to_playoff_boundary"
                    ),
                    f"{side}_qualification_still_possible": qualification.get(
                        "can_still_qualify"
                    ),
                    f"{side}_mathematically_qualified": qualification.get(
                        "mathematically_qualified"
                    ),
                    f"{side}_mathematically_eliminated": qualification.get(
                        "mathematically_eliminated"
                    ),
                    f"{side}_mathematically_promoted": qualification.get(
                        "mathematically_promoted"
                    ),
                    f"{side}_mathematically_relegated": relegation.get(
                        "mathematically_relegated"
                    ),
                    f"{side}_mathematically_safe_from_relegation": relegation.get(
                        "mathematically_safe_from_relegation"
                    ),
                    f"{side}_promotion_still_possible": qualification.get(
                        "can_be_promoted"
                    ),
                    f"{side}_relegation_still_possible": relegation.get(
                        "can_be_relegated"
                    ),
                    f"{side}_win_required_for_mathematical_goal": primitives.get(
                        "win_required_for_mathematical_goal"
                    ),
                    f"{side}_draw_sufficient_for_mathematical_goal": primitives.get(
                        "draw_sufficient_for_mathematical_goal"
                    ),
                    f"{side}_loss_eliminates": primitives.get("loss_eliminates"),
                }
            )
        feature["points_diff_home_minus_away"] = (
            (feature["home_points_before"] - feature["away_points_before"])
            if feature["home_points_before"] is not None
            and feature["away_points_before"] is not None
            else None
        )
        feature["mathematically_consequential"] = (
            any(value is True for value in consequential_values)
            if consequential_values
            else None
        )
        output.append(feature)
    return pd.DataFrame(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competition-state", required=True, type=Path)
    parser.add_argument("--coverage", required=True, type=Path)
    parser.add_argument("--results-cache", required=True, type=Path)
    parser.add_argument("--source-main-sha", required=True)
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
    dataset = read_json(args.competition_state)
    coverage = read_json(args.coverage)
    blockers = validate_b4_artifact(dataset, coverage)
    dataset_digest = canonical_digest(dataset)
    audit: dict[str, Any]
    if blockers:
        audit = {
            "schema": "nations-league-context-ablation-audit-v1",
            "status": "NL_CONTEXT_BLOCKED",
            "evidence_state": "dependency_not_ready",
            "dependency": "NL_COMPETITION_STATE_DATASET_READY",
            "competition_state_dataset": str(args.competition_state),
            "competition_state_dataset_sha256": dataset_digest,
            "competition_state_artifact_file_sha256": file_digest(
                args.competition_state
            ),
            "b4_coverage_digest": coverage.get("coverage_digest"),
            "b4_coverage_file_sha256": file_digest(args.coverage),
            "source_main_sha": args.source_main_sha,
            "artifact_source_status": "unmerged_builder4_intermediate_not_ready",
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
                "empirical metrics: withheld until B4 dataset readiness and exact causal baseline join",
                "subjective motivation: not defined or used",
                "causal GBT/stacker: no independently verified row-level replay supplied",
            ],
            "stacker_recommendation": "Do not include until a canonical B4 dataset is ready and the paired causal ablation is complete.",
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
    results, baseline_source_sha = load_local_results(args.results_cache)
    matches = pd.DataFrame(
        [
            {
                "date": pd.Timestamp(record["fixture_date"]),
                "home_team": record["home_team"],
                "away_team": record["away_team"],
                "home_score": record["home_score"],
                "away_score": record["away_score"],
                "neutral": record["neutral"],
                "edition": record["edition"],
                "validation_period": record["validation_period"],
            }
            for record in records
        ]
    )
    baseline_predictions = predict_dc_event_walk_forward(results, matches)
    examples = derive_context_rows(records)
    probability_rows = []
    expected_keys = {
        (row.date, row.home_team, row.away_team)
        for row in matches.itertuples(index=False)
    }
    if len(expected_keys) != EXPECTED_FIXTURES or len(baseline_predictions) != len(
        expected_keys
    ):
        raise ValueError(
            "Causal DC baseline did not cover the exact B4 fixture set; no partial ablation is permitted"
        )
    prediction_by_fixture = {
        record["fixture_id"]: baseline_predictions[
            (
                pd.Timestamp(record["fixture_date"]),
                record["home_team"],
                record["away_team"],
            )
        ]["probabilities"]
        for record in records
    }
    for _, row in examples.iterrows():
        baseline_probability = prediction_by_fixture[row["fixture_id"]]
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
        [record["fixture_id"] for record in records],
        numeric_context=CONTEXT_NUMERIC_FEATURES,
        categorical_context=CONTEXT_CATEGORICAL_FEATURES,
        provenance={
            "competition_state_dataset_sha256": dataset_digest,
            "competition_state_artifact_path": str(args.competition_state),
            "competition_state_artifact_file_sha256": file_digest(
                args.competition_state
            ),
            "b4_coverage_digest": coverage["coverage_digest"],
            "b4_coverage_file_sha256": file_digest(args.coverage),
            "baseline_source_sha256": baseline_source_sha,
            "baseline_method": "existing causal event-level Dixon-Coles walk-forward",
            "source_main_sha": args.source_main_sha,
        },
    )
    audit["excluded_features"] = [
        "causal GBT and stacker variants: no independently verified B1 row-level replay supplied",
        "subjective motivation: not defined or used",
        "exact table position where pre-match tiebreak remains unresolved: rank interval retained instead",
    ]
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
