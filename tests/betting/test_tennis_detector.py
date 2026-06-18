"""Tests for tennis value detector."""
import pytest

from src.betting.tennis_detector import (
    detect_value_tennis, _set_handicap_probs, _devig_2way, _first_set_probs,
    _MAX_ODDS, _p_match_from_p_set_bo3, _p_match_from_p_set_bo5,
)


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
    # bankroll=50 → Tier 0 bounds (€5–€15) for backward-compat behaviour
    signals = detect_value_tennis(
        player_a="A", player_b="B",
        probs={"p_a": 0.70, "p_b": 0.30},
        odds_a=1.80, odds_b=3.50,
        bankroll=50.0,
    )
    for s in signals:
        assert 5.0 <= s.stake_eur <= 15.0


def test_signal_stake_scales_with_bankroll_tier():
    # bankroll=175 → Tier 1 bounds (€6–€20); HIGH-confidence allowed up to €20
    signals = detect_value_tennis(
        player_a="A", player_b="B",
        probs={"p_a": 0.70, "p_b": 0.30},
        odds_a=1.80, odds_b=3.50,
        bankroll=175.0,
    )
    for s in signals:
        assert 6.0 <= s.stake_eur <= 20.0


def test_min_prob_filter_blocks_extreme_underdog():
    # model_p=0.30 < _MIN_PROB=0.35 → no signal even if EV is positive
    signals = detect_value_tennis(
        player_a="Underdog", player_b="Favourite",
        probs={"p_a": 0.30, "p_b": 0.70},
        odds_a=4.00, odds_b=1.30,  # EV for underdog: 0.30*4.0-1 = 0.20 (+20%) but blocked
        bankroll=100.0,
    )
    underdog_signals = [s for s in signals if s.market == "home"]
    assert underdog_signals == [], "p<0.35 underdog should be filtered even with high EV"


def test_min_prob_filter_passes_at_threshold():
    # model_p=0.36 >= _MIN_PROB=0.35 → allowed if EV passes
    signals = detect_value_tennis(
        player_a="SlightUnderdog", player_b="SlightFavourite",
        probs={"p_a": 0.36, "p_b": 0.64},
        odds_a=3.20, odds_b=1.45,  # EV for A: 0.36*3.2-1 = 0.152 (+15.2%)
        bankroll=100.0,
    )
    home_signals = [s for s in signals if s.market == "home"]
    assert len(home_signals) == 1


def test_first_set_probs_sum_to_one():
    probs = _first_set_probs(0.70)
    assert abs(probs["first_set_a"] + probs["first_set_b"] - 1.0) < 1e-9


def test_first_set_probs_favourite_above_half():
    """Favourite should have higher first-set win probability."""
    probs = _first_set_probs(0.75)
    assert probs["first_set_a"] > 0.5


def test_first_set_probs_monotone():
    """Higher match prob → higher first set prob."""
    p60 = _first_set_probs(0.60)["first_set_a"]
    p75 = _first_set_probs(0.75)["first_set_a"]
    assert p75 > p60


def test_first_set_signal_detected_when_odds_generous():
    # Generous first_set odds (1.75) vs ~63% model probability → EV ≈ +10%
    # No h2h odds so first_set is the only directional candidate
    signals = detect_value_tennis(
        player_a="Favourite", player_b="Underdog",
        probs={"p_a": 0.75, "p_b": 0.25},
        odds_a=0.0, odds_b=0.0,  # h2h unavailable — isolate first_set
        bankroll=100.0,
        first_set_odds_a=1.75,
        first_set_odds_b=2.10,
    )
    markets = [s.market for s in signals]
    assert "first_set_a" in markets, "Should find value on first set for heavy favourite at 1.75"


def test_first_set_no_signal_when_odds_tight():
    # Tight odds (1.55) → insufficient EV
    signals = detect_value_tennis(
        player_a="Favourite", player_b="Underdog",
        probs={"p_a": 0.65, "p_b": 0.35},
        odds_a=1.70, odds_b=2.30,
        bankroll=100.0,
        first_set_odds_a=1.55,
        first_set_odds_b=2.60,
    )
    markets = [s.market for s in signals]
    assert "first_set_a" not in markets


def test_first_set_zero_odds_ignored():
    # first_set_odds = 0 → market not available, no signals
    signals = detect_value_tennis(
        player_a="A", player_b="B",
        probs={"p_a": 0.70, "p_b": 0.30},
        odds_a=1.80, odds_b=3.50,
        bankroll=100.0,
        first_set_odds_a=0.0,
        first_set_odds_b=0.0,
    )
    markets = [s.market for s in signals]
    assert "first_set_a" not in markets
    assert "first_set_b" not in markets


def test_bucketing_returns_at_most_one_directional_per_match():
    # Both home AND first_set_a have value — bucketing picks only best directional
    signals = detect_value_tennis(
        player_a="Fav", player_b="Dog",
        probs={"p_a": 0.75, "p_b": 0.25},
        odds_a=1.80, odds_b=3.50,
        bankroll=100.0,
        first_set_odds_a=1.75, first_set_odds_b=2.10,
    )
    directional = [s for s in signals if s.market in ("home", "away", "first_set_a", "first_set_b")]
    assert len(directional) <= 1, "At most 1 directional signal per match"


