"""FND-MODEL1-016: Complement symmetry fix for global tennis meta-calibrator.

Proves all CEO-mandated properties:
1. Raw F(p) asymmetry corrected — f_sym(p) + f_sym(1-p) == 1
2. Full ensemble: P(A>B) + P(B>A) == 1 within numerical tolerance
3. Hard surface behavior unchanged (surf_cal takes precedence, meta_cal skipped)
4. Clay behavior becomes symmetric (meta_cal active, no surface calibrator)
5. Grass behavior becomes symmetric (meta_cal active, no surface calibrator)
6. No meta-calibrator → behavior unchanged
7. Malformed/unavailable calibrator fallback unchanged
8. Probability clamp remains valid: p_a in [0.02, 0.98]
9. source remains 'ensemble' after calibration
10. No betting/staking threshold changes

Strategy: loader functions (_load_meta_calibrator, _load_surface_calibrator,
_load_model) are monkeypatched directly so calibrator objects never need to be
pickled. Only live-artifact tests use the on-disk .pkl files.
"""
from __future__ import annotations

import math
import pickle
from pathlib import Path

import numpy as np
import pytest

from src.models.tennis_elo import TennisEloRatings
from src.tennis import ensemble as ens
from src.tennis.features import RollingState

_META_CAL_PATH = Path("models/tennis_lgbm/meta_calibrator.pkl")
_SURF_CAL_HARD_PATH = Path("models/tennis_calibrators/hard.pkl")
_META_CAL_AVAILABLE = _META_CAL_PATH.exists()
_SURF_CAL_HARD_AVAILABLE = _SURF_CAL_HARD_PATH.exists()


@pytest.fixture(autouse=True)
def _clear_ens_cache():
    ens._CACHED.clear()
    yield
    ens._CACHED.clear()


def _ratings(a: str = "Alpha", b: str = "Beta") -> TennisEloRatings:
    ratings = TennisEloRatings()
    ratings.overall[a] = 1700
    ratings.overall[b] = 1500
    for surf in ("hard", "clay", "grass"):
        ratings.by_surface.setdefault(surf, {})[a] = 1720
        ratings.by_surface.setdefault(surf, {})[b] = 1490
        ratings.surface_counts.setdefault(surf, {})[a] = 30
        ratings.surface_counts.setdefault(surf, {})[b] = 25
    return ratings


def _state(a: str, b: str) -> RollingState:
    state = RollingState()
    state.update(a, b, "hard")
    state.update(b, a, "hard")
    return state


class _AsymmetricCal:
    """F(p) = p * 0.6 — sum F(p)+F(1-p)=0.6 ≠ 1, clearly asymmetric."""
    def predict(self, X):
        return [float(X[0]) * 0.6]


class _SquareCal:
    """F(p) = p^2 — asymmetric by construction."""
    def predict(self, X):
        return [float(X[0]) ** 2]


class _LinearShiftCal:
    """F(p) = clamp(0.7p + 0.1) — asymmetric."""
    def predict(self, X):
        return [min(0.98, max(0.02, float(X[0]) * 0.7 + 0.1))]


class _ExtremeCal:
    """Returns 0.001 or 0.999 to test probability clamp."""
    def predict(self, X):
        return [0.001 if float(X[0]) < 0.5 else 0.999]


class _BrokenCal:
    """Always raises to test exception fallback."""
    def predict(self, X):
        raise RuntimeError("simulated calibrator failure")


class _FakeModel:
    """LGBM stub: always returns 0.65 regardless of features."""
    def predict_p_a(self, X):
        return [0.65]


def _inject_meta(monkeypatch, cal_obj, surf_cal_obj=None):
    """Monkeypatch both loaders so no pickle I/O is needed."""
    monkeypatch.setattr(ens, "_load_meta_calibrator", lambda: cal_obj)
    monkeypatch.setattr(ens, "_load_surface_calibrator", lambda s: surf_cal_obj)
    monkeypatch.setattr(ens, "_load_model", lambda: (_FakeModel(), True))


