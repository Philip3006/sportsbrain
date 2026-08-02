"""Hebel 4 — Line-Movement + CLV Signal (Roadmap Phase 4).

Utility zum Berechnen von Bewegungs- + CLV-Signalen aus einem Odds-Snapshot-Paar.

Semantik:
    line_move_pct = (opening_odds - closing_odds) / opening_odds
        >0 → Odds sind gefallen (Player A stärker vom Markt gebacken)
        <0 → Odds gestiegen (Markt gegen Player A)

Verwendung als Feature:
    - Post-training: `line_move_pct` als zusätzliche Column im DataFrame
    - Live-Bet-Filter: nur betten wenn (unsere_edge > threshold) UND
      (line_move_pct * sign(edge) >= 0) — d.h. Markt bestätigt Richtung.

Historische Sammlung: benötigt periodisches Odds-Snapshot in
`data/odds_history/tennis/{yyyy-mm-dd}/{match_id}.json`. Bis das läuft,
`compute_line_move` als reines Rechenmodul + Test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LineMovement:
    opening_odds: float
    closing_odds: float
    move_pct: float          # Δ Odds normiert auf Opening
    implied_move_pp: float   # Change in implied probability (Prozentpunkte)
    sharp_direction: int     # +1 = Markt backt Player A, -1 = gegen, 0 = neutral


def compute_line_move(opening: float, closing: float) -> Optional[LineMovement]:
    """Berechnet Line-Movement zwischen Opening und Closing Odds für einen Player.

    Return None bei ungültigen Odds (<1.01). Sonst LineMovement mit:
      - move_pct in (-1, ∞), positiv = Odds gefallen
      - implied_move_pp in [-1, 1], positiv = implizite Wahrscheinlichkeit gestiegen
      - sharp_direction in {-1, 0, +1}, +1 bei ≥3% Bewegung Richtung Backing
    """
    if opening is None or closing is None:
        return None
    if opening <= 1.01 or closing <= 1.01:
        return None
    move_pct = (opening - closing) / opening
    ip_open = 1.0 / opening
    ip_close = 1.0 / closing
    implied_move = ip_close - ip_open
    # Sharp-Direction: >=3% Odds-Bewegung als Signalschwelle (empirisch,
    # unterhalb ist typisches Markt-Rauschen). Kalibrierung bei Retrain.
    if move_pct >= 0.03:
        direction = 1
    elif move_pct <= -0.03:
        direction = -1
    else:
        direction = 0
    return LineMovement(
        opening_odds=float(opening),
        closing_odds=float(closing),
        move_pct=float(move_pct),
        implied_move_pp=float(implied_move),
        sharp_direction=direction,
    )


def line_move_confirms_edge(
    our_p_a: float,
    market_p_a: float,
    lm_a: LineMovement | None,
    *,
    edge_threshold: float = 0.03,
) -> bool:
    """True wenn (a) wir eine Kante auf Player A haben UND (b) der Markt sich
    in die gleiche Richtung bewegt (contrarian-safe Filter).

    Beispiele:
      - Wir p_a=0.60, Market p_a=0.55 (Edge +5pp) + Line-Move stark Richtung A → True
      - Wir p_a=0.60, Market p_a=0.55 + Line-Move gegen A → False (Warnsignal)
      - Wir p_a=0.55, Market p_a=0.55 → False (keine Edge)
    """
    if lm_a is None:
        return False
    edge = our_p_a - market_p_a
    if abs(edge) < edge_threshold:
        return False
    if edge > 0 and lm_a.sharp_direction >= 0:
        return True
    if edge < 0 and lm_a.sharp_direction <= 0:
        return True
    return False
