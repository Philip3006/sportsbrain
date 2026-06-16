"""
Goalscorer betting module — per-player goal probability from StatsBomb xG.

Uses Poisson CDF: P(player scores ≥ 1) = 1 - exp(-xg_per_game).

Opponent strength adjustment (when dc_params supplied):
  xg_adj = xg_raw / exp((defence_opp - mean_defence) × 0.5)
  → Goals vs elite defence (Spain, Portugal) are worth more
  → Goals vs weak minnows are discounted

Covers WC/Euro/Copa (StatsBomb open data). Graceful fallback for other tournaments.
"""
from __future__ import annotations

import math
import re
import unicodedata

import numpy as np
import pandas as pd

from src.features.player_rating import _opp_defence_factor, _mean_defence

# Higher than match MIN_EDGE (0.03) — bookmaker margin on player props is ~20%.
_MIN_EV_SCORER = 0.10


def _name_tokens(name: str) -> list[str]:
    """ASCII-normalize, lowercase, split on space/hyphen."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.split(r"[\s\-]+", s)


def _names_match(sb_name: str, bm_name: str) -> bool:
    """True if StatsBomb name and bookmaker name refer to the same player."""
    sb = set(_name_tokens(sb_name))
    bm = _name_tokens(bm_name)
    bm_set = set(bm)
    # 1. Last token of bookmaker name (≥4 chars) found in StatsBomb tokens
    if bm and len(bm[-1]) >= 4 and bm[-1] in sb:
        return True
    # 2. Any long shared token (≥5 chars)
    if {t for t in sb if len(t) >= 5} & {t for t in bm_set if len(t) >= 5}:
        return True
    return False


def player_goal_probability(xg_per_game: float) -> float:
    """P(player scores ≥ 1 goal) in a match given their expected goals per game."""
    return 1.0 - math.exp(-max(xg_per_game, 0.0))


def get_top_goalscorer_predictions(
    team: str,
    before_date: pd.Timestamp,
    player_xg_df: pd.DataFrame,
    n_games: int = 5,
    top_n: int = 5,
    decay: float = 0.85,
    dc_params=None,
) -> list[dict]:
    """
    Returns the top-N scorers by anytime goal probability for team's next match.

    With dc_params: each game's xG is adjusted for opponent defensive strength
    relative to the average DC opponent — prevents inflated ratings from goals
    scored against weak defences, boosts ratings for goals vs elite defences.

    Each entry:
        player        — player name
        p_score       — P(scores ≥ 1) from decay-weighted adj. xG
        xg_per_game   — decay-weighted opponent-adjusted xG per game
        xg_raw        — decay-weighted raw xG (for comparison)
        n_games       — number of games in rolling window
        data_source   — "statsbomb"

    Returns [] when no data available (NL/qualifier matches not covered).
    """
    if player_xg_df is None or player_xg_df.empty:
        return []

    mask = (player_xg_df["team"] == team) & (player_xg_df["date"] < before_date)
    team_df = player_xg_df[mask].copy()

    if team_df.empty:
        return []

    recent_dates = sorted(team_df["date"].unique(), reverse=True)[:n_games]
    team_df = team_df[team_df["date"].isin(recent_dates)].copy()

    if dc_params is not None:
        md = _mean_defence(dc_params)
        team_df["_factor"] = team_df.apply(
            lambda r: _opp_defence_factor(r, dc_params, md), axis=1
        )
        team_df["xg_adj"] = team_df["xg"] / team_df["_factor"]
    else:
        team_df["xg_adj"] = team_df["xg"]

    date_rank = {d: i for i, d in enumerate(sorted(recent_dates, reverse=True))}
    team_df["weight"] = team_df["date"].map(lambda d: decay ** date_rank.get(d, 0))

    results = []
    for player, grp in team_df.groupby("player"):
        total_weight = float(grp["weight"].sum())
        if total_weight == 0:
            continue
        xg_adj_w = float((grp["xg_adj"] * grp["weight"]).sum()) / total_weight
        xg_raw_w = float((grp["xg"] * grp["weight"]).sum()) / total_weight
        n = grp["date"].nunique()
        results.append({
            "player": player,
            "p_score": player_goal_probability(xg_adj_w),
            "xg_per_game": xg_adj_w,
            "xg_raw": xg_raw_w,
            "n_games": n,
            "data_source": "statsbomb",
        })

    results.sort(key=lambda x: x["p_score"], reverse=True)
    return results[:top_n]


def format_goalscorer_section(
    home: str,
    away: str,
    home_preds: list[dict],
    away_preds: list[dict],
) -> list[str]:
    """Renders goalscorer predictions as markdown lines for the scan report."""
    lines = []
    if not home_preds and not away_preds:
        return lines

    lines.append("  - 🎯 **Torschützen-Prognose** (StatsBomb xG, gegnerstärke-korrigiert):")
    for side, team, preds in (("home", home, home_preds), ("away", away, away_preds)):
        if not preds:
            continue
        top3 = preds[:3]
        entries = ", ".join(
            f"{p['player']} ({p['p_score']*100:.0f}%)"
            for p in top3
        )
        lines.append(f"    - {team}: {entries}")
    return lines


def detect_value_goalscorer(
    match_id: str,
    home: str,
    away: str,
    home_preds: list[dict],
    away_preds: list[dict],
    player_props: dict[str, float],
    bankroll: float,
    min_ev: float = _MIN_EV_SCORER,
) -> list["BetSignal"]:
    """
    Compares model goalscorer probabilities (Poisson) against bookmaker odds.
    Returns BetSignal list for players with EV > min_ev.
    Confidence always LOW — player props have structural uncertainty.
    Only players with n_games >= 2 in the rolling window are considered.
    """
    if not player_props or not (home_preds or away_preds):
        return []

    from src.config import MAX_EV
    from src.betting.kelly import kelly_fraction, dynamic_stake_eur
    from src.betting.value_detector import BetSignal

    signals = []
    for pred in (*home_preds, *away_preds):
        if pred.get("n_games", 0) < 2:
            continue
        sb_name = pred["player"]
        model_p = pred["p_score"]
        for bm_name, odds in player_props.items():
            if _names_match(sb_name, bm_name):
                ev = model_p * odds - 1.0
                if min_ev <= ev <= MAX_EV:
                    kf = kelly_fraction(model_p, odds)
                    stake_eur = dynamic_stake_eur(ev, "LOW", bankroll)
                    signals.append(BetSignal(
                        match_id=match_id,
                        home=home, away=away,
                        market=f"scorer_{sb_name}",
                        model_prob=model_p,
                        fair_prob=1.0 / odds,
                        decimal_odds=odds,
                        ev=ev,
                        kelly_f=kf,
                        stake_pct=stake_eur / bankroll if bankroll > 0 else 0.0,
                        confidence="LOW",
                        stake_eur=stake_eur,
                    ))
                break
    return signals