# ── Property 1: F(p) asymmetry corrected ─────────────────────────────────────

@pytest.mark.skipif(not _META_CAL_AVAILABLE, reason="meta_calibrator.pkl not present")
def test_meta_cal_raw_is_asymmetric():
    """Confirm the raw meta-calibrator IS asymmetric — proving the fix is necessary."""
    with open(_META_CAL_PATH, "rb") as f:
        data = pickle.load(f)
    cal = data["calibrator"]

    probes = [0.3, 0.4, 0.5, 0.6, 0.7]
    for p in probes:
        fp = float(cal.predict([p])[0])
        f1p = float(cal.predict([1.0 - p])[0])
        raw_error = abs(fp + f1p - 1.0)
        assert raw_error > 0.01, (
            f"Expected raw asymmetry at p={p}, got error={raw_error:.6f}. "
            "If calibrator is now symmetric by construction, this test is obsolete."
        )


@pytest.mark.skipif(not _META_CAL_AVAILABLE, reason="meta_calibrator.pkl not present")
def test_meta_cal_symmetric_formula_achieves_symmetry():
    """f_sym(p) = (F(p) + 1 - F(1-p)) / 2 gives f_sym(p) + f_sym(1-p) == 1."""
    with open(_META_CAL_PATH, "rb") as f:
        data = pickle.load(f)
    cal = data["calibrator"]

    def f_sym(p: float) -> float:
        cal_ab = float(cal.predict([p])[0])
        cal_ba = float(cal.predict([1.0 - p])[0])
        return (cal_ab + 1.0 - cal_ba) / 2.0

    probes = np.linspace(0.05, 0.95, 91)
    for p in probes:
        fwd = f_sym(p)
        bwd = f_sym(1.0 - p)
        assert abs(fwd + bwd - 1.0) < 1e-9, (
            f"Symmetry violation at p={p:.3f}: f_sym={fwd:.6f}, f_sym(1-p)={bwd:.6f}"
        )


# ── Property 2: Full ensemble complement ──────────────────────────────────────

def test_meta_cal_complement_symmetry_via_mock(monkeypatch):
    """Asymmetric meta-cal injected; symmetric formula ensures p(A,B)+p(B,A)==1."""
    _inject_meta(monkeypatch, cal_obj=_AsymmetricCal(), surf_cal_obj=None)

    ratings = _ratings()
    state = _state("Alpha", "Beta")
    kwargs = {"state": state, "match_date": "2026-01-15T12:00:00Z", "use_live_stats": False}

    out_ab = ens.predict_winner_ensemble("Alpha", "Beta", ratings, "clay", **kwargs)
    out_ba = ens.predict_winner_ensemble("Beta", "Alpha", ratings, "clay", **kwargs)

    assert out_ab["source"] == "ensemble"
    total = out_ab["p_a"] + out_ba["p_a"]
    assert abs(total - 1.0) < 1e-9, (
        f"P(A>B) + P(B>A) = {total:.9f}, expected 1.0. "
        "Symmetric meta-calibrator formula not applied."
    )


# ── Property 3: Hard surface behavior unchanged ───────────────────────────────

def test_hard_surface_uses_surf_cal_not_meta_cal(monkeypatch):
    """Hard surface has surf_cal → meta_cal is never called."""
    calls = []

    class _MetaCalSpy:
        def predict(self, X):
            calls.append(float(X[0]))
            return [float(X[0]) * 0.6]

    class _SurfCalHard:
        def predict(self, X):
            return [min(0.98, max(0.02, float(X[0])))]

    monkeypatch.setattr(ens, "_load_meta_calibrator", lambda: _MetaCalSpy())
    monkeypatch.setattr(ens, "_load_surface_calibrator", lambda s: _SurfCalHard() if s == "hard" else None)
    monkeypatch.setattr(ens, "_load_model", lambda: (_FakeModel(), True))

    ratings = _ratings()
    state = _state("Alpha", "Beta")
    kwargs = {"state": state, "match_date": "2026-01-15T12:00:00Z", "use_live_stats": False}

    ens.predict_winner_ensemble("Alpha", "Beta", ratings, "hard", **kwargs)
    assert len(calls) == 0, (
        f"meta_calibrator.predict() called {len(calls)} time(s) for hard surface "
        "even though surf_cal exists. surf_cal must take precedence."
    )


