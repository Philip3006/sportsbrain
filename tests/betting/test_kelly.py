import pytest
from src.betting.kelly import expected_value, kelly_fraction, stake
from src.config import KELLY_FRAC, MAX_STAKE_PCT


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


class TestStake:
    def test_respects_max_cap(self):
        # kelly_f=0.10, bankroll=10_000, max=0.02 → capped at 200
        assert stake(0.10, 10_000, max_pct=0.02) == 200.0

    def test_below_cap(self):
        # kelly_f=0.01, bankroll=1000, max=0.02 → 10.0
        assert stake(0.01, 1000, max_pct=0.02) == 10.0

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
