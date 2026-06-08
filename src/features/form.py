import pandas as pd
import numpy as np

from src.config import COMPETITIVE_TOURNAMENTS


def _is_friendly(tournament: str) -> bool:
    if not tournament:
        return True
    t = str(tournament).strip()
    if "friendly" in t.lower():
        return True
    return not any(comp.lower() in t.lower() for comp in COMPETITIVE_TOURNAMENTS)


def rolling_form(
    team: str,
    before_date: pd.Timestamp,
    matches: pd.DataFrame,
    n_games: int = 5,
    decay: float = 0.85,
    competitive_only: bool = False,
) -> dict[str, float]:
    """
    Exponentially decayed form metrics for `team` in the n_games before before_date.
    competitive_only=True downweights friendlies to 20% (max 1 slot equivalent).
    """
    mask = (
        ((matches["home_team"] == team) | (matches["away_team"] == team))
        & (matches["date"] < before_date)
    )
    recent = matches[mask].sort_values("date", ascending=False)

    if competitive_only and "tournament" in recent.columns:
        is_friendly = recent["tournament"].apply(_is_friendly)
        competitive = recent[~is_friendly].head(n_games)
        friendly = recent[is_friendly].head(2)
        parts = [df for df in [competitive, friendly] if not df.empty]
        recent = pd.concat(parts).sort_values("date", ascending=False) if parts else recent.iloc[:0]

    recent = recent.head(n_games)

    if recent.empty:
        return {
            "form_pts": 0.0,
            "form_gf": 0.0,
            "form_ga": 0.0,
            "form_gd": 0.0,
            "form_n": 0.0,
        }

    weights = np.array([decay ** i for i in range(len(recent))])

    if competitive_only and "tournament" in recent.columns:
        friendly_mask = recent["tournament"].apply(_is_friendly).values
        weights[friendly_mask] *= 0.2

    pts, gf_list, ga_list = [], [], []
    for _, row in recent.iterrows():
        is_home = row["home_team"] == team
        gf = int(row["home_score"]) if is_home else int(row["away_score"])
        ga = int(row["away_score"]) if is_home else int(row["home_score"])
        pts.append(3.0 if gf > ga else (1.0 if gf == ga else 0.0))
        gf_list.append(gf)
        ga_list.append(ga)

    w = weights[:len(pts)]
    w_sum = w.sum()
    if w_sum == 0:
        w = np.ones(len(pts)) / len(pts)
    else:
        w = w / w_sum

    return {
        "form_pts": float(np.dot(w, pts)),
        "form_gf": float(np.dot(w, gf_list)),
        "form_ga": float(np.dot(w, ga_list)),
        "form_gd": float(np.dot(w, np.array(gf_list) - np.array(ga_list))),
        "form_n": float(len(recent)),
    }


def days_since_last_match(
    team: str,
    before_date: pd.Timestamp,
    matches: pd.DataFrame,
) -> float:
    """Returns days since team's last match, or 90.0 if no history."""
    mask = (
        ((matches["home_team"] == team) | (matches["away_team"] == team))
        & (matches["date"] < before_date)
    )
    recent = matches[mask]
    if recent.empty:
        return 90.0
    return float((before_date - recent["date"].max()).days)


def momentum_score(
    team: str,
    before_date: pd.Timestamp,
    matches: pd.DataFrame,
    n_games: int = 10,
    competitive_only: bool = True,
) -> dict[str, float]:
    """
    Win streak, unbeaten streak, and form trend (last-3 vs prior-3 points per game).
    Uses competitive matches by default; filters friendlies if competitive_only=True.
    """
    mask = (
        ((matches["home_team"] == team) | (matches["away_team"] == team))
        & (matches["date"] < before_date)
    )
    recent = matches[mask].sort_values("date", ascending=False)

    if competitive_only and "tournament" in recent.columns:
        is_friendly = recent["tournament"].apply(_is_friendly)
        recent = recent[~is_friendly]

    recent = recent.head(n_games)

    if recent.empty:
        return {
            "win_streak": 0.0,
            "unbeaten_streak": 0.0,
            "form_trend": 0.0,
            "pts_last3": 0.0,
            "pts_prev3": 0.0,
        }

    results = []
    for _, row in recent.iterrows():
        is_home = row["home_team"] == team
        gf = int(row["home_score"]) if is_home else int(row["away_score"])
        ga = int(row["away_score"]) if is_home else int(row["home_score"])
        if gf > ga:
            results.append("W")
        elif gf == ga:
            results.append("D")
        else:
            results.append("L")

    win_streak = 0
    for r in results:
        if r == "W":
            win_streak += 1
        else:
            break

    unbeaten_streak = 0
    for r in results:
        if r in ("W", "D"):
            unbeaten_streak += 1
        else:
            break

    pts = [3.0 if r == "W" else (1.0 if r == "D" else 0.0) for r in results]
    pts_last3 = float(np.mean(pts[:3])) if len(pts) >= 3 else float(np.mean(pts))
    pts_prev3 = float(np.mean(pts[3:6])) if len(pts) >= 6 else (
        float(np.mean(pts[3:])) if len(pts) > 3 else 0.0
    )
    form_trend = pts_last3 - pts_prev3

    return {
        "win_streak": float(win_streak),
        "unbeaten_streak": float(unbeaten_streak),
        "form_trend": float(form_trend),
        "pts_last3": pts_last3,
        "pts_prev3": pts_prev3,
    }


def match_load(
    team: str,
    before_date: pd.Timestamp,
    matches: pd.DataFrame,
    windows: tuple[int, ...] = (30, 60),
) -> dict[str, float]:
    """
    Number of matches played in last N days — fatigue proxy.
    Returns matches_{N}d for each window and avg_days_between.
    """
    mask = (
        ((matches["home_team"] == team) | (matches["away_team"] == team))
        & (matches["date"] < before_date)
    )
    history = matches[mask].sort_values("date", ascending=False)

    result: dict[str, float] = {}
    for w in windows:
        cutoff = before_date - pd.Timedelta(days=w)
        n = int((history["date"] >= cutoff).sum())
        result[f"matches_{w}d"] = float(n)

    if len(history) >= 2:
        dates = history["date"].head(6).values
        gaps = [
            (pd.Timestamp(dates[i]) - pd.Timestamp(dates[i + 1])).days
            for i in range(len(dates) - 1)
        ]
        result["avg_days_between"] = float(np.mean(gaps))
    else:
        result["avg_days_between"] = 90.0

    return result


def form_direction_label(form_trend: float) -> str:
    """Returns ↑/→/↓ for use in scan reports."""
    if form_trend > 0.3:
        return "↑"
    elif form_trend < -0.3:
        return "↓"
    return "→"
