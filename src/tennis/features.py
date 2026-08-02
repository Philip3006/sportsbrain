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
from src.data.tennis_bios import PlayerBio, age_years, hand_matchup_code  # noqa: F401
from src.tennis.context import lookup_venue, altitude_factor


# Feature-Reihenfolge ist stabil (dient als LightGBM `feature_name`).
# WICHTIG: neue Spalten am ENDE anhängen — sonst bricht ein trainiertes
# LGBM-Modell dessen `feature_columns.json` einen älteren Snapshot enthält.
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
    # J8-C1 Serve/BP-Detail (TA matchhead-verifizierte Columns 2026-08-03)
    "serve_first_in_a", "serve_first_in_b", "serve_first_in_diff",
    "serve_first_win_a", "serve_first_win_b", "serve_first_win_diff",
    "serve_second_win_a", "serve_second_win_b", "serve_second_win_diff",
    "serve_bp_save_a", "serve_bp_save_b", "serve_bp_save_diff",
    "return_bp_conv_a", "return_bp_conv_b", "return_bp_conv_diff",
    # === Phase 2: Form-Erweiterung ===
    "form_hot_diff",           # 5-Match-Fenster (heiße Serie), a-b
    "form_stable_diff",        # 20-Match-Fenster (stabile Form), a-b
    "form_quality_a", "form_quality_b", "form_quality_diff",  # quality-adjusted (Gegner-Rank-gewichtet)
    "sets_dropped_rate_a", "sets_dropped_rate_b",  # letzten 10 Matches: Sätze verloren / gespielt
    "tb_wr_a", "tb_wr_b", "tb_wr_diff",           # Tiebreak-Winrate (letzten 30)
    "sets_last7d_a", "sets_last7d_b",              # Fatigue-Load (Sätze letzte 7 Tage)
    # === Phase 3: Detail-Daten (Biometrie) ===
    "age_a", "age_b", "age_diff",
    "height_a", "height_b", "height_diff",
    "hand_matchup",                                # 0=RR, 1=RL, 2=LR, 3=LL, -1=unknown
    # === Hebel 1: Kontext (context.py Altitude + TZ) ===
    "altitude_factor",                             # 0..1, >0 bei Höhen-Venues
    # === Hebel 3: Bayesian Elo Uncertainty ===
    "elo_uncertainty_a", "elo_uncertainty_b",      # 1/sqrt(n+1) — hoch = wenig Info
    "elo_confidence",                              # gemittelte Confidence (1 - avg uncertainty)
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
        # Phase 2 — Form-Erweiterung
        self.form_hot: dict[str, deque] = defaultdict(lambda: deque(maxlen=5))
        self.form_stable: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        self.form_quality: dict[str, deque] = defaultdict(lambda: deque(maxlen=self.window))
        self.sets_dropped: dict[str, deque] = defaultdict(lambda: deque(maxlen=self.window))
        self.tiebreak: dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        self.sets_by_date: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))

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

    # ---- Phase 2 accessors ----
    def wr_hot(self, player: str) -> float:
        d = self.form_hot.get(player)
        return sum(d) / len(d) if d else 0.5

    def wr_stable(self, player: str) -> float:
        d = self.form_stable.get(player)
        return sum(d) / len(d) if d else 0.5

    def quality_form(self, player: str) -> float:
        d = self.form_quality.get(player)
        if not d:
            return 0.5
        num = sum(v for v, _w in d)
        den = sum(w for _v, w in d)
        return (num / den) if den > 0 else 0.5

    def sets_dropped_rate(self, player: str) -> float:
        d = self.sets_dropped.get(player)
        if not d:
            return 0.3
        num = sum(x[0] for x in d)
        den = sum(x[1] for x in d)
        return (num / den) if den > 0 else 0.3

    def tiebreak_wr(self, player: str) -> float:
        d = self.tiebreak.get(player)
        return (sum(d) / len(d)) if d else 0.5

    def sets_last_7d(self, player: str, now) -> int:
        d = self.sets_by_date.get(player)
        if not d or now is None:
            return 0
        total = 0
        for dt, sets in d:
            try:
                delta = (now - dt).days
            except Exception:
                continue
            if 0 <= delta <= 7:
                total += sets
        return total

    @staticmethod
    def _quality_weight(opp_rank: float | None) -> float:
        if opp_rank is None or opp_rank <= 0:
            return 1.0
        if opp_rank <= 20:
            return 2.0
        if opp_rank <= 100:
            return 1.0
        return 0.5

    def update(
        self,
        winner: str,
        loser: str,
        surface: str,
        date=None,
        *,
        winner_rank: float | None = None,
        loser_rank: float | None = None,
        sets_w: int | None = None,
        sets_l: int | None = None,
        tiebreaks_won_by_winner: int = 0,
        tiebreaks_won_by_loser: int = 0,
    ) -> None:
        # Original core (backward-compat).
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
        # Phase 2 — Form-Erweiterung
        self.form_hot[winner].append(1); self.form_hot[loser].append(0)
        self.form_stable[winner].append(1); self.form_stable[loser].append(0)
        w_w = self._quality_weight(loser_rank)
        w_l = self._quality_weight(winner_rank)
        self.form_quality[winner].append((1.0 * w_w, w_w))
        self.form_quality[loser].append((0.0 * w_l, w_l))
        if sets_w is not None and sets_l is not None and (sets_w + sets_l) > 0:
            total = sets_w + sets_l
            self.sets_dropped[winner].append((sets_l, total))
            self.sets_dropped[loser].append((sets_w, total))
            if date is not None:
                self.sets_by_date[winner].append((date, total))
                self.sets_by_date[loser].append((date, total))
        if tiebreaks_won_by_winner or tiebreaks_won_by_loser:
            for _ in range(tiebreaks_won_by_winner):
                self.tiebreak[winner].append(1); self.tiebreak[loser].append(0)
            for _ in range(tiebreaks_won_by_loser):
                self.tiebreak[loser].append(1); self.tiebreak[winner].append(0)


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
    # === Phase 3 / Hebel 1+3 optional inputs (backward-compat) ===
    bio_a: "PlayerBio | None" = None,
    bio_b: "PlayerBio | None" = None,
    tournament_slug: str | None = None,
    surface_count_a: int = 0,
    surface_count_b: int = 0,
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

    # J2-M Serve-/Return-Aggregates.
    # J8-B8: Fallback-Semantik dokumentiert.
    #   dominance_rate=0.5 → neutrale Mitte (Baseline "unbekannt = 50/50")
    #   ace_rate=0.0 / df_rate=0.0 → Zero-Baseline (kein Signal)
    #   serve_stats_n_a=0 → explizites Missing-Flag; LGBM soll darauf lernen dass
    #     die drei anderen Serve-Features bei n=0 nicht aussagekräftig sind.
    # Konsequenz für Retrain (J2-N): `serve_stats_n_a` als Interaktions-Feature nutzen,
    # bevor Fallback-Werte semantisch aufgewertet werden.
    def _fill_serve(a, prefix: str) -> None:
        if a is not None:
            feats[f"serve_dom_{prefix}"] = float(a.dominance_rate)
            feats[f"serve_ace_rate_{prefix}"] = float(a.ace_rate)
            feats[f"serve_df_rate_{prefix}"] = float(a.df_rate)
            feats[f"serve_stats_n_{prefix}"] = float(a.n_matches)
            feats[f"serve_first_in_{prefix}"] = float(a.first_serve_pct)
            feats[f"serve_first_win_{prefix}"] = float(a.first_serve_win_pct)
            feats[f"serve_second_win_{prefix}"] = float(a.second_serve_win_pct)
            feats[f"serve_bp_save_{prefix}"] = float(a.bp_save_pct)
            feats[f"return_bp_conv_{prefix}"] = float(a.bp_conv_pct)
        else:
            feats[f"serve_dom_{prefix}"] = 0.5
            feats[f"serve_ace_rate_{prefix}"] = 0.0
            feats[f"serve_df_rate_{prefix}"] = 0.0
            feats[f"serve_stats_n_{prefix}"] = 0.0
            feats[f"serve_first_in_{prefix}"] = 0.60
            feats[f"serve_first_win_{prefix}"] = 0.65
            feats[f"serve_second_win_{prefix}"] = 0.50
            feats[f"serve_bp_save_{prefix}"] = 0.60
            feats[f"return_bp_conv_{prefix}"] = 0.40
    _fill_serve(serve_stats_a, "a")
    _fill_serve(serve_stats_b, "b")
    feats["serve_dom_diff"] = feats["serve_dom_a"] - feats["serve_dom_b"]
    feats["serve_ace_diff"] = feats["serve_ace_rate_a"] - feats["serve_ace_rate_b"]
    feats["serve_df_diff"] = feats["serve_df_rate_a"] - feats["serve_df_rate_b"]
    feats["serve_first_in_diff"] = feats["serve_first_in_a"] - feats["serve_first_in_b"]
    feats["serve_first_win_diff"] = feats["serve_first_win_a"] - feats["serve_first_win_b"]
    feats["serve_second_win_diff"] = feats["serve_second_win_a"] - feats["serve_second_win_b"]
    feats["serve_bp_save_diff"] = feats["serve_bp_save_a"] - feats["serve_bp_save_b"]
    feats["return_bp_conv_diff"] = feats["return_bp_conv_a"] - feats["return_bp_conv_b"]

    # === Phase 2 — Form-Erweiterung ===
    feats["form_hot_diff"] = float(state.wr_hot(player_a) - state.wr_hot(player_b))
    feats["form_stable_diff"] = float(state.wr_stable(player_a) - state.wr_stable(player_b))
    fq_a = state.quality_form(player_a); fq_b = state.quality_form(player_b)
    feats["form_quality_a"] = float(fq_a)
    feats["form_quality_b"] = float(fq_b)
    feats["form_quality_diff"] = float(fq_a - fq_b)
    feats["sets_dropped_rate_a"] = float(state.sets_dropped_rate(player_a))
    feats["sets_dropped_rate_b"] = float(state.sets_dropped_rate(player_b))
    tb_a = state.tiebreak_wr(player_a); tb_b = state.tiebreak_wr(player_b)
    feats["tb_wr_a"] = float(tb_a); feats["tb_wr_b"] = float(tb_b)
    feats["tb_wr_diff"] = float(tb_a - tb_b)
    feats["sets_last7d_a"] = float(state.sets_last_7d(player_a, date))
    feats["sets_last7d_b"] = float(state.sets_last_7d(player_b, date))

    # === Phase 3 — Biometrie ===
    _ref = date.date() if hasattr(date, "date") else None
    age_a_v = age_years(bio_a, _ref) if bio_a else None
    age_b_v = age_years(bio_b, _ref) if bio_b else None
    feats["age_a"] = float(age_a_v) if age_a_v else 26.0
    feats["age_b"] = float(age_b_v) if age_b_v else 26.0
    feats["age_diff"] = feats["age_a"] - feats["age_b"]
    h_a = bio_a.height_cm if (bio_a and bio_a.height_cm) else 185
    h_b = bio_b.height_cm if (bio_b and bio_b.height_cm) else 185
    feats["height_a"] = float(h_a); feats["height_b"] = float(h_b)
    feats["height_diff"] = float(h_a - h_b)
    hand_a = bio_a.hand if bio_a else "U"
    hand_b = bio_b.hand if bio_b else "U"
    feats["hand_matchup"] = float(hand_matchup_code(hand_a, hand_b))

    # === Hebel 1 — Kontext (Altitude aus context.VENUE_META) ===
    venue = lookup_venue(tournament_slug) if tournament_slug else None
    feats["altitude_factor"] = float(altitude_factor(venue))

    # === Hebel 3 — Bayesian Elo Uncertainty ===
    # 1/sqrt(n+1) → hoch bei wenig Historie, gegen 0 mit steigendem n
    import math as _m
    unc_a = 1.0 / _m.sqrt(max(surface_count_a, 0) + 1)
    unc_b = 1.0 / _m.sqrt(max(surface_count_b, 0) + 1)
    feats["elo_uncertainty_a"] = float(unc_a)
    feats["elo_uncertainty_b"] = float(unc_b)
    feats["elo_confidence"] = float(1.0 - (unc_a + unc_b) / 2.0)

    # Sicherstellen dass alle FEATURE_COLUMNS präsent sind
    for k in FEATURE_COLUMNS:
        feats.setdefault(k, 0.0)
    return feats


def features_to_row(feats: dict[str, float]) -> list[float]:
    """Ordnet Feature-Dict in stabiler Reihenfolge (für DataFrame/Matrix)."""
    return [feats[c] for c in FEATURE_COLUMNS]
