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

import math
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

_DEFAULT_RATING = 1500.0
_DECAY = 0.90  # 10% annual pull toward 1500 — applied at start of each new calendar year

_RECENCY_HALFLIFE = 3.0  # years; older matches lose half their K every 3 years

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
    `surface_counts` tracks how many matches each player has played per surface
    and drives the dynamic blend weight in get_blended().
    """
    overall: dict[str, float] = field(default_factory=dict)
    by_surface: dict[str, dict[str, float]] = field(default_factory=dict)
    surface_counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def get_overall(self, player: str) -> float:
        return self.overall.get(player, _DEFAULT_RATING)

    def get_surface(self, player: str, surface: str) -> float:
        return self.by_surface.get(surface, {}).get(player, _DEFAULT_RATING)

    def get_surface_count(self, player: str, surface: str) -> int:
        """Number of matches played on this surface (winner + loser)."""
        return self.surface_counts.get(surface, {}).get(player, 0)

    def _dynamic_w_surface(self, player: str, surface: str) -> float:
        """Surface weight that grows with match count: new player→0.15, experienced→cap.
        Ramps linearly from 0.15 to the surface cap over the first 20 matches."""
        n = self.get_surface_count(player, surface)
        cap = _SURFACE_WEIGHTS.get(surface.lower(), 0.70)
        return min(cap, 0.15 + (cap - 0.15) * min(n, 20) / 20)

    def get_blended(self, player: str, surface: str, w_surface: float | None = None) -> float:
        """Blend surface Elo with overall. Uses per-player dynamic weight if w_surface is None."""
        if w_surface is None:
            w_surface = self._dynamic_w_surface(player, surface)
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
        if surf not in self.surface_counts:
            self.surface_counts[surf] = {}
        self._update(self.by_surface[surf], winner, loser, k)
        self.surface_counts[surf][winner] = self.surface_counts[surf].get(winner, 0) + 1
        self.surface_counts[surf][loser] = self.surface_counts[surf].get(loser, 0) + 1


def _apply_decay(pool: dict[str, float]) -> None:
    """Pull all ratings 10% toward the default — applied once per calendar year."""
    for player in pool:
        pool[player] = _DEFAULT_RATING + _DECAY * (pool[player] - _DEFAULT_RATING)


def compute_tennis_elo(
    matches: pd.DataFrame,
    reference_date: datetime | None = None,
) -> TennisEloRatings:
    """
    Iterates all matches chronologically and returns final TennisEloRatings.
    Applies 10% annual decay at the start of each new calendar year.

    reference_date: if given, scales each match's K-factor by recency:
      K *= exp(-ln(2)/HALFLIFE * years_ago), half-life = 3 years.
      Matches from last week get full K; matches from 3 years ago get ~50% K.

    Expects columns: tourney_date, winner_name, loser_name, surface, tourney_level.
    Rows with NaN winner/loser are skipped.
    """
    ratings = TennisEloRatings()
    df = matches.dropna(subset=["winner_name", "loser_name"]).sort_values("tourney_date")
    _lambda = math.log(2) / _RECENCY_HALFLIFE

    current_year: int | None = None

    for _, row in df.iterrows():
        tourney_date = row["tourney_date"]
        match_year = tourney_date.year if hasattr(tourney_date, "year") else None
        if match_year and match_year != current_year:
            if current_year is not None:
                _apply_decay(ratings.overall)
                for surf_pool in ratings.by_surface.values():
                    _apply_decay(surf_pool)
            current_year = match_year

        winner = str(row["winner_name"])
        loser  = str(row["loser_name"])
        surface = str(row.get("surface", "hard")).lower()
        level   = str(row.get("tourney_level", "")).strip()

        k_override = None
        if reference_date is not None and hasattr(tourney_date, "year"):
            years_ago = max(0.0, (reference_date - tourney_date).days / 365.25)
            recency = math.exp(-_lambda * years_ago)
            k_override = _k(level) * recency

        if k_override is not None:
            ratings._update(ratings.overall, winner, loser, k_override)
            surf = surface.lower()
            if surf not in ratings.by_surface:
                ratings.by_surface[surf] = {}
            if surf not in ratings.surface_counts:
                ratings.surface_counts[surf] = {}
            ratings._update(ratings.by_surface[surf], winner, loser, k_override)
            ratings.surface_counts[surf][winner] = ratings.surface_counts[surf].get(winner, 0) + 1
            ratings.surface_counts[surf][loser] = ratings.surface_counts[surf].get(loser, 0) + 1
        else:
            ratings.update(winner, loser, surface, level)

    return ratings


# Surface-specific Elo blend weights, calibrated via backtest (2021-2025 Grand Slams):
#   Grass: 60% surface / 40% overall — fewer grass matches → trust overall Elo more
#   Clay/Hard: 70% surface / 30% overall — more matches per surface, better calibrated
_SURFACE_WEIGHTS: dict[str, float] = {"grass": 0.60, "clay": 0.70, "hard": 0.70}


def predict_winner(
    player_a: str,
    player_b: str,
    ratings: TennisEloRatings,
    surface: str,
    w_surface: float | None = None,
) -> dict[str, float]:
    """
    Returns {'p_a': float, 'p_b': float} for a 2-outcome match.
    Each player's surface blend weight is computed independently from their match count
    (via _dynamic_w_surface). Pass explicit w_surface to override for both players.
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
