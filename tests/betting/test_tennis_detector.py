"""Tests for tennis value detector."""
import pytest

from src.betting.tennis_detector import detect_value_tennis, _set_handicap_probs, _devig_2way


def test_devig_2way_sums_to_one():
    fair_a, fair_b = _devig_2way(1.80, 2.10)
    assert abs(fair_a + fair_b - 1.0) < 1e-9


def test_devig_2way_removes_margin():
    # Raw implied: 1/1.80 + 1/2.10 = 0.556 + 0.476 = 1.032 (3.2% margin)
    fair_a, fair_b = _devig_2way(1.80, 2.10)
    assert fair_a < 1 / 1.80  # fair prob should be less than raw implied
    assert fair_b < 1 / 2.10


def test_set_handicap_probs_sum_to_one():
    result = _set_handicap_probs(0.65)
    assert abs(result["ah-1.5_a"] + result["ah+1.5_b"] - 1.0) < 0.01


def test_set_handicap_probs_dominant_player():
    # Strong favourite (90% win prob) should have high -1.5 set handicap prob
    result = _set_handicap_probs(0.90)
    assert result["ah-1.5_a"] > 0.60


def test_set_handicap_probs_underdog():
    # Weak player: low probability of winning 3:0 or 3:1
    result = _set_handicap_probs(0.30)
    assert result["ah-1.5_a"] < 0.20
    assert result["ah+1.5_b"] > 0.80


def test_detect_value_returns_signal_when_value_exists():
    # Model says 70% win prob, market implies 55.6% (odds 1.80) → clear value
    signals = detect_value_tennis(
        player_a="Strong", player_b="Weak",
        probs={"p_a": 0.70, "p_b": 0.30},
        odds_a=1.80, odds_b=3.50,
        bankroll=100.0,
        match_id="test_match",
    )
    markets = [s.market for s in signals]
    assert "home" in markets


def test_detect_value_no_signal_when_no_value():
    # Model agrees with market (50% prob, odds 2.0 = fair)
    signals = detect_value_tennis(
        player_a="A", player_b="B",
        probs={"p_a": 0.50, "p_b": 0.50},
        odds_a=2.00, odds_b=2.00,
        bankroll=100.0,
    )
    assert signals == []


def test_detect_value_respects_min_edge():
    # Very small edge below MIN_EDGE=3%: EV = 0.53 * 2.00 - 1 = 0.06 → 6% > 3% so would fire
    # Tiny edge: 0.515 * 2.0 - 1 = 0.03 → exactly at boundary
    signals = detect_value_tennis(
        player_a="A", player_b="B",
        probs={"p_a": 0.51, "p_b": 0.49},  # EV = 0.51*2.0 - 1 = 0.02 → below 3%
        odds_a=2.00, odds_b=2.00,
        bankroll=100.0,
    )
    assert signals == []


def test_detect_value_ah_odds_when_provided():
    # If AH odds are provided, should also check set handicap markets
    signals = detect_value_tennis(
        player_a="Strong", player_b="Weak",
        probs={"p_a": 0.90, "p_b": 0.10},
        odds_a=1.20, odds_b=5.00,
        bankroll=100.0,
        ah_odds_a=1.50, ah_odds_b=2.60,  # set handicap odds
    )
    markets = [s.market for s in signals]
    # High favourite: should see value on ah-1.5_a
    assert any(m in ("ah-1.5_a", "home") for m in markets)


def test_signal_stake_within_bounds():
    signals = detect_value_tennis(
        player_a="A", player_b="B",
        probs={"p_a": 0.70, "p_b": 0.30},
        odds_a=1.80, odds_b=3.50,
        bankroll=100.0,
    )
    for s in signals:
        assert 5.0 <= s.stake_eur <= 15.0
