"""Tennis-Markt-Kalibrierung (Roadmap J2-G).

Bewertet wie gut das Modell für Set-Märkte (O/U Sets, Set Betting) und
Game-Märkte (O/U Games) historisch kalibriert ist. Da tennis-data.co.uk
KEINE historischen Quoten für diese Märkte liefert, fokussieren wir auf
Brier-Score + Hit-Rate (Kalibrierung), nicht ROI.

Live-Schaltung dieser Märkte erfolgt erst nach realen Shadow-Bets via
scripts/tennis_gate_review.py.
"""
from __future__ import annotations

from src.tennis.sim import (
    set_score_probs,
    p_total_sets_over,
    simulate_match,
    p_total_games_over,
)


def invert_p_match_to_p_set(p_match: float, best_of: int) -> float:
    """Bisection: p_match → p_set. p_match_bo3/5 sind monoton in p_set."""
    p_match = max(0.05, min(0.95, p_match))
    lo, hi = 0.05, 0.95
    for _ in range(40):
        mid = (lo + hi) / 2
        if best_of == 3:
            pm = mid * mid * (3.0 - 2.0 * mid)
        else:
            q = 1.0 - mid
            pm = mid**3 * (1.0 + 3.0 * q + 6.0 * q**2)
        if pm < p_match:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------------------
# Set-Markt-Evaluation
# ---------------------------------------------------------------------------

def evaluate_set_markets(
    p_match: float,
    best_of: int,
    actual_wsets: int,
    actual_lsets: int,
) -> dict[str, dict]:
    """Vergleicht Modell-Wkten mit tatsächlichem Set-Outcome.

    Return: {market_key: {"model_p": float, "actual": 0|1, "brier_term": float}}

    market_keys:
      o_u_sets_<line>_over    (line=2.5 für BO3, 3.5 für BO5)
      score_<W>-<L>           (Scoreline aus Sicht des Winners)
    """
    out: dict[str, dict] = {}
    p_set = invert_p_match_to_p_set(p_match, best_of)
    line = 2.5 if best_of == 3 else 3.5
    actual_total = actual_wsets + actual_lsets

    # O/U Sets — "over"-Perspektive
    p_over = p_total_sets_over(p_set, best_of, line)
    actual_over = 1 if actual_total > line else 0
    out[f"o_u_sets_{line}_over"] = {
        "model_p": p_over,
        "actual": actual_over,
        "brier_term": (p_over - actual_over) ** 2,
    }

    # Set Betting — Wahrscheinlichkeit der tatsächlichen Scoreline
    scores = set_score_probs(p_set, best_of)
    # Scoreline aus Sicht des "Spieler A" = Winner → wsets-lsets
    actual_score = f"{actual_wsets}-{actual_lsets}"
    p_actual = scores.get(actual_score, 0.0)
    out[f"score_{actual_score}"] = {
        "model_p": p_actual,
        "actual": 1,   # die tatsächlich aufgetretene Scoreline
        "brier_term": (p_actual - 1) ** 2,
    }
    # Auch alle nicht-aufgetretenen Scorelines mit actual=0 mitführen, damit
    # die Brier-Mittelung über die volle Scoreline-Distribution geht.
    for sc, p in scores.items():
        if sc == actual_score:
            continue
        out[f"score_{sc}"] = {
            "model_p": p,
            "actual": 0,
            "brier_term": (p - 0) ** 2,
        }

    return out


# ---------------------------------------------------------------------------
# Game-Markt-Evaluation
# ---------------------------------------------------------------------------

def _extract_total_games(row) -> int | None:
    """Summiert W1+L1+...+W5+L5 wenn vorhanden, sonst None."""
    total = 0
    found_any = False
    for i in range(1, 6):
        for col in (f"W{i}", f"L{i}"):
            v = row.get(col)
            if v is None:
                continue
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv >= 0:
                total += iv
                found_any = True
    return total if found_any else None


def evaluate_game_markets(
    p_match: float,
    best_of: int,
    tour: str,
    row,
) -> dict[str, dict] | None:
    """Wie evaluate_set_markets, aber für O/U Games. None wenn keine Game-Scores."""
    actual_total = _extract_total_games(row)
    if actual_total is None:
        return None

    p_set = invert_p_match_to_p_set(p_match, best_of)
    sim = simulate_match(p_set, best_of, tour, n_sim=500, seed=42)

    line = 21.5 if best_of == 3 else 38.5
    p_over = p_total_games_over(sim, line)
    actual_over = 1 if actual_total > line else 0

    return {
        f"o_u_games_{line}_over": {
            "model_p": p_over,
            "actual": actual_over,
            "brier_term": (p_over - actual_over) ** 2,
        }
    }
