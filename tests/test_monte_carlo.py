"""Tests for src/analysis/monte_carlo.py"""
import numpy as np
import pytest
from src.analysis.monte_carlo import scoreline_distribution


def _uniform_matrix(size=6):
    m = np.ones((size, size))
    m /= m.sum()
    return m


def test_top_scores_count():
    m = _uniform_matrix()
    result = scoreline_distribution(m)
    assert len(result["top_scores"]) == 3


def test_top_scores_sorted_descending():
    m = np.zeros((6, 6))
    m[1, 0] = 0.4
    m[0, 0] = 0.3
    m[2, 1] = 0.2
    m[3, 3] = 0.1
    result = scoreline_distribution(m)
    probs = [s["p"] for s in result["top_scores"]]
    assert probs == sorted(probs, reverse=True)


def test_top_scores_keys():
    m = _uniform_matrix()
    result = scoreline_distribution(m)
    for s in result["top_scores"]:
        assert "h" in s and "a" in s and "p" in s
        assert isinstance(s["h"], int)
        assert isinstance(s["a"], int)
        assert 0.0 <= s["p"] <= 100.0


def test_goal_dist_keys():
    m = _uniform_matrix()
    result = scoreline_distribution(m)
    gd = result["goal_dist"]
    assert set(gd.keys()) == {"0", "1", "2", "3+"}


def test_goal_dist_sums_to_100():
    m = _uniform_matrix()
    result = scoreline_distribution(m)
    gd = result["goal_dist"]
    total = sum(gd.values())
    assert abs(total - 100.0) < 0.5, f"Sum {total} not ~100"


def test_goal_dist_sparse_matrix():
    m = np.zeros((6, 6))
    m[0, 0] = 0.5   # 0 total goals
    m[1, 0] = 0.3   # 1 total goal
    m[0, 1] = 0.2   # 1 total goal
    result = scoreline_distribution(m)
    gd = result["goal_dist"]
    assert abs(gd["0"] - 50.0) < 0.1
    assert abs(gd["1"] - 50.0) < 0.1
    assert gd["2"] == 0.0
    assert gd["3+"] == 0.0


def test_top_score_values_match_known_matrix():
    m = np.zeros((6, 6))
    m[2, 1] = 0.6
    m[1, 0] = 0.3
    m[0, 0] = 0.1
    result = scoreline_distribution(m)
    top = result["top_scores"][0]
    assert top["h"] == 2 and top["a"] == 1
    assert abs(top["p"] - 60.0) < 0.1
