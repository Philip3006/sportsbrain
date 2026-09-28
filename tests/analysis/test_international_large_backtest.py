import json

import pandas as pd

from src.analysis import international_large_backtest as backtest


def _row(date, home, away, hg, ag, tournament="UEFA Nations League", neutral=False):
    return {
        "date": date,
        "home_team": home,
        "away_team": away,
        "home_score": hg,
        "away_score": ag,
        "tournament": tournament,
        "neutral": neutral,
    }


def _normalized(rows):
    from src.analysis.nations_league_validation import normalize_results

    return backtest.add_classification(normalize_results(pd.DataFrame(rows)))


def test_competition_classification_preserves_required_tournament_separation():
    assert backtest.tournament_class("UEFA Nations League") == "uefa_nations_league"
    assert backtest.tournament_class("FIFA World Cup") == "world_cup"
    assert (
        backtest.tournament_class("FIFA World Cup qualification")
        == "world_cup_qualification"
    )
    assert backtest.tournament_class("UEFA Euro") == "uefa_euro"
    assert (
        backtest.tournament_class("UEFA Euro qualification")
        == "uefa_euro_qualification"
    )
    assert backtest.tournament_class("Friendly") == "friendly"
    assert backtest.tournament_class("Muratti Vase") == "other_international"


def test_friendlies_are_isolated_and_uefa_transfer_set_excludes_nl_and_friendlies():
    frame = _normalized(
        [
            _row("2020-01-01", "France", "Spain", 1, 0, "UEFA Euro qualification"),
            _row("2020-01-02", "France", "Spain", 0, 0, "Friendly"),
            _row("2020-01-03", "France", "Brazil", 1, 1, "FIFA World Cup"),
            _row("2020-01-04", "France", "Spain", 2, 1, "UEFA Nations League"),
            _row("2020-01-05", "Japan", "South Korea", 1, 0, "AFC Asian Cup"),
        ]
    )
    uefa = backtest.build_uefa_team_set(frame)
    cohorts = backtest.build_cohorts(frame, uefa)
    assert cohorts["friendlies"].sum() == 1
    assert cohorts["uefa_competitive_non_nl"].sum() == 2
    assert cohorts["uefa_nations_league"].sum() == 1
    assert cohorts["all_competitive"].sum() == 4
    assert cohorts["combined_all_international_diagnostic"].sum() == 5


def test_dc_block_fit_and_predictions_exclude_cutoff_day_and_future(monkeypatch):
    frame = _normalized(
        [
            _row("1999-12-31", "France", "Spain", 1, 0, "UEFA Euro qualification"),
            _row("2000-01-01", "Germany", "Italy", 2, 0, "UEFA Nations League"),
            _row("2000-01-01", "Spain", "Portugal", 0, 0, "Friendly"),
            _row("2000-01-02", "Italy", "France", 1, 1, "UEFA Euro qualification"),
        ]
    )
    calls = []

    def fake_fit(training, today, max_iter, prior_params):
        calls.append((training.copy(), today, max_iter, prior_params))
        return object()

    monkeypatch.setattr(backtest.dc, "fit", fake_fit)
    monkeypatch.setattr(
        backtest.dc,
        "predict_match",
        lambda home, away, params, neutral: {
            "p_home": 0.5,
            "p_draw": 0.3,
            "p_away": 0.2,
        },
    )
    predictions, blocks = backtest.predict_dc_block_frozen(frame, max_iter=77)
    assert len(calls) == 1
    training, cutoff, iterations, prior = calls[0]
    assert cutoff == pd.Timestamp("2000-01-01")
    assert training["date"].max() == pd.Timestamp("1999-12-31")
    assert not training["date"].ge(cutoff).any()
    assert iterations == 77
    assert prior is None
    assert len(predictions) == 3
    assert blocks[0]["strict_pre_fixture_cutoff"] is True
    assert all(row["training_max_date"] == "1999-12-31" for row in predictions.values())


