import ast
import json
from pathlib import Path

import pandas as pd
import pytest

from src.analysis import nations_league_validation as validation


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


def test_selects_exact_uefa_nations_league_periods_and_excludes_other_events():
    source = pd.DataFrame([
        _row("2020-09-03", "Korea Republic", "England", 1, 0),
        _row("2020-09-04", "France", "Spain", 0, 0, "Friendly"),
        _row("2020-09-05", "A", "B", 1, 1, "UEFA Nations League qualification"),
        _row("2020-09-06", "A", "B", 1, 1, "CONCACAF Nations League"),
        _row("2021-10-10", "Spain", "France", 1, 2),
        _row("2021-10-11", "Spain", "France", 1, 2),
        _row("2022-06-01", "Italy", "Germany", 1, 1),
        _row("2024-03-21", "Gibraltar", "Lithuania", 0, 1),
        _row("2024-09-05", "Portugal", "Croatia", 2, 1),
        _row("2025-06-08", "Portugal", "Spain", 2, 2),
        _row("2025-06-09", "Portugal", "Spain", 2, 2),
    ])

    normalized = validation.normalize_results(source)
    selected = validation.select_historical_matches(normalized)

    assert set(selected["validation_period"]) == {
        "2020/21", "2022/23", "2022/23-delayed-relegation-playoffs", "2024/25"
    }
    assert "Friendly" not in set(selected["tournament"])
    assert "UEFA Nations League qualification" not in set(selected["tournament"])
    assert "CONCACAF Nations League" not in set(selected["tournament"])
    assert "South Korea" in set(selected["home_team"])
    assert bool(selected.loc[selected["home_team"].eq("South Korea"), "neutral"].iloc[0]) is False


def test_source_neutral_flag_is_preserved_and_invalid_flag_fails_closed():
    normalized = validation.normalize_results(pd.DataFrame([
        _row("2024-09-05", "Portugal", "Croatia", 2, 1, neutral=True),
        _row("2024-09-06", "Spain", "France", 1, 1, neutral=False),
    ]))
    selected = validation.select_historical_matches(normalized)
    assert selected.set_index("home_team")["neutral"].to_dict() == {
        "Portugal": True, "Spain": False
    }

    invalid = pd.DataFrame([_row("2024-09-05", "Portugal", "Croatia", 2, 1)])
    invalid["neutral"] = None
    try:
        validation.normalize_results(invalid)
    except ValueError as exc:
        assert "neutral" in str(exc)
    else:
        raise AssertionError("Missing source neutral flag must fail closed")


def test_training_prefix_excludes_future_and_same_day_results():
    frame = pd.DataFrame([
        _row("2019-11-15", "Germany", "Spain", 1, 0, "UEFA Euro qualification"),
        _row("2024-09-05", "Portugal", "Croatia", 2, 1),
        _row("2024-09-06", "France", "Italy", 1, 0),
    ])
    normalized = validation.normalize_results(frame)
    first = validation.strict_training_prefix(normalized, pd.Timestamp("2024-09-05"))

    mutated = normalized.copy()
    mutated.loc[mutated["date"].ge(pd.Timestamp("2024-09-05")), "home_score"] = 99
    second = validation.strict_training_prefix(mutated, pd.Timestamp("2024-09-05"))
    pd.testing.assert_frame_equal(first, second)
    assert first["date"].max() < pd.Timestamp("2024-09-05")
    assert "UEFA Nations League" not in set(first["tournament"])


def test_dc_fit_and_prediction_use_only_pre_block_competitive_rows(monkeypatch):
    rows = pd.DataFrame([
        _row("2024-09-01", "Portugal", "Croatia", 3, 0, "UEFA Euro qualification"),
        _row("2024-09-05", "Portugal", "Croatia", 1, 0),
        _row("2024-09-06", "Spain", "France", 0, 0, neutral=True),
        _row("2024-09-07", "France", "Italy", 4, 0, "UEFA Euro qualification"),
    ])
    normalized = validation.normalize_results(rows)
    target = validation.select_historical_matches(normalized)
    calls = []

    def fake_fit(training, today, max_iter, prior_params):
        calls.append((training.copy(), today, max_iter, prior_params))
        return object()

    monkeypatch.setattr(validation.dc, "fit", fake_fit)
    monkeypatch.setattr(
        validation.dc,
        "predict_match",
        lambda home, away, params, neutral: {
            "p_home": 0.5 if not neutral else 0.4,
            "p_draw": 0.3,
            "p_away": 0.2 if not neutral else 0.3,
        },
    )

    predictions = validation.predict_dc_event_walk_forward(normalized, target, max_iter=77)

    assert len(calls) == 1
    training, cutoff, max_iter, prior_params = calls[0]
    assert cutoff == pd.Timestamp("2024-09-05")
    assert training["date"].max() < cutoff
    assert len(training) == 1
    assert max_iter == 77
    assert prior_params is None
    assert len(predictions) == 2
    assert predictions[(pd.Timestamp("2024-09-05"), "Portugal", "Croatia")]["probabilities"] == [0.5, 0.3, 0.2]
    assert predictions[(pd.Timestamp("2024-09-06"), "Spain", "France")]["probabilities"] == [0.4, 0.3, 0.3]


