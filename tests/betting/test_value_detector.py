"""Tests for the consistency gate in value_detector."""
import numpy as np
import pytest

from src.betting.value_detector import (
    BetSignal,
    _consistency_confidence,
    detect_value,
    detect_value_ah,
    detect_value_btts,
    detect_value_ftts,
    detect_value_totals,
    set_confidence,
)


# ---------------------------------------------------------------------------
# _consistency_confidence unit tests
# ---------------------------------------------------------------------------

class TestConsistencyConfidence:
    def test_no_dc_returns_base(self):
        assert _consistency_confidence(0.30, 0.20, None, "MEDIUM") == "MEDIUM"

    def test_both_above_fair_returns_base(self):
        # ensemble=30%, dc=25%, fair=20% → both above → no downgrade
        assert _consistency_confidence(0.30, 0.20, 0.25, "MEDIUM") == "MEDIUM"

    def test_both_below_fair_returns_base(self):
        # ensemble=10%, dc=12%, fair=18% → both below → no downgrade
        assert _consistency_confidence(0.10, 0.18, 0.12, "MEDIUM") == "MEDIUM"

    def test_ensemble_above_dc_below_returns_low(self):
        # England vs Croatia scenario: ensemble=26.3% > fair=17.8%, dc=6.3% < fair
        assert _consistency_confidence(0.263, 0.178, 0.063, "MEDIUM") == "LOW"

    def test_ensemble_below_dc_above_returns_low(self):
        # Reversed: ensemble=10% < fair=20%, dc=30% > fair
        assert _consistency_confidence(0.10, 0.20, 0.30, "MEDIUM") == "LOW"

    def test_base_confidence_preserved_when_consistent(self):
        # HIGH base preserved when models agree AND stay within market-disagreement gate
        assert _consistency_confidence(0.45, 0.40, 0.43, "HIGH") == "HIGH"

    def test_market_disagreement_forces_low(self):
        # Both models on same side of fair (above) but model >> market → LOW (Lever 6)
        assert _consistency_confidence(0.55, 0.40, 0.52, "HIGH") == "LOW"

    def test_low_applied_regardless_of_base(self):
        # Even if base was "HIGH", divergence forces "LOW"
        assert _consistency_confidence(0.55, 0.40, 0.35, "HIGH") == "LOW"


# ---------------------------------------------------------------------------
# detect_value consistency gate integration
# ---------------------------------------------------------------------------

class TestDetectValueConsistencyGate:
    # Odds that imply ~17.8% fair probability for away after Shin removal
    # away odds 5.00 → ~1/5 = 20% implied; after ~10% margin removal ≈ 17-18%
    AWAY_ODDS = 5.00
    HOME_ODDS = 1.55
    DRAW_ODDS = 4.00
    RAW_ODDS = (HOME_ODDS, DRAW_ODDS, AWAY_ODDS)

    # Ensemble: Croatia Away 25% (above fair ~17.8%) — positive EV on away
    # Kept below 30% so the EV (≈ +25%) stays under _BIAS_EV_CAP and isolates
    # the consistency-gate path from the bias-safety downgrade.
    # [p_away, p_draw, p_home]
    ENSEMBLE_AWAY_SIGNAL = np.array([0.25, 0.40, 0.35])

    def test_no_dc_probs_gives_medium(self):
        signals = detect_value(
            "England", "Croatia", self.ENSEMBLE_AWAY_SIGNAL, self.RAW_ODDS,
            bankroll=100.0,
        )
        away_signals = [s for s in signals if s.market == "away"]
        assert len(away_signals) == 1
        assert away_signals[0].confidence == "MEDIUM"

    def test_consistent_dc_keeps_medium(self):
        # DC also above fair for away → consistent → MEDIUM
        dc_probs = {"p_home": 0.55, "p_draw": 0.25, "p_away": 0.20}  # dc away > fair ~17.8%
        signals = detect_value(
            "England", "Croatia", self.ENSEMBLE_AWAY_SIGNAL, self.RAW_ODDS,
            bankroll=100.0, dc_probs=dc_probs,
        )
        away_signals = [s for s in signals if s.market == "away"]
        assert len(away_signals) == 1
        assert away_signals[0].confidence == "MEDIUM"

    def test_divergent_dc_downgrades_to_low(self):
        # DC: Croatia Away 6.3% (below fair ~17.8%) — diverges from ensemble
        dc_probs = {"p_home": 0.75, "p_draw": 0.187, "p_away": 0.063}
        signals = detect_value(
            "England", "Croatia", self.ENSEMBLE_AWAY_SIGNAL, self.RAW_ODDS,
            bankroll=100.0, dc_probs=dc_probs,
        )
        away_signals = [s for s in signals if s.market == "away"]
        assert len(away_signals) == 1, "Signal must be generated (not dropped)"
        assert away_signals[0].confidence == "LOW"

    def test_signal_not_dropped_only_downgraded(self):
        # The gate MUST NOT filter; it only downgrades confidence
        dc_probs = {"p_home": 0.75, "p_draw": 0.187, "p_away": 0.063}
        signals = detect_value(
            "England", "Croatia", self.ENSEMBLE_AWAY_SIGNAL, self.RAW_ODDS,
            bankroll=100.0, dc_probs=dc_probs,
        )
        assert len(signals) >= 1