def test_elo_same_day_predictions_are_frozen_before_same_day_outcomes():
    rows = [
        _row("1999-12-31", "France", "Spain", 1, 0, "UEFA Euro qualification"),
        _row("2000-01-01", "Germany", "Italy", 2, 0, "UEFA Nations League"),
        _row("2000-01-01", "Portugal", "Netherlands", 1, 1, "Friendly"),
        _row("2000-01-02", "Germany", "Portugal", 0, 1, "UEFA Euro qualification"),
    ]
    baseline = backtest.predict_elo_point_in_time(_normalized(rows))
    changed = list(rows)
    changed[1] = {**changed[1], "home_score": 0, "away_score": 5}
    after = backtest.predict_elo_point_in_time(_normalized(changed))
    same_day_ids = [1, 2]
    for source_id in same_day_ids:
        assert baseline[source_id]["probabilities"] == after[source_id]["probabilities"]
        assert baseline[source_id]["training_max_date"] == "1999-12-31"
    assert baseline[3]["probabilities"] != after[3]["probabilities"]


def test_recency_window_definitions_are_fixed_and_modern_slices_are_present():
    assert backtest.RECENCY_WINDOWS == {
        "2000_onward": "2000-01-01",
        "2010_onward": "2010-01-01",
        "2016_onward": "2016-01-01",
        "2020_onward": "2020-01-01",
    }
    assert backtest.CALENDAR_ERAS["2016_2019"] == ("2016-01-01", "2020-01-01")


def test_metric_computation_reports_multiclass_scores_calibration_and_favorites():
    rows = [
        {"outcome": 0, "probabilities": [0.7, 0.2, 0.1]},
        {"outcome": 2, "probabilities": [0.2, 0.2, 0.6]},
        {"outcome": 1, "probabilities": [0.4, 0.35, 0.25]},
    ]
    summary = backtest.summarize_prediction_rows(rows, eligible_count=4)
    assert summary["matches_evaluated"] == 3
    assert summary["coverage"] == 0.75
    assert "brier_score_multiclass" in summary["metrics"]
    assert "multiclass_log_loss" in summary["metrics"]
    assert set(summary["metrics"]["class_calibration"]) == {"home", "draw", "away"}
    assert summary["metrics"]["predicted_argmax_count"] == {
        "home": 2,
        "draw": 0,
        "away": 1,
    }
    assert (
        summary["metrics"]["favorite_strength_calibration"]["strong_favorite"]["count"]
        == 1
    )


def test_historical_market_matching_requires_complete_odds_before_match_date(tmp_path):
    path = tmp_path / "odds.json"
    path.write_text(
        json.dumps(
            [
                {
                    "ts": "2020-01-01T00:00:00Z",
                    "odds": {
                        "France vs Spain": {"home": 2.0, "draw": 3.0, "away": 4.0}
                    },
                },
                {
                    "ts": "2020-01-03T00:00:00Z",
                    "odds": {
                        "France vs Spain": {"home": 2.0, "draw": 3.0, "away": 4.0}
                    },
                },
                {
                    "ts": "2019-12-31T23:00:00Z",
                    "odds": {
                        "France vs Spain": {"home": 2.0, "draw": None, "away": 4.0}
                    },
                },
            ]
        )
    )
    frame = _normalized(
        [
            _row("2020-01-02", "France", "Spain", 1, 0, "UEFA Euro qualification"),
        ]
    )
    audit = backtest.audit_historical_odds(path, frame)
    assert audit["exact_fixture_name_matches"] == 3
    assert audit["complete_numeric_1x2_entries"] == 2
    assert audit["strict_pre_match_entries_using_pre_match_date_boundary"] == 1
    assert audit["matched_genuine_pre_match_international_1x2"] == 0
    assert audit["canonical_detect_value_run"] is False


def test_team_metrics_are_omitted_below_reporting_count_not_treated_as_gate():
    frame = _normalized(
        [
            _row(
                f"2016-01-{day:02d}",
                "Germany",
                "France",
                1,
                0,
                "UEFA Euro qualification",
            )
            for day in range(1, 25)
        ]
    )
    records = []
    for row in frame.itertuples(index=False):
        records.append(
            {
                "source_row_id": int(row.source_row_id),
                "outcome": int(row.outcome),
                "predictions": {
                    "elo": {"probabilities": [0.6, 0.2, 0.2]},
                },
            }
        )
    uefa = backtest.build_uefa_team_set(frame)
    report = backtest._team_level(frame, records, uefa)
    germany = report["teams"]["Germany"]
    assert germany["modern_competitive_appearances_2016_onward"] == 24
    assert germany["model_metrics"] is None
    assert report["threshold_is_launch_gate"] is False


