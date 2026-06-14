"""Phase 2.2 — Bayesian Hierarchical DC.

cluster_strength + cluster_map adds a per-confederation soft prior that
shrinks each team toward its cluster mean. The effect should be:
  • Default behaviour (cluster_strength=0) is identical to plain fit().
  • With a prior + cluster_strength > 0, an outlier team gets pulled toward
    the cluster mean compared to the un-clustered fit.
"""
import numpy as np
import pandas as pd
import pytest

from src.models.dixon_coles import (
    DixonColesParams,
    _compute_cluster_centers,
    fit,
)


def _matches():
    """Synthetic dataset with two clusters (A,B,C in UEFA; D,E,F in CAF)."""
    teams = list("ABCDEF")
    rng = np.random.default_rng(42)
    n_games = 240
    rows = []
    for _ in range(n_games):
        h, a = rng.choice(teams, 2, replace=False)
        rows.append({
            "home_team": h, "away_team": a,
            "home_score": rng.integers(0, 4),
            "away_score": rng.integers(0, 4),
            "date": pd.Timestamp("2025-01-01"),
            "tournament": "Friendly",
            "neutral": False,
        })
    return pd.DataFrame(rows)


def _prior():
    return DixonColesParams(
        attack={"A": 1.0, "B": 0.5, "C": 0.6, "D": -0.5, "E": -0.4, "F": -0.6},
        defence={"A": -0.5, "B": -0.4, "C": -0.6, "D": 0.5, "E": 0.6, "F": 0.4},
        home_adv=0.25, rho=-0.13,
        fit_date=pd.Timestamp("2024-12-31"),
    )


class TestComputeClusterCenters:
    def test_no_cluster_map_returns_zeros(self):
        atk, def_ = _compute_cluster_centers(["A", "B"], _prior(), None)
        assert (atk == 0).all() and (def_ == 0).all()

    def test_no_prior_returns_zeros(self):
        cmap = {"A": "UEFA", "B": "UEFA"}
        atk, def_ = _compute_cluster_centers(["A", "B"], None, cmap)
        assert (atk == 0).all() and (def_ == 0).all()

    def test_cluster_means_aggregate_correctly(self):
        cmap = {"A": "UEFA", "B": "UEFA", "C": "UEFA",
                "D": "CAF",  "E": "CAF",  "F": "CAF"}
        atk, _ = _compute_cluster_centers(list("ABCDEF"), _prior(), cmap)
        uefa_mean = (1.0 + 0.5 + 0.6) / 3
        caf_mean = (-0.5 + -0.4 + -0.6) / 3
        # First three rows (UEFA teams) all map to uefa_mean
        for i in range(3):
            assert abs(atk[i] - uefa_mean) < 1e-9
        # Last three rows (CAF teams) map to caf_mean
        for i in range(3, 6):
            assert abs(atk[i] - caf_mean) < 1e-9

    def test_unmapped_team_uses_global_mean(self):
        cmap = {"A": "UEFA", "B": "UEFA"}  # C/D/E/F omitted
        atk, _ = _compute_cluster_centers(["A", "B", "C"], _prior(), cmap)
        global_mean = float(np.mean(list(_prior().attack.values())))
        assert abs(atk[2] - global_mean) < 1e-9


class TestHierarchicalFitInfluence:
    def test_cluster_strength_zero_matches_default(self):
        df = _matches()
        p1 = fit(df, max_iter=300, prior_params=_prior(),
                  regularization=0.05, cluster_strength=0.0)
        p2 = fit(df, max_iter=300, prior_params=_prior(), regularization=0.05)
        # When cluster_strength is 0, results equal the default behaviour.
        for t in p1.attack:
            assert abs(p1.attack[t] - p2.attack[t]) < 1e-3
            assert abs(p1.defence[t] - p2.defence[t]) < 1e-3

    def test_strong_cluster_pull_converges_to_cluster_mean(self):
        """With a very large cluster_strength the optimizer should be dominated
        by the cluster prior — non-reference teams should land close to their
        confederation cluster mean. (Team "A" is the reference team whose
        attack is pinned to 0, so it's excluded from this check.)"""
        df = _matches()
        cmap = {"A": "UEFA", "B": "UEFA", "C": "UEFA",
                "D": "CAF",  "E": "CAF",  "F": "CAF"}
        prior = _prior()
        uefa_mean = (prior.attack["A"] + prior.attack["B"] + prior.attack["C"]) / 3
        caf_mean  = (prior.attack["D"] + prior.attack["E"] + prior.attack["F"]) / 3
        clustered = fit(df, max_iter=500, prior_params=prior,
                          regularization=0.0,  # only cluster prior contributes
                          cluster_map=cmap, cluster_strength=5000.0)
        # Non-reference UEFA teams pulled toward UEFA mean
        for t in ("B", "C"):
            assert abs(clustered.attack[t] - uefa_mean) < 0.2, t
        # All CAF teams pulled toward CAF mean
        for t in ("D", "E", "F"):
            assert abs(clustered.attack[t] - caf_mean) < 0.2, t


def test_hierarchical_keeps_save_loadable(tmp_path):
    """Sanity: a clustered fit still produces a valid DixonColesParams that
    save() accepts (i.e. all params stay within validate_params bounds)."""
    from src.models.dixon_coles import save
    df = _matches()
    cmap = {"A": "UEFA", "B": "UEFA", "C": "UEFA",
            "D": "CAF",  "E": "CAF",  "F": "CAF"}
    p = fit(df, max_iter=300, prior_params=_prior(),
             regularization=0.05, cluster_map=cmap, cluster_strength=0.05)
    out = tmp_path / "p.pkl"
    save(p, out)
    assert out.exists()
