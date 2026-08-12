"""W2: 5% bankroll risk cap enforcement.

Hard invariant: final_stake_eur <= bankroll * 0.05 for any new recommendation.
Stake tier / Kelly = theoretical generator.  apply_risk_cap() = final gate.
Historical records with stake_pct > 5 remain untouched (legacy evidence).
"""
from __future__ import annotations

import pytest

from src.betting.kelly import apply_risk_cap
from src.betting.tennis_detector import detect_value_tennis
from src.betting.value_detector import _make_signal
from src.config import MIN_STAKE_EUR, BANKROLL_RISK_CAP_PCT


# ---------------------------------------------------------------------------
# apply_risk_cap() unit tests
# ---------------------------------------------------------------------------

class TestApplyRiskCap:

    def test_100_bankroll_15_theoretical_capped_to_5(self):
        final, cap_applied, placeable = apply_risk_cap(15.0, 100.0)
        assert placeable
        assert cap_applied
        assert final == pytest.approx(5.0, abs=0.5)
        assert final <= 100.0 * BANKROLL_RISK_CAP_PCT + 0.001

    def test_200_bankroll_15_theoretical_capped_to_10(self):
        final, cap_applied, placeable = apply_risk_cap(15.0, 200.0)
        assert placeable
        assert cap_applied
        assert final == pytest.approx(10.0, abs=0.5)
        assert final <= 200.0 * BANKROLL_RISK_CAP_PCT + 0.001

    def test_500_bankroll_15_theoretical_passes_uncapped(self):
        final, cap_applied, placeable = apply_risk_cap(15.0, 500.0)
        assert placeable
        assert not cap_applied
        assert final == pytest.approx(15.0, abs=0.5)
        assert final <= 500.0 * BANKROLL_RISK_CAP_PCT + 0.001

    def test_exact_5pct_boundary_allowed(self):
        # 5% exactly should be allowed (not capped)
        cap = 100.0 * BANKROLL_RISK_CAP_PCT  # 5.0
        final, cap_applied, placeable = apply_risk_cap(cap, 100.0)
        assert placeable
        assert final <= cap + 0.001

    def test_over_5pct_rejected(self):
        # 5.01% theoretical must be capped
        cap = 100.0 * BANKROLL_RISK_CAP_PCT  # 5.0
        final, cap_applied, placeable = apply_risk_cap(cap + 0.01, 100.0)
        assert placeable
        assert cap_applied
        assert final <= cap + 0.001

    def test_high_ev_cannot_bypass_cap(self):
        # Extreme EV still cannot produce stake > 5%
        final, _, placeable = apply_risk_cap(40.0, 100.0)
        assert placeable
        assert final <= 100.0 * BANKROLL_RISK_CAP_PCT + 0.001

    def test_high_confidence_cannot_bypass_cap(self):
        # Simulate HIGH confidence theoretical stake (e.g. €22 after +10%)
        final, cap_applied, placeable = apply_risk_cap(22.0, 100.0)
        assert placeable
        assert cap_applied
        assert final <= 5.0 + 0.001

    def test_min_stake_exceeds_cap_not_placeable(self):
        # Bankroll so small that 5% < MIN_STAKE_EUR → not placeable
        small_bankroll = 50.0  # 5% = €2.50, MIN_STAKE_EUR = €5
        final, cap_applied, placeable = apply_risk_cap(5.0, small_bankroll)
        assert not placeable
        assert cap_applied
        assert final == 0.0

    def test_zero_bankroll_not_placeable(self):
        final, cap_applied, placeable = apply_risk_cap(10.0, 0.0)
        assert not placeable
        assert final == 0.0

    def test_negative_bankroll_not_placeable(self):
        final, cap_applied, placeable = apply_risk_cap(10.0, -100.0)
        assert not placeable
        assert final == 0.0

    def test_zero_theoretical_stake_not_placeable(self):
        final, cap_applied, placeable = apply_risk_cap(0.0, 100.0)
        assert not placeable
        assert not cap_applied
        assert final == 0.0


# ---------------------------------------------------------------------------
# Sport-agnostic cap enforcement via _make_signal (football path)
# ---------------------------------------------------------------------------

class TestMakeSignalCap:

    def _call(self, bankroll):
        return _make_signal(
            "match1", "Home", "Away", "home",
            model_p=0.60, fair_p=0.55, odds=2.10, ev=0.10,
            kf=0.05, confidence="MEDIUM", bankroll=bankroll,
        )

    def test_football_100_bankroll_capped(self):
        sig = self._call(100.0)
        assert sig.stake_pct <= 0.05 + 1e-4
        assert sig.stake_eur <= 5.0 + 0.001

    def test_football_200_bankroll_capped(self):
        sig = self._call(200.0)
        assert sig.stake_pct <= 0.05 + 1e-4
        assert sig.stake_eur <= 10.0 + 0.001

    def test_football_500_bankroll_theoretical_within_cap(self):
        sig = self._call(500.0)
        assert sig.stake_pct <= 0.05 + 1e-4

    def test_theoretical_stake_preserved_separately(self):
        sig = self._call(100.0)
        assert sig.theoretical_stake_eur >= sig.stake_eur - 0.001
        assert sig.cap_applied or sig.theoretical_stake_eur == pytest.approx(sig.stake_eur, abs=0.5)


