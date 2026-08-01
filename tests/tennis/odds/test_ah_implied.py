"""J8-M5: impliziter AH-Fallback aus H2H + p_set."""
from __future__ import annotations

from src.tennis.odds.ah_implied import implied_ah_from_h2h


def test_favorite_gets_shorter_ah_odd():
    # Starker Favorit (p_a=0.75) → ah-1.5_a soll unter 3.0 liegen
    p_ah_a, p_ah_b, q_a, q_b = implied_ah_from_h2h(0.75, "A", "B", bo5=True)
    assert p_ah_a > p_ah_b
    assert q_a < q_b
    assert 1.20 < q_a < 4.0


def test_underdog_gets_long_ah_odd():
    p_ah_a, p_ah_b, q_a, q_b = implied_ah_from_h2h(0.30, "A", "B", bo5=True)
    assert p_ah_a < p_ah_b
    assert q_a > q_b


def test_bo3_vs_bo5_shape_differs():
    _, _, q_a3, _ = implied_ah_from_h2h(0.75, "A", "B", bo5=False)
    _, _, q_a5, _ = implied_ah_from_h2h(0.75, "A", "B", bo5=True)
    # BO3 dominant-2:0 seltener als BO5 dominant-3:0-or-3:1 → höhere Quote
    assert q_a3 >= q_a5


def test_asymmetric_at_p_half_bo5():
    # p_a=0.5 heißt nicht dass ah-1.5_a == ah+1.5_b! Ah+1.5 deckt sowohl
    # "B gewinnt" als auch "A gewinnt knapp" ab → bei p_match=0.5 klar häufiger.
    p_ah_a, p_ah_b, _, _ = implied_ah_from_h2h(0.50, "A", "B", bo5=True)
    assert p_ah_a + p_ah_b == 1.0
    assert p_ah_a < 0.4  # A dominant seltener als A-knapp-oder-B-Sieg
