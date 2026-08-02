"""Hebel 2 — Playing-Style Cluster (Roadmap Phase 4).

Rule-based Klassifikator statt K-Means-Sklearn-Dep. Input: ServeAggregate.
Cluster:
  - SERVE_BOT       hoher ace_rate (≥8%), niedriger df_rate — hält Aufschlag stark
  - AGGRESSOR       hoher dominance_rate + moderate ace_rate
  - BASELINER       neutrale ace/df, hoher win_rate — konsistent
  - COUNTER_PUNCHER niedriger ace_rate + hoher dominance_rate → gewinnt via Return
  - UNKNOWN         zu wenig Samples

Matchup-Edge-Matrix (empirisch, kalibrierungsbedürftig — konservativ ±3pp):
  SERVE_BOT vs COUNTER_PUNCHER: SB gewinnt (+3pp) — kein Rythmus für CP
  SERVE_BOT vs BASELINER:       neutral
  AGGRESSOR vs BASELINER:       AG gewinnt (+2pp)
  BASELINER vs COUNTER_PUNCHER: BL gewinnt (+2pp)
  Rest:                         0
"""
from __future__ import annotations

from src.data.tennis_stats import ServeAggregate

SERVE_BOT = "serve_bot"
AGGRESSOR = "aggressor"
BASELINER = "baseliner"
COUNTER_PUNCHER = "counter_puncher"
UNKNOWN = "unknown"


def classify_style(agg: ServeAggregate | None) -> str:
    if agg is None or agg.n_matches < 10:
        return UNKNOWN
    if agg.ace_rate >= 0.08 and agg.df_rate <= 0.04:
        return SERVE_BOT
    if agg.dominance_rate >= 0.52 and agg.ace_rate >= 0.05:
        return AGGRESSOR
    if agg.dominance_rate >= 0.52 and agg.ace_rate < 0.05:
        return COUNTER_PUNCHER
    if 0.48 <= agg.dominance_rate < 0.52:
        return BASELINER
    return UNKNOWN


_MATCHUP: dict[tuple[str, str], float] = {
    (SERVE_BOT, COUNTER_PUNCHER): 0.03,
    (COUNTER_PUNCHER, SERVE_BOT): -0.03,
    (AGGRESSOR, BASELINER): 0.02,
    (BASELINER, AGGRESSOR): -0.02,
    (BASELINER, COUNTER_PUNCHER): 0.02,
    (COUNTER_PUNCHER, BASELINER): -0.02,
}


def style_matchup_edge(style_a: str, style_b: str) -> float:
    """Wahrscheinlichkeits-Bias in [-0.03, +0.03] für Player A."""
    if style_a == UNKNOWN or style_b == UNKNOWN:
        return 0.0
    return _MATCHUP.get((style_a, style_b), 0.0)