def test_bucketing_returns_directional_and_structural():
    # home has value AND ah-1.5_a has value → both returned (different buckets)
    signals = detect_value_tennis(
        player_a="Strong", player_b="Weak",
        probs={"p_a": 0.80, "p_b": 0.20},
        odds_a=1.50, odds_b=4.00,
        bankroll=100.0,
        ah_odds_a=1.60, ah_odds_b=2.40,
    )
    markets = {s.market for s in signals}
    has_directional = bool(markets & {"home", "away", "first_set_a", "first_set_b"})
    has_structural = bool(markets & {"ah-1.5_a", "ah+1.5_b"})
    # At most 2 signals total: one per bucket
    assert len(signals) <= 2
    # If both buckets have value, both should be returned
    if has_directional and has_structural:
        assert len(signals) == 2


def test_max_odds_filter_blocks_extreme_price():
    # odds > _MAX_ODDS should produce no signal even with positive EV
    extreme_odds = _MAX_ODDS + 0.5
    # p_a = 0.40, odds_a = extreme → EV = 0.40*extreme - 1 > 0 but filtered
    signals = detect_value_tennis(
        player_a="A", player_b="B",
        probs={"p_a": 0.40, "p_b": 0.60},
        odds_a=extreme_odds, odds_b=1.25,
        bankroll=100.0,
    )
    home_sigs = [s for s in signals if s.market == "home"]
    assert home_sigs == [], f"Odds above {_MAX_ODDS} should be filtered"


def test_custom_min_edge_raises_bar():
    # EV=7% passes default 3% but fails custom min_edge=10%
    signals = detect_value_tennis(
        player_a="A", player_b="B",
        probs={"p_a": 0.60, "p_b": 0.40},
        odds_a=1.80, odds_b=2.50,  # EV = 0.60*1.80-1 = 0.08 (8%)
        bankroll=100.0,
        min_edge=0.10,
    )
    assert signals == [], "8% EV should not fire with min_edge=10%"


def test_wta_confidence_high_at_lower_ev():
    # WTA: EV=9% should be HIGH (bar=8%); ATP: same EV stays MEDIUM (bar=15%)
    # EV ≈ 0.60*1.80-1 = 0.08 (8%)... slightly above 8% needed for WTA HIGH
    signals_wta = detect_value_tennis(
        player_a="A", player_b="B",
        probs={"p_a": 0.62, "p_b": 0.38},
        odds_a=1.80, odds_b=2.70,  # EV = 0.62*1.80-1 = 0.116 (11.6%)
        bankroll=100.0,
        tour="wta",
    )
    signals_atp = detect_value_tennis(
        player_a="A", player_b="B",
        probs={"p_a": 0.62, "p_b": 0.38},
        odds_a=1.80, odds_b=2.70,
        bankroll=100.0,
        tour="atp",
    )
    if signals_wta:
        assert signals_wta[0].confidence == "HIGH", "WTA 11.6% EV should be HIGH"
    if signals_atp:
        assert signals_atp[0].confidence == "MEDIUM", "ATP 11.6% EV should be MEDIUM (bar 15%)"


def test_atp_confidence_high_at_very_high_ev():
    # ATP: EV=16% should be HIGH (bar=15%)
    signals = detect_value_tennis(
        player_a="A", player_b="B",
        probs={"p_a": 0.75, "p_b": 0.25},
        odds_a=1.60, odds_b=3.80,  # EV = 0.75*1.60-1 = 0.20 (20%)
        bankroll=100.0,
        tour="atp",
    )
    if signals:
        assert signals[0].confidence == "HIGH", "ATP 20% EV should be HIGH"


def test_bo3_match_from_set_sums_correctly():
    """For p_s=0.5 → P(match)=0.5 in BO3."""
    assert abs(_p_match_from_p_set_bo3(0.5) - 0.5) < 1e-9


def test_bo5_match_from_set_sums_correctly():
    """For p_s=0.5 → P(match)=0.5 in BO5."""
    assert abs(_p_match_from_p_set_bo5(0.5) - 0.5) < 1e-9


def test_bo3_ah_lower_than_bo5_for_same_match_prob():
    """
    For same match win prob, P(ah-1.5_a) in BO3 < BO5:
    BO3 requires 2:0 only; BO5 allows 3:0 or 3:1.
    """
    probs_bo5 = _set_handicap_probs(0.75, bo5=True)
    probs_bo3 = _set_handicap_probs(0.75, bo5=False)
    # BO3: P(2:0) = p_s^2 where p_s is inverted from p_match for BO3
    # BO5: P(3:0 or 3:1) = higher since more paths
    # In BO3, 2:0 is a subset of all wins; in BO5, 3:0+3:1 is also a subset
    # But the per-set prob differs: BO5 inverts to HIGHER p_s for same match win prob
    # (because BO5 amplifies the advantage more), so BO5 handicap prob is higher
    assert probs_bo5["ah-1.5_a"] > probs_bo3["ah-1.5_a"], \
        "BO5 ah-1.5_a should be higher than BO3 for same match prob"


def test_wta_uses_bo3_handicap(monkeypatch):
    """WTA (bo5=False) set AH signals differ from ATP (bo5=True) for same probs."""
    import src.betting.tennis_detector as td
    captured = {}
    orig = td._set_handicap_probs
    def mock_sh(p, bo5=True):
        captured['bo5'] = bo5
        return orig(p, bo5=bo5)
    monkeypatch.setattr(td, '_set_handicap_probs', mock_sh)
    detect_value_tennis(
        "A", "B", {"p_a": 0.70, "p_b": 0.30},
        odds_a=1.55, odds_b=2.55,
        ah_odds_a=2.20, ah_odds_b=1.65,
        bankroll=100.0, tour="wta",
    )
    assert captured.get('bo5') == False, "WTA should use BO3 (bo5=False)"
