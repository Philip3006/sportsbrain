"""Phase 2.3 — Inductive Conformal Predictor for 1X2 probabilities.

Wraps any probability source (stacker output, blended ensemble, raw DC) and
turns each match's prediction into a distribution-free prediction *set* with
calibrated coverage. The signal pipeline then asks:

  "If the model claims p_home=0.85, would a 90 %-coverage set still contain
   only HOME, or would it expand to include DRAW (and so warrant LOW)?"

If a single class fills the prediction set, the model is confident in the
direct sense the audit cares about (Canada-Bosnia at 98.3 % is *not* this kind
of confident — its set at α=0.10 would include DRAW). When ≥2 classes are in
the set, the signal pipeline downgrades to LOW.

Non-conformity score
  s(prob, y) = 1 - prob[y]     (1 - probability assigned to the true class)
Calibration
  Compute s on a held-out calibration set, take the (1-α)·(n+1)/n quantile q.
Prediction set for a new match with prob p
  {y : 1 - p[y] ≤ q}  = {y : p[y] ≥ 1 - q}

This guarantees marginal coverage of (1 - α) when calibration data is
exchangeable with future matches.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import pickle

import numpy as np


@dataclass
class ConformalPredictor:
    """Inductive Conformal Predictor for 3-class 1X2 probabilities.

    Fit on (probs, outcomes) from a calibration set; produces prediction sets
    at any α ∈ (0, 1) at inference time.
    """
    alpha: float = 0.10  # nominal mis-coverage; default 90 % coverage
    nonconformity: np.ndarray = field(default_factory=lambda: np.zeros(0))
    fit_calibration_size: int = 0

    def fit(self, probs: np.ndarray, outcomes: np.ndarray) -> "ConformalPredictor":
        """Compute non-conformity scores on the calibration set.

        probs: (N, 3) probability matrix in [p_away, p_draw, p_home] order.
        outcomes: (N,) integers in {0, 1, 2}.
        """
        if probs.ndim != 2 or probs.shape[1] != 3:
            raise ValueError(f"probs must be (N, 3), got {probs.shape}")
        if len(probs) != len(outcomes):
            raise ValueError("probs and outcomes length mismatch")
        # s_i = 1 - p_i[y_i]
        idx = np.arange(len(outcomes))
        true_probs = probs[idx, outcomes]
        self.nonconformity = 1.0 - true_probs
        self.fit_calibration_size = int(len(probs))
        return self

    def quantile(self, alpha: float | None = None) -> float:
        """Returns the (1-α)·(n+1)/n quantile of non-conformity scores."""
        if alpha is None:
            alpha = self.alpha
        n = len(self.nonconformity)
        if n == 0:
            raise RuntimeError("ConformalPredictor has not been fit yet")
        # Conformal correction: use ceil((1-α)*(n+1))/n-th order statistic.
        k = int(np.ceil((1.0 - alpha) * (n + 1)))
        k = max(1, min(k, n))  # clamp into [1, n]
        sorted_scores = np.sort(self.nonconformity)
        return float(sorted_scores[k - 1])

    def predict_set(
        self,
        probs: np.ndarray,
        alpha: float | None = None,
    ) -> list[set[int]]:
        """Returns a list of prediction sets (one per row of `probs`).

        Each set contains every class y where `probs[y] >= 1 - q` for the
        calibration quantile q at the requested α.
        """
        if probs.ndim == 1:
            probs = probs.reshape(1, -1)
        q = self.quantile(alpha)
        threshold = 1.0 - q
        sets: list[set[int]] = []
        for row in probs:
            s = {int(i) for i in range(3) if row[i] >= threshold}
            if not s:
                # Coverage guarantee: include the argmax so the set is never empty
                s = {int(np.argmax(row))}
            sets.append(s)
        return sets

    def is_confident(
        self,
        probs: np.ndarray,
        market: str,
        alpha: float | None = None,
    ) -> bool:
        """True iff the prediction set for `market` contains only that class.

        market ∈ {"home", "draw", "away"}.
        """
        cls = {"away": 0, "draw": 1, "home": 2}[market]
        if probs.ndim == 1:
            probs = probs.reshape(1, -1)
        sets = self.predict_set(probs, alpha=alpha)
        return sets[0] == {cls}

    def empirical_coverage(
        self,
        probs: np.ndarray,
        outcomes: np.ndarray,
        alpha: float | None = None,
    ) -> float:
        """Returns the fraction of cases where the prediction set contains
        the true outcome. Useful for the test gate (Phase 2 verification:
        empirical coverage 88-92% at α=0.10)."""
        sets = self.predict_set(probs, alpha=alpha)
        hits = sum(1 for s, y in zip(sets, outcomes) if int(y) in s)
        return hits / len(outcomes)

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls, path: Path) -> "ConformalPredictor":
        with open(path, "rb") as f:
            obj = pickle.load(f)
        if not isinstance(obj, cls):
            raise TypeError(f"Loaded object is {type(obj).__name__}, expected ConformalPredictor")
        return obj


def conformal_confidence_filter(
    confidence: str,
    probs: np.ndarray,
    market: str,
    predictor: ConformalPredictor,
    alpha: float | None = None,
) -> str:
    """If the conformal prediction set for the bet's market contains > 1 class,
    downgrade confidence to LOW.

    Used inside value_detector.set_confidence-like cascades — composes with
    the existing _consistency_confidence + _bias_safety_confidence chain.
    """
    if confidence == "LOW":
        return confidence
    if not predictor.is_confident(probs, market, alpha=alpha):
        return "LOW"
    return confidence
