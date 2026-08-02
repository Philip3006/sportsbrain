"""Hebel 5 — In-Play Momentum Modell (Roadmap Phase 4).

Momentum-Score aus laufendem Match-State: kombiniert
  - Break-Point-Bilanz letzten 5 Games
  - Service-Hold-Streak
  - Tiebreak-Lead
  - Set-Verlauf (welche Sets gewonnen)

Ziel: dynamische Wahrscheinlichkeits-Anpassung Set 2+ wenn Momentum-Shift
klar erkennbar.

Konservative Anwendung: max ±5pp Verschiebung der Pre-Match-Prob.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InPlayState:
    """Kompakter Snapshot des laufenden Matches (Player-A-Perspektive)."""
    sets_won_a: int
    sets_won_b: int
    games_won_a_current_set: int
    games_won_b_current_set: int
    breaks_last5_games_a: int
    breaks_last5_games_b: int
    service_hold_streak_a: int
    service_hold_streak_b: int
    on_serve_a: bool  # True wenn A gerade aufschlägt


def momentum_score(state: InPlayState) -> float:
    """Score in [-1, +1]. Positiv = Momentum bei Player A."""
    score = 0.0
    # Set-Lead dominant (jeder Satz-Vorsprung = 0.3)
    score += 0.3 * (state.sets_won_a - state.sets_won_b)
    # Game-Lead im aktuellen Satz
    game_diff = state.games_won_a_current_set - state.games_won_b_current_set
    score += 0.05 * game_diff
    # Breaks (Serve gebrochen zu haben ist starkes Signal)
    break_diff = state.breaks_last5_games_a - state.breaks_last5_games_b
    score += 0.15 * break_diff
    # Serve-Hold-Streak (Selbstvertrauen)
    hold_diff = min(3, state.service_hold_streak_a) - min(3, state.service_hold_streak_b)
    score += 0.05 * hold_diff
    return float(max(-1.0, min(1.0, score)))


def momentum_prob_adjustment(state: InPlayState, base_p_a: float, cap: float = 0.05) -> float:
    """Adjustiert Pre-Match-Prob um Momentum-Score, gecapped bei ±cap."""
    if base_p_a <= 0 or base_p_a >= 1:
        return base_p_a
    shift = momentum_score(state) * cap
    adj = base_p_a + shift
    return float(max(0.02, min(0.98, adj)))
