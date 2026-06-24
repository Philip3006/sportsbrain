"""Tests für src.tennis.calibration (Roadmap J2-G)."""
from __future__ import annotations

import pytest

from src.tennis.calibration import (
    evaluate_game_markets,
    evaluate_set_markets,
    invert_p_match_to_p_set,
)


# ---- invert_p_match_to_p_set ----------------------------------------

def test_invert_bo3_05_returns_05():
    assert invert_p_match_to_p_set(0.5, 3) == pytest.approx(0.5, abs=0.02)


def test_invert_bo5_05_returns_05():
    assert invert_p_match_to_p_set(0.5, 5) == pytest.approx(0.5, abs=0.02)


def test_invert_high_p_match_above_05():
    p = invert_p_match_to_p_set(0.85, 3)
    assert 0.55 < p < 0.85


def test_invert_clamps_extremes():
    # Sollte nicht crashen, sondern auf [0.05, 0.95] clampen
    p = invert_p_match_to_p_set(0.001, 3)
    assert 0 < p < 0.5


# ---- evaluate_set_markets -------------------------------------------

def test_set_markets_bo3_score_2_0_actual():
    out = evaluate_set_markets(p_match=0.7, best_of=3, actual_wsets=2, actual_lsets=0)
    assert "o_u_sets_2.5_over" in out
    assert "score_2-0" in out
    # 2-0 ist die actual scoreline → actual=1
    assert out["score_2-0"]["actual"] == 1
    # 2-1 nicht actual → 0
    assert out["score_2-1"]["actual"] == 0


def test_set_markets_bo3_over_under_outcomes():
    # actual 2-1 → total=3 → over 2.5
    out = evaluate_set_markets(p_match=0.6, best_of=3, actual_wsets=2, actual_lsets=1)
    assert out["o_u_sets_2.5_over"]["actual"] == 1


def test_set_markets_bo5_three_sets_under():
    # actual 3-0 → total=3 → under 3.5
    out = evaluate_set_markets(p_match=0.7, best_of=5, actual_wsets=3, actual_lsets=0)
    assert out["o_u_sets_3.5_over"]["actual"] == 0


def test_set_markets_bo5_five_sets_over():
    out = evaluate_set_markets(p_match=0.55, best_of=5, actual_wsets=3, actual_lsets=2)
    assert out["o_u_sets_3.5_over"]["actual"] == 1


def test_set_markets_brier_sum_finite():
    out = evaluate_set_markets(p_match=0.6, best_of=3, actual_wsets=2, actual_lsets=1)
    for v in out.values():
        assert 0.0 <= v["brier_term"] <= 1.0


# ---- evaluate_game_markets ------------------------------------------

class _MockRow(dict):
    """dict-Wrapper für row.get(...) Kompatibilität."""


def test_game_markets_returns_none_without_scores():
    row = _MockRow({})
    out = evaluate_game_markets(0.6, 3, "atp", row)
    assert out is None


def test_game_markets_with_bo3_scores():
    row = _MockRow({"W1": 6, "L1": 4, "W2": 6, "L2": 3})
    out = evaluate_game_markets(0.6, 3, "atp", row)
    assert out is not None
    assert "o_u_games_21.5_over" in out
    # total = 6+4+6+3 = 19 → under 21.5 → actual=0
    assert out["o_u_games_21.5_over"]["actual"] == 0


def test_game_markets_bo5_over_threshold():
    row = _MockRow({"W1": 7, "L1": 6, "W2": 6, "L2": 7, "W3": 7, "L3": 6,
                    "W4": 6, "L4": 7, "W5": 7, "L5": 5})
    out = evaluate_game_markets(0.55, 5, "atp", row)
    assert out is not None
    # total = 13+13+13+13+12 = 64 → > 38.5
    assert out["o_u_games_38.5_over"]["actual"] == 1


def test_game_markets_partial_scores_still_used():
    # Nur 2 von 5 Sätzen befüllt — die ersten beiden, Match endete 6:4 6:3
    row = _MockRow({"W1": 6, "L1": 4, "W2": 6, "L2": 3,
                    "W3": None, "L3": None})
    out = evaluate_game_markets(0.6, 3, "wta", row)
    assert out is not None
    assert out["o_u_games_21.5_over"]["actual"] == 0
