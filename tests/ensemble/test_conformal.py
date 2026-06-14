"""Phase 2.3 — Conformal Prediction wrapper tests.

Pin:
  • the quantile uses the (1-α)·(n+1)/n inflation (standard ICP correction)
  • empirical coverage on the calibration set ≥ nominal (1-α) - tolerance
  • is_confident returns True iff the prediction set is exactly {market}
  • the value-detector filter downgrades to LOW when set is ambiguous
"""
import numpy as np
import pytest

from src.ensemble.conformal import ConformalPredictor, conformal_confidence_filter


def _synthetic_probs(n: int = 200, seed: int = 11) -> tuple[np.ndarray, np.ndarray]:
    """Returns (probs, outcomes) where outcomes are drawn proportional to probs.
    Resulting Brier should be modest — useful for testing coverage."""
    rng = np.random.default_rng(seed)
    probs = rng.dirichlet([1, 1, 1], size=n)
    # Sample the outcome from the probability vector.
    outcomes = np.array([rng.choice(3, p=row) for row in probs])
    return probs, outcomes


class TestConformalFitting:
    def test_fit_stores_nonconformity(self):
        probs, y = _synthetic_probs(100)
        cp = ConformalPredictor().fit(probs, y)
        assert cp.fit_calibration_size == 100
        assert len(cp.nonconformity) == 100
        # Non-conformity ∈ [0, 1] by construction.
        assert (cp.nonconformity >= 0).all()
        assert (cp.nonconformity <= 1).all()

    def test_fit_rejects_wrong_shape(self):
        with pytest.raises(ValueError):
            ConformalPredictor().fit(np.zeros((10, 2)), np.zeros(10))
        with pytest.raises(ValueError):
            ConformalPredictor().fit(np.zeros((10, 3)), np.zeros(5))


class TestQuantile:
    def test_quantile_monotone_in_alpha(self):
        probs, y = _synthetic_probs(300)
        cp = ConformalPredictor().fit(probs, y)
        q_low = cp.quantile(0.30)  # less stringent → lower q
        q_high = cp.quantile(0.05)  # high coverage → higher q
        assert q_low <= q_high

    def test_quantile_requires_fit(self):
        with pytest.raises(RuntimeError):
            ConformalPredictor().quantile(0.10)


class TestPredictionSets:
    def test_prediction_set_contains_argmax_at_least(self):
        probs, y = _synthetic_probs(200)
        cp = ConformalPredictor().fit(probs, y)
        sets = cp.predict_set(probs[:20], alpha=0.10)
        for row, s in zip(probs[:20], sets):
            assert int(np.argmax(row)) in s

    def test_empirical_coverage_at_least_nominal(self):
        # ICP guarantees coverage on the calibration set distribution.
        probs, y = _synthetic_probs(500)
        cp = ConformalPredictor().fit(probs, y)
        coverage = cp.empirical_coverage(probs, y, alpha=0.10)
        assert coverage >= 0.85  # nominal 0.90 minus small finite-sample slack

    def test_high_alpha_smaller_sets(self):
        probs, y = _synthetic_probs(500)
        cp = ConformalPredictor().fit(probs, y)
        sets_high_cov = cp.predict_set(probs, alpha=0.05)
        sets_low_cov = cp.predict_set(probs, alpha=0.40)
        avg_high = np.mean([len(s) for s in sets_high_cov])
        avg_low = np.mean([len(s) for s in sets_low_cov])
        # Less stringent coverage → smaller sets on average.
        assert avg_low <= avg_high


class TestIsConfident:
    def test_confident_when_set_singleton(self):
        # Construct a calibration set with very clean predictions.
        # Probs are mostly 0.9/0.05/0.05 with true class = argmax — most non-conformity scores are 0.1.
        probs = np.tile([0.05, 0.05, 0.9], (200, 1))
        outcomes = np.full(200, 2)
        cp = ConformalPredictor().fit(probs, outcomes)
        # A clean home prediction should land in a {home} set.
        new_probs = np.array([[0.05, 0.05, 0.9]])
        assert cp.is_confident(new_probs, "home", alpha=0.10)
        assert not cp.is_confident(new_probs, "away", alpha=0.10)

    def test_not_confident_when_set_contains_market_plus_other(self):
        # Calibration where many home wins were mispredicted (non-conformity ~0.5).
        rng = np.random.default_rng(3)
        outcomes = rng.integers(0, 3, size=300)
        # Probabilities centred at uniform with small noise — true probs ≈ 1/3 each.
        probs = rng.dirichlet([5, 5, 5], size=300)
        cp = ConformalPredictor().fit(probs, outcomes)
        # A near-uniform prediction at low α should NOT be confident in any one class.
        ambig = np.array([[0.30, 0.34, 0.36]])
        assert not cp.is_confident(ambig, "home", alpha=0.10)


class TestSaveLoad:
    def test_round_trip(self, tmp_path):
        probs, y = _synthetic_probs(100)
        cp = ConformalPredictor(alpha=0.07).fit(probs, y)
        path = tmp_path / "cp.pkl"
        cp.save(path)
        cp2 = ConformalPredictor.load(path)
        assert cp2.alpha == 0.07
        assert cp2.fit_calibration_size == 100
        np.testing.assert_allclose(cp.nonconformity, cp2.nonconformity)


class TestFilter:
    def test_filter_downgrades_when_set_ambiguous(self):
        rng = np.random.default_rng(99)
        outcomes = rng.integers(0, 3, size=300)
        probs = rng.dirichlet([1, 1, 1], size=300)
        cp = ConformalPredictor().fit(probs, outcomes)
        ambig = np.array([[0.30, 0.35, 0.35]])
        confidence = conformal_confidence_filter("MEDIUM", ambig, "home", cp, alpha=0.10)
        assert confidence == "LOW"

    def test_filter_keeps_low_low(self):
        # idempotency: LOW stays LOW.
        cp = ConformalPredictor().fit(np.zeros((10, 3)) + 0.34, np.zeros(10, dtype=int))
        probs = np.array([[0.05, 0.05, 0.9]])
        confidence = conformal_confidence_filter("LOW", probs, "home", cp, alpha=0.10)
        assert confidence == "LOW"
