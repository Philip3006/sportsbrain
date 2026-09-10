"""Top-5 — Standalone Elo orchestration.

Simple Elo suitable for a generic Top-5 framework: draw allowed, K default 20,
HA 100. Not identical to the SportsBrain-repo Elo (which supports margin
scaling) — for research parity we accept a minor implementation difference,
verified against BL1 v7 M5/M6 observables.

Used by M6 (blend with market) and M7 (feature).
"""
from __future__ import annotations

import pandas as pd
import numpy as np

ELO_DEFAULT = 1500.0
K = 20.0
HA = 100.0


def _expected(elo_home_adj: float, elo_away: float) -> float:
    """Expected home-win probability (2-way, then split for draw)."""
    return 1.0 / (1.0 + 10 ** ((elo_away - elo_home_adj) / 400.0))


def elo_win_probability(elo_home: float, elo_away: float, neutral: bool = False):
    """Return (p_home, p_draw, p_away). Draw handled as a symmetric split
    proportional to closeness — canonical convention for BL1 v7 parity.
    """
    ha = 0.0 if neutral else HA
    p_h_raw = _expected(elo_home + ha, elo_away)
    # Simple draw allocation: draw prob peaks when teams are balanced.
    diff = abs((elo_home + ha) - elo_away)
    p_draw = 0.28 - 0.15 * min(diff / 400.0, 1.0)
    p_draw = max(0.15, p_draw)
    remaining = 1.0 - p_draw
    p_home = remaining * p_h_raw
    p_away = remaining * (1 - p_h_raw)
    return p_home, p_draw, p_away


def compute_elo_series(df: pd.DataFrame,
                       k: float = K, ha: float = HA,
                       default: float = ELO_DEFAULT) -> pd.DataFrame:
    """Cumulative walk-forward Elo series.

    Input: match rows sorted by date, with home_team, away_team, home_score,
    away_score.

    Output: DataFrame with elo_home_pre, elo_away_pre for each match — the
    Elo values BEFORE the match was played (i.e. usable as a feature at
    prediction time).
    """
    df = df.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    ratings: dict[str, float] = {}
    rows = []
    for _, r in df.iterrows():
        h, a = r["home_team"], r["away_team"]
        eh = ratings.get(h, default)
        ea = ratings.get(a, default)
        rows.append({"date": r["date"], "home_team": h, "away_team": a,
                     "elo_home_pre": eh, "elo_away_pre": ea})
        hs, as_ = int(r["home_score"]), int(r["away_score"])
        outcome = 1.0 if hs > as_ else (0.5 if hs == as_ else 0.0)
        expected_home = _expected(eh + ha, ea)
        delta = k * (outcome - expected_home)
        ratings[h] = eh + delta
        ratings[a] = ea - delta
    return pd.DataFrame(rows)