# ---------------------------------------------------------------------------
# Tennis cannot bypass cap
# ---------------------------------------------------------------------------

class TestTennisCap:

    def _tennis_signals(self, bankroll):
        return detect_value_tennis(
            player_a="Djokovic",
            player_b="Alcaraz",
            probs={"p_a": 0.62, "p_b": 0.38},
            odds_a=1.85,
            odds_b=2.10,
            bankroll=bankroll,
            match_id="t1",
            tour="atp",
        )

    def test_tennis_100_bankroll_capped(self):
        sigs = self._tennis_signals(100.0)
        for s in sigs:
            assert s.stake_pct <= 0.05 + 1e-4, f"Tennis stake_pct {s.stake_pct:.4f} > 5%"

    def test_tennis_200_bankroll_capped(self):
        sigs = self._tennis_signals(200.0)
        for s in sigs:
            assert s.stake_pct <= 0.05 + 1e-4

    def test_tennis_500_bankroll_within_cap(self):
        sigs = self._tennis_signals(500.0)
        for s in sigs:
            assert s.stake_pct <= 0.05 + 1e-4


# ---------------------------------------------------------------------------
# BL2 / football multi-market: cap applies regardless of sport or market
# ---------------------------------------------------------------------------

class TestBL2Cap:

    def _call(self, market, bankroll=100.0):
        return _make_signal(
            "bl2_match", "Wolfsburg", "Cottbus", market,
            model_p=0.55, fair_p=0.50, odds=2.20, ev=0.08,
            kf=0.04, confidence="MEDIUM", bankroll=bankroll,
        )

    def test_bl2_home_market_capped(self):
        sig = self._call("home")
        assert sig.stake_pct <= 0.05 + 1e-4

    def test_bl2_away_market_capped(self):
        sig = self._call("away")
        assert sig.stake_pct <= 0.05 + 1e-4

    def test_bl2_draw_market_capped(self):
        sig = self._call("draw")
        assert sig.stake_pct <= 0.05 + 1e-4


# ---------------------------------------------------------------------------
# Min-stake conflict: must not raise stake above cap
# ---------------------------------------------------------------------------

def test_min_stake_conflict_marks_not_placeable():
    # Bankroll of €50 → 5% = €2.50 < MIN_STAKE_EUR (€5) → not placeable
    sig = _make_signal(
        "m1", "A", "B", "home",
        model_p=0.60, fair_p=0.55, odds=2.10, ev=0.10,
        kf=0.05, confidence="MEDIUM", bankroll=50.0,
    )
    assert sig.no_bet_flag
    assert sig.stake_eur == 0.0
    assert sig.stake_reason == "risk_cap_not_placeable"


# ---------------------------------------------------------------------------
# Historical records: stake_pct > 5% in the BetSignal struct is still possible
# (historical signals are not retroactively rewritten)
# ---------------------------------------------------------------------------

def test_historical_signal_not_rewritten():
    """Directly constructing a BetSignal with high stake_pct does not raise."""
    from src.betting.value_detector import BetSignal
    sig = BetSignal(
        match_id="hist1", home="A", away="B", market="home",
        model_prob=0.60, fair_prob=0.55, decimal_odds=2.10,
        ev=0.10, kelly_f=0.05, stake_pct=0.15, confidence="MEDIUM",
        stake_eur=15.0, theoretical_stake_eur=15.0, cap_applied=False,
    )
    assert sig.stake_pct == pytest.approx(0.15)  # historical value unchanged


# ---------------------------------------------------------------------------
# Invariant: no production recommendation path emits final_stake_pct > 5%
# (spot-check via _make_signal across a range of bankrolls and EVs)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bankroll", [100.0, 150.0, 200.0, 300.0, 500.0, 1000.0])
@pytest.mark.parametrize("ev", [0.05, 0.10, 0.20, 0.35])
@pytest.mark.parametrize("confidence", ["MEDIUM", "HIGH"])
def test_no_recommendation_exceeds_5pct(bankroll, ev, confidence):
    sig = _make_signal(
        "inv_test", "A", "B", "home",
        model_p=0.60, fair_p=0.55, odds=2.10, ev=ev,
        kf=0.05, confidence=confidence, bankroll=bankroll,
    )
    if not sig.no_bet_flag:
        assert sig.stake_pct <= 0.05 + 1e-4, (
            f"bankroll={bankroll} ev={ev} confidence={confidence} → stake_pct={sig.stake_pct:.4f}"
        )