def test_neutral_qualifier_and_confidence_strata_preserve_sample_counts():
    frame = _normalized(
        [
            _row(
                "2020-01-01",
                "Germany",
                "France",
                1,
                0,
                "FIFA World Cup qualification",
                True,
            ),
            _row("2020-01-02", "France", "Spain", 0, 0, "UEFA Nations League", False),
            _row("2020-01-03", "Spain", "Portugal", 2, 1, "Friendly", True),
            _row("2020-01-04", "Italy", "Netherlands", 1, 1, "UEFA Euro", False),
        ]
    )
    teams = backtest.build_uefa_team_set(frame)
    cohorts = backtest.build_cohorts(frame, teams)
    records = [
        {
            "source_row_id": int(row.source_row_id),
            "outcome": int(row.outcome),
            "date": row.date.date().isoformat(),
            "neutral": bool(row.neutral),
            "is_qualifier": bool(row.is_qualifier),
            "predictions": {
                "dixon_coles": {"probabilities": [0.60, 0.25, 0.15]},
                "elo": {"probabilities": [0.40, 0.30, 0.30]},
                "empirical_frequency": {"probabilities": [0.34, 0.33, 0.33]},
            },
        }
        for row in frame.itertuples(index=False)
    ]

    report = backtest._stratified_performance(frame, records, cohorts)
    competitive = report["all_competitive"]["2020_onward"]["strata"]
    assert competitive["venue"]["neutral"]["eligible_matches"] == 1
    assert competitive["venue"]["non_neutral"]["eligible_matches"] == 2
    assert competitive["qualification"]["qualifier"]["eligible_matches"] == 1
    assert competitive["qualification"]["non_qualifier"]["eligible_matches"] == 2
    confidence = competitive["forecast_confidence"]
    assert confidence["dixon_coles"]["moderate_favorite"]["matches_evaluated"] == 3
    assert confidence["elo"]["balanced"]["matches_evaluated"] == 3
    assert confidence["empirical_frequency"]["balanced"]["matches_evaluated"] == 3


def test_backtest_output_keeps_market_model_unavailable_and_side_effects_false(
    monkeypatch,
):
    frame = _normalized(
        [
            _row("1999-12-31", "France", "Spain", 1, 0, "UEFA Euro qualification"),
            _row("2000-01-01", "Germany", "Italy", 2, 0, "UEFA Nations League"),
            _row("2001-01-01", "Spain", "Portugal", 0, 0, "Friendly"),
        ]
    )
    monkeypatch.setattr(backtest.dc, "fit", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        backtest.dc,
        "predict_match",
        lambda *args, **kwargs: {"p_home": 0.5, "p_draw": 0.3, "p_away": 0.2},
    )
    first = backtest.run_backtest(
        frame,
        source_row_count=len(frame),
        source_sha256="a" * 64,
        source_fetched_at="2026-09-28T00:00:00Z",
        main_sha="b" * 40,
        generated_at="2026-09-28T00:01:00Z",
        bootstrap_replicates=10,
    )
    second = backtest.run_backtest(
        frame,
        source_row_count=len(frame),
        source_sha256="a" * 64,
        source_fetched_at="2026-09-28T00:00:00Z",
        main_sha="b" * 40,
        generated_at="2026-09-28T00:01:00Z",
        bootstrap_replicates=10,
    )
    assert first == second
    assert (
        first["models"]["causal_gbt"]["status"]
        == "unavailable_without_historical_point_in_time_feature_store"
    )
    assert first["models"]["canonical_stacker"]["status"].startswith(
        "CANONICAL_STACKER_CAUSAL_BACKTEST_UNAVAILABLE"
    )
    assert first["signal_policy_simulation"]["canonical_detect_value_invoked"] is False
    assert (
        "causal_recency_metrics" in first["tournament_breakdown"]["uefa_nations_league"]
    )
    assert set(first["cohorts"]["all_competitive"]["calendar_era_metrics"]) == set(
        backtest.CALENDAR_ERAS
    )
    assert "uefa_nations_league" in first["cohorts"]
    assert (
        "neutral_qualifier_and_confidence_strata"
        in first["calibration_and_signal_diagnostics"]
    )
    assert (
        "dc_vs_elo_disagreement_by_window"
        in first["calibration_and_signal_diagnostics"]
    )
    assert first["safety"]["provider_requests"] == 0
    assert first["safety"]["runtime_mutations"] == 0
