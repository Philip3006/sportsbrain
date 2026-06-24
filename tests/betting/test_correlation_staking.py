"""Tests für Stake-v2 Korrelations-Adjustment."""
from src.betting.correlation import apply_correlation_adjustments
from src.betting.value_detector import BetSignal


def _sig(market, stake_eur, odds=2.0, model_prob=0.5, match_id="m1", player_team=""):
    return BetSignal(
        match_id=match_id,
        home="Czechia", away="Mexico",
        market=market,
        model_prob=model_prob,
        fair_prob=0.5,
        decimal_odds=odds,
        ev=0.10,
        kelly_f=0.05,
        stake_pct=stake_eur / 100.0,
        confidence="MEDIUM",
        stake_eur=stake_eur,
        player_team=player_team,
    )


class TestNegativeCorrelation:
    def test_mexico_win_vs_czech_scorer(self):
        # ah-0.5_away (Mexico covers) = away-side; scorer (Hložek, Czech) = home-side
        mex_ah = _sig("ah-0.5_away", stake_eur=8.0)
        scorer = _sig("scorer_Adam Hložek", stake_eur=2.0, odds=5.5, player_team="home")
        out = apply_correlation_adjustments([mex_ah, scorer], bankroll=100.0)
        # home-side total (€2) < away-side total (€8) → home leg reduced
        scorer_out = [s for s in out if s.market.startswith("scorer_")][0]
        assert scorer_out.stake_eur < 2.0
        assert "neg_corr" in scorer_out.stake_reason

    def test_no_adjustment_when_one_side(self):
        # Two signals both on away side → no neg-corr
        a = _sig("away", stake_eur=8.0)
        b = _sig("ah-0.5_away", stake_eur=6.0)
        out = apply_correlation_adjustments([a, b], bankroll=100.0)
        # Match cap = 1.5 * 20 = 30 → 8+6=14 not affected
        assert all(s.stake_eur in (8.0, 6.0) for s in out)
        assert all(s.stake_reason == "" for s in out)


class TestPositiveCorrelation:
    def test_away_win_plus_over_correlated(self):
        # Korea-Sieg + Over 3.0 mit Modell p_over > 0.55 → beide × 0.70
        korea = _sig("away", stake_eur=6.0, model_prob=0.50)
        over = _sig("o/u3.0_over", stake_eur=4.0, odds=2.85, model_prob=0.60)
        out = apply_correlation_adjustments([korea, over], bankroll=100.0)
        for s in out:
            assert s.stake_eur < 6.0  # both reduced
            assert "pos_corr" in s.stake_reason

    def test_low_p_over_no_pos_corr(self):
        # Over mit Modell p_over = 0.50 → kein Trigger
        korea = _sig("away", stake_eur=6.0, model_prob=0.50)
        over = _sig("o/u3.0_over", stake_eur=4.0, odds=2.85, model_prob=0.50)
        out = apply_correlation_adjustments([korea, over], bankroll=100.0)
        assert all(s.stake_reason == "" for s in out)


class TestMatchExposureCap:
    def test_three_legs_proportionally_scaled(self):
        # BR 100 → tier_hi=20, cap=30. Drei Legs Σ=45 → factor=30/45
        a = _sig("home", stake_eur=15.0, match_id="m1")
        b = _sig("o/u2.5_over", stake_eur=15.0, match_id="m1", model_prob=0.5)
        c = _sig("btts_yes", stake_eur=15.0, match_id="m1")
        out = apply_correlation_adjustments([a, b, c], bankroll=100.0)
        total = sum(s.stake_eur for s in out)
        assert abs(total - 30.0) < 0.05
        assert any("match_cap" in s.stake_reason for s in out)