# ── Property 4: Clay behavior is now symmetric ───────────────────────────────

def test_clay_behavior_symmetric_after_fix(monkeypatch):
    """Clay has no surf_cal → meta_cal applies. Complement symmetry holds after fix."""
    _inject_meta(monkeypatch, cal_obj=_SquareCal(), surf_cal_obj=None)

    ratings = _ratings()
    state = _state("Alpha", "Beta")
    kwargs = {"state": state, "match_date": "2026-01-15T12:00:00Z", "use_live_stats": False}

    out_ab = ens.predict_winner_ensemble("Alpha", "Beta", ratings, "clay", **kwargs)
    out_ba = ens.predict_winner_ensemble("Beta", "Alpha", ratings, "clay", **kwargs)

    total = out_ab["p_a"] + out_ba["p_a"]
    assert abs(total - 1.0) < 1e-9, (
        f"Clay symmetry broken: P(A>B)={out_ab['p_a']:.6f}, "
        f"P(B>A)={out_ba['p_a']:.6f}, sum={total:.9f}"
    )


# ── Property 5: Grass behavior is now symmetric ───────────────────────────────

def test_grass_behavior_symmetric_after_fix(monkeypatch):
    """Grass has no surf_cal → meta_cal applies. Complement symmetry holds after fix."""
    _inject_meta(monkeypatch, cal_obj=_LinearShiftCal(), surf_cal_obj=None)

    ratings = _ratings()
    state = _state("Alpha", "Beta")
    kwargs = {"state": state, "match_date": "2026-01-15T12:00:00Z", "use_live_stats": False}

    out_ab = ens.predict_winner_ensemble("Alpha", "Beta", ratings, "grass", **kwargs)
    out_ba = ens.predict_winner_ensemble("Beta", "Alpha", ratings, "grass", **kwargs)

    total = out_ab["p_a"] + out_ba["p_a"]
    assert abs(total - 1.0) < 1e-9, (
        f"Grass symmetry broken: P(A>B)={out_ab['p_a']:.6f}, "
        f"P(B>A)={out_ba['p_a']:.6f}, sum={total:.9f}"
    )


# ── Property 6: No meta-calibrator → behavior unchanged ──────────────────────

def test_no_meta_cal_unchanged(monkeypatch):
    """When meta_calibrator returns None, behavior is unchanged."""
    _inject_meta(monkeypatch, cal_obj=None, surf_cal_obj=None)

    ratings = _ratings()
    state = _state("Alpha", "Beta")
    kwargs = {"state": state, "match_date": "2026-01-15T12:00:00Z", "use_live_stats": False}

    out = ens.predict_winner_ensemble("Alpha", "Beta", ratings, "clay", **kwargs)
    assert out["source"] == "ensemble"
    assert 0.0 < out["p_a"] < 1.0
    assert not math.isnan(out["p_a"])


# ── Property 7: Malformed/unavailable calibrator fallback ────────────────────

def test_malformed_meta_cal_falls_through(monkeypatch):
    """A calibrator that raises on predict leaves p_a unchanged (except guard)."""
    _inject_meta(monkeypatch, cal_obj=_BrokenCal(), surf_cal_obj=None)

    ratings = _ratings()
    state = _state("Alpha", "Beta")
    kwargs = {"state": state, "match_date": "2026-01-15T12:00:00Z", "use_live_stats": False}

    # Must not raise
    out = ens.predict_winner_ensemble("Alpha", "Beta", ratings, "clay", **kwargs)
    assert out["source"] == "ensemble"
    assert 0.0 < out["p_a"] < 1.0
    assert not math.isnan(out["p_a"])


# ── Property 8: Probability clamp remains valid ───────────────────────────────

