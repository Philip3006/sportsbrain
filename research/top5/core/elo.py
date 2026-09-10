"""Top-5 — Standalone Elo module.

Matches the production Elo used by BL1 v7 (frozen reference
569741b4ad571a38492e4d7cfd014cf82daec396) when called as:

  compute_elo_series(df, initial_ratings={}, k_competitive=20, k_friendly=20)

Key design decisions carried from production src.models.elo:
  - Goal-difference multiplier applied to K (1-goal: ×1.0, 2-goal: ×1.5,
    3+-goal: (11+GD)/8). BL1 domestic data has no "tournament" column,
    so k=k_competitive for every match.
  - elo_win_probability draw band: 0.27 × exp(-|delta|/200), clamped [0.05, 0.35].
  - Home advantage: 100 rating points added to home Elo for expected-score calc.
  - Outputs elo_home_pre, elo_away_pre, elo_home_post, elo_away_post.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

ELO_DEFAULT = 1500.0
K = 20.0          # default K used by BL1 v7 walk-forward (k_competitive=k_friendly=20)
HA = 100.0        # home-advantage rating points
DRAW_BAND = 200.0  # calibrated empirically — matches production src.models.elo


def _expected_score(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def _gd_multiplier(goal_diff: int) -> float:
    """Goal-difference K-multiplier — identical to production src.models.elo."""
    gd = abs(goal_diff)
    if gd <= 1:
        return 1.0
    if gd == 2:
        return 1.5
    return (11.0 + gd) / 8.0


def elo_win_probability(elo_home: float, elo_away: float,
                        neutral: bool = False) -> tuple[float, float, float]:
    """Return (p_home, p_draw, p_away).

    Formula identical to production src.models.elo.elo_win_probability.
    """
    effective_home = elo_home + (0.0 if neutral else HA)
    delta = effective_home - elo_away
    p_home_or_draw = _expected_score(effective_home, elo_away)
    p_draw = max(0.05, 0.27 * math.exp(-abs(delta) / DRAW_BAND))
    p_draw = min(p_draw, 0.35)
    p_home = p_home_or_draw * (1.0 - p_draw)
    p_away = (1.0 - p_home_or_draw) * (1.0 - p_draw)
    total = p_home + p_draw + p_away
    return p_home / total, p_draw / total, p_away / total


def _update(ratings: dict[str, float], home: str, away: str,
            home_goals: int, away_goals: int,
            k: float = K, neutral: bool = False) -> tuple[float, float]:
    """Update ratings in-place and return (new_home, new_away)."""
    r_h = ratings.get(home, ELO_DEFAULT)
    r_a = ratings.get(away, ELO_DEFAULT)
    effective_home = r_h + (0.0 if neutral else HA)
    e_home = _expected_score(effective_home, r_a)
    s_home = 1.0 if home_goals > away_goals else (0.5 if home_goals == away_goals else 0.0)
    mult = _gd_multiplier(home_goals - away_goals)
    new_h = r_h + k * mult * (s_home - e_home)
    new_a = r_a + k * mult * ((1 - s_home) - (1 - e_home))
    ratings[home] = new_h
    ratings[away] = new_a
    return new_h, new_a


def compute_elo_series(df: pd.DataFrame,
                       k_competitive: float = K,
                       k_friendly: float = K,
                       initial_ratings: dict[str, float] | None = None,
                       default: float = ELO_DEFAULT) -> pd.DataFrame:
    """Cumulative walk-forward Elo series matching BL1 v7 compute_elo_series.

    Input DataFrame must have: date, home_team, away_team, home_score, away_score.
    Optional 'tournament' column: if present and value == 'Friendly', k_friendly
    is used; otherwise k_competitive.

    Output: input DataFrame columns plus elo_home_pre, elo_away_pre,
    elo_home_post, elo_away_post.
    """
    df = df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    ratings: dict[str, float] = dict(initial_ratings or {})
    pre_home, pre_away, post_home, post_away = [], [], [], []

    for _, r in df.iterrows():
        h, a = r["home_team"], r["away_team"]
        r_h = ratings.get(h, default)
        r_a = ratings.get(a, default)
        pre_home.append(r_h)
        pre_away.append(r_a)

        tournament = str(r.get("tournament", "")) if "tournament" in r.index else ""
        k = k_friendly if (not tournament or tournament == "Friendly") else k_competitive

        new_h, new_a = _update(ratings, h, a,
                                int(r["home_score"]), int(r["away_score"]),
                                k=k, neutral=bool(r.get("neutral", False)))
        post_home.append(new_h)
        post_away.append(new_a)

    result = df.copy()
    result["elo_home_pre"] = pre_home
    result["elo_away_pre"] = pre_away
    result["elo_home_post"] = post_home
    result["elo_away_post"] = post_away
    return result
