from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts.run_nations_league_context_ablation import (
    build_parser,
    canonical_digest,
    derive_context_rows,
    result_identity_team,
    validate_b4_artifact,
    validate_git_sha,
)
from src.analysis.nations_league_context import (
    build_context_features,
    evaluate_paired_probabilities,
    render_context_ablation_markdown,
    run_causal_context_ablation,
    run_paired_ablation,
)


def test_result_identity_uses_only_the_explicit_turkey_endonym_alias():
    assert result_identity_team("Turkey") == "Türkiye"
    assert result_identity_team("Türkiye") == "Türkiye"
    assert result_identity_team("Hungary") == "Hungary"


def _schedule() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("f1", "2024/25", "A", "A1", 1, "2024-09-01T18:00:00Z", "Alpha", "Bravo"),
            ("f2", "2024/25", "A", "A1", 1, "2024-09-01T18:00:00Z", "Charlie", "Delta"),
            ("f3", "2024/25", "A", "A1", 2, "2024-09-05T18:00:00Z", "Alpha", "Charlie"),
            ("f4", "2024/25", "A", "A1", 2, "2024-09-05T18:00:00Z", "Bravo", "Delta"),
            ("f5", "2024/25", "A", "A1", 3, "2024-09-09T18:00:00Z", "Alpha", "Delta"),
            ("f6", "2024/25", "A", "A1", 3, "2024-09-09T18:00:00Z", "Bravo", "Charlie"),
        ],
        columns=[
            "fixture_id",
            "edition",
            "league",
            "group",
            "matchday",
            "kickoff",
            "home_team",
            "away_team",
        ],
    )


def _rules() -> dict:
    return {
        ("2024/25", "A", "A1"): {
            "qualification_slots": 1,
            "promotion_slots": 0,
            "relegation_slots": 1,
            "relegation_playoff_slots": 0,
            "expected_fixtures_per_team": 3,
            "table_tiebreakers": ("points", "goal_difference", "goals_for"),
            "mathematical_goal": "qualification",
        }
    }


def _results() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("f1", 2, 0),
            ("f2", 1, 0),
            # Deliberately include target and later scores: the target's features must ignore them.
            ("f3", 99, 0),
            ("f4", 0, 99),
            ("f5", 0, 0),
            ("f6", 0, 0),
        ],
        columns=["fixture_id", "home_score", "away_score"],
    )


def test_features_use_explicit_context_and_only_prior_kickoff_results():
    features = build_context_features(_schedule(), _results(), _rules())
    target = features.set_index("fixture_id").loc["f3"]
    assert target["league"] == "A"
    assert target["group"] == "A1"
    assert target["matchday"] == 2
    assert target["home_points_before"] == 3
    assert target["home_matches_played_before"] == 1
    assert target["points_diff_home_minus_away"] == 0
    assert target["home_goal_difference_before"] == 2
    assert target["home_table_position_before"] == 1
    assert target["home_remaining_games_before"] == 2
    assert target["home_points_gap_to_qualification"] == 0
    assert (
        target["home_draw_sufficient_for_mathematical_goal"]
        == target["home_draw_sufficient_for_qualification"]
    )
    assert bool(target["home_mathematically_qualified"]) is False
    assert pd.isna(target["home_mathematically_promoted"])

    changed_future = _results().copy()
    changed_future.loc[
        changed_future["fixture_id"].ne("f1") & changed_future["fixture_id"].ne("f2"),
        ["home_score", "away_score"],
    ] = [0, 75]
    after = (
        build_context_features(_schedule(), changed_future, _rules())
        .set_index("fixture_id")
        .loc["f3"]
    )
    invariant = [
        "home_points_before",
        "home_goal_difference_before",
        "home_table_position_before",
        "away_points_before",
        "away_goal_difference_before",
        "away_table_position_before",
        "home_qualification_still_possible",
        "home_draw_sufficient_for_qualification",
    ]
    assert target[invariant].to_dict() == after[invariant].to_dict()