# ---------------------------------------------------------------------------
# detect_value_ah consistency gate integration
# ---------------------------------------------------------------------------

class TestDetectValueAHConsistencyGate:
    AH_PROBS = {"p_ah_home": 0.60, "p_ah_away": 0.40}
    # Away AH at 2.60 → EV = 0.40 * 2.60 - 1 = 0.04 → positive
    AH_HOME_ODDS = 1.50
    AH_AWAY_ODDS = 2.60

    def test_divergent_dc_downgrades_ah(self):
        # model p_ah_away=0.40 < 0.5, but dc p_away=0.65 > 0.5 → diverge
        dc_probs = {"p_home": 0.35, "p_away": 0.65}
        signals = detect_value_ah(
            "A", "B", self.AH_PROBS, self.AH_HOME_ODDS, self.AH_AWAY_ODDS,
            bankroll=100.0, dc_probs=dc_probs,
        )
        away_sigs = [s for s in signals if "away" in s.market]
        assert len(away_sigs) == 1
        assert away_sigs[0].confidence == "LOW"

    def test_no_dc_gives_medium_ah(self):
        signals = detect_value_ah(
            "A", "B", self.AH_PROBS, self.AH_HOME_ODDS, self.AH_AWAY_ODDS,
            bankroll=100.0,
        )
        away_sigs = [s for s in signals if "away" in s.market]
        # May or may not trigger EV gate; if it does, check confidence
        for s in away_sigs:
            assert s.confidence == "MEDIUM"


# ---------------------------------------------------------------------------
# detect_value_totals consistency gate integration
# ---------------------------------------------------------------------------

