import pytest
from src.betting.kelly import (
    dynamic_stake_eur,
    expected_value,
    get_stake_bounds,
    goals_range_max_for,
    kelly_fraction,
    stake,
)
from src.config import KELLY_FRAC, MIN_STAKE_EUR, MAX_STAKE_EUR


class TestKellyFraction:
    def test_zero_when_no_edge(self):
        # EV = 0.3 * 3.0 - 1 = -0.1 → no edge
        assert kelly_fraction(0.3, 3.0) == 0.0

    def test_zero_when_negative_ev(self):
        assert kelly_fraction(0.2, 2.0) == 0.0

    def test_positive_with_edge(self):
        # EV = 0.4 * 3.0 - 1 = 0.2 → positive Kelly
        kf = kelly_fraction(0.4, 3.0)
        assert kf > 0

    def test_fractional_scaling(self):
        kf_full = kelly_fraction(0.5, 2.5, fraction=1.0)
        kf_quarter = kelly_fraction(0.5, 2.5, fraction=0.25)
        assert abs(kf_quarter - kf_full * 0.25) < 1e-9

    def test_respects_fraction_default(self):
        kf = kelly_fraction(0.5, 2.5)
        kf_explicit = kelly_fraction(0.5, 2.5, fraction=KELLY_FRAC)
        assert abs(kf - kf_explicit) < 1e-9

    def test_invalid_odds_returns_zero(self):
        assert kelly_fraction(0.5, 1.0) == 0.0  # b = 0


class TestDynamicStakeEur:
    def test_min_at_low_ev(self):
        assert dynamic_stake_eur(0.03, "MEDIUM") == MIN_STAKE_EUR

    def test_max_at_high_ev(self):
        assert dynamic_stake_eur(0.20, "MEDIUM") == MAX_STAKE_EUR

    def test_artifact_ev_capped(self):
        # EV > 20% must not exceed MAX_STAKE_EUR
        assert dynamic_stake_eur(0.50, "MEDIUM") == MAX_STAKE_EUR

    def test_scales_monotonically(self):
        s1 = dynamic_stake_eur(0.05, "MEDIUM")
        s2 = dynamic_stake_eur(0.10, "MEDIUM")
        s3 = dynamic_stake_eur(0.15, "MEDIUM")
        assert s1 < s2 < s3

    def test_high_confidence_bonus(self):
        medium = dynamic_stake_eur(0.10, "MEDIUM")
        high = dynamic_stake_eur(0.10, "HIGH")
        assert high > medium
        assert high <= MAX_STAKE_EUR


class TestStake:
    def test_respects_max_cap(self):
        # kelly_f=0.10, bankroll=10_000, max_eur=200 → capped at 200
        assert stake(0.10, 10_000, max_eur=200.0) == 200.0

    def test_below_cap(self):
        # kelly_f=0.01, bankroll=1000, max_eur=20 → 10.0
        assert stake(0.01, 1000, max_eur=20.0) == 10.0

    def test_zero_kelly_returns_zero(self):
        assert stake(0.0, 1000) == 0.0

    def test_zero_bankroll_returns_zero(self):
        assert stake(0.05, 0.0) == 0.0


class TestStakeBounds:
    @pytest.mark.parametrize("bankroll,expected", [
        (0.0,    (5.0, 15.0)),
        (50.0,   (5.0, 15.0)),
        (99.99,  (5.0, 15.0)),
        (100.0,  (6.0, 20.0)),
        (150.0,  (6.0, 20.0)),
        (199.99, (6.0, 20.0)),
        (200.0,  (7.0, 25.0)),
        (299.99, (7.0, 25.0)),
        (300.0,  (8.0, 30.0)),
        (400.0, (10.0, 35.0)),
        (500.0, (12.0, 40.0)),
        (1000.0,(12.0, 40.0)),
    ])
    def test_per_tier_bounds(self, bankroll, expected):
        assert get_stake_bounds(bankroll) == expected


class TestDynamicStakeBankroll:
    def test_default_is_tier_0(self):
        assert dynamic_stake_eur(0.03, "MEDIUM") == MIN_STAKE_EUR
        assert dynamic_stake_eur(0.20, "MEDIUM") == MAX_STAKE_EUR

    def test_explicit_tier_0_matches_default(self):
        assert dynamic_stake_eur(0.10, "MEDIUM", bankroll=50.0) == \
            dynamic_stake_eur(0.10, "MEDIUM")

    def test_max_scales_per_tier(self):
        # At EV=20% the stake hits the tier MAX
        assert dynamic_stake_eur(0.20, "MEDIUM", bankroll=100.0) == 20.0
        assert dynamic_stake_eur(0.20, "MEDIUM", bankroll=175.0) == 20.0
        assert dynamic_stake_eur(0.20, "MEDIUM", bankroll=250.0) == 25.0
        assert dynamic_stake_eur(0.20, "MEDIUM", bankroll=350.0) == 30.0
        assert dynamic_stake_eur(0.20, "MEDIUM", bankroll=600.0) == 40.0

    def test_min_scales_per_tier(self):
        assert dynamic_stake_eur(0.03, "MEDIUM", bankroll=175.0) == 6.0
        assert dynamic_stake_eur(0.03, "MEDIUM", bankroll=250.0) == 7.0
        assert dynamic_stake_eur(0.03, "MEDIUM", bankroll=350.0) == 8.0

    def test_high_bonus_respects_tier_cap(self):
        # EV=20% already at MAX → HIGH must NOT exceed tier cap
        assert dynamic_stake_eur(0.20, "HIGH", bankroll=175.0) == 20.0
        assert dynamic_stake_eur(0.20, "HIGH", bankroll=250.0) == 25.0

    def test_high_bonus_applies_below_cap(self):
        # At lower EV, HIGH is +10% over MEDIUM
        m = dynamic_stake_eur(0.05, "MEDIUM", bankroll=175.0)
        h = dynamic_stake_eur(0.05, "HIGH", bankroll=175.0)
        assert abs(h - m * 1.10) < 1e-9

    def test_negative_bankroll_falls_back_to_lowest_tier(self):
        # Defensive: shouldn't happen but must not crash
        assert dynamic_stake_eur(0.20, "MEDIUM", bankroll=-50.0) == 15.0


class TestGoalsRangeCap:
    def test_tier_0(self):
        # 5 + 0.2 * (15-5) = 7
        assert abs(goals_range_max_for(50.0) - 7.0) < 1e-9

    def test_tier_1(self):
        # 6 + 0.2 * (20-6) = 8.8
        assert abs(goals_range_max_for(175.0) - 8.8) < 1e-9

    def test_tier_2(self):
        # 7 + 0.2 * (25-7) = 10.6
        assert abs(goals_range_max_for(250.0) - 10.6) < 1e-9

    def test_default_is_tier_0(self):
        assert abs(goals_range_max_for(None) - 7.0) < 1e-9


class TestExpectedValue:
    def test_positive_ev(self):
        ev = expected_value(0.5, 2.5)
        assert abs(ev - 0.25) < 1e-9

    def test_zero_ev(self):
        ev = expected_value(0.5, 2.0)
        assert abs(ev) < 1e-9

    def test_negative_ev(self):
        ev = expected_value(0.3, 2.0)
        assert ev < 0
