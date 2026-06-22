"""Tests for src/analysis/empirical_prior.py"""
import numpy as np
import pandas as pd
import pytest
from pathlib import Path
from src.analysis.empirical_prior import build_wc_prior


def _make_df(rows):
    return pd.DataFrame(rows, columns=["date", "home_team", "away_team",
                                        "home_score", "away_score", "tournament", "neutral"])


def test_normalised_to_one(tmp_path):
    df = _make_df([
        ("2022-11-20", "A", "B", 2, 0, "FIFA World Cup", True),
        ("2018-06-15", "C", "D", 1, 1, "FIFA World Cup", True),
        ("2014-07-01", "E", "F", 0, 1, "FIFA World Cup", True),
    ])
    pkl = tmp_path / "results.pkl"
    df.to_pickle(pkl)
    prior = build_wc_prior(data_path=pkl, max_goals=10)
    assert abs(prior.sum() - 1.0) < 1e-9


def test_shape(tmp_path):
    df = _make_df([("2022-11-20", "A", "B", 1, 0, "FIFA World Cup", True)])
    pkl = tmp_path / "r.pkl"
    df.to_pickle(pkl)
    prior = build_wc_prior(data_path=pkl, max_goals=10)
    assert prior.shape == (11, 11)


def test_non_wc_matches_excluded(tmp_path):
    df = _make_df([
        ("2022-11-20", "A", "B", 5, 0, "FIFA World Cup", True),
        ("2022-06-01", "C", "D", 0, 0, "Friendly", False),
    ])
    pkl = tmp_path / "r.pkl"
    df.to_pickle(pkl)
    prior = build_wc_prior(data_path=pkl, max_goals=10)
    # 5-0 should be in prior, 0-0 from Friendly should not dominate
    assert prior[5, 0] > prior[0, 0]


def test_recency_weighting(tmp_path):
    df = _make_df([
        ("2022-11-20", "A", "B", 3, 0, "FIFA World Cup", True),  # recent → high weight
        ("1990-06-10", "C", "D", 3, 0, "FIFA World Cup", True),  # old → low weight
        ("1990-06-11", "E", "F", 0, 3, "FIFA World Cup", True),  # old → low weight
    ])
    pkl = tmp_path / "r.pkl"
    df.to_pickle(pkl)
    prior = build_wc_prior(data_path=pkl, max_goals=10, half_life_years=10, reference_year=2026)
    # 3-0 (recent 2022) should dominate over 0-3 (old 1990)
    assert prior[3, 0] > prior[0, 3]


def test_scores_above_max_goals_truncated(tmp_path):
    df = _make_df([
        ("2022-11-20", "A", "B", 15, 0, "FIFA World Cup", True),
    ])
    pkl = tmp_path / "r.pkl"
    df.to_pickle(pkl)
    prior = build_wc_prior(data_path=pkl, max_goals=10)
    # 15-0 should be clamped to 10-0
    assert prior[10, 0] > 0.0


def test_blend_shifts_toward_prior(tmp_path):
    from src.analysis.monte_carlo import scoreline_distribution
    # Prior: uniform matrix
    prior = np.ones((11, 11)) / (11 * 11)
    # DC matrix: all mass on 2-0
    dc = np.zeros((11, 11))
    dc[2, 0] = 1.0
    result = scoreline_distribution(dc, prior_matrix=prior, alpha=0.6, n_mc=100, seed=0)
    # Blended 2-0 should be less than 100% (prior pulled it down)
    assert result["top_scores"][0]["p"] < 100.0
    assert result["top_scores"][0]["p"] > 50.0  # DC still dominates at alpha=0.6
    assert result["prior_used"] is True


def test_no_prior_unchanged(tmp_path):
    from src.analysis.monte_carlo import scoreline_distribution
    dc = np.zeros((11, 11))
    dc[1, 0] = 0.6
    dc[0, 0] = 0.4
    result = scoreline_distribution(dc, prior_matrix=None, alpha=1.0, n_mc=100, seed=0)
    assert result["prior_used"] is False
    assert result["top_scores"][0]["h"] == 1
    assert result["top_scores"][0]["a"] == 0