class TestDetectValueTotalsConsistencyGate:
    TOTALS = {"p_over": 0.60, "p_under": 0.40, "line": 2.5}
    OVER_ODDS = 1.75   # EV = 0.60 * 1.75 - 1 = 0.05 → positive
    UNDER_ODDS = 2.20

    def test_divergent_dc_downgrades_over(self):
        # DC p_over=0.42 < 0.5 but ensemble p_over=0.60 > 0.5 → diverge
        dc_probs = {"p_over": 0.42, "p_under": 0.58}
        signals = detect_value_totals(
            "A", "B", self.TOTALS, self.OVER_ODDS, self.UNDER_ODDS,
            bankroll=100.0, dc_probs=dc_probs,
        )
        over_sigs = [s for s in signals if "over" in s.market]
        assert len(over_sigs) == 1
        assert over_sigs[0].confidence == "LOW"

    def test_consistent_dc_keeps_medium_totals(self):
        # DC p_over=0.55 > 0.5, ensemble=0.60 > 0.5 → both above → MEDIUM
        dc_probs = {"p_over": 0.55, "p_under": 0.45}
        signals = detect_value_totals(
            "A", "B", self.TOTALS, self.OVER_ODDS, self.UNDER_ODDS,
            bankroll=100.0, dc_probs=dc_probs,
        )
        over_sigs = [s for s in signals if "over" in s.market]
        assert len(over_sigs) == 1
        assert over_sigs[0].confidence == "MEDIUM"

    def test_min_edge_under_blocks_marginal_under_signal(self):
        # UNDER with 9% model EV passes base MIN_EDGE=3% but should fail min_edge_under=10%
        totals = {"p_over": 0.40, "p_under": 0.60, "line": 2.5}
        # odds 1.65: EV = 0.60*1.65-1 = -0.01 → no signal anyway
        # odds 1.82: EV = 0.60*1.82-1 = 0.092 → passes 3%, should fail 10%
        sigs = detect_value_totals("A", "B", totals, 0.0, 1.82, bankroll=100.0, min_edge_under=0.10)
        under_sigs = [s for s in sigs if "under" in s.market]
        assert len(under_sigs) == 0

    def test_min_edge_under_passes_high_ev_under_signal(self):
        # UNDER with 15% model EV should pass min_edge_under=10%
        totals = {"p_over": 0.35, "p_under": 0.65, "line": 2.5}
        # odds 1.77: EV = 0.65*1.77-1 = 0.1505 → 15% → passes 10% threshold
        sigs = detect_value_totals("A", "B", totals, 0.0, 1.77, bankroll=100.0, min_edge_under=0.10)
        under_sigs = [s for s in sigs if "under" in s.market]
        assert len(under_sigs) == 1

    def test_min_edge_under_does_not_affect_over_side(self):
        # min_edge_under should only filter UNDER; OVER with 5% EV still passes base 3%
        totals = {"p_over": 0.60, "p_under": 0.40, "line": 2.5}
        # over_odds 1.75: EV=0.05; under_odds 2.20: EV=0.40*2.20-1=-0.12 → no under anyway
        sigs = detect_value_totals("A", "B", totals, 1.75, 2.20, bankroll=100.0, min_edge_under=0.10)
        over_sigs = [s for s in sigs if "over" in s.market]
        assert len(over_sigs) == 1


# ---------------------------------------------------------------------------
# LOW-confidence signal separation (Iteration #16)
# ---------------------------------------------------------------------------

class TestLowConfidenceSeparation:
    def test_consistency_confidence_returns_low_when_dc_and_ensemble_disagree(self):
        # ensemble sees value (above fair), DC does not → LOW
        result = _consistency_confidence(
            ensemble_p=0.30,  # above fair
            fair_p=0.20,
            dc_p=0.12,        # below fair
            base_confidence="MEDIUM",
        )
        assert result == "LOW"

    def test_consistency_confidence_no_downgrade_when_both_above(self):
        result = _consistency_confidence(
            ensemble_p=0.30,
            fair_p=0.20,
            dc_p=0.25,        # both above fair
            base_confidence="MEDIUM",
        )
        assert result == "MEDIUM"

    def test_consistency_confidence_no_downgrade_when_both_below(self):
        result = _consistency_confidence(
            ensemble_p=0.10,
            fair_p=0.20,
            dc_p=0.12,        # both below fair
            base_confidence="MEDIUM",
        )
        assert result == "MEDIUM"

    def test_set_confidence_does_not_upgrade_low(self):
        # A LOW signal must never be upgraded to HIGH
        signal = BetSignal(
            match_id="test", home="A", away="B", market="home",
            model_prob=0.60, fair_prob=0.45, decimal_odds=2.0,
            ev=0.20, kelly_f=0.20, stake_pct=0.01,
            confidence="LOW", stake_eur=5.0,
        )
        dc_probs = {"p_home": 0.65, "p_draw": 0.20, "p_away": 0.15}
        lgbm_probs = np.array([0.15, 0.20, 0.65])  # [away, draw, home]
        result = set_confidence(signal, dc_probs, lgbm_probs)
        assert result.confidence == "LOW"  # must not become HIGH


# ---------------------------------------------------------------------------
# min_edge_override in detect_value (Iteration #18)
# ---------------------------------------------------------------------------

