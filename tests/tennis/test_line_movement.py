"""Tests für src/tennis/line_movement.py (Hebel 4)."""
from src.tennis.line_movement import compute_line_move, line_move_confirms_edge


def test_line_move_odds_dropped_positive():
    lm = compute_line_move(2.10, 1.85)
    assert lm is not None
    assert lm.move_pct > 0
    assert lm.sharp_direction == 1
    assert lm.implied_move_pp > 0


def test_line_move_odds_rose_negative():
    lm = compute_line_move(1.80, 2.10)
    assert lm is not None
    assert lm.move_pct < 0
    assert lm.sharp_direction == -1


def test_line_move_neutral_below_threshold():
    lm = compute_line_move(2.00, 1.98)
    assert lm is not None
    assert lm.sharp_direction == 0


def test_line_move_invalid_returns_none():
    assert compute_line_move(1.0, 1.5) is None
    assert compute_line_move(None, 1.5) is None
    assert compute_line_move(2.0, 1.0) is None


def test_confirms_edge_positive_backed_by_market():
    lm = compute_line_move(2.10, 1.85)  # market backs A
    assert line_move_confirms_edge(our_p_a=0.60, market_p_a=0.55, lm_a=lm) is True


def test_confirms_edge_rejected_when_market_against():
    lm = compute_line_move(1.80, 2.10)  # market against A
    assert line_move_confirms_edge(our_p_a=0.60, market_p_a=0.55, lm_a=lm) is False


def test_confirms_edge_no_edge_rejected():
    lm = compute_line_move(2.00, 1.85)
    assert line_move_confirms_edge(our_p_a=0.55, market_p_a=0.55, lm_a=lm) is False
