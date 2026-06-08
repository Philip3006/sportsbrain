import pytest
from src.betting.kelly import dynamic_stake_eur, expected_value, kelly_fraction, stake
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
