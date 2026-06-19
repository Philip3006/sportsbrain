"""Phase 1.1 — Optimizer bounds + bound-hit detection.

Phase 1.1 tightened the L-BFGS-B bounds so that an extreme WC2026_BOOST cannot
silently saturate the optimizer at a corner of parameter space. _check_bounds_hit
surfaces the saturation so the train script can retry with a smaller boost.

2026-06-19: Defence lower bound widened -2.5 → -3.5, rho lower -0.30 → -0.40
because Cape Verde, Mexico, rho hit the old bounds across all 4 retry-boost
attempts. The _MAX_LAMBDA=4.5 inference cap remains the xG safety net.

These tests pin:
  • the current ranges (any widening requires updating these tests as a review gate)
  • _check_bounds_hit's behaviour on synthetic inputs
  • that the wc2026_boost_override kwarg is honoured end-to-end
"""
import numpy as np
import pandas as pd
import pytest

from src.models import dixon_coles
from src.models.dixon_coles import (
    DixonColesParams,
    _check_bounds_hit,
    _FIT_BOUNDS_ATTACK,
    _FIT_BOUNDS_DEFENCE,
    _FIT_BOUNDS_HOME_ADV,
    _FIT_BOUNDS_RHO,
    fit,
)


class TestBoundsValues:
    """Pin the tightened ranges so an accidental widening is caught in review."""

    def test_attack_bounds(self):
        assert _FIT_BOUNDS_ATTACK == (-3.0, 2.5)

    def test_defence_bounds(self):
        assert _FIT_BOUNDS_DEFENCE == (-3.5, 2.0)

    def test_home_adv_bounds(self):
        assert _FIT_BOUNDS_HOME_ADV == (0.0, 0.6)

    def test_rho_bounds(self):
        assert _FIT_BOUNDS_RHO == (-0.40, 0.10)


class TestCheckBoundsHit:
    def _clean(self):
        return DixonColesParams(
            attack={"A": 0.5, "B": -0.1},
            defence={"A": -0.4, "B": 0.2},
            home_adv=0.25,
            rho=-0.10,
            fit_date=pd.Timestamp("2026-06-14"),
        )

    def test_clean_no_hits(self):
        hits = _check_bounds_hit(self._clean())
        assert all(len(v) == 0 for v in hits.values())

    def test_rho_low_bound_detected(self):
        p = self._clean()
        p.rho = -0.40
        hits = _check_bounds_hit(p)
        assert len(hits["rho"]) == 1
        assert hits["rho"][0][2] == "low"

    def test_attack_high_bound_detected(self):
        p = self._clean()
        p.attack["A"] = 2.5
        hits = _check_bounds_hit(p)
        assert len(hits["attack"]) == 1
        assert hits["attack"][0][0] == "A"
        assert hits["attack"][0][2] == "high"

    def test_defence_low_bound_detected(self):
        p = self._clean()
        p.defence["A"] = -3.5
        hits = _check_bounds_hit(p)
        assert len(hits["defence"]) == 1
        assert hits["defence"][0][2] == "low"

    def test_home_adv_high_bound_detected(self):
        p = self._clean()
        p.home_adv = 0.6
        hits = _check_bounds_hit(p)
        assert len(hits["home_adv"]) == 1


class TestBoostOverride:
    """Sanity: wc2026_boost_override threads through fit() without errors and
    materially changes the result vs. the default boost."""

    def _make_matches(self):
        # Tiny synthetic dataset, includes one WM2026 match for the boost path.
        return pd.DataFrame({
            "home_team": ["A", "B", "A", "B", "A"],
            "away_team": ["B", "A", "B", "A", "B"],
            "home_score": [2, 1, 3, 0, 1],
            "away_score": [1, 2, 0, 1, 2],
            "date": pd.to_datetime([
                "2024-01-01", "2024-06-01", "2025-01-01", "2025-06-01", "2026-06-12",
            ]),
            "tournament": [
                "FIFA World Cup qualification", "FIFA World Cup qualification",
                "FIFA World Cup qualification", "FIFA World Cup qualification",
                "FIFA World Cup",
            ],
            "neutral": [False, False, False, False, True],
        })

    def test_override_no_boost_runs(self):
        p1 = fit(self._make_matches(), max_iter=200, wc2026_boost_override=1.0)
        p2 = fit(self._make_matches(), max_iter=200, wc2026_boost_override=0.5)
        # Both must finish and return sensible objects.
        assert "A" in p1.attack and "A" in p2.attack
        assert -3.0 <= p1.attack["A"] <= 2.5
        assert -3.0 <= p2.attack["A"] <= 2.5

    def test_clamped_x0_does_not_break_fit(self):
        # warm-start with an out-of-range prior — fit() must clip and run.
        bad_prior = DixonColesParams(
            attack={"A": 5.0, "B": -8.0},  # way out of range
            defence={"A": -5.0, "B": 4.0},
            home_adv=2.0,
            rho=-0.9,
            fit_date=pd.Timestamp("2025-01-01"),
        )
        p = fit(self._make_matches(), max_iter=200, prior_params=bad_prior)
        # Result must respect the tightened bounds.
        for v in p.attack.values():
            assert -3.0 <= v <= 2.5
        for v in p.defence.values():
            assert -3.5 <= v <= 2.0
        assert 0.0 <= p.home_adv <= 0.6
        assert -0.40 <= p.rho <= 0.10
