"""Central EV-Gate registry.

Vor 2026-08-05 waren die Gate-Konstanten über 4+ Module verteilt:
- config.MIN_EDGE (0.03), config.MAX_EV (0.40)
- tennis_detector._MIN_PROB (0.35), _MAX_ODDS (4.50)
- value_detector._BIAS_EV_CAP (0.30), _MARKET_DISAGREEMENT_THRESHOLD (0.10)
- goalscorer lokales min_ev=0.08 (bookmaker-margin auf Player-Props ist ~20%)

Diese Datei ist die Single-Source-of-Truth. Bestehende Konstanten werden hier
importiert, damit ein zentraler Änderungsort existiert. Sport-spezifische
Overrides via `gate_for(sport, market)`.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from src.config import (
    MAX_EV,
    MIN_EDGE,
    TENNIS_MIN_EDGE_BY_CATEGORY,
)


@dataclass(frozen=True)
class Gate:
    min_edge: float
    max_ev: float
    min_prob: float
    max_odds: float
    bias_ev_cap: float  # EV >= diesem Wert → Confidence-Downgrade
    market_disagreement: float  # Model vs Market Δ ≥ diesem Wert → LOW

    def with_min_edge(self, edge: float) -> "Gate":
        return replace(self, min_edge=edge)


# Default (Football / generic)
DEFAULT_GATE = Gate(
    min_edge=MIN_EDGE,       # 0.03
    max_ev=MAX_EV,           # 0.40
    min_prob=0.30,
    max_odds=10.0,
    bias_ev_cap=0.30,
    market_disagreement=0.10,
)

# Tennis: schärfer, weil Qualifier/Junioren-Overfit-Risiko und Volatilität
TENNIS_GATE = Gate(
    min_edge=MIN_EDGE,
    max_ev=MAX_EV,
    min_prob=0.35,           # vorher tennis_detector._MIN_PROB
    max_odds=4.50,           # vorher tennis_detector._MAX_ODDS
    bias_ev_cap=0.30,
    market_disagreement=0.10,
)

# Player-Props / Scorer: Bookmaker-Margin ~20% → höheres min_edge
GOALSCORER_GATE = Gate(
    min_edge=0.08,           # vorher goalscorer lokal
    max_ev=MAX_EV,
    min_prob=0.15,           # Torschütze-Base-Rate deutlich unter 0.30
    max_odds=15.0,           # long-shot Torschützen erlaubt (mit stake-cap)
    bias_ev_cap=0.30,
    market_disagreement=0.15,
)


def gate_for(sport: str, market: str = "", *, category: str = "") -> Gate:
    """Liefert das relevante Gate für (sport, market).

    sport: "football" | "tennis" | "goalscorer"
    category: optional Tennis-Category (atp/wta/challenger) für per-cat Override
    """
    s = sport.lower()
    if s == "tennis":
        g = TENNIS_GATE
        if category:
            per_cat = TENNIS_MIN_EDGE_BY_CATEGORY.get(category.lower())
            if per_cat is not None:
                g = g.with_min_edge(per_cat)
        return g
    if s == "goalscorer" or market.startswith("scorer_"):
        return GOALSCORER_GATE
    return DEFAULT_GATE
