"""Tests for src/tennis/draw_difficulty.py (N10)."""
from __future__ import annotations

import pytest

from src.tennis.draw_difficulty import draw_difficulty_adj, apply_draw_adj, bracket_to_opp_elos


def test_returns_zero_when_no_opponents():
    adj = draw_difficulty_adj(1700, 1600, [], [])
    assert adj == 0.0


def test_returns_zero_below_n_min():
    adj = draw_difficulty_adj(1700, 1600, [1800], [1500], n_min=2)
    assert adj == 0.0


def test_positive_when_b_draw_harder():
    """If B's opponents are much tougher relative to B's Elo → A gets positive adj."""
    # A: Elo 1700, faces [1720] — slightly harder (Δ+20)
    # B: Elo 1600, faces [1750] — much harder (Δ+150)
    adj = draw_difficulty_adj(1700, 1600, [1720, 1730], [1750, 1760])
    # B's draw is harder → A gets positive adjustment
    assert adj > 0.0


def test_negative_when_a_draw_harder():
    """If A's opponents are much tougher → A gets negative adj."""
    # A: Elo 1700, faces [1900, 1850] — Δ+175
    # B: Elo 1600, faces [1500, 1520] — Δ-90
    adj = draw_difficulty_adj(1700, 1600, [1900, 1850], [1500, 1520])
    assert adj < 0.0


def test_bounded_by_max_adj():
    """Adjustment must not exceed ±MAX_ADJ."""
    adj_pos = draw_difficulty_adj(1700, 1700, [1700] * 5, [2200] * 5)
    adj_neg = draw_difficulty_adj(1700, 1700, [2200] * 5, [1700] * 5)
    assert abs(adj_pos) <= 0.05
    assert abs(adj_neg) <= 0.05


def test_symmetric():
    """Swapping A and B negates the adjustment."""
    adj_ab = draw_difficulty_adj(1700, 1600, [1800, 1750], [1500, 1550])
    adj_ba = draw_difficulty_adj(1600, 1700, [1500, 1550], [1800, 1750])
    assert adj_ab == pytest.approx(-adj_ba, abs=1e-6)


def test_apply_draw_adj_normalizes():
    """After adjustment, p_a + p_b must sum to 1."""
    p_a, p_b = apply_draw_adj(0.60, 0.03)
    assert p_a + p_b == pytest.approx(1.0)
    assert p_a == pytest.approx(0.63)


def test_apply_draw_adj_clips_to_valid_range():
    """Extreme adjustments must be clipped to [0.01, 0.99]."""
    p_a, p_b = apply_draw_adj(0.98, 0.05)
    assert p_a <= 0.99
    assert p_b >= 0.01


def test_bracket_to_opp_elos_extracts_correctly():
    bracket = {
        "Alcaraz C.": {"quarter": "Q1", "opponents": ["Djokovic N.", "Medvedev D."]},
    }
    ratings = {"Djokovic N.": 1950.0, "Medvedev D.": 1900.0}
    elos = bracket_to_opp_elos(bracket, "Alcaraz C.", ratings)
    assert elos == [1950.0, 1900.0]


def test_bracket_to_opp_elos_returns_empty_for_unknown_player():
    elos = bracket_to_opp_elos({}, "Nobody", {})
    assert elos == []


def test_bracket_to_opp_elos_skips_missing_elo():
    bracket = {
        "Sinner J.": {"opponents": ["Unknown Player", "Zverev A."]},
    }
    ratings = {"Zverev A.": 1850.0}
    elos = bracket_to_opp_elos(bracket, "Sinner J.", ratings)
    assert elos == [1850.0]  # Unknown Player skipped
