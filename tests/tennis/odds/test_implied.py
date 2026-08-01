"""J8-B12: implied_from_prob — no_bet_flag korrekt gesetzt."""
from __future__ import annotations

from src.tennis.odds.implied import implied_from_prob


def test_implied_produces_no_bet_flag():
    q = implied_from_prob(0.60, "A", "B")
    assert q.no_bet_flag is True
    assert q.source_tier == 5


def test_implied_odds_reflect_probability():
    q = implied_from_prob(0.75, "A", "B")
    # p_a=0.75 → q_a ~= 1/(0.75*1.05) ≈ 1.27
    assert 1.20 < q.h2h_a < 1.35
    # p_b=0.25 → q_b ~= 1/(0.25*1.05) ≈ 3.81
    assert 3.50 < q.h2h_b < 4.20


def test_extreme_probabilities_clipped():
    q = implied_from_prob(0.999, "A", "B")
    # Modell darf keine 1.0-Quote produzieren
    assert q.h2h_a > 1.00
    assert q.h2h_b > 1.00
