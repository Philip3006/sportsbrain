"""
Surface-adjusted Elo ratings for ATP tennis.

Separate Elo pools per surface (grass / clay / hard / carpet).
Wimbledon → grass pool is the primary input for scanner predictions.

K-factors:
  Grand Slam:        40
  Masters 1000:      32
  ATP 500:           24
  ATP 250 / other:   16

Usage:
  matches = fetch_atp_matches()
  ratings = compute_tennis_elo(matches)
  probs   = predict_winner("Carlos Alcaraz", "Novak Djokovic", ratings, "grass")
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

_DEFAULT_RATING = 1500.0
_DECAY = 0.90  # 10% annual pull toward 1500 — applied at start of each new calendar year

_K_BY_LEVEL: dict[str, float] = {
    "g": 40.0,   # Grand Slams (tourney_level = 'G')
    "m": 32.0,   # Masters (= 'M')
    "a": 24.0,   # ATP 500 (= 'A')
    "f": 20.0,   # ATP Finals (= 'F')
    "d": 16.0,   # Davis Cup (= 'D')
    "c": 16.0,   # Challenger
    "s": 12.0,   # Satellite / ITF
}
_K_DEFAULT = 16.0


def _k(level: str) -> float:
    return _K_BY_LEVEL.get(level.lower(), _K_DEFAULT)


def _expected(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


@dataclass
class TennisEloRatings:
    """
    Holds per-surface + overall Elo ratings for all players seen so far.
    `overall` is updated on every match; `by_surface` only on matching surface.
    """
    overall: dict[str, float] = field(default_factory=dict)
    by_surface: dict[str, dict[str, float]] = field(default_factory=dict)

    def get_overall(self, player: str) -> float:
        return self.overall.get(player, _DEFAULT_RATING)

    def get_surface(self, player: str, surface: str) -> float:
        return self.by_surface.get(surface, {}).get(player, _DEFAULT_RATING)

    def get_blended(self, player: str, surface: str, w_surface: float = 0.70) -> float:
        """Blend surface Elo (70%) with overall (30%) to handle sparse data."""
        s = self.get_surface(player, surface)
        o = self.get_overall(player)
        return w_surface * s + (1.0 - w_surface) * o

    def _update(self, pool: dict, winner: str, loser: str, k: float) -> None:
        r_w = pool.get(winner, _DEFAULT_RATING)
        r_l = pool.get(loser, _DEFAULT_RATING)
        e_w = _expected(r_w, r_l)
        pool[winner] = r_w + k * (1.0 - e_w)
        pool[loser]  = r_l + k * (0.0 - (1.0 - e_w))

    def update(self, winner: str, loser: str, surface: str, level: str) -> None:
        k = _k(level)
        self._update(self.overall, winner, loser, k)
        surf = surface.lower()
        if surf not in self.by_surface:
            self.by_surface[surf] = {}
        self._update(self.by_surface[surf], winner, loser, k)


def _apply_decay(pool: dict[str, float]) -> None:
    """Pull all ratings 10% toward the default — applied once per calendar year."""
    for player in pool:
        pool[player] = _DEFAULT_RATING + _DECAY * (pool[player] - _DEFAULT_RATING)


def compute_tennis_elo(matches: pd.DataFrame) -> TennisEloRatings:
    """
    Iterates all matches chronologically and returns final TennisEloRatings.
    Applies 10% annual decay at the start of each new calendar year to keep
    ratings responsive to recent form.
    Expects columns: tourney_date, winner_name, loser_name, surface, tourney_level.
    Rows with NaN winner/loser are skipped.
    """
    ratings = TennisEloRatings()
    df = matches.dropna(subset=["winner_name", "loser_name"]).sort_values("tourney_date")

    current_year: int | None = None

    for _, row in df.iterrows():
        match_year = row["tourney_date"].year if hasattr(row["tourney_date"], "year") else None
        if match_year and match_year != current_year:
            if current_year is not None:
                # Decay all pools at start of each new year
                _apply_decay(ratings.overall)
                for surf_pool in ratings.by_surface.values():
                    _apply_decay(surf_pool)
            current_year = match_year

        winner = str(row["winner_name"])
        loser  = str(row["loser_name"])
        surface = str(row.get("surface", "hard")).lower()
        level   = str(row.get("tourney_level", "")).strip()
        ratings.update(winner, loser, surface, level)

    return ratings


def predict_winner(
    player_a: str,
    player_b: str,
    ratings: TennisEloRatings,
    surface: str,
    w_surface: float = 0.70,
) -> dict[str, float]:
    """
    Returns {'p_a': float, 'p_b': float} for a 2-outcome match.
    Uses surface-blended Elo.
    """
    r_a = ratings.get_blended(player_a, surface, w_surface)
    r_b = ratings.get_blended(player_b, surface, w_surface)
    p_a = _expected(r_a, r_b)
    return {"p_a": p_a, "p_b": 1.0 - p_a}


def top_players(ratings: TennisEloRatings, surface: str = "overall", n: int = 20) -> list[tuple[str, float]]:
    """Returns top-n players by Elo on a given surface (or 'overall')."""
    if surface == "overall":
        pool = ratings.overall
    else:
        pool = ratings.by_surface.get(surface, {})
    return sorted(pool.items(), key=lambda x: x[1], reverse=True)[:n]
