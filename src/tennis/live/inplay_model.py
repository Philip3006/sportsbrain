"""J8-I8 — Tennis Live In-Play-Wahrscheinlichkeits-Modell (Phase 5).

Rechnet aus dem aktuellen Match-State (Sätze, Games, Server) die Live-
Wahrscheinlichkeit, dass Spieler A das Match gewinnt. Baut auf der bestehenden
Set-Level-Sim (`src.tennis.sim`) auf, ergänzt um:

  1. Break-Adjustment: bei Break-Rückstand innerhalb eines Satzes senken wir
     die p_hold-Erwartung dieses Satzes.
  2. Momentum-Bias: die letzten N Games (last-5) beeinflussen p_hold ±3%.
  3. Server-Info: wer aufschlägt bekommt „Return-in-play"-Rückgabe (game-level).

Vereinfachungen (bewusst): kein Point-Level, kein Break-Point-Erkennung — nur
Game-Score reicht als Input.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.betting.tennis_detector import _p_set_from_p_match
from src.tennis.momentum import InPlayState, momentum_prob_adjustment


@dataclass(frozen=True)
class LiveMatchState:
    """Aktueller Match-State für Live-Prediction."""
    p_pre_match_a: float          # Pre-Match Model-Prob p_a (aus Ensemble)
    sets_a: int                   # bereits gewonnene Sätze A
    sets_b: int
    current_set_games_a: int      # Games im laufenden Satz
    current_set_games_b: int
    server: str = "a"             # 'a' | 'b' — wer serviert grade
    best_of: int = 3
    tour: str = "atp"
    recent_games_a: int = 0       # in letzten 6 Games gewonnen (0..6)
    # Hebel 5 — Momentum-Signale (optional, defaults = neutral).
    breaks_last5_games_a: int = 0
    breaks_last5_games_b: int = 0
    service_hold_streak_a: int = 0
    service_hold_streak_b: int = 0


def _p_hold(tour: str) -> float:
    return 0.80 if tour.lower() == "atp" else 0.72


def _sets_needed(best_of: int) -> int:
    return 3 if best_of == 5 else 2


def _p_win_current_set(state: LiveMatchState, p_set_a: float) -> float:
    """Wahrscheinlichkeit dass A den laufenden Satz gewinnt, gegeben Games-Score
    und Server. Nutzt vereinfachtes Recursion-Modell mit p_hold Baseline."""
    g_a, g_b = state.current_set_games_a, state.current_set_games_b
    if g_a >= 6 and (g_a - g_b) >= 2:
        return 1.0
    if g_b >= 6 and (g_b - g_a) >= 2:
        return 0.0
    if g_a == 7:
        return 1.0
    if g_b == 7:
        return 0.0

    # Approximation: verbleibende Games via Set-Prob p_set_a, aber gewichtet mit
    # aktuellem Game-Rückstand. Break-Rückstand = g_a - g_b < 0 → Malus.
    lead = g_a - g_b
    # Malus ~ 12% pro Break-Diff, saturiert bei ±0.3
    lead_adj = max(-0.30, min(0.30, lead * 0.12))
    p = max(0.05, min(0.95, p_set_a + lead_adj))

    # Momentum: letzte 6 Games — 3=neutral. Deviation ±0.03 pro Game weg von Mitte.
    momentum = (state.recent_games_a - 3) * 0.03
    p = max(0.05, min(0.95, p + momentum))

    # Server-Bias — Aufschläger bekommt +hold_advantage-Bonus im nächsten Game
    if state.server == "a":
        p = max(0.05, min(0.95, p + 0.02))
    else:
        p = max(0.05, min(0.95, p - 0.02))
    return p


def _p_win_remaining_sets(need_more_a: int, need_more_b: int, p_set_a: float) -> float:
    """P(A gewinnt Best-of-Serie in restlichen Sätzen). Rekursion mit Cache."""
    if need_more_a <= 0:
        return 1.0
    if need_more_b <= 0:
        return 0.0
    memo: dict[tuple[int, int], float] = {}
    def _rec(a: int, b: int) -> float:
        if a <= 0:
            return 1.0
        if b <= 0:
            return 0.0
        key = (a, b)
        if key in memo:
            return memo[key]
        v = p_set_a * _rec(a - 1, b) + (1 - p_set_a) * _rec(a, b - 1)
        memo[key] = v
        return v
    return _rec(need_more_a, need_more_b)


def predict_live_p_a(state: LiveMatchState) -> float:
    """Return P(A wins match | current live state)."""
    max_sets = _sets_needed(state.best_of)
    if state.sets_a >= max_sets:
        return 1.0
    if state.sets_b >= max_sets:
        return 0.0

    p_set_a = _p_set_from_p_match(state.p_pre_match_a, bo5=state.best_of == 5)
    p_current_set_a = _p_win_current_set(state, p_set_a)

    need_after_a_wins_current_a = max_sets - state.sets_a - 1
    need_after_a_wins_current_b = max_sets - state.sets_b
    p_if_a_wins = _p_win_remaining_sets(need_after_a_wins_current_a,
                                         need_after_a_wins_current_b, p_set_a)

    need_after_b_wins_current_a = max_sets - state.sets_a
    need_after_b_wins_current_b = max_sets - state.sets_b - 1
    p_if_b_wins = _p_win_remaining_sets(need_after_b_wins_current_a,
                                         need_after_b_wins_current_b, p_set_a)

    p_base = p_current_set_a * p_if_a_wins + (1 - p_current_set_a) * p_if_b_wins

    # Hebel 5 — Momentum-Adjustment (max ±5pp, capped im Modul).
    momentum_state = InPlayState(
        sets_won_a=state.sets_a,
        sets_won_b=state.sets_b,
        games_won_a_current_set=state.current_set_games_a,
        games_won_b_current_set=state.current_set_games_b,
        breaks_last5_games_a=state.breaks_last5_games_a,
        breaks_last5_games_b=state.breaks_last5_games_b,
        service_hold_streak_a=state.service_hold_streak_a,
        service_hold_streak_b=state.service_hold_streak_b,
        on_serve_a=(state.server == "a"),
    )
    return momentum_prob_adjustment(momentum_state, base_p_a=p_base, cap=0.05)


def value_live_signal(state: LiveMatchState, live_odds_a: float,
                       live_odds_b: float, min_edge: float = 0.03,
                       max_ev: float = 0.40) -> list[dict]:
    """Returns list of live-value signals ({side, live_p, edge, ev}).

    Nur einfache Match-Winner-Live-Bets — kein AH, kein Set-Betting im ersten Wurf.
    """
    from src.betting.kelly import expected_value
    p_a = predict_live_p_a(state)
    signals: list[dict] = []
    for side, p, odds in (("a", p_a, live_odds_a), ("b", 1 - p_a, live_odds_b)):
        if odds is None or odds < 1.05:
            continue
        implied = 1.0 / odds
        edge = p - implied
        if edge < min_edge:
            continue
        ev = expected_value(p, odds)
        if ev > max_ev or ev < 0:
            continue
        signals.append({
            "side": side,
            "live_p": round(p, 4),
            "implied": round(implied, 4),
            "edge_pp": round(edge * 100, 2),
            "ev_pct": round(ev * 100, 2),
            "odds": round(odds, 2),
        })
    return signals