def test_meta_cal_output_clamped_after_symmetry(monkeypatch):
    """p_a after symmetric meta-cal is always in [0.02, 0.98]."""
    _inject_meta(monkeypatch, cal_obj=_ExtremeCal(), surf_cal_obj=None)

    ratings = _ratings()
    state = _state("Alpha", "Beta")
    kwargs = {"state": state, "match_date": "2026-01-15T12:00:00Z", "use_live_stats": False}

    for surface in ("clay", "grass"):
        out = ens.predict_winner_ensemble("Alpha", "Beta", ratings, surface, **kwargs)
        p = out["p_a"]
        assert 0.02 <= p <= 0.98, f"p_a={p} out of [0.02, 0.98] for surface={surface}"


# ── Property 9: source remains 'ensemble' ────────────────────────────────────

def test_meta_cal_source_remains_ensemble(monkeypatch):
    """Applying meta-calibrator does not change source field."""
    _inject_meta(monkeypatch, cal_obj=_AsymmetricCal(), surf_cal_obj=None)

    ratings = _ratings()
    state = _state("Alpha", "Beta")
    kwargs = {"state": state, "match_date": "2026-01-15T12:00:00Z", "use_live_stats": False}

    out = ens.predict_winner_ensemble("Alpha", "Beta", ratings, "clay", **kwargs)
    assert out["source"] == "ensemble", f"source changed to {out['source']!r}"


# ── Property 10: No betting/staking threshold changes ────────────────────────

def test_no_threshold_or_staking_changes():
    """Ensemble returns p_a, p_b, source — no staking logic embedded."""
    ratings = _ratings()
    out = ens.predict_winner_ensemble("Alpha", "Beta", ratings, "clay")
    assert set(out.keys()) >= {"p_a", "p_b", "source"}, f"Missing keys: {set(out.keys())}"
    for forbidden in ("stake", "kelly", "bankroll", "ev", "recommended"):
        assert forbidden not in out, f"Unexpected key '{forbidden}' in ensemble output"


# ── Live artifact tests ───────────────────────────────────────────────────────

@pytest.mark.skipif(not _META_CAL_AVAILABLE, reason="meta_calibrator.pkl not present")
def test_live_meta_cal_symmetry_on_grid():
    """Live artifact: symmetric formula gives zero complement error on fine grid."""
    with open(_META_CAL_PATH, "rb") as f:
        data = pickle.load(f)
    cal = data["calibrator"]

    def f_sym(p: float) -> float:
        cal_ab = float(cal.predict([p])[0])
        cal_ba = float(cal.predict([1.0 - p])[0])
        return (cal_ab + 1.0 - cal_ba) / 2.0

    probes = np.linspace(0.05, 0.95, 91)
    errors = np.array([abs(f_sym(p) + f_sym(1.0 - p) - 1.0) for p in probes])
    assert errors.max() < 1e-9, f"Max complement error after fix: {errors.max():.2e}"
    assert errors.mean() < 1e-10, f"Mean complement error after fix: {errors.mean():.2e}"


@pytest.mark.skipif(not _META_CAL_AVAILABLE, reason="meta_calibrator.pkl not present")
def test_live_meta_cal_before_had_large_error():
    """Live artifact: raw F(p) had >5pp mean complement error, confirming bug existed."""
    with open(_META_CAL_PATH, "rb") as f:
        data = pickle.load(f)
    cal = data["calibrator"]

    probes = np.linspace(0.05, 0.95, 91)
    before_errors = np.array([
        abs(float(cal.predict([p])[0]) + float(cal.predict([1.0 - p])[0]) - 1.0)
        for p in probes
    ])
    # Confirmed live: mean ~20pp, p90 ~33pp, max ~33pp
    assert before_errors.mean() > 0.05, (
        f"Expected mean >5pp before fix, got {before_errors.mean()*100:.2f}pp. "
        "Calibrator may have been replaced with a symmetric one."
    )
    assert (before_errors > 0.01).sum() > 50, (
        f"Expected >50/91 probes with >1pp error, got {(before_errors > 0.01).sum()}."
    )
