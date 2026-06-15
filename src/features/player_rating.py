from __future__ import annotations

import numpy as np
import pandas as pd


def _opp_defence_factor(row: pd.Series, dc_params, mean_defence: float = 0.0) -> float:
    """
    Returns the multiplicative factor to adjust a player's raw xG for opponent quality.

    xg_adj = xg_raw / factor

    Logic (DC log-space):
      - dc_params.defence stores log-space values; exp(defence) is the lambda multiplier
      - defence < mean → stronger-than-average defence → factor < 1 → xg_adj > xg_raw (boost)
      - defence > mean → weaker-than-average defence  → factor > 1 → xg_adj < xg_raw (discount)
      - deviation is halved (smoothing=0.5) so max single-game adjustment is ~±75%

    Clamped to [0.5, 1.75]: max 2× boost (elite defence) / max 57% discount (minnow).
    Falls back to 1.0 (no adjustment) when dc_params is None or team not found.
    """
    if dc_params is None:
        return 1.0
    team = row.get("team", "")
    home = row.get("home_team", "")
    away = row.get("away_team", "")
    opponent = away if team == home else home
    if not opponent:
        return 1.0
    defence_log = dc_params.defence.get(opponent)
    if defence_log is None:
        return 1.0
    # Normalize to "vs average opponent = 1.0", then smooth by 0.5
    deviation = (float(defence_log) - mean_defence) * 0.5
    factor = float(np.exp(deviation))
    return float(np.clip(factor, 0.5, 1.75))


def _mean_defence(dc_params) -> float:
    """Pre-compute mean log-defence across all teams for normalization."""
    if dc_params is None or not dc_params.defence:
        return 0.0
    return float(np.mean(list(dc_params.defence.values())))


def rolling_shot_quality(
    team: str,
    before_date: pd.Timestamp,
    player_xg_df: pd.DataFrame,
    n_games: int = 5,
    top_n: int = 3,
    dc_params=None,
) -> dict[str, float]:
    """
    Computes two team-level player quality metrics from StatsBomb event data.

    When dc_params is provided, each game's xG is adjusted for opponent defensive
    strength relative to the average opponent in the DC model:
        xg_adj = xg_raw / exp((defence_opp - mean_defence) × 0.5)

    shot_quality   — adj. xG per shot (clinical finishing vs opposition quality)
    key_player_xg  — fraction of total adj. xG from top-N players (star dependency)

    Returns zeros when no StatsBomb coverage exists (graceful fallback for NL/qualifier matches).
    """
    _zero = {"shot_quality": 0.0, "key_player_xg": 0.0}

    if player_xg_df is None or player_xg_df.empty:
        return _zero

    mask = (player_xg_df["team"] == team) & (player_xg_df["date"] < before_date)
    recent = player_xg_df[mask].copy()

    if recent.empty:
        return _zero

    recent_dates = sorted(recent["date"].unique(), reverse=True)[:n_games]
    recent = recent[recent["date"].isin(recent_dates)].copy()

    if dc_params is not None:
        md = _mean_defence(dc_params)
        recent["_factor"] = recent.apply(
            lambda r: _opp_defence_factor(r, dc_params, md), axis=1
        )
        recent["xg_adj"] = recent["xg"] / recent["_factor"]
    else:
        recent["xg_adj"] = recent["xg"]

    total_xg = float(recent["xg_adj"].sum())
    total_shots = int(recent["shots"].sum())
    shot_quality = total_xg / max(total_shots, 1)

    top_xg = float(
        recent.groupby("player")["xg_adj"]
        .sum()
        .sort_values(ascending=False)
        .head(top_n)
        .sum()
    )
    key_player_xg = top_xg / max(total_xg, 1e-6)

    return {
        "shot_quality": shot_quality,
        "key_player_xg": key_player_xg,
    }
