"""Post-hoc Elo-Prediction Adjustments (Hebel 1 + 3, Elo-only).

Wendet Bayesian-Uncertainty + Altitude-Adjustment auf eine bereits berechnete
Elo-Prediction an. Nutzt keine LGBM-Features → funktioniert im pure-Elo-Backtest
ohne Retrain.

Hebel 2 (Style), 4 (Line-Movement), 5 (Momentum) sind hier NICHT enthalten —
sie benötigen Serve-Stats / Historic-Odds / In-Play-Feed die im XLSX-Backtest
nicht verfügbar sind.
"""
from __future__ import annotations

import math

from src.tennis.context import lookup_venue, altitude_factor


def bayesian_dampen(
    p_a: float,
    surface_count_a: int,
    surface_count_b: int,
    prior: float = 0.5,
    n_ref: int = 20,
) -> float:
    """Zieht Prediction gegen Prior wenn wenig Historie auf dieser Surface.

    weight = min(1, n_min / n_ref)  wobei n_min = min der beiden surface_counts
    return: weight * p_a + (1 - weight) * prior

    Beispiel: n_min=5 → weight=0.25 → 75% Prior-Pull, 25% Elo-Prediction.
    Bei n_min≥20 → weight=1 → unverändertes p_a zurück.
    """
    n_min = max(0, min(surface_count_a, surface_count_b))
    weight = min(1.0, n_min / max(1, n_ref))
    return weight * p_a + (1.0 - weight) * prior


def altitude_adjust(
    p_a: float,
    tourney_slug: str | None,
    *,
    serve_bias_a: float = 0.0,
    serve_bias_b: float = 0.0,
    max_shift: float = 0.03,
) -> float:
    """Höhen-Boost auf Aufschläger. Ohne Serve-Signal (Backtest-Fall) neutral.

    serve_bias_a/b: Serve-Dominanz relativ (kann 0 bleiben wenn unbekannt →
      dann greift Adjustment nur wenn Bias-Delta bekannt).
    max_shift: cap in Wahrscheinlichkeits-Prozentpunkten (Default 3pp).
    """
    factor = altitude_factor(lookup_venue(tourney_slug or ""))
    if factor <= 0.0:
        return p_a
    bias_delta = serve_bias_a - serve_bias_b
    shift = max_shift * factor * math.tanh(bias_delta)  # ∈ [-max_shift*factor, +...]
    return max(0.02, min(0.98, p_a + shift))


def apply_elo_hebels(
    p_a: float,
    *,
    surface_count_a: int = 0,
    surface_count_b: int = 0,
    tourney_slug: str | None = None,
    serve_bias_a: float = 0.0,
    serve_bias_b: float = 0.0,
    enable_bayesian: bool = True,
    enable_altitude: bool = True,
) -> float:
    """Wendet Hebel 1 + 3 auf eine Elo-Prediction an. Reihenfolge:
      1. Bayesian-Dämpfung (zieht gegen 50/50 bei wenig Historie)
      2. Altitude-Boost (nur wenn Serve-Bias bekannt UND Höhen-Venue)
    """
    p = p_a
    if enable_bayesian:
        p = bayesian_dampen(p, surface_count_a, surface_count_b)
    if enable_altitude:
        p = altitude_adjust(p, tourney_slug,
                            serve_bias_a=serve_bias_a, serve_bias_b=serve_bias_b)
    return p