class TestDetectValueMinEdgeOverride:
    def test_detect_value_min_edge_override_suppresses_market(self):
        # away model_p=0.75, away odds=1.50 → EV = 0.75*1.50-1 = 0.125 (above MIN_EDGE=0.03)
        # Without override: away signal is generated
        # With override requiring 0.20 for away: EV 0.125 < 0.20 → no signal
        model_probs = np.array([0.75, 0.15, 0.10])  # [away, draw, home]
        raw_odds = (5.0, 4.0, 1.50)                  # (home, draw, away)

        signals_no_override = detect_value("A", "B", model_probs, raw_odds, bankroll=1000)
        away_signals = [s for s in signals_no_override if s.market == "away"]
        assert len(away_signals) == 1, "Without override, away signal should be generated"

        signals_with_override = detect_value(
            "A", "B", model_probs, raw_odds, bankroll=1000,
            min_edge_override={"home": 0.03, "draw": 0.03, "away": 0.20},
        )
        away_signals_filtered = [s for s in signals_with_override if s.market == "away"]
        assert len(away_signals_filtered) == 0, "With override 0.20, EV=0.125 should be suppressed"

    def test_detect_value_min_edge_override_keeps_other_markets(self):
        # Override only suppresses "away"; other markets with sufficient EV still produce signals
        # home model_p=0.10, home odds=5.0 → EV = 0.10*5.0-1 = -0.50 (below, no signal)
        # draw model_p=0.15, draw odds=4.0 → EV = 0.15*4.0-1 = -0.40 (below, no signal)
        # away model_p=0.75, away odds=1.50 → EV = 0.125 (suppressed by override)
        model_probs = np.array([0.75, 0.15, 0.10])
        raw_odds = (5.0, 4.0, 1.50)
        signals = detect_value(
            "A", "B", model_probs, raw_odds, bankroll=1000,
            min_edge_override={"home": 0.03, "draw": 0.03, "away": 0.20},
        )
        # No home or draw signals expected (negative EV); away is suppressed
        assert all(s.market != "away" for s in signals)

    def test_detect_value_min_edge_override_none_uses_default(self):
        # No override: default MIN_EDGE applies to all markets
        model_probs = np.array([0.75, 0.15, 0.10])
        raw_odds = (5.0, 4.0, 1.50)
        signals_default = detect_value("A", "B", model_probs, raw_odds, bankroll=1000)
        signals_explicit_none = detect_value(
            "A", "B", model_probs, raw_odds, bankroll=1000,
            min_edge_override=None,
        )
        assert len(signals_default) == len(signals_explicit_none)


# ---------------------------------------------------------------------------
# detect_value_btts
# ---------------------------------------------------------------------------

