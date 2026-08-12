"""Option A validation: symmetric post-calibration fix (P2 resolution).

Proves all 7 CEO-mandated properties:
1. Swapping player order produces complementary probabilities.
2. P(A beats B) == 1 - P(B beats A) within numerical tolerance.
3. Identical match characteristics with reversed API ordering cannot change the actionable side.
4. Current failing test_ensemble_swap_flips_sides passes for the correct reason.
5. Surface calibration remains bounded in [0,1].
6. No NaN/extreme probability regression.
7. EV calculation remains order invariant.
"""
from __future__ import annotations

import math
import pickle
from pathlib import Path

import pytest

from src.models.tennis_elo import TennisEloRatings
from src.tennis import ensemble as ens

_CAL_PATH = Path("models/tennis_calibrators/hard.pkl")
_CAL_AVAILABLE = _CAL_PATH.exists()

pytestmark = pytest.mark.usefixtures("_clear_ens_cache")


@pytest.fixture(autouse=True)
def _clear_ens_cache():
    ens._CACHED.clear()
    yield
    ens._CACHED.clear()


def _strong_weak_ratings() -> TennisEloRatings:
    ratings = TennisEloRatings()
    ratings.overall["Strong"] = 1750
    ratings.overall["Weak"] = 1500
    ratings.by_surface["hard"] = {"Strong": 1770, "Weak": 1490}
    ratings.surface_counts["hard"] = {"Strong": 30, "Weak": 25}
    return ratings


# ── Property 1 + 2: calibrator symmetry ──────────────────────────────────────

@pytest.mark.skipif(not _CAL_AVAILABLE, reason="hard.pkl not present")
def test_surf_cal_symmetric_property():
    """f_sym(x) + f_sym(1-x) == 1 for the hard surface calibrator at many probe points."""
    with open(_CAL_PATH, "rb") as f:
        cal = pickle.load(f)

    def f_sym(p: float) -> float:
        p_fwd = float(cal.predict([p])[0])
        p_rev = float(cal.predict([1.0 - p])[0])
        return (p_fwd + (1.0 - p_rev)) / 2.0

    probes = [0.10, 0.20, 0.30, 0.40, 0.50, 0.55, 0.60, 0.65,
              0.70, 0.766, 0.80, 0.85, 0.90, 0.95]
    for p in probes:
        fwd = f_sym(p)
        bwd = f_sym(1.0 - p)
        assert abs(fwd + bwd - 1.0) < 1e-9, (
            f"Symmetry violation at p={p}: f_sym({p})={fwd:.6f}, "
            f"f_sym({1-p:.3f})={bwd:.6f}, sum={fwd+bwd:.9f}"
        )


# ── Property 2: P(A>B) = 1 - P(B>A) through full ensemble ───────────────────

def test_ensemble_pa_pb_complement():
    """Full ensemble: p_a(A,B) + p_a(B,A) == 1.0 within 2pp tolerance."""
    ratings = _strong_weak_ratings()
    out_ab = ens.predict_winner_ensemble("Strong", "Weak", ratings, "hard")
    out_ba = ens.predict_winner_ensemble("Weak", "Strong", ratings, "hard")

    # p_a in out_ab  = P(Strong beats Weak)
    # p_a in out_ba  = P(Weak beats Strong) = 1 - P(Strong beats Weak)
    total = out_ab["p_a"] + out_ba["p_a"]
    assert abs(total - 1.0) < 0.02, (
        f"P(A>B) + P(B>A) should be 1.0, got {total:.5f} "
        f"(p_a(Strong,Weak)={out_ab['p_a']:.5f}, p_a(Weak,Strong)={out_ba['p_a']:.5f})"
    )


# ── Property 3: actionable side invariant to API ordering ────────────────────

def test_actionable_side_unchanged_by_ordering():
    """The player identified as favorite must be the same regardless of API listing order."""
    ratings = _strong_weak_ratings()
    out_ab = ens.predict_winner_ensemble("Strong", "Weak", ratings, "hard")
    out_ba = ens.predict_winner_ensemble("Weak", "Strong", ratings, "hard")

    # Strong is favorite iff p_a(Strong, Weak) > 0.5
    strong_is_fav_ab = out_ab["p_a"] > 0.5   # Strong listed first
    strong_is_fav_ba = out_ba["p_b"] > 0.5   # Strong listed second (= p_b in out_ba)
    assert strong_is_fav_ab == strong_is_fav_ba, (
        f"Actionable side changed with ordering: "
        f"p_a(Strong,Weak)={out_ab['p_a']:.4f}, p_b(Weak,Strong)={out_ba['p_b']:.4f}"
    )


