"""Bias-safety EV cap (docs/audit_2026-06-12.md, section H).

EVs ≥ _BIAS_EV_CAP are virtually always Qualifier-training artefacts in the
current dataset (Algeria 37 %, Côte d'Ivoire 38 %, USA-Mexico o/u3.5_under
37 %). The gate forces such signals to confidence="LOW" so they don't reach
the Wetten-Tab as actionable picks.
"""
import numpy as np

from src.betting.value_detector import (
    _bias_safety_confidence,
    _BIAS_EV_CAP,
    detect_value,
)


class TestBiasSafetyConfidence:
    def test_low_ev_unchanged_high(self):
        assert _bias_safety_confidence("HIGH", 0.10) == "HIGH"

    def test_low_ev_unchanged_medium(self):
        assert _bias_safety_confidence("MEDIUM", 0.29) == "MEDIUM"

    def test_at_cap_downgrades(self):
        # boundary triggers
        assert _bias_safety_confidence("MEDIUM", _BIAS_EV_CAP) == "LOW"

    def test_above_cap_downgrades_high(self):
        assert _bias_safety_confidence("HIGH", 0.40) == "LOW"

    def test_low_stays_low(self):
        # idempotent — already-LOW signals don't change
        assert _bias_safety_confidence("LOW", 0.50) == "LOW"

    def test_cap_value_documented(self):
        # if someone moves the cap they must update the audit memory too.
        assert _BIAS_EV_CAP == 0.30


class TestDetectValueIntegration:
    """Constructs model/odds combos that produce EV around the cap and
    verifies the resulting signal carries confidence='LOW'."""

    def _signal_for(self, model_p_home: float, home_odds: float) -> "BetSignal | None":
        # model_probs order: [p_away, p_draw, p_home]
        model_probs = np.array([0.20, 0.20, model_p_home])
        # Set draw/away odds so they don't generate value
        raw_odds = (home_odds, 10.0, 10.0)
        signals = detect_value(
            "TeamA", "TeamB", model_probs, raw_odds,
            bankroll=100.0, match_id="x",
        )
        return next((s for s in signals if s.market == "home"), None)

    def test_high_ev_signal_downgraded(self):
        # EV = 0.60 * 2.5 - 1 = 0.50 → well above cap
        s = self._signal_for(0.60, 2.5)
        assert s is not None
        assert s.confidence == "LOW"

    def test_modest_ev_stays_medium(self):
        # EV = 0.45 * 2.5 - 1 = 0.125 → below cap, stays MEDIUM (no DC ⇒
        # consistency gate only checks market-disagreement)
        s = self._signal_for(0.45, 2.5)
        assert s is not None
        assert s.confidence in {"MEDIUM", "LOW"}  # market-disagreement could still trigger
        # Whatever the consistency gate decided, the bias gate must NOT have
        # been the reason — re-running the helper proves it would not lower
        # a MEDIUM at this EV.
        assert _bias_safety_confidence("MEDIUM", s.ev) == "MEDIUM"

    def test_boundary_ev_downgrades(self):
        # Pick model/odds so EV is exactly at the cap.
        # EV = p × o - 1 = 0.30 → p × o = 1.30 → e.g. p=0.65, o=2.0
        s = self._signal_for(0.65, 2.0)
        assert s is not None
        assert s.ev >= _BIAS_EV_CAP - 1e-9
        assert s.confidence == "LOW"
