"""Tests for src/analysis/monte_carlo.py"""
import numpy as np
import pytest
from src.analysis.monte_carlo import scoreline_distribution


def _uniform_matrix(size=6):
    m = np.ones((size, size))
    m /= m.sum()
    return m


# ── Analytical ──────────────────────────────────────────────────────────────

def test_top_scores_count():
    result = scoreline_distribution(_uniform_matrix(), n_mc=100, seed=0)
    assert len(result["top_scores"]) == 3


def test_top_scores_sorted_descending():
    m = np.zeros((6, 6))
    m[1, 0] = 0.4
    m[0, 0] = 0.3
    m[2, 1] = 0.2
    m[3, 3] = 0.1
    result = scoreline_distribution(m, n_mc=100, seed=0)
    probs = [s["p"] for s in result["top_scores"]]
    assert probs == sorted(probs, reverse=True)


def test_top_scores_keys():
    result = scoreline_distribution(_uniform_matrix(), n_mc=100, seed=0)
    for s in result["top_scores"]:
        assert "h" in s and "a" in s and "p" in s
        assert isinstance(s["h"], int)
        assert isinstance(s["a"], int)
        assert 0.0 <= s["p"] <= 100.0


def test_goal_dist_keys():
    result = scoreline_distribution(_uniform_matrix(), n_mc=100, seed=0)
    assert set(result["goal_dist"].keys()) == {"0", "1", "2", "3+"}


def test_goal_dist_sums_to_100():
    result = scoreline_distribution(_uniform_matrix(), n_mc=100, seed=0)
    total = sum(result["goal_dist"].values())
    assert abs(total - 100.0) < 0.5, f"Sum {total} not ~100"


def test_goal_dist_sparse_matrix():
    m = np.zeros((6, 6))
    m[0, 0] = 0.5
    m[1, 0] = 0.3
    m[0, 1] = 0.2
    result = scoreline_distribution(m, n_mc=100, seed=0)
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
    result = scoreline_distribution(m, n_mc=100, seed=0)
    top = result["top_scores"][0]
    assert top["h"] == 2 and top["a"] == 1
    assert abs(top["p"] - 60.0) < 0.1


# ── Monte Carlo ──────────────────────────────────────────────────────────────

def test_mc_keys_present():
    result = scoreline_distribution(_uniform_matrix(), n_mc=500, seed=42)
    assert "mc_top_scores" in result
    assert "mc_goal_dist" in result
    assert result["n_mc"] == 500


def test_mc_top_scores_count():
    result = scoreline_distribution(_uniform_matrix(), n_mc=500, seed=42)
    assert len(result["mc_top_scores"]) == 3


def test_mc_goal_dist_keys():
    result = scoreline_distribution(_uniform_matrix(), n_mc=500, seed=42)
    assert set(result["mc_goal_dist"].keys()) == {"0", "1", "2", "3+"}


def test_mc_goal_dist_sums_to_100():
    result = scoreline_distribution(_uniform_matrix(), n_mc=1000, seed=42)
    total = sum(result["mc_goal_dist"].values())
    assert abs(total - 100.0) < 1.0, f"MC sum {total} not ~100"


def test_mc_converges_to_analytical():
    """With N=50k, MC top score should be within 2pp of analytical."""
    m = np.zeros((6, 6))
    m[1, 0] = 0.5
    m[0, 1] = 0.3
    m[2, 0] = 0.2
    result = scoreline_distribution(m, n_mc=50_000, seed=7)
    assert abs(result["mc_top_scores"][0]["p"] - result["top_scores"][0]["p"]) < 2.0


def test_mc_seed_reproducible():
    m = _uniform_matrix()
    r1 = scoreline_distribution(m, n_mc=200, seed=99)
    r2 = scoreline_distribution(m, n_mc=200, seed=99)
    assert r1["mc_top_scores"] == r2["mc_top_scores"]
    assert r1["mc_goal_dist"] == r2["mc_goal_dist"]