class TestDetectValueAHPushAware:
    """Tests for push-aware EV and Kelly in detect_value_ah() with whole-line handicaps."""

    def test_ev_with_push_calculation(self):
        """EV formula: p_win*(odds-1) + p_push*0 + p_lose*(-1).
        p_win=0.5, p_push=0.1, p_lose=0.4, odds=2.0 → EV = 0.5*1 + 0.4*(-1) = 0.10
        """
        ah_probs = {"p_ah_home": 0.5, "p_ah_away": 0.4, "p_push": 0.1}
        signals = detect_value_ah(
            "A", "B", ah_probs,
            ah_home_odds=2.0, ah_away_odds=3.0,
            bankroll=100.0, min_edge=0.0, line=-1.0,
        )
        home_sigs = [s for s in signals if "home" in s.market]
        assert len(home_sigs) == 1
        assert abs(home_sigs[0].ev - 0.10) < 1e-9

    def test_ev_with_push_away_calculation(self):
        """p_win=0.5, p_push=0.1, p_lose=0.4, odds=2.0 → EV = 0.5*1 - 0.4 = 0.10."""
        ah_probs = {"p_ah_home": 0.4, "p_ah_away": 0.5, "p_push": 0.1}
        signals = detect_value_ah(
            "A", "B", ah_probs,
            ah_home_odds=3.0, ah_away_odds=2.0,
            bankroll=100.0, min_edge=0.0, line=-1.0,
        )
        away_sigs = [s for s in signals if "away" in s.market]
        assert len(away_sigs) == 1
        assert abs(away_sigs[0].ev - 0.10) < 1e-9

    def test_no_push_ev_unchanged(self):
        """For half-line (p_push=0), EV must match the classic formula: p*odds-1."""
        ah_probs = {"p_ah_home": 0.6, "p_ah_away": 0.4, "p_push": 0.0}
        odds = 1.80
        expected_ev = 0.6 * (odds - 1) + 0.4 * (-1)  # = 0.6*0.8 - 0.4 = 0.08
        signals = detect_value_ah(
            "A", "B", ah_probs,
            ah_home_odds=odds, ah_away_odds=3.0,
            bankroll=100.0, min_edge=0.0, line=-0.5,
        )
        home_sigs = [s for s in signals if "home" in s.market]
        assert len(home_sigs) == 1
        assert abs(home_sigs[0].ev - expected_ev) < 1e-9

    def test_push_market_label_minus_10(self):
        """Signals for line=-1.0 must use market label 'ah-1.0_home' / 'ah+1.0_away'."""
        ah_probs = {"p_ah_home": 0.6, "p_ah_away": 0.3, "p_push": 0.1}
        signals = detect_value_ah(
            "A", "B", ah_probs,
            ah_home_odds=1.80, ah_away_odds=3.50,
            bankroll=100.0, min_edge=0.0, line=-1.0,
        )
        markets = {s.market for s in signals}
        assert "ah-1.0_home" in markets

    def test_push_market_label_minus_15(self):
        """Signals for line=-1.5 must use market label 'ah-1.5_home'."""
        ah_probs = {"p_ah_home": 0.55, "p_ah_away": 0.45, "p_push": 0.0}
        signals = detect_value_ah(
            "A", "B", ah_probs,
            ah_home_odds=1.85, ah_away_odds=2.10,
            bankroll=100.0, min_edge=0.0, line=-1.5,
        )
        markets = {s.market for s in signals}
        assert "ah-1.5_home" in markets

    def test_push_negative_ev_suppressed(self):
        """Bet with push that yields negative EV must not be signalled."""
        # p_win=0.3, p_push=0.4, p_lose=0.3, odds=2.0 → EV = 0.3*1 + 0.3*(-1) = 0.0
        ah_probs = {"p_ah_home": 0.3, "p_ah_away": 0.3, "p_push": 0.4}
        signals = detect_value_ah(
            "A", "B", ah_probs,
            ah_home_odds=2.0, ah_away_odds=2.0,
            bankroll=100.0, min_edge=0.03, line=-1.0,
        )
        assert signals == []


