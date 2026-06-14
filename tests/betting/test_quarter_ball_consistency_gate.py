"""Phase 1.2 verification — Quarter-Ball detectors apply the same
consistency + bias-safety gates as standard O/U and AH detectors.

The audit (docs/audit_2026-06-12.md, section B) flagged that
detect_value_totals_quarter and detect_value_ah_quarter could ship a
99 % model_prob + 37 % EV signal as confidence=MEDIUM because they
bypassed _consistency_confidence. These tests pin that behaviour so
the gate cannot regress silently.
"""
import pytest

from src.betting.value_detector import (
    detect_value_ah_quarter,
    detect_value_totals_quarter,
)


def _quarter_totals_probs(line=2.25, p_over=0.80, p_push=0.05):
    """Builds the dict shape returned by predict_totals_all() for a quarter line."""
    p_under = max(0.0, 1.0 - p_over - p_push)
    return {
        "line": line,
        "lower_probs": {"p_over": p_over, "p_under": p_under, "p_push": p_push},
        "upper_probs": {"p_over": p_over, "p_under": p_under, "p_push": 0.0},
        "quarter_ball": True,
    }


def _quarter_ah_probs(line=-1.25, p_home=0.80, p_push=0.05):
    """Builds the dict shape returned by predict_asian_handicap_all() for quarter."""
    p_away = max(0.0, 1.0 - p_home - p_push)
    return {
        "line": line,
        "lower_probs": {"p_ah_home": p_home, "p_ah_away": p_away, "p_push": p_push},
        "upper_probs": {"p_ah_home": p_home, "p_ah_away": p_away, "p_push": 0.0},
        "quarter_ball": True,
    }


class TestQuarterBallTotalsGate:
    def test_high_ev_without_consistent_dc_downgrades_low(self):
        # Model 80% OVER, odds 2.4 → EV ≈ 0.92 (well above _BIAS_EV_CAP)
        # DC disagrees (p_over=0.30 < 0.5 fair) — must be LOW via consistency.
        signals = detect_value_totals_quarter(
            "A", "B", _quarter_totals_probs(2.25, p_over=0.80, p_push=0.05),
            over_odds=2.4, under_odds=2.4, bankroll=100.0, min_edge=0.04,
            match_id="t", dc_probs={"p_over": 0.30, "p_under": 0.70},
        )
        over_sig = next((s for s in signals if "over" in s.market), None)
        assert over_sig is not None
        assert over_sig.confidence == "LOW"

    def test_consistent_dc_and_modest_ev_stays_medium(self):
        # Both ensemble and DC say over (0.55, 0.58) at fair 0.5; modest EV.
        signals = detect_value_totals_quarter(
            "A", "B", _quarter_totals_probs(2.25, p_over=0.55, p_push=0.05),
            over_odds=2.0, under_odds=2.0, bankroll=100.0, min_edge=0.04,
            match_id="t", dc_probs={"p_over": 0.58, "p_under": 0.42},
        )
        over_sig = next((s for s in signals if "over" in s.market), None)
        if over_sig is not None:
            assert over_sig.confidence in {"MEDIUM", "HIGH"}

    def test_bias_safety_cap_downgrades_when_no_dc(self):
        # Without dc_probs, market-disagreement gate kicks in. EV well above
        # _BIAS_EV_CAP must downgrade to LOW.
        signals = detect_value_totals_quarter(
            "A", "B", _quarter_totals_probs(2.25, p_over=0.85, p_push=0.05),
            over_odds=2.5, under_odds=2.5, bankroll=100.0, min_edge=0.04,
            match_id="t", dc_probs=None,
        )
        over_sig = next((s for s in signals if "over" in s.market), None)
        assert over_sig is not None
        assert over_sig.confidence == "LOW"


class TestQuarterBallAHGate:
    def test_high_ev_inconsistent_dc_downgrades(self):
        signals = detect_value_ah_quarter(
            "A", "B", _quarter_ah_probs(-1.25, p_home=0.75, p_push=0.05),
            ah_home_odds=2.3, ah_away_odds=2.3, bankroll=100.0, min_edge=0.04,
            match_id="t", line=-1.25,
            dc_probs={"p_ah_home": 0.30, "p_ah_away": 0.65},
        )
        home_sig = next((s for s in signals if "home" in s.market), None)
        assert home_sig is not None
        assert home_sig.confidence == "LOW"

    def test_bias_safety_cap_downgrades_when_no_dc(self):
        signals = detect_value_ah_quarter(
            "A", "B", _quarter_ah_probs(-1.25, p_home=0.85, p_push=0.05),
            ah_home_odds=2.5, ah_away_odds=2.5, bankroll=100.0, min_edge=0.04,
            match_id="t", line=-1.25, dc_probs=None,
        )
        home_sig = next((s for s in signals if "home" in s.market), None)
        assert home_sig is not None
        assert home_sig.confidence == "LOW"


class TestGateRegressionGuard:
    """Source-level guard: the four signal-emit sites in the quarter-ball
    detectors must call both _consistency_confidence and _bias_safety_confidence.
    A regression that removed either would re-open the audit-2026-06-12 hole.
    """
    def test_quarter_detectors_call_gates(self):
        from pathlib import Path
        src = Path(__file__).resolve().parents[2] / "src" / "betting" / "value_detector.py"
        text = src.read_text()
        # crude but resilient: count gate calls inside the two quarter funcs
        for marker in ("def detect_value_totals_quarter", "def detect_value_ah_quarter"):
            start = text.index(marker)
            end = text.index("\ndef ", start + 1) if "\ndef " in text[start + 1:] else len(text)
            body = text[start:end]
            assert body.count("_consistency_confidence(") >= 2, (
                f"{marker}: missing _consistency_confidence call"
            )
            assert body.count("_bias_safety_confidence(") >= 2, (
                f"{marker}: missing _bias_safety_confidence call"
            )
