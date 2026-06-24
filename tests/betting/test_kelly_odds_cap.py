"""Tests für den Odds-Bucket-Cap in dynamic_stake_eur (Stake-v2)."""
import pytest

from src.betting.kelly import dynamic_stake_eur, odds_cap_factor


class TestOddsCapFactor:
    @pytest.mark.parametrize("odds,expected", [
        (1.50, 1.00),
        (2.00, 1.00),
        (2.01, 0.75),
        (3.00, 0.75),
        (3.01, 0.55),
        (5.00, 0.55),
        (5.01, 0.35),
        (10.0, 0.35),
    ])
    def test_factor_per_bucket(self, odds, expected):
        assert odds_cap_factor(odds) == expected

    def test_none_returns_one(self):
        assert odds_cap_factor(None) == 1.0

    def test_invalid_returns_one(self):
        assert odds_cap_factor(0) == 1.0
        assert odds_cap_factor(-1.0) == 1.0


class TestDynamicStakeWithOddsCap:
    def test_low_odds_unchanged(self):
        # 1.60 @ EV 15% / BR 100 → tier_hi=20, factor=1.0 → ~€18
        s_no_odds = dynamic_stake_eur(0.15, "MEDIUM", bankroll=100.0)
        s_with_odds = dynamic_stake_eur(0.15, "MEDIUM", bankroll=100.0, decimal_odds=1.60)
        assert abs(s_no_odds - s_with_odds) < 1e-9

    def test_longshot_capped_at_35pct(self):
        # 5.50 @ EV 15% / BR 100 → tier_hi=20, factor=0.35 → effective_hi=7, MIN=6
        # EV=15% → amount close to effective_hi
        stake = dynamic_stake_eur(0.15, "MEDIUM", bankroll=100.0, decimal_odds=5.50)
        assert stake <= 7.0 + 1e-9
        assert stake >= 6.0  # at least MIN

    def test_longshot_high_ev_still_capped(self):
        # 5.50 @ EV 30% (auto-clipped to 20%) + HIGH bonus → can't exceed effective_hi*1.10
        stake = dynamic_stake_eur(0.30, "HIGH", bankroll=100.0, decimal_odds=5.50)
        assert stake <= 7.0 + 1e-9  # cap holds even with HIGH

    def test_midrange_odds_55pct(self):
        # 4.0 @ EV 20% / BR 100 → tier_hi=20, factor=0.55 → effective_hi=11
        stake = dynamic_stake_eur(0.20, "MEDIUM", bankroll=100.0, decimal_odds=4.0)
        assert stake <= 11.0 + 1e-9

    def test_75pct_bucket(self):
        # 2.85 @ EV 20% / BR 100 → tier_hi=20, factor=0.75 → effective_hi=15
        stake = dynamic_stake_eur(0.20, "MEDIUM", bankroll=100.0, decimal_odds=2.85)
        assert stake <= 15.0 + 1e-9

    def test_higher_bankroll_tier_scales(self):
        # BR 300, tier_hi=30, factor=0.35 (odds=5.5) → effective_hi=10.5
        stake = dynamic_stake_eur(0.20, "MEDIUM", bankroll=300.0, decimal_odds=5.50)
        assert stake <= 10.5 + 1e-9

    def test_min_floor_respected(self):
        # MIN=6 at BR=100, even if effective_hi < MIN we don't fall below MIN
        # 100x odds → factor=0.35 → effective_hi=max(6, 20*0.35)=7 OK
        stake = dynamic_stake_eur(0.20, "MEDIUM", bankroll=100.0, decimal_odds=100.0)
        assert stake >= 6.0  # MIN holds
