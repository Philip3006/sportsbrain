import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts.nations_league_native_research import blocked_report
from src.analysis import nations_league_native_research as research
from src.analysis import nations_league_validation as validation


def _row(date, home, away, hg, ag, tournament="UEFA Nations League", neutral=False):
    return {
        "date": pd.Timestamp(date),
        "home_team": home,
        "away_team": away,
        "home_score": hg,
        "away_score": ag,
        "tournament": tournament,
        "neutral": neutral,
    }


def test_training_source_classes_keep_nl_uefa_friendlies_and_ambiguous_global_rows_separate():
    assert (
        research.classify_training_match(
            pd.Series(
                _row("2020-01-01", "France", "Germany", 1, 0, "UEFA Nations League")
            )
        )
        == "nations_league"
    )
    assert (
        research.classify_training_match(
            pd.Series(
                _row("2020-01-01", "France", "Germany", 1, 0, "UEFA Euro qualification")
            )
        )
        == "uefa_competitive"
    )
    assert (
        research.classify_training_match(
            pd.Series(
                _row("2020-01-01", "France", "Germany", 1, 0, "International Friendly")
            )
        )
        == "friendly"
    )
    assert (
        research.classify_training_match(
            pd.Series(
                _row(
                    "2020-01-01", "Haiti", "Japan", 1, 0, "FIFA World Cup qualification"
                )
            )
        )
        == "excluded_or_unclassified"
    )
    assert (
        research.classify_training_match(
            pd.Series(
                _row(
                    "2020-01-01",
                    "France",
                    "Germany",
                    1,
                    0,
                    "UEFA Nations League qualification",
                )
            )
        )
        == "excluded_or_unclassified"
    )


def test_weight_grid_is_explicit_and_nl_gets_highest_weight():
    specs = research.weight_specs()
    assert len(specs) == 13
    assert {spec.ablation for spec in specs} == {
        "nl_only",
        "nl_plus_uefa_competitive",
        "nl_uefa_competitive_plus_downweighted_friendlies",
    }
    spec = next(
        item
        for item in specs
        if item.ablation == "nl_uefa_competitive_plus_downweighted_friendlies"
        and item.uefa_competitive_weight == 0.5
        and item.recency_half_life_years is None
    )
    frame = pd.DataFrame(
        [
            _row("2020-01-01", "France", "Germany", 1, 0),
            _row("2020-01-02", "France", "Germany", 1, 0, "UEFA Euro"),
            _row("2020-01-03", "France", "Germany", 1, 0, "Friendly"),
            _row("2020-01-04", "Haiti", "Japan", 1, 0, "FIFA World Cup qualification"),
        ]
    )
    weights = research.training_weights(frame, pd.Timestamp("2021-01-01"), spec)
    assert weights[:3] == pytest.approx([1.0, 0.5, 0.05])
    assert weights[3] == 0.0


def test_training_cutoff_is_exclusive():
    frame = pd.DataFrame(
        [
            _row("2020-01-01", "France", "Germany", 1, 0),
            _row("2020-01-02", "France", "Germany", 1, 0),
        ]
    )
    with pytest.raises(ValueError, match="strictly earlier"):
        research.training_weights(
            frame, pd.Timestamp("2020-01-02"), research.weight_specs()[0]
        )


def test_result_features_exclude_same_day_and_future_outcomes():
    source = pd.DataFrame(
        [
            _row("2020-01-01", "France", "Germany", 1, 0, "UEFA Euro qualification"),
            _row("2024-09-05", "Spain", "Italy", 2, 0),
            _row("2024-09-05", "Spain", "Portugal", 0, 1),
            _row("2024-09-06", "France", "Spain", 1, 1),
        ]
    ).reset_index(drop=True)
    source["native_row_id"] = np.arange(len(source))
    baseline = research.causal_feature_frame(source)

    same_day_changed = source.copy()
    same_day_changed.loc[1, ["home_score", "away_score"]] = [0, 5]
    changed = research.causal_feature_frame(same_day_changed)
    pd.testing.assert_series_equal(baseline.loc[2], changed.loc[2])

    future_changed = source.copy()
    future_changed.loc[1:, ["home_score", "away_score"]] = [4, 0]
    changed = research.causal_feature_frame(future_changed)
    pd.testing.assert_series_equal(baseline.loc[1], changed.loc[1])


def test_gbt_training_and_prediction_are_reproducible_without_random_split():
    x = pd.DataFrame(
        {"x": np.arange(60, dtype=float), "z": np.arange(60, dtype=float) % 7}
    )
    y = pd.Series(np.tile([0, 1, 2], 20))
    params = {**research.RESEARCH_GBT_PARAMS, "max_iter": 12, "min_samples_leaf": 2}
    first = research.train(x, y, params=params)
    second = research.train(x, y, params=params)
    np.testing.assert_array_equal(
        research.predict_proba(first, x), research.predict_proba(second, x)
    )
    assert params["early_stopping"] is False


