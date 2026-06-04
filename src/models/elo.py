import math

import numpy as np
import pandas as pd

from src.config import ELO_K_BASE, TOURNAMENT_K_FACTORS

ELO_K_COMPETITIVE = 40
ELO_K_FRIENDLY = 20
ELO_DEFAULT = 1500.0
HOME_ADVANTAGE = 100.0
DRAW_BAND = 200.0  # calibrated empirically; used in Bradley-Terry draw approximation


def expected_score(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def _gd_multiplier(goal_diff: int) -> float:
    gd = abs(goal_diff)
    if gd <= 1:
        return 1.0
    if gd == 2:
        return 1.5
    return (11.0 + gd) / 8.0


def update_ratings(
    ratings: dict[str, float],
    home: str,
    away: str,
    home_goals: int,
    away_goals: int,
    k: float = ELO_K_COMPETITIVE,
    neutral: bool = False,
) -> dict[str, float]:
    ratings = dict(ratings)
    r_home = ratings.get(home, ELO_DEFAULT)
    r_away = ratings.get(away, ELO_DEFAULT)

    effective_home = r_home + (0.0 if neutral else HOME_ADVANTAGE)
    e_home = expected_score(effective_home, r_away)
    e_away = 1.0 - e_home

    if home_goals > away_goals:
        s_home, s_away = 1.0, 0.0
    elif home_goals < away_goals:
        s_home, s_away = 0.0, 1.0
    else:
        s_home, s_away = 0.5, 0.5

    mult = _gd_multiplier(home_goals - away_goals)
    ratings[home] = r_home + k * mult * (s_home - e_home)
    ratings[away] = r_away + k * mult * (s_away - e_away)
    return ratings


def compute_elo_series(
    matches: pd.DataFrame,
    initial_ratings: dict[str, float] | None = None,
    k_competitive: float = ELO_K_COMPETITIVE,
    k_friendly: float = ELO_K_FRIENDLY,
) -> pd.DataFrame:
    """
    Iterates matches chronologically, tracking Elo before and after each match.
    Returns matches DataFrame with added columns:
        elo_home_pre, elo_away_pre, elo_home_post, elo_away_post
    """
    ratings: dict[str, float] = dict(initial_ratings or {})
    pre_home, pre_away, post_home, post_away = [], [], [], []

    for _, row in matches.iterrows():
        home, away = row["home_team"], row["away_team"]
        r_h = ratings.get(home, ELO_DEFAULT)
        r_a = ratings.get(away, ELO_DEFAULT)
        pre_home.append(r_h)
        pre_away.append(r_a)

        tournament = str(row.get("tournament", ""))
        if not tournament or tournament == "Friendly":
            k = k_friendly
        elif tournament in TOURNAMENT_K_FACTORS:
            k = ELO_K_BASE * TOURNAMENT_K_FACTORS[tournament]
        else:
            k = k_competitive
        ratings = update_ratings(
            ratings, home, away,
            int(row["home_score"]), int(row["away_score"]),
            k=k, neutral=bool(row.get("neutral", False)),
        )
        post_home.append(ratings[home])
        post_away.append(ratings[away])

    result = matches.copy()
    result["elo_home_pre"] = pre_home
    result["elo_away_pre"] = pre_away
    result["elo_home_post"] = post_home
    result["elo_away_post"] = post_away
    return result


def elo_win_probability(
    elo_home: float,
    elo_away: float,
    neutral: bool = False,
) -> tuple[float, float, float]:
    """
    Returns (p_home, p_draw, p_away).
    Draw probability estimated using a normal approximation over the Elo gap.
    """
    effective_home = elo_home + (0.0 if neutral else HOME_ADVANTAGE)
    delta = effective_home - elo_away

    # P(home wins outright) from standard Elo
    p_home_or_draw = expected_score(effective_home, elo_away)

    # Draw band: narrower gap -> more draws. Calibrated to ~0.27 at equal strength.
    p_draw = max(0.05, 0.27 * math.exp(-abs(delta) / DRAW_BAND))
    p_draw = min(p_draw, 0.35)

    p_home = p_home_or_draw * (1.0 - p_draw)
    p_away = (1.0 - p_home_or_draw) * (1.0 - p_draw)

    total = p_home + p_draw + p_away
    return p_home / total, p_draw / total, p_away / total


def current_ratings(elo_series: pd.DataFrame) -> dict[str, float]:
    """Extracts the most recent post-match rating for every team."""
    ratings: dict[str, float] = {}
    for _, row in elo_series.iterrows():
        ratings[row["home_team"]] = row["elo_home_post"]
        ratings[row["away_team"]] = row["elo_away_post"]
    return ratings