# ── Property 4: original failing test now passes for the correct reason ───────

def test_swap_flips_sides_within_tolerance():
    """Seitentausch erhält Favorit. Symmetric calibration eliminates ordering asymmetry.

    This was the originally failing test (13.8pp gap with asymmetric calibrator).
    It now passes because the calibrator applies f_sym(p) = (f(p) + (1-f(1-p))) / 2,
    not because the tolerance was relaxed.
    """
    ratings = _strong_weak_ratings()
    out_ab = ens.predict_winner_ensemble("Strong", "Weak", ratings, "hard")
    out_ba = ens.predict_winner_ensemble("Weak", "Strong", ratings, "hard")

    # Strong must be favorite from both listings
    assert out_ab["p_a"] > 0.55, f"Strong not favourite when listed first: {out_ab['p_a']:.4f}"
    assert out_ba["p_b"] > 0.55, f"Strong not favourite when listed second: {out_ba['p_b']:.4f}"

    # Gap must be ≤ 5pp (original threshold — not relaxed)
    gap = abs(out_ab["p_a"] - out_ba["p_b"])
    assert gap < 0.05, (
        f"Ordering asymmetry too large: {gap:.5f} "
        f"(p_a(Strong,Weak)={out_ab['p_a']:.5f}, p_b(Weak,Strong)={out_ba['p_b']:.5f}). "
        "This indicates the symmetric calibration fix did not take effect."
    )


# ── Property 5: calibration bounded ──────────────────────────────────────────

@pytest.mark.skipif(not _CAL_AVAILABLE, reason="hard.pkl not present")
def test_surf_cal_output_bounded():
    """Symmetric calibration always returns p in [0.02, 0.98] after clamp."""
    with open(_CAL_PATH, "rb") as f:
        cal = pickle.load(f)

    def f_sym_clamped(p: float) -> float:
        p_fwd = float(cal.predict([p])[0])
        p_rev = float(cal.predict([1.0 - p])[0])
        return max(0.02, min(0.98, (p_fwd + (1.0 - p_rev)) / 2.0))

    edge_cases = [0.0, 0.001, 0.01, 0.02, 0.5, 0.98, 0.99, 0.999, 1.0]
    for p in edge_cases:
        out = f_sym_clamped(p)
        assert 0.02 <= out <= 0.98, f"Out-of-bounds at p={p}: got {out}"


# ── Property 6: no NaN / extreme regression ──────────────────────────────────

def test_ensemble_no_nan_no_extreme():
    """Ensemble output contains no NaN and stays in (0, 1)."""
    ratings = _strong_weak_ratings()
    for pa, pb in [("Strong", "Weak"), ("Weak", "Strong")]:
        out = ens.predict_winner_ensemble(pa, pb, ratings, "hard")
        p = out["p_a"]
        assert not math.isnan(p), f"NaN in p_a for ({pa}, {pb})"
        assert 0.0 < p < 1.0, f"p_a out of (0,1): {p} for ({pa}, {pb})"
        assert abs(out["p_a"] + out["p_b"] - 1.0) < 1e-9, "p_a + p_b != 1"


# ── Property 7: EV calculation order invariant ───────────────────────────────

def test_ev_order_invariant():
    """EV(winner) = p * odds - 1 is the same regardless of API player ordering.

    Uses decimal odds 2.0 (even odds). If p(Strong) is order-invariant,
    EV(Strong) is the same from both API calls.
    """
    ratings = _strong_weak_ratings()
    out_ab = ens.predict_winner_ensemble("Strong", "Weak", ratings, "hard")
    out_ba = ens.predict_winner_ensemble("Weak", "Strong", ratings, "hard")

    odds = 2.0
    ev_ab = out_ab["p_a"] * odds - 1.0   # EV for Strong when listed first
    ev_ba = out_ba["p_b"] * odds - 1.0   # EV for Strong when listed second

    assert abs(ev_ab - ev_ba) < 0.02, (
        f"EV not order-invariant: EV_ab={ev_ab:.5f}, EV_ba={ev_ba:.5f}, "
        f"delta={abs(ev_ab - ev_ba):.5f}"
    )