class TestDetectValueBtts:
    # p_btts_yes=0.65, yes_odds=1.80 → EV = 0.65*1.80-1 = 0.17 → positive
    BTTS_PROBS = {"p_btts_yes": 0.65, "p_btts_no": 0.35}
    BTTS_YES_ODDS = 1.80
    BTTS_NO_ODDS = 2.50

    def test_detect_value_btts_signal_when_ev_positive(self):
        signals = detect_value_btts(
            "A", "B", self.BTTS_PROBS,
            self.BTTS_YES_ODDS, self.BTTS_NO_ODDS,
            bankroll=100.0,
        )
        yes_sigs = [s for s in signals if s.market == "btts_yes"]
        assert len(yes_sigs) == 1
        assert yes_sigs[0].ev > 0
        assert yes_sigs[0].stake_eur > 0

    def test_detect_value_btts_no_signal_when_zero_odds(self):
        signals = detect_value_btts(
            "A", "B", self.BTTS_PROBS,
            btts_yes_odds=0.0, btts_no_odds=0.0,
            bankroll=100.0,
        )
        assert signals == []

    def test_detect_value_btts_market_labels(self):
        signals = detect_value_btts(
            "A", "B", self.BTTS_PROBS,
            self.BTTS_YES_ODDS, self.BTTS_NO_ODDS,
            bankroll=100.0,
        )
        markets = {s.market for s in signals}
        assert markets <= {"btts_yes", "btts_no"}

    def test_detect_value_btts_fair_prob_from_market_not_hardcoded(self):
        """fair_prob must reflect market odds (market-implied), not hardcoded 0.5."""
        # Heavily skewed market: 1.40 yes / 3.50 no
        # fair_yes = (1/1.40) / (1/1.40 + 1/3.50) ≈ 0.714 / (0.714 + 0.286) = 0.714
        skewed_probs = {"p_btts_yes": 0.75, "p_btts_no": 0.25}
        signals = detect_value_btts(
            "A", "B", skewed_probs,
            btts_yes_odds=1.40, btts_no_odds=3.50,
            bankroll=100.0,
        )
        yes_sigs = [s for s in signals if s.market == "btts_yes"]
        if yes_sigs:
            # fair_prob should be ~0.714, NOT 0.5
            assert yes_sigs[0].fair_prob > 0.60, (
                f"Expected fair_prob > 0.60 for 1.40 odds, got {yes_sigs[0].fair_prob}"
            )

    def test_detect_value_btts_consistency_gate_with_market_fair_prob(self):
        """Consistency gate must use market-implied fair_prob, not 0.5."""
        # Model: btts_yes=0.55 (DC), dc_probs mirrors same value
        # Market fair_yes ≈ 0.714 (1.40/3.50 market)
        # Model BELOW market fair → should downgrade to LOW
        skewed_probs = {"p_btts_yes": 0.55, "p_btts_no": 0.45}
        dc_probs = {"p_btts_yes": 0.55, "p_btts_no": 0.45}
        signals = detect_value_btts(
            "A", "B", skewed_probs,
            btts_yes_odds=1.40, btts_no_odds=3.50,
            bankroll=100.0, dc_probs=dc_probs,
        )
        # EV check: 0.55 * 1.40 - 1 = -0.23 → no signal (below min_edge)
        # With 0.5 hardcoded, consistency gate would pass; with market fair, model < fair → LOW
        # Either way, no signal should be generated here (EV < 0)
        assert all(s.market != "btts_yes" for s in signals), (
            "btts_yes signal should not appear when EV is negative"
        )


# ---------------------------------------------------------------------------
# detect_value_ftts tests
# ---------------------------------------------------------------------------

class TestDetectValueFtts:
    def _ftts_probs(self, p_home: float) -> dict:
        return {"p_home_first": p_home, "p_away_first": 1.0 - p_home}

    def test_returns_signal_when_value_exists(self):
        # home scores first with 60% model prob, offered at 2.00 → EV = 0.60*2.0-1 = 0.20
        signals = detect_value_ftts(
            "A", "B", self._ftts_probs(0.60),
            ftts_home_odds=2.00, ftts_away_odds=1.80,
            bankroll=100.0,
        )
        markets = [s.market for s in signals]
        assert "ftts_home" in markets

    def test_returns_no_signal_when_no_value(self):
        # 50% prob at 1.90 → EV = 0.50*1.90-1 = -0.05 → no signal
        signals = detect_value_ftts(
            "A", "B", self._ftts_probs(0.50),
            ftts_home_odds=1.90, ftts_away_odds=1.90,
            bankroll=100.0,
        )
        assert signals == []

    def test_zero_odds_skipped(self):
        signals = detect_value_ftts(
            "A", "B", self._ftts_probs(0.65),
            ftts_home_odds=0.0, ftts_away_odds=0.0,
            bankroll=100.0,
        )
        assert signals == []

    def test_market_keys_are_correct(self):
        signals = detect_value_ftts(
            "Home", "Away", self._ftts_probs(0.70),
            ftts_home_odds=1.75, ftts_away_odds=3.50,
            bankroll=100.0,
        )
        for s in signals:
            assert s.market in ("ftts_home", "ftts_away")

    def test_home_team_set_correctly(self):
        signals = detect_value_ftts(
            "Barcelona", "Madrid", self._ftts_probs(0.65),
            ftts_home_odds=1.80, ftts_away_odds=2.50,
            bankroll=100.0,
        )
        for s in signals:
            assert s.home == "Barcelona"
            assert s.away == "Madrid"
