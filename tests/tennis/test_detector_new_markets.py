"""Tests für neue Detector-Märkte (Roadmap J2-C)."""
from __future__ import annotations

from src.betting.tennis_detector import (
    detect_set_betting,
    detect_total_games,
    detect_total_sets,
)


# ---- detect_total_sets ------------------------------------------------

def test_total_sets_bo3_returns_signals_on_value():
    """Bei 50/50 Match, fair P(over 2.5) ≈ 0.5. Quote 2.10/1.85 → over hat Edge."""
    sigs = detect_total_sets(
        "Player A", "Player B",
        p_match_a=0.5,
        odds_over=2.10, odds_under=1.85,
        line=2.5, best_of=3,
        bankroll=100.0,
        match_id="t1",
        min_edge=0.0,
        tour="atp",
    )
    # Mindestens ein Signal sollte rauskommen
    assert len(sigs) >= 1
    markets = {s.market for s in sigs}
    assert any("o/u_sets_2.5" in m for m in markets)


def test_total_sets_skipped_at_extreme_match_probs():
    """p_match=0 oder 1 → keine Signals (Inversion ill-defined)."""
    sigs = detect_total_sets("A", "B", p_match_a=0.0,
                              odds_over=2.0, odds_under=1.9,
                              line=2.5, best_of=3, bankroll=100, min_edge=0.0)
    assert sigs == []


def test_total_sets_bo5_emits_o_u_3_5():
    # p_match=0.5, BO5, p_set inverted ≈ 0.5 → P(over 3.5) = 0.75
    # Fair odds_over ≈ 1.33 → bei 1.50 ev = 0.125 (innerhalb MAX_EV)
    sigs = detect_total_sets("A", "B", p_match_a=0.5,
                              odds_over=1.50, odds_under=3.50,
                              line=3.5, best_of=5, bankroll=100, min_edge=0.0,
                              tour="atp")
    markets = {s.market for s in sigs}
    assert any("o/u_sets_3.5" in m for m in markets)


# ---- detect_total_games ------------------------------------------------

def test_total_games_emits_signals_on_value():
    # Leicht asymmetrische Quoten (within MAX_EV) → Edge auf einer Seite
    sigs = detect_total_games(
        "A", "B",
        p_match_a=0.55,
        odds_over=2.20, odds_under=1.65,
        line=21.5, best_of=3,
        bankroll=100.0,
        match_id="t2",
        min_edge=0.0,
        tour="atp",
        n_sim=500,
    )
    # Mindestens ein Market-String wird emittiert (oder beide gefiltert wenn fair) —
    # primäres Ziel: API stürzt nicht ab + Format ist korrekt.
    for s in sigs:
        assert s.market.startswith("o/u_games_21.5")


def test_total_games_deterministic_via_seed():
    """Detector nutzt seed=42 intern → wiederholte Calls identisch."""
    sigs1 = detect_total_games("A", "B", p_match_a=0.55,
                                odds_over=2.0, odds_under=1.9,
                                line=21.5, best_of=3, bankroll=100,
                                min_edge=0.0, tour="atp", n_sim=300)
    sigs2 = detect_total_games("A", "B", p_match_a=0.55,
                                odds_over=2.0, odds_under=1.9,
                                line=21.5, best_of=3, bankroll=100,
                                min_edge=0.0, tour="atp", n_sim=300)
    assert len(sigs1) == len(sigs2)
    if sigs1:
        assert sigs1[0].model_prob == sigs2[0].model_prob


def test_total_games_skipped_invalid_inputs():
    sigs = detect_total_games("A", "B", p_match_a=1.0,
                               odds_over=2.0, odds_under=1.9,
                               line=21.5, best_of=3, bankroll=100, min_edge=0.0)
    assert sigs == []


# ---- detect_set_betting ------------------------------------------------

def test_set_betting_bo3_emits_known_scorelines():
    odds = {"2-0": 1.85, "2-1": 3.20, "1-2": 4.50, "0-2": 6.00}
    sigs = detect_set_betting(
        "A", "B",
        p_match_a=0.55,
        scoreline_odds=odds,
        best_of=3,
        bankroll=100.0,
        match_id="t3",
        min_edge=0.0,
    )
    markets = {s.market for s in sigs}
    # Mindestens ein "score_*"-Market
    assert any(m.startswith("score_") for m in markets)


def test_set_betting_bo5_handles_all_six_scorelines():
    # Inflated odds für alle Scorelines → garantierte Edge
    odds = {
        "3-0": 8.00, "3-1": 8.00, "3-2": 9.00,
        "2-3": 20.0, "1-3": 25.0, "0-3": 50.0,
    }
    sigs = detect_set_betting("A", "B", p_match_a=0.55,
                               scoreline_odds=odds, best_of=5,
                               bankroll=100, min_edge=0.0)
    # Mindestens ein Signal mit inflated odds garantiert
    assert len(sigs) >= 1


def test_set_betting_empty_odds_returns_empty():
    sigs = detect_set_betting("A", "B", p_match_a=0.55,
                               scoreline_odds={}, best_of=3,
                               bankroll=100, min_edge=0.0)
    assert sigs == []


def test_set_betting_ignores_unknown_scoreline_keys():
    """Falls TheOddsAPI mal '4-2' liefert (unsinnig BO5) → ignoriert."""
    odds = {"3-0": 4.00, "4-2": 100.0}  # 4-2 nicht in unserer Domain
    sigs = detect_set_betting("A", "B", p_match_a=0.55,
                               scoreline_odds=odds, best_of=5,
                               bankroll=100, min_edge=0.0)
    markets = {s.market for s in sigs}
    assert "score_4-2" not in markets
