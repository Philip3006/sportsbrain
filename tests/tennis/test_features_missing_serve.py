"""J8-B8: serve_stats_n_a=0 als eindeutiges Missing-Flag.

Regression-Guard: LGBM-Retrain soll das Missing-Flag beibehalten damit die
Fallback-Werte (dom=0.5, ace=0, df=0) NICHT als reguläre Signale gelernt werden.
"""
from __future__ import annotations

from src.tennis.features import RollingState, build_match_features


def test_missing_serve_stats_flagged_via_n_zero():
    feats = build_match_features(
        player_a="A", player_b="B", surface="hard", best_of=3,
        category="atp250", round_str="R32",
        rank_a=100, rank_b=100,
        elo_a=1500, elo_b=1500,
        elo_surface_a=1500, elo_surface_b=1500,
        state=RollingState(), date=None,
        serve_stats_a=None, serve_stats_b=None,
    )
    assert feats["serve_stats_n_a"] == 0.0
    assert feats["serve_stats_n_b"] == 0.0
    assert feats["serve_dom_a"] == 0.5
    assert feats["serve_dom_b"] == 0.5


def test_present_serve_stats_non_zero_n():
    from src.data.tennis_stats import ServeAggregate
    a = ServeAggregate(n_matches=15, dominance_rate=0.58, ace_rate=0.06,
                       df_rate=0.03, win_rate=0.7, ace_df_ratio=2.0)
    feats = build_match_features(
        player_a="A", player_b="B", surface="hard", best_of=3,
        category="atp250", round_str="R32",
        rank_a=100, rank_b=100,
        elo_a=1500, elo_b=1500,
        elo_surface_a=1500, elo_surface_b=1500,
        state=RollingState(), date=None,
        serve_stats_a=a, serve_stats_b=None,
    )
    assert feats["serve_stats_n_a"] == 15.0
    assert feats["serve_stats_n_b"] == 0.0  # Missing-Flag bleibt für B
    assert feats["serve_dom_a"] == 0.58
