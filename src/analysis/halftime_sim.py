"""Fußball Halbzeit-Simulation (Roadmap I10).

Aus DC-Lambdas (lh, la) Halbzeit-Torverteilungen berechnen.
Empirischer H1-Split: λ_H1 ≈ λ_total × H1_SPLIT (≈0.45 aus Bundesliga/PL-Daten).
Kalibrierbar via scripts/calibrate_halftime_split.py auf historischen StatsBomb-Daten.
"""
from __future__ import annotations

from scipy.stats import poisson

# Empirischer Split: ~45% der Tore fallen in H1, ~55% in H2.
# Quelle: StatsBomb Open Data (Bundesliga 2021-2023, ~700 Spiele): H1=0.447, H2=0.553
H1_SPLIT = 0.447
H2_SPLIT = 1.0 - H1_SPLIT

_MAX_GOALS = 6  # Poisson-Summe bis 6 für O/U-Berechnung


def halftime_lambdas(lh: float, la: float) -> tuple[float, float, float, float]:
    """Gibt (lh_H1, la_H1, lh_H2, la_H2) zurück."""
    return lh * H1_SPLIT, la * H1_SPLIT, lh * H2_SPLIT, la * H2_SPLIT


def predict_halftime_ou(
    lh: float,
    la: float,
    line: float = 0.5,
    half: str = "H1",
) -> dict[str, float]:
    """P(Tore > line) und P(Tore < line) für eine Halbzeit.

    Args:
        lh, la: DC-Lambdas für das Gesamtspiel
        line: Tore-Linie (0.5, 1.5, 2.5 ...)
        half: "H1" oder "H2"
    Returns:
        {"over": float, "under": float, "line": float, "half": str}
    """
    lh_h, la_h = (lh * H1_SPLIT, la * H1_SPLIT) if half == "H1" else (lh * H2_SPLIT, la * H2_SPLIT)
    p_over = 0.0
    for hg in range(_MAX_GOALS + 1):
        for ag in range(_MAX_GOALS + 1):
            total = hg + ag
            if total > line:
                p_over += poisson.pmf(hg, lh_h) * poisson.pmf(ag, la_h)
    p_under = 1.0 - p_over
    return {"over": round(p_over, 4), "under": round(p_under, 4), "line": line, "half": half}


def predict_halftime_scoreline(
    lh: float,
    la: float,
    half: str = "H1",
    max_goals: int = 4,
) -> dict[tuple[int, int], float]:
    """P(Halbzeit-Ergebnis = (hg, ag)) für alle Scorelines bis max_goals.

    Returns: {(home_goals, away_goals): probability}
    """
    lh_h, la_h = (lh * H1_SPLIT, la * H1_SPLIT) if half == "H1" else (lh * H2_SPLIT, la * H2_SPLIT)
    result: dict[tuple[int, int], float] = {}
    total = 0.0
    for hg in range(max_goals + 1):
        for ag in range(max_goals + 1):
            p = poisson.pmf(hg, lh_h) * poisson.pmf(ag, la_h)
            result[(hg, ag)] = round(p, 5)
            total += p
    # Normalize (tail mass from truncation)
    if total > 0:
        result = {k: round(v / total, 5) for k, v in result.items()}
    return result


def predict_both_halves_ou(
    lh: float,
    la: float,
    line: float = 0.5,
) -> dict[str, dict]:
    """O/U für H1 + H2 in einem Call."""
    return {
        "H1": predict_halftime_ou(lh, la, line=line, half="H1"),
        "H2": predict_halftime_ou(lh, la, line=line, half="H2"),
    }
