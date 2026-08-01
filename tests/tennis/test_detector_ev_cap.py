"""J8-B7: MAX_EV cap gilt für ALLE Tennis-Detector-Funktionen.

Regression-Guard: der Audit befürchtete dass detect_total_sets/total_games/set_betting
den EV-Cap umgehen. Tatsächlich rufen sie alle `_signal()` das den Cap enthält —
dieser Test asserted das strukturell für alle vier Detektoren.
"""
from __future__ import annotations

from src.betting.tennis_detector import (
    _signal,
    detect_set_betting,
    detect_total_games,
    detect_total_sets,
    detect_value_tennis,
)
from src.config import MAX_EV


def test_signal_rejects_ev_above_cap():
    # Sehr hohe model_p bei schlechter Quote → EV massiv über MAX_EV
    sig = _signal("m1", "A", "B", "home",
                  model_p=0.99, fair_p=0.10, odds=5.0, bankroll=100.0)
    assert sig is None


def test_signal_accepts_ev_in_band():
    # model_p 0.55, odds 2.0 → EV = 0.55*2.0 - 1 = 0.10 → in [MIN_EDGE, MAX_EV]
    sig = _signal("m1", "A", "B", "home",
                  model_p=0.55, fair_p=0.50, odds=2.0, bankroll=100.0)
    assert sig is not None
    assert sig.ev <= MAX_EV


def test_detect_total_sets_enforces_cap():
    # p_match=0.99, odds_over=5.0 → EV of over huge
    sigs = detect_total_sets("A", "B", p_match_a=0.99,
                             odds_over=5.0, odds_under=1.10, line=2.5,
                             best_of=3, bankroll=100.0)
    for s in sigs:
        assert s.ev <= MAX_EV


def test_detect_total_games_enforces_cap():
    sigs = detect_total_games("A", "B", p_match_a=0.99,
                              odds_over=5.0, odds_under=1.10, line=22.5,
                              best_of=3, bankroll=100.0, n_sim=200)
    for s in sigs:
        assert s.ev <= MAX_EV


def test_detect_set_betting_enforces_cap():
    # Extreme scoreline_odds vs Modell-Distribution → sollte capped werden
    sigs = detect_set_betting("A", "B", p_match_a=0.90,
                              scoreline_odds={"2-0": 30.0, "2-1": 30.0},
                              best_of=3, bankroll=100.0)
    for s in sigs:
        assert s.ev <= MAX_EV


def test_detect_value_tennis_enforces_cap():
    sigs = detect_value_tennis("A", "B", probs={"p_a": 0.99, "p_b": 0.01},
                               odds_a=5.0, odds_b=1.10, bankroll=100.0)
    for s in sigs:
        assert s.ev <= MAX_EV