def test_paired_date_bootstrap_is_seeded_and_reports_paired_difference():
    rows = [
        {
            "date": f"2024-09-0{day}",
            "validation_period": "2024/25",
            "outcome_index_home_draw_away": outcome,
            "predictions": {
                "candidate": {"probabilities": candidate},
                "elo": {"probabilities": [1 / 3, 1 / 3, 1 / 3]},
            },
        }
        for day, outcome, candidate in (
            (5, 0, [0.7, 0.2, 0.1]),
            (6, 1, [0.2, 0.6, 0.2]),
            (7, 2, [0.1, 0.2, 0.7]),
        )
    ]
    first = research.paired_date_cluster_bootstrap(
        rows, [("candidate", "elo")], n_bootstrap=100, seed=19
    )
    second = research.paired_date_cluster_bootstrap(
        rows, [("candidate", "elo")], n_bootstrap=100, seed=19
    )
    assert first == second
    assert "candidate_minus_elo" in first["brier_difference_intervals"]


def test_walk_forward_runner_enforces_block_start_cutoffs_and_emits_all_metrics(
    monkeypatch,
):
    teams = ["France", "Germany", "Italy", "Spain", "Portugal", "England"]
    rows = []
    for index, score in enumerate(((1, 0), (0, 0), (0, 1), (2, 0), (1, 1), (0, 2))):
        rows.append(
            _row(
                pd.Timestamp("2018-01-01") + pd.Timedelta(days=index),
                teams[index],
                teams[(index + 1) % len(teams)],
                *score,
            )
        )
    for index, score in enumerate(((1, 0), (0, 0), (0, 1))):
        rows.append(
            _row(
                pd.Timestamp("2018-06-01") + pd.Timedelta(days=index),
                teams[(index + 2) % len(teams)],
                teams[(index + 4) % len(teams)],
                *score,
                "UEFA Euro qualification",
            )
        )

    targets = [
        ("2020-09-03", (1, 0)),
        ("2020-09-04", (0, 0)),
        ("2021-10-10", (0, 1)),
        ("2022-06-01", (1, 0)),
        ("2022-06-02", (0, 0)),
        ("2023-06-18", (0, 1)),
        ("2024-03-21", (1, 0)),
        ("2024-03-26", (0, 1)),
        ("2024-09-05", (1, 0)),
        ("2024-09-06", (0, 0)),
        ("2025-06-08", (0, 1)),
    ]
    for index, (date, score) in enumerate(targets):
        rows.append(_row(date, teams[index % 6], teams[(index + 2) % 6], *score))
    normalized = validation.normalize_results(pd.DataFrame(rows))

    class FakeModel:
        classes_ = np.asarray([0, 1, 2])

    monkeypatch.setattr(research, "train", lambda *args, **kwargs: FakeModel())
    monkeypatch.setattr(
        research,
        "predict_proba",
        lambda model, frame: np.tile([0.25, 0.35, 0.40], (len(frame), 1)),
    )

    def baseline_predictions(results, matches, **kwargs):
        return {
            (row.date, str(row.home_team), str(row.away_team)): {
                "probabilities": [0.4, 0.3, 0.3],
                "training_max_date": (pd.Timestamp(row.date) - pd.Timedelta(days=1))
                .date()
                .isoformat(),
                "training_match_count": 1,
                "training_cutoff_exclusive": pd.Timestamp(row.date).date().isoformat(),
            }
            for row in matches.itertuples()
        }

    monkeypatch.setattr(
        research.validation, "predict_dc_event_walk_forward", baseline_predictions
    )
    monkeypatch.setattr(research.validation, "predict_elo_asof", baseline_predictions)
    result = research.run_native_research(normalized)

    assert result["evaluation_match_count"] == len(targets)
    assert result["feature_provenance"]["same_day_information"].startswith(
        "features for all fixtures"
    )
    assert len(result["variants"]) == len(research.weight_specs())
    assert len(result["comparison_table"]) == len(research.weight_specs())
    assert len(result["evaluation_blocks"]) == 4
    for block in result["evaluation_blocks"].values():
        for variant in block["variants"].values():
            if variant["status"] == "evaluated":
                assert variant["no_lookahead"] is True
                assert variant["training_max_date"] < block["cutoff_exclusive"]
    assert (
        "home_draw_away_calibration"
        in result["variants"][research.weight_specs()[0].name]["metrics"]
    )
    assert (
        result["paired_date_cluster_bootstrap"][research.weight_specs()[0].name][
            "status"
        ]
        == "computed"
    )


def test_research_module_has_no_network_runtime_betting_or_ledger_dependency():
    module = ast.parse(Path(research.__file__).read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    forbidden = {"requests", "src.runtime", "src.scanner", "src.betting", "src.ledger"}
    assert not any(
        name == prefix or name.startswith(f"{prefix}.")
        for name in imports
        for prefix in forbidden
    )
    assert "fetch_international_results" not in {
        node.func.id
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_missing_explicit_cache_report_does_not_promote_prior_audit_to_current_evidence():
    prior = Path("results/audits/nations_league_model_validation_20260927.json")
    report = blocked_report(
        source_sha="research-test",
        verified_current_main_sha="main-test",
        requested_cache=None,
        prior_audit=prior,
    )
    assert report["status"] == "BLOCKED_MISSING_LOCAL_RESULTS_CACHE"
    assert report["dataset_sizes"]["current_task_evaluation_rows"] is None
    elo = next(row for row in report["model_comparison_table"] if row["model"] == "Elo")
    assert elo["status"] == "REFERENCE_ONLY_NOT_RERUN"
    candidate = next(
        row
        for row in report["model_comparison_table"]
        if row["model"] == "nations_league_v1 weighted result-only GBT"
    )
    assert candidate["status"] == "NOT_EVALUATED"
    assert report["snapshot_written"] is False
    assert report["data_provenance"]["network_fetch_performed"] is False
