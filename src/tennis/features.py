"""Tennis Feature Engineering (Roadmap J2-K).

Baut Match-Level-Features für ein LightGBM-Modell aus dem tennis-data.co.uk
XLSX-Datensatz (Winner/Loser-Format, ATP+WTA, 2019-2025, ~32k Matches).

Grundprinzip: pro Match werden Features für einen der beiden Spieler (`player_a`)
berechnet und der Gegner (`player_b`) als Referenz herangezogen. Beim Training
wird die Seite random geswapped (Ziel-Label y = 1 wenn player_a gewinnt).

Verfügbare Roh-Signale aus XLSX:
  - Ranking (WRank/LRank) → rank_diff, rank_a/b
  - Odds (B365W/L, AvgW/L, MaxW/L) → implied prob, closing-odds
  - Sets (Wsets/Lsets, W1-W5/L1-L5) → dominance
  - Meta (Best of, Surface, Series/Tier, Round)

Serve-Stats fehlen im XLSX — J2-M ist optional dafür.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd

from src.data.tennis_stats import ServeAggregate  # noqa: F401 (type hint)


# Feature-Reihenfolge ist stabil (dient als LightGBM `feature_name`).
FEATURE_COLUMNS: tuple[str, ...] = (
    # Ranking
    "rank_a", "rank_b", "rank_diff", "rank_log_ratio",
    # Elo (surface-blended + delta)
    "elo_a", "elo_b", "elo_diff", "elo_surface_diff",
    # Form (letzte N Matches, surface-aware)
    "form_a_wr", "form_b_wr", "form_diff",
    "form_a_wr_surface", "form_b_wr_surface", "form_diff_surface",
    # H2H (surface-aware)
    "h2h_a_wr", "h2h_n", "h2h_surface_a_wr", "h2h_surface_n",
    # Rest days seit letztem Match
    "rest_a", "rest_b", "rest_diff",
    # Match-Meta
    "best_of", "is_grand_slam", "is_masters", "is_grass", "is_clay", "is_hard",
    # Round / Progression
    "round_ordinal",  # 1=Qualifying/First, 7=Final
    # Interaktionen
    "elo_diff_x_surface_diff",  # elo_diff * elo_surface_diff (Surface-Verstärker)
    "rank_diff_x_bo5",          # rank_diff auf Grand-Slams (BO5) — Top-Spieler stärker
    # J2-M Serve-/Return-Stats (Tennis Abstract matchmx, surface-aware last-20)
    "serve_dom_a", "serve_dom_b", "serve_dom_diff",
    "serve_ace_rate_a", "serve_ace_rate_b", "serve_ace_diff",
    "serve_df_rate_a", "serve_df_rate_b", "serve_df_diff",
    "serve_stats_n_a", "serve_stats_n_b",  # sample-size feature (0 = kein Live-Stat verfügbar)
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

_ROUND_MAP: dict[str, int] = {
    "1st Round": 1, "2nd Round": 2, "3rd Round": 3, "4th Round": 4,
    "Quarterfinals": 5, "Semifinals": 6, "The Final": 7, "Round Robin": 3,
    # WTA / alt names
    "Round of 128": 1, "Round of 64": 2, "Round of 32": 3, "Round of 16": 4,
}


def _round_ordinal(round_str: str) -> int:
    return _ROUND_MAP.get((round_str or "").strip(), 3)


def _category_flags(category: str) -> dict[str, int]:
    return {
        "is_grand_slam": int(category == "grand_slam"),
        "is_masters":    int(category in ("m1000", "wta1000")),
    }


def _surface_flags(surface: str) -> dict[str, int]:
    s = (surface or "").lower()
    return {
        "is_grass": int(s == "grass"),
        "is_clay":  int(s == "clay"),
        "is_hard":  int(s == "hard"),
    }


# ---------------------------------------------------------------------------
# Rolling State (Form + H2H)
# ---------------------------------------------------------------------------

@dataclass
class RollingState:
    """Hält Rolling-Statistiken pro Player. Muss chronologisch gefüttert werden.

    - form: deque der letzten N Ergebnisse (1=win, 0=loss)
    - form_surface: pro Surface separat
    - h2h: dict[opponent] → (wins, losses)
    - h2h_surface: dict[(opponent, surface)] → (wins, losses)
    """
    window: int = 10

    def __post_init__(self):
        self.form: dict[str, deque] = defaultdict(lambda: deque(maxlen=self.window))
        self.form_surface: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=self.window))
        self.h2h: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        self.h2h_surface: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
        self.last_match: dict[str, "pd.Timestamp | None"] = defaultdict(lambda: None)

    def rest_days(self, player: str, now) -> float:
        prev = self.last_match.get(player)
        if prev is None:
            return 14.0  # neutral prior (2 weeks)
        delta = (now - prev).days
        return float(min(max(delta, 0), 60))  # cap 60 days

    def wr(self, player: str) -> float:
        d = self.form.get(player)
        return sum(d) / len(d) if d else 0.5

    def wr_surface(self, player: str, surface: str) -> float:
        d = self.form_surface.get((player, surface))
        return sum(d) / len(d) if d else 0.5

    def h2h_wr(self, a: str, b: str) -> tuple[float, int]:
        w, l = self.h2h.get((a, b), [0, 0])
        n = w + l
        return (w / n, n) if n else (0.5, 0)

    def h2h_wr_surface(self, a: str, b: str, surface: str) -> tuple[float, int]:
        w, l = self.h2h_surface.get((a, b, surface), [0, 0])
        n = w + l
        return (w / n, n) if n else (0.5, 0)

    def update(self, winner: str, loser: str, surface: str, date=None) -> None:
        self.form[winner].append(1)
        self.form[loser].append(0)
        self.form_surface[(winner, surface)].append(1)
        self.form_surface[(loser, surface)].append(0)
        self.h2h[(winner, loser)][0] += 1
        self.h2h[(loser, winner)][1] += 1
        self.h2h_surface[(winner, loser, surface)][0] += 1
        self.h2h_surface[(loser, winner, surface)][1] += 1
        if date is not None:
            self.last_match[winner] = date
            self.last_match[loser] = date


# ---------------------------------------------------------------------------
# Feature-Extraction
# ---------------------------------------------------------------------------

def build_match_features(
    player_a: str,
    player_b: str,
    surface: str,
    best_of: int,
    category: str,
    round_str: str,
    rank_a: float,
    rank_b: float,
    elo_a: float,
    elo_b: float,
    elo_surface_a: float,
    elo_surface_b: float,
    state: RollingState,
    date=None,
    serve_stats_a: "ServeAggregate | None" = None,
    serve_stats_b: "ServeAggregate | None" = None,
) -> dict[str, float]:
    """Baut Feature-Dict für ein einzelnes Match (Prediction-Zeit).

    Zustand (state) darf nur Matches enthalten die VOR diesem Match liegen
    (Walk-forward-Grundregel — keine Leakage).
    """
    # Ranking
    rank_diff = rank_a - rank_b
    rank_log_ratio = np.log(max(rank_a, 1)) - np.log(max(rank_b, 1))

    # Form
    form_a = state.wr(player_a)
    form_b = state.wr(player_b)
    form_a_s = state.wr_surface(player_a, surface)
    form_b_s = state.wr_surface(player_b, surface)

    # H2H
    h2h_a_wr, h2h_n = state.h2h_wr(player_a, player_b)
    h2h_s_a_wr, h2h_s_n = state.h2h_wr_surface(player_a, player_b, surface)

    # Rest days
    if date is not None:
        rest_a = state.rest_days(player_a, date)
        rest_b = state.rest_days(player_b, date)
    else:
        rest_a = rest_b = 14.0

    # Interaktionen
    elo_diff_x_surf = (elo_a - elo_b) * (elo_surface_a - elo_surface_b)
    rank_diff_x_bo5 = (rank_a - rank_b) * (1.0 if best_of == 5 else 0.0)

    feats: dict[str, float] = {
        "rank_a": float(rank_a),
        "rank_b": float(rank_b),
        "rank_diff": float(rank_diff),
        "rank_log_ratio": float(rank_log_ratio),
        "elo_a": float(elo_a),
        "elo_b": float(elo_b),
        "elo_diff": float(elo_a - elo_b),
        "elo_surface_diff": float(elo_surface_a - elo_surface_b),
        "form_a_wr": float(form_a),
        "form_b_wr": float(form_b),
        "form_diff": float(form_a - form_b),
        "form_a_wr_surface": float(form_a_s),
        "form_b_wr_surface": float(form_b_s),
        "form_diff_surface": float(form_a_s - form_b_s),
        "h2h_a_wr": float(h2h_a_wr),
        "h2h_n": float(h2h_n),
        "h2h_surface_a_wr": float(h2h_s_a_wr),
        "h2h_surface_n": float(h2h_s_n),
        "rest_a": float(rest_a),
        "rest_b": float(rest_b),
        "rest_diff": float(rest_a - rest_b),
        "best_of": float(best_of),
        "round_ordinal": float(_round_ordinal(round_str)),
        "elo_diff_x_surface_diff": float(elo_diff_x_surf),
        "rank_diff_x_bo5": float(rank_diff_x_bo5),
    }
    feats.update({k: float(v) for k, v in _category_flags(category).items()})
    feats.update({k: float(v) for k, v in _surface_flags(surface).items()})

    # J2-M Serve-/Return-Aggregates
    if serve_stats_a is not None:
        feats["serve_dom_a"] = float(serve_stats_a.dominance_rate)
        feats["serve_ace_rate_a"] = float(serve_stats_a.ace_rate)
        feats["serve_df_rate_a"] = float(serve_stats_a.df_rate)
        feats["serve_stats_n_a"] = float(serve_stats_a.n_matches)
    else:
        feats["serve_dom_a"] = 0.5
        feats["serve_ace_rate_a"] = 0.0
        feats["serve_df_rate_a"] = 0.0
        feats["serve_stats_n_a"] = 0.0
    if serve_stats_b is not None:
        feats["serve_dom_b"] = float(serve_stats_b.dominance_rate)
        feats["serve_ace_rate_b"] = float(serve_stats_b.ace_rate)
        feats["serve_df_rate_b"] = float(serve_stats_b.df_rate)
        feats["serve_stats_n_b"] = float(serve_stats_b.n_matches)
    else:
        feats["serve_dom_b"] = 0.5
        feats["serve_ace_rate_b"] = 0.0
        feats["serve_df_rate_b"] = 0.0
        feats["serve_stats_n_b"] = 0.0
    feats["serve_dom_diff"] = feats["serve_dom_a"] - feats["serve_dom_b"]
    feats["serve_ace_diff"] = feats["serve_ace_rate_a"] - feats["serve_ace_rate_b"]
    feats["serve_df_diff"] = feats["serve_df_rate_a"] - feats["serve_df_rate_b"]

    # Sicherstellen dass alle FEATURE_COLUMNS präsent sind
    for k in FEATURE_COLUMNS:
        feats.setdefault(k, 0.0)
    return feats


def features_to_row(feats: dict[str, float]) -> list[float]:
    """Ordnet Feature-Dict in stabiler Reihenfolge (für DataFrame/Matrix)."""
    return [feats[c] for c in FEATURE_COLUMNS]
