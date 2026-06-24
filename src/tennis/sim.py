"""Tennis-Match-Simulation (Roadmap J2-C).

Closed-form Set-Distribution + Monte-Carlo Game-Total-Distribution.
Beide Pfade nutzen die per-Set-Prob aus tennis_detector._p_set_from_p_match
und benötigen kein zusätzliches Modell-Training.

Game-Modell-Approximation:
  p_hold_X ≈ HOLD_BASELINE_<TOUR> + HOLD_SLOPE × (elo_X - elo_opp) / 400
  Clipped auf [HOLD_FLOOR, HOLD_CEIL].
Stimmt nicht spielergenau (echte Serve-Stats wären besser), aber konsistent
mit Elo-Diff → ausreichend für Edge-Detection auf O/U-Spreads ≥ 0.5pp.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


# ATP-Standard: starke Server, höhere Hold-Rate (≈82-86% Top-10)
# WTA: niedrigere Hold-Rate (≈70-74% Top-10) — mehr Breaks pro Set
HOLD_BASELINE_ATP = 0.80
HOLD_BASELINE_WTA = 0.72
HOLD_SLOPE = 0.10
HOLD_FLOOR = 0.55
HOLD_CEIL = 0.95


# ---------------------------------------------------------------------------
# Closed-form Set-Distribution
# ---------------------------------------------------------------------------

def set_score_probs(p_set: float, best_of: int) -> dict[str, float]:
    """Wahrscheinlichkeit pro mögliches Set-Endergebnis aus Sicht von Spieler A.

    BO3 → {"2-0", "2-1", "1-2", "0-2"}
    BO5 → {"3-0", "3-1", "3-2", "2-3", "1-3", "0-3"}

    Summe = 1.
    """
    p = max(1e-6, min(1.0 - 1e-6, p_set))
    q = 1.0 - p

    if best_of == 3:
        return {
            "2-0": p * p,
            "2-1": 2 * p * p * q,
            "1-2": 2 * p * q * q,
            "0-2": q * q,
        }
    elif best_of == 5:
        return {
            "3-0": p**3,
            "3-1": 3 * (p**3) * q,
            "3-2": 6 * (p**3) * (q**2),
            "2-3": 6 * (p**2) * (q**3),
            "1-3": 3 * p * (q**3),
            "0-3": q**3,
        }
    raise ValueError(f"best_of must be 3 or 5, got {best_of}")


def total_sets_probs(p_set: float, best_of: int) -> dict[int, float]:
    """P(Total-Sets = N) als Lookup-Dict, aus set_score_probs aggregiert."""
    scores = set_score_probs(p_set, best_of)
    out: dict[int, float] = {}
    for k, v in scores.items():
        a, b = k.split("-")
        total = int(a) + int(b)
        out[total] = out.get(total, 0.0) + v
    return out


def p_total_sets_over(p_set: float, best_of: int, line: float) -> float:
    """P(Total-Sets > line) — z.B. line=2.5 für BO3, line=3.5 für BO5."""
    dist = total_sets_probs(p_set, best_of)
    return sum(p for n, p in dist.items() if n > line)


def p_total_sets_under(p_set: float, best_of: int, line: float) -> float:
    return 1.0 - p_total_sets_over(p_set, best_of, line)


# ---------------------------------------------------------------------------
# Game-Total (Monte Carlo)
# ---------------------------------------------------------------------------

def _hold_probs(p_set: float, tour: str) -> tuple[float, float]:
    """Schätzt Serve-Hold-Wkt. für Spieler A und B aus per-Set-Prob.

    Heuristik: p_set encodes relative Stärke. Wenn p_set=0.5, gleiche Hold.
    Wenn p_set=0.7 (A stark), A's Hold steigt, B's sinkt.

    Rückgabe: (p_hold_a, p_hold_b)
    """
    baseline = HOLD_BASELINE_WTA if tour.lower() == "wta" else HOLD_BASELINE_ATP
    # p_set 0.5 → diff 0 → beide auf baseline. p_set 0.7 → +0.4 = +0.04 spread
    spread = (p_set - 0.5) * HOLD_SLOPE * 2
    p_hold_a = max(HOLD_FLOOR, min(HOLD_CEIL, baseline + spread))
    p_hold_b = max(HOLD_FLOOR, min(HOLD_CEIL, baseline - spread))
    return p_hold_a, p_hold_b


def _simulate_set(p_hold_a: float, p_hold_b: float, a_serves_first: bool, rng: random.Random) -> tuple[int, int, int]:
    """Simuliert einen Satz spielweise. Return: (games_a, games_b, total_games)."""
    games_a = games_b = 0
    a_serving = a_serves_first
    while True:
        if a_serving:
            if rng.random() < p_hold_a:
                games_a += 1
            else:
                games_b += 1
        else:
            if rng.random() < p_hold_b:
                games_b += 1
            else:
                games_a += 1
        a_serving = not a_serving

        # Set-Logik: erster auf 6 mit ≥2 Vorsprung, Tiebreak bei 6:6
        if (games_a >= 6 and games_a - games_b >= 2) or games_a == 7:
            return games_a, games_b, games_a + games_b
        if (games_b >= 6 and games_b - games_a >= 2) or games_b == 7:
            return games_a, games_b, games_a + games_b
        if games_a == 6 and games_b == 6:
            # Tiebreak: zufällig (50/50 Approximation)
            if rng.random() < 0.5 + (p_hold_a - p_hold_b) * 0.5:
                return 7, 6, 13
            return 6, 7, 13


def simulate_match(
    p_set: float,
    best_of: int,
    tour: str,
    n_sim: int = 2000,
    seed: int | None = None,
) -> dict[str, float]:
    """Monte-Carlo Match-Simulation. Return: Aggregat-Stats.

    Keys: mean_games, median_games, p_set_a_wins, p_match_a_wins,
          plus alle ganzzahligen total_games-Bins (für O/U-Computation).
    """
    rng = random.Random(seed)
    p_hold_a, p_hold_b = _hold_probs(p_set, tour)

    needed = (best_of // 2) + 1  # 2 für BO3, 3 für BO5
    totals: list[int] = []
    a_wins = 0
    set_a_wins = 0
    set_b_wins = 0

    for _ in range(n_sim):
        sets_a = sets_b = total_games = 0
        # A serves first in match — bei jedem neuen Satz wechselt es (nicht abgebildet, weil
        # erster Aufschlag hängt von letztem Satz ab — Approximation: alternierend pro Set).
        a_serves_first = True
        while sets_a < needed and sets_b < needed:
            ga, gb, tg = _simulate_set(p_hold_a, p_hold_b, a_serves_first, rng)
            total_games += tg
            if ga > gb:
                sets_a += 1
                set_a_wins += 1
            else:
                sets_b += 1
                set_b_wins += 1
            a_serves_first = not a_serves_first
        if sets_a > sets_b:
            a_wins += 1
        totals.append(total_games)

    sorted_totals = sorted(totals)
    mean_g = sum(sorted_totals) / len(sorted_totals)
    median_g = sorted_totals[len(sorted_totals) // 2]

    return {
        "mean_games": mean_g,
        "median_games": float(median_g),
        "p_match_a_wins": a_wins / n_sim,
        "p_set_a_wins": set_a_wins / (set_a_wins + set_b_wins),
        "totals": sorted_totals,  # für O/U-Lookups
    }


def p_total_games_over(sim: dict, line: float) -> float:
    """P(Total-Games > line) aus simulate_match-Output."""
    totals = sim["totals"]
    n_over = sum(1 for t in totals if t > line)
    return n_over / len(totals)


def p_total_games_under(sim: dict, line: float) -> float:
    return 1.0 - p_total_games_over(sim, line)