def test_same_kickoff_matches_never_enter_each_others_standings():
    altered = _results().copy()
    altered.loc[altered["fixture_id"].eq("f4"), ["home_score", "away_score"]] = [0, 50]
    baseline = build_context_features(_schedule(), _results(), _rules()).set_index(
        "fixture_id"
    )
    changed = build_context_features(_schedule(), altered, _rules()).set_index(
        "fixture_id"
    )
    assert (
        baseline.loc["f3", "home_points_before"]
        == changed.loc["f3", "home_points_before"]
    )
    assert (
        baseline.loc["f3", "away_points_before"]
        == changed.loc["f3", "away_points_before"]
    )


def test_missing_prior_result_and_missing_group_rules_fail_closed():
    missing_score = _results().loc[lambda frame: frame["fixture_id"].ne("f1")]
    with pytest.raises(ValueError, match="lack final results"):
        build_context_features(_schedule(), missing_score, _rules())
    with pytest.raises(ValueError, match="Missing explicit group rules"):
        build_context_features(_schedule(), _results(), {})
    with pytest.raises(ValueError, match="Schedule is incomplete"):
        build_context_features(_schedule().iloc[:-1], _results().iloc[:-1], _rules())


def test_duplicate_fixtures_and_unknown_tiebreakers_fail_closed():
    duplicate = pd.concat([_schedule(), _schedule().iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="fixture_id values must be unique"):
        build_context_features(duplicate, _results(), _rules())
    invalid_rules = _rules()
    invalid_rules[("2024/25", "A", "A1")]["table_tiebreakers"] = (
        "latest_news_sentiment",
    )
    with pytest.raises(ValueError, match="Unsupported table tiebreaker"):
        build_context_features(_schedule(), _results(), invalid_rules)
    excess_slots = _rules()
    excess_slots[("2024/25", "A", "A1")]["relegation_slots"] = 3
    excess_slots[("2024/25", "A", "A1")]["relegation_playoff_slots"] = 2
    with pytest.raises(ValueError, match="slots exceed group size"):
        build_context_features(_schedule(), _results(), excess_slots)


def test_unresolved_table_tie_is_not_reported_as_a_false_exact_rank():
    schedule = _schedule()
    result = _results()
    result.loc[result["fixture_id"].eq("f1"), ["home_score", "away_score"]] = [1, 0]
    result.loc[result["fixture_id"].eq("f2"), ["home_score", "away_score"]] = [1, 0]
    # Both winners have the same points and goal difference only after adjusting the first score.
    result.loc[result["fixture_id"].eq("f1"), ["home_score", "away_score"]] = [1, 0]
    tied = (
        build_context_features(schedule, result, _rules())
        .set_index("fixture_id")
        .loc["f3"]
    )
    assert bool(tied["home_table_position_tied"]) is True
    assert pd.isna(tied["home_table_position_before"])
    assert tied["home_table_position_min"] == 1
    assert tied["home_table_position_max"] == 2


def test_near_must_win_is_not_invented_without_a_declared_threshold():
    row = (
        build_context_features(_schedule(), _results(), _rules())
        .set_index("fixture_id")
        .loc["f3"]
    )
    assert pd.isna(row["home_near_must_win"])
    assert "threshold" in row["home_near_must_win_reason"]


def test_mathematically_required_win_is_based_on_complete_future_schedule():
    schedule = pd.DataFrame(
        [
            (
                "leg-1",
                "2024/25",
                "A",
                "A1",
                1,
                "2024-09-01T18:00:00Z",
                "Bravo",
                "Alpha",
            ),
            (
                "leg-2",
                "2024/25",
                "A",
                "A1",
                2,
                "2024-09-05T18:00:00Z",
                "Alpha",
                "Bravo",
            ),
        ],
        columns=[
            "fixture_id",
            "edition",
            "league",
            "group",
            "matchday",
            "kickoff",
            "home_team",
            "away_team",
        ],
    )
    rules = {
        ("2024/25", "A", "A1"): {
            "qualification_slots": 1,
            "promotion_slots": 0,
            "relegation_slots": 0,
            "relegation_playoff_slots": 0,
            "expected_fixtures_per_team": 2,
            "table_tiebreakers": ("points",),
            "mathematical_goal": "qualification",
        }
    }
    results = pd.DataFrame(
        [("leg-1", 1, 0)], columns=["fixture_id", "home_score", "away_score"]
    )
    target = (
        build_context_features(schedule, results, rules)
        .set_index("fixture_id")
        .loc["leg-2"]
    )
    assert bool(target["home_qualification_still_possible"]) is True
    assert bool(target["home_must_win_for_qualification"]) is True
    assert bool(target["home_draw_sufficient_for_qualification"]) is False
    assert bool(target["home_win_required_for_mathematical_goal"]) is True
    assert bool(target["home_loss_eliminates"]) is True
    assert bool(target["home_mathematically_qualified"]) is False
    assert target["home_mathematically_promoted"] is None


def test_feature_module_has_no_production_or_network_imports():
    module_path = (
        Path(__file__).parents[2] / "src" / "analysis" / "nations_league_context.py"
    )
    module = ast.parse(module_path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {"requests", "src.scanner", "src.runtime", "src.betting", "src.ledger"}
    assert not any(
        name == blocked or name.startswith(f"{blocked}.")
        for name in imported
        for blocked in forbidden
    )


def test_paired_metrics_include_bootstrap_calibration_coverage_and_strata():
    rows = []
    for index in range(120):
        outcome = index % 3
        base = [0.275, 0.275, 0.275]
        base[outcome] = 0.45
        context = [0.265, 0.265, 0.265]
        context[outcome] = 0.47
        tier = "ABCD"[(index // 30) % 4]
        rows.append(
            {
                "fixture_id": f"synthetic-unit-{index}",
                "kickoff": pd.Timestamp("2024-01-01", tz="UTC")
                + pd.Timedelta(days=index),
                "edition": "2024/25",
                "league_tier": tier,
                "group": "G1",
                "group_phase_progress": (
                    "early_by_matches_played" if index % 2 else "late_by_matches_played"
                ),
                "points_gap_band": "tight_1_to_3" if index % 4 else "level",
                "points_bound_constraint": (
                    "constrained_by_supported_points_bound"
                    if index % 5 == 0
                    else "no_closed_supported_points_bound"
                ),
                "outcome": outcome,
                "baseline_probabilities": base,
                "context_probabilities": context,
            }
        )
    report = evaluate_paired_probabilities(
        rows,
        eligible_count=120,
        n_bootstrap=100,
        minimum_evaluation_count=100,
        seed=7,
    )
    assert report["schema"] == "nations-league-safe-context-ablation-audit-v1"
    assert report["fixtures_evaluated"] == report["eligible_fixtures"] == 120
    assert report["coverage"] == 1.0
    assert report["baseline"]["home_calibration"]
    assert report["baseline"]["draw_calibration"]
    assert report["baseline"]["away_calibration"]
    assert report["paired_date_cluster_bootstrap"]["confidence_level"] == 0.95
    assert (
        report["paired_date_cluster_bootstrap"]["brier_context_minus_baseline"][
            "bootstrap_replicates"
        ]
        == 100
    )
    assert set(report["strata"]) >= {
        "group_phase_progress",
        "points_gap_band",
        "points_bound_constraint",
        "league_tier",
        "edition",
        "observed_outcome",
    }
    assert (
        report["global_claim_basis"] == "full_paired_out_of_sample_fixture_cohort_only"
    )


def test_paired_ablation_is_expanding_window_and_uses_identical_prediction_rows():
    count = 72
    outcomes = np.asarray([index % 3 for index in range(count)])
    examples = pd.DataFrame(
        {
            "fixture_id": [f"fixture-{index}" for index in range(count)],
            "kickoff": pd.date_range("2020-01-01", periods=count, freq="7D", tz="UTC"),
            "outcome": outcomes,
            "base_p_home": np.where(outcomes == 0, 0.5, 0.25),
            "base_p_draw": np.where(outcomes == 1, 0.5, 0.25),
            "base_p_away": np.where(outcomes == 2, 0.5, 0.25),
            "home_points_before": np.arange(count) % 10,
            "league": ["A", "B", "C", "D"] * 18,
        }
    )
    report = run_paired_ablation(
        examples,
        numeric_context=["home_points_before"],
        categorical_context=["league"],
        minimum_training_rows=18,
    )
    assert report["schema"] == "nations-league-context-ablation-v1"
    assert report["evaluation"] == "paired_expanding_window_causal"
    assert report["no_lookahead"] is True
    assert report["predicted_rows"] == 54
    assert report["baseline"]["multiclass_brier"] >= 0
    assert report["baseline_plus_context"]["log_loss"] >= 0
    assert len(report["predictions"]) == 54
    assert all(
        pd.Timestamp(row["training_max_kickoff"]) < pd.Timestamp(row["kickoff"])
        for row in report["predictions"]
    )


def test_end_to_end_runner_requires_exact_pre_kickoff_cohort_and_emits_report():
    count = 150
    outcomes = np.asarray([index % 3 for index in range(count)])
    examples = pd.DataFrame(
        {
            "fixture_id": [f"synthetic-unit-{index}" for index in range(count)],
            "kickoff": pd.date_range("2020-01-01", periods=count, freq="7D", tz="UTC"),
            "state_cutoff": pd.date_range(
                "2020-01-01", periods=count, freq="7D", tz="UTC"
            ),
            "record_digest": ["a" * 64] * count,
            "causal_information_verified": [True] * count,
            "edition": ["2020/21"] * count,
            "league_tier": ["ABCD"[(index // 30) % 4] for index in range(count)],
            "group": ["G1"] * count,
            "group_phase_progress": ["early_by_matches_played"] * count,
            "points_gap_band": ["tight_1_to_3"] * count,
            "points_bound_constraint": ["no_closed_supported_points_bound"] * count,
            "outcome": outcomes,
            "base_p_home": np.where(outcomes == 0, 0.5, 0.25),
            "base_p_draw": np.where(outcomes == 1, 0.5, 0.25),
            "base_p_away": np.where(outcomes == 2, 0.5, 0.25),
            "home_points_before": np.arange(count) % 12,
        }
    )
    examples["points_bound_constraint"] = examples["points_bound_constraint"].astype(
        object
    )
    examples.loc[149, "points_bound_constraint"] = None
    provenance = {
        "competition_state_dataset_sha256": "b" * 64,
        "baseline_source_sha256": "c" * 64,
        "source_main_sha": "d" * 40,
    }
    report = run_causal_context_ablation(
        examples,
        examples["fixture_id"].tolist(),
        numeric_context=["home_points_before"],
        categorical_context=["league_tier", "group"],
        provenance=provenance,
        minimum_training_rows=30,
        n_bootstrap=100,
        seed=9,
    )
    assert report["eligible_fixtures"] == 150
    assert report["fixtures_evaluated"] == 120
    assert len(report["walk_forward"]["warmup_exclusions"]) == 30
    assert report["walk_forward"]["no_lookahead"] is True
    assert (
        report["strata"]["points_bound_constraint"]["unavailable"]["sample_count"] == 1
    )
    assert report["synthetic_evidence_used"] is False
    assert "Primary paired metrics" in render_context_ablation_markdown(report)
    with pytest.raises(ValueError, match="exactly match"):
        run_causal_context_ablation(
            examples.iloc[:-1],
            examples["fixture_id"].tolist(),
            numeric_context=["home_points_before"],
            categorical_context=["league_tier", "group"],
            provenance=provenance,
            minimum_training_rows=30,
            n_bootstrap=100,
        )
    late_cutoff = examples.copy()
    late_cutoff.loc[0, "causal_information_verified"] = False
    with pytest.raises(ValueError, match="verified strictly pre-kickoff"):
        run_causal_context_ablation(
            late_cutoff,
            late_cutoff["fixture_id"].tolist(),
            numeric_context=["home_points_before"],
            categorical_context=["league_tier", "group"],
            provenance=provenance,
            minimum_training_rows=30,
            n_bootstrap=100,
        )


def _safe_partial_b4_bundle():
    fixture_id = "uefa-nl:synthetic-unit-fixture"
    kickoff = "2024-09-05T18:00:00Z"
    timeline_record = {
        "fixture_id": fixture_id,
        "edition": "2024/25",
        "date": "2024-09-05",
        "kickoff_utc": kickoff,
        "group": "A1",
        "home_team": "Alpha",
        "away_team": "Bravo",
        "home_score": 0,
        "away_score": 0,
        "status": "completed_result_recorded",
        "result_safe_available_at": "2024-09-06T00:00:00Z",
    }
    timeline_record["record_digest"] = canonical_digest(timeline_record)
    timeline = {
        "schema_version": "uefa-nations-league-fixture-timeline-v1",
        "records": [timeline_record],
    }
    timeline["dataset_digest"] = canonical_digest(timeline)
    standing_rows = [
        {
            "team": team,
            "competition_status": "active",
            "matches_played_before": 0,
            "points_before": 0,
            "goals_for_before": 0,
            "goals_against_before": 0,
            "goal_difference_before": 0,
            "wins_before": 0,
            "draws_before": 0,
            "losses_before": 0,
            "remaining_group_matches": 0,
            "rank_min": None,
            "rank_max": None,
            "rank_status": "unresolved_points_tie",
        }
        for team in ("Alpha", "Bravo")
    ]
    record = {
        "fixture_id": fixture_id,
        "fixture_date": "2024-09-05",
        "kickoff": kickoff,
        "kickoff_status": "verified_from_fixture_timeline",
        "state_cutoff": kickoff,
        "state_cutoff_basis": "strict target kickoff instant; only result_safe_available_at strictly before kickoff is included",
        "result_safe_available_at": timeline_record["result_safe_available_at"],
        "timeline_record_digest": timeline_record["record_digest"],
        "stage": "league_phase",
        "edition": "2024/25",
        "validation_period": "2024/25",
        "league_tier": "A",
        "group": "A1",
        "home_league_tier": "A",
        "away_league_tier": "A",
        "home_group": "A1",
        "away_group": "A1",
        "home_team": "Alpha",
        "away_team": "Bravo",
        "home_score": 0,
        "away_score": 0,
        "neutral": False,
        "remaining_schedule": {
            "status": "timeline_bound_without_future_results",
            "fixtures": [],
        },
        "standings_before": [
            {
                "group": "A1",
                "league_tier": "A",
                "prior_result_fixture_ids": [],
                "standing_rows": standing_rows,
            }
        ],
        "qualification_state": {
            "participants": {
                "Alpha": {
                    "promotion": {
                        "can_be_promoted": True,
                        "status": "points_bounds_only_tiebreaks_preserved_as_unresolved",
                    },
                    "relegation": {
                        "can_be_relegated": False,
                        "status": "points_bounds_only_tiebreaks_preserved_as_unresolved",
                    },
                },
                "Bravo": {
                    "promotion": {
                        "can_be_promoted": True,
                        "status": "points_bounds_only_tiebreaks_preserved_as_unresolved",
                    },
                    "relegation": {
                        "can_be_relegated": True,
                        "status": "points_bounds_only_tiebreaks_preserved_as_unresolved",
                    },
                },
            }
        },
    }
    dataset = {
        "schema_version": "uefa-nations-league-causal-competition-state-v1",
        "source_snapshot_digest": "f" * 64,
        "records": [record],
    }
    record["source_digest"] = dataset["source_snapshot_digest"]
    record["record_digest"] = canonical_digest(record)
    dataset_digest = canonical_digest(dataset)
    coverage = {
        "status": "NL_COMPETITION_STATE_PARTIAL",
        "expected_evaluation_fixture_count": 1,
        "output_record_count": 1,
        "fixture_coverage_complete": True,
        "official_schedule_match_coverage_verified": True,
        "dataset_digest": dataset_digest,
        "fields": {
            "kickoff_timestamp": {
                "present": 1,
                "missing": 0,
                "status": "verified_from_fixture_timeline",
            },
            "causal_date_cutoff": {"strict_kickoff_timestamp_comparison": 1},
        },
        "leakage_checks": {
            "final_standings_backfilled": False,
            "final_tables_read": False,
            "future_match_scores_in_state": False,
            "same_day_results_excluded": True,
            "uses_only_result_dates_strictly_before_fixture_date": True,
        },
        "timeline_join": {
            "joined_complete": True,
            "joined_records": 1,
            "missing_fixture_ids": [],
            "timeline_dataset_digest": timeline["dataset_digest"],
        },
    }
    coverage["coverage_digest"] = canonical_digest(coverage)
    return dataset, coverage, timeline


def test_partial_b4_artifact_is_accepted_when_safe_subset_is_complete_and_hashed():
    dataset, coverage, timeline = _safe_partial_b4_bundle()
    assert validate_b4_artifact(dataset, coverage, timeline, expected_fixtures=1) == []
    assert "B4_fixture_timeline_missing" in validate_b4_artifact(dataset, coverage)


def test_partial_b4_artifact_digest_tampering_fails_closed():
    dataset, coverage, timeline = _safe_partial_b4_bundle()
    dataset["records"][0]["home_score"] = 2
    blockers = validate_b4_artifact(dataset, coverage, timeline, expected_fixtures=1)
    assert "B4_dataset_digest_mismatch" in blockers
    assert "B4_record_digest_mismatch" in blockers


def test_safe_feature_projection_ignores_unsupported_matchday_and_motivation_fields():
    dataset, _, _ = _safe_partial_b4_bundle()
    record = dataset["records"][0]
    record["matchday"] = 4
    record["official_fixture_id"] = "must-not-be-a-feature"
    record["qualification_state"]["mathematically_qualified"] = True
    record["must_win_primitives"] = {"win_required_for_mathematical_goal": True}
    projected = (
        derive_context_rows([record]).set_index("fixture_id").loc[record["fixture_id"]]
    )
    assert projected["home_points_before"] == 0
    assert pd.isna(projected["home_points_per_game_before"])
    assert bool(projected["home_table_position_tied"]) is True
    assert pd.isna(projected["home_table_position_min"])
    assert bool(projected["home_points_bound_relegation_possible"]) is False
    assert (
        projected["points_bound_constraint"] == "constrained_by_supported_points_bound"
    )
    assert projected["group_phase_progress"] == "early_by_matches_played"
    assert "matchday" not in projected.index
    assert "official_fixture_id" not in projected.index
    assert "home_mathematically_qualified" not in projected.index
    assert "home_win_required_for_mathematical_goal" not in projected.index


def test_ablation_models_missing_context_as_unavailable_and_rejects_invalid_probabilities():
    examples = pd.DataFrame(
        {
            "kickoff": pd.date_range("2020-01-01", periods=6, freq="D", tz="UTC"),
            "outcome": [0, 1, 2, 0, 1, 2],
            "base_p_home": [0.4] * 6,
            "base_p_draw": [0.3] * 6,
            "base_p_away": [0.3] * 6,
            "context": [1, 2, None, 4, 5, 6],
        }
    )
    missing_safe = run_paired_ablation(
        examples, numeric_context=["context"], minimum_training_rows=3
    )
    assert missing_safe["no_lookahead"] is True
    examples["context"] = 1
    examples.loc[0, "base_p_home"] = 2.0
    with pytest.raises(ValueError, match="sum to one"):
        run_paired_ablation(
            examples, numeric_context=["context"], minimum_training_rows=3
        )


def test_runner_binds_the_exact_b4_source_commit_and_optional_pr():
    parser = build_parser()
    common = [
        "--competition-state",
        "state.json",
        "--coverage",
        "coverage.json",
        "--timeline",
        "timeline.json",
        "--results-cache",
        "results.csv",
        "--source-main-sha",
        "a" * 40,
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(common)
    parsed = parser.parse_args(
        [
            *common,
            "--competition-state-source-sha",
            "B" * 40,
            "--competition-state-source-pr",
            "215",
        ]
    )
    assert parsed.competition_state_source_sha == "B" * 40
    assert parsed.competition_state_source_pr == 215
    assert (
        validate_git_sha("B4 source", parsed.competition_state_source_sha) == "b" * 40
    )
    with pytest.raises(ValueError, match="full 40- or 64-character Git SHA"):
        validate_git_sha("B4 source", "short")