def test_elo_predictions_ignore_evaluation_date_and_future_scores_and_use_actual_neutral():
    rows = pd.DataFrame([
        _row("2024-09-01", "Portugal", "Croatia", 4, 0, "UEFA Euro qualification"),
        _row("2024-09-05", "Portugal", "Croatia", 1, 0, neutral=False),
        _row("2024-09-05", "Spain", "France", 1, 0, neutral=True),
        _row("2024-09-06", "Portugal", "Spain", 0, 1, "UEFA Euro qualification"),
    ])
    normalized = validation.normalize_results(rows)
    target = validation.select_historical_matches(normalized)
    baseline = validation.predict_elo_asof(normalized, target)

    changed = normalized.copy()
    changed.loc[changed["date"].ge(pd.Timestamp("2024-09-05")), "home_score"] = 7
    changed.loc[changed["date"].ge(pd.Timestamp("2024-09-05")), "away_score"] = 0
    after = validation.predict_elo_asof(changed, target)

    key = (pd.Timestamp("2024-09-05"), "Portugal", "Croatia")
    assert baseline[key]["probabilities"] == after[key]["probabilities"]
    assert baseline[key]["training_cutoff_exclusive"] == "2024-09-05"
    assert baseline[key]["training_max_date"] == "2024-09-01"
    neutral_key = (pd.Timestamp("2024-09-05"), "Spain", "France")
    assert target.loc[target["home_team"].eq("Spain"), "neutral"].item() is True
    assert baseline[neutral_key]["probabilities"] != baseline[key]["probabilities"]


def test_metrics_use_canonical_brier_and_calibration_and_report_probability_shape():
    rows = [
        {"outcome_index_home_draw_away": 0, "probabilities": [0.7, 0.2, 0.1]},
        {"outcome_index_home_draw_away": 2, "probabilities": [0.2, 0.2, 0.6]},
    ]
    summary = validation.summarize_metrics(rows)
    assert summary["matches_evaluated"] == 2
    assert summary["coverage"] == 1.0
    assert 0 <= summary["metrics"]["expected_calibration_error_10_bins_mean_one_vs_rest"] <= 1
    assert set(summary["metrics"]["home_draw_away_calibration"]) == {"home", "draw", "away"}
    assert summary["metrics"]["mean_max_probability_sharpness"] == pytest.approx(0.65)


def test_odds_absence_is_not_replaced_with_synthetic_market_input(tmp_path):
    odds_path = tmp_path / "odds.json"
    odds_path.write_text(json.dumps([
        {"ts": "2026-08-01T12:00:00Z", "odds": {
            "Portugal vs Croatia": {"home": 2.0, "draw": None, "away": 4.0}
        }}
    ]))
    matches = pd.DataFrame([{
        "date": pd.Timestamp("2024-09-05"), "home_team": "Portugal", "away_team": "Croatia"
    }])
    audit = validation.audit_local_odds_history(odds_path, matches)
    assert audit["exact_fixture_name_and_pre_kickoff_timestamp_matches"] == 0
    assert audit["market_only_and_model_market_blend_evaluated"] is False
    assert audit["status"] == "unavailable_for_valid_market_comparison"


def test_retrospective_frozen_diagnostic_is_explicitly_non_causal():
    assert validation.RETROSPECTIVE_LABEL == "RETROSPECTIVE_TRANSFER_DIAGNOSTIC_ONLY"


def test_module_has_no_production_or_network_side_effect_dependencies():
    module = ast.parse(Path(validation.__file__).read_text(encoding="utf-8"))
    imported_modules = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    forbidden_roots = {"requests", "src.scanner", "src.runtime", "src.betting", "src.ledger"}
    assert not any(
        imported == forbidden or imported.startswith(f"{forbidden}.")
        for imported in imported_modules
        for forbidden in forbidden_roots
    )
    assert "fetch_international_results" not in {
        node.func.id for node in ast.walk(module) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
