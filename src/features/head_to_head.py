import pandas as pd
import numpy as np


def h2h_stats(
    home: str,
    away: str,
    before_date: pd.Timestamp,
    matches: pd.DataFrame,
    n_meetings: int = 10,
) -> dict[str, float]:
    """
    Head-to-head stats between home and away over last n_meetings strictly before before_date.
    Considers both directions (home played either side).
    Returns zeros if no meetings found.
    """
    mask = (
        (
            ((matches["home_team"] == home) & (matches["away_team"] == away))
            | ((matches["home_team"] == away) & (matches["away_team"] == home))
        )
        & (matches["date"] < before_date)
    )
    meetings = matches[mask].sort_values("date", ascending=False).head(n_meetings)

    if meetings.empty:
        return {
            "h2h_home_wins": 0.0,
            "h2h_draws": 0.0,
            "h2h_away_wins": 0.0,
            "h2h_home_gf_avg": 0.0,
            "h2h_away_gf_avg": 0.0,
            "h2h_n": 0.0,
        }

    hw, dr, aw = 0, 0, 0
    home_goals, away_goals = [], []

    for _, row in meetings.iterrows():
        if row["home_team"] == home:
            hg, ag = int(row["home_score"]), int(row["away_score"])
        else:
            hg, ag = int(row["away_score"]), int(row["home_score"])

        home_goals.append(hg)
        away_goals.append(ag)

        if hg > ag:
            hw += 1
        elif hg == ag:
            dr += 1
        else:
            aw += 1

    n = len(meetings)
    return {
        "h2h_home_wins": hw / n,
        "h2h_draws": dr / n,
        "h2h_away_wins": aw / n,
        "h2h_home_gf_avg": float(np.mean(home_goals)),
        "h2h_away_gf_avg": float(np.mean(away_goals)),
        "h2h_n": float(n),
    }
