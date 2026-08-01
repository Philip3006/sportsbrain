"""J8-I8: Tennis Live In-Play — Wahrscheinlichkeits-Rekursion + Value-Signals."""
from __future__ import annotations

from src.tennis.live.inplay_model import (
    LiveMatchState,
    predict_live_p_a,
    value_live_signal,
)


def _neutral_state(**kw) -> LiveMatchState:
    base = dict(
        p_pre_match_a=0.5, sets_a=0, sets_b=0,
        current_set_games_a=0, current_set_games_b=0,
        server="a", best_of=3, tour="atp", recent_games_a=3,
    )
    base.update(kw)
    return LiveMatchState(**base)


def test_start_state_reflects_pre_match():
    s = _neutral_state(p_pre_match_a=0.65)
    p = predict_live_p_a(s)
    # Pre-Match 65% + kleine Server-Boost → 0.55..0.80
    assert 0.55 < p < 0.85


def test_leading_by_two_sets_wins_all():
    s = _neutral_state(p_pre_match_a=0.5, sets_a=2, sets_b=0, best_of=3)
    assert predict_live_p_a(s) == 1.0


def test_trailing_by_two_sets_loses_all():
    s = _neutral_state(p_pre_match_a=0.5, sets_a=0, sets_b=2, best_of=3)
    assert predict_live_p_a(s) == 0.0


def test_bo5_final_set_symmetric():
    # BO5, 2:2 in Sets, 0:0 im Decider, kein Momentum → nah 50/50
    s = _neutral_state(p_pre_match_a=0.5, sets_a=2, sets_b=2,
                       current_set_games_a=0, current_set_games_b=0,
                       best_of=5)
    p = predict_live_p_a(s)
    # Nur Server-Boost von +0.02 sollte greifen
    assert 0.45 < p < 0.60


def test_break_lead_boosts_probability():
    fav_no_break = _neutral_state(p_pre_match_a=0.55)
    fav_with_break = _neutral_state(p_pre_match_a=0.55,
                                     current_set_games_a=4, current_set_games_b=1)
    assert predict_live_p_a(fav_with_break) > predict_live_p_a(fav_no_break)


def test_momentum_effect():
    cold = _neutral_state(recent_games_a=0)
    hot = _neutral_state(recent_games_a=6)
    assert predict_live_p_a(hot) > predict_live_p_a(cold)


def test_value_signal_returns_when_edge_positive():
    s = _neutral_state(p_pre_match_a=0.65, sets_a=1, sets_b=0)
    live_p = predict_live_p_a(s)
    # Odds slightly worse than fair → clear edge
    fair_odds = 1.0 / live_p if live_p > 0.05 else 5.0
    sigs = value_live_signal(s, live_odds_a=fair_odds * 1.15,
                              live_odds_b=fair_odds * 0.5)
    assert len(sigs) >= 1
    assert sigs[0]["side"] == "a"


def test_value_signal_empty_when_odds_below_edge():
    s = _neutral_state(p_pre_match_a=0.5)
    # Odds sehr niedrig auf beiden Seiten → keine Value
    sigs = value_live_signal(s, live_odds_a=1.05, live_odds_b=1.05)
    assert sigs == []
