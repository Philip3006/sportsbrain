"""Phase 2.1 — Stacking Meta-Learner.

Replaces the fixed `DC × dc_weight + LGBM × (1 - dc_weight)` linear blend with
a learned meta-classifier. Stage-1 outputs (DC, optional LGBM, market-implied
Shin probabilities, plus context flags) are concatenated into a feature vector
and a logistic regression maps them to calibrated 1X2 probabilities.

The fixed blend in `combiner.blend()` was optimised on a single WC2022 holdout
(`find_optimal_weight`) and applied identically to every match. The stacker can
learn:
  • Different weights for home- vs away-favoured matches.
  • Trust adjustments when DC and market disagree by > 10pp (Lever 6).
  • Confederation / neutral / knockout interaction effects.

Walk-forward training (scripts/train_stacker.py) avoids leakage: for each
tournament event we train on all earlier events and predict on the held-out one.
The final shipped stacker is fit on all events combined.

Feature-flagged via `STACKER_ENABLED` (defaults False) so Phase 2.1 can roll
out without disturbing the current scanner until the WC2022 backtest gate is
re-validated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import pickle

import numpy as np
from sklearn.linear_model import LogisticRegression


# Feature column order — frozen at fit() time so save/load round-trip stays
# stable. Adding a feature requires bumping the model version.
_FEATURE_COLUMNS = [
    "dc_p_away", "dc_p_draw", "dc_p_home",
    "lgbm_p_away", "lgbm_p_draw", "lgbm_p_home",
    "shin_p_away", "shin_p_draw", "shin_p_home",
    "is_knockout", "is_neutral",
    "dc_vs_shin_home", "dc_vs_shin_draw", "dc_vs_shin_away",  # divergence
    "dc_vs_lgbm_home", "dc_vs_lgbm_draw", "dc_vs_lgbm_away",  # ensemble disagreement
]


def build_stacker_features(
    dc_probs: dict[str, float],
    lgbm_probs: "np.ndarray | None",
    shin_probs: "tuple[float, float, float] | None",
    is_knockout: bool = False,
    is_neutral: bool = False,
) -> np.ndarray:
    """Returns a (n_features,) numpy array in `_FEATURE_COLUMNS` order.

    lgbm_probs: optional (3,) array [p_away, p_draw, p_home]. When None,
        falls back to DC probs (i.e. the stacker reduces to a context-aware
        DC re-calibrator for matches missing an ensemble model).
    shin_probs: optional (p_home, p_draw, p_away) — Shin-debiased market.
        When None, uses uniform 1/3 baseline so the divergence terms are 0.
    """
    dc_a, dc_d, dc_h = dc_probs["p_away"], dc_probs["p_draw"], dc_probs["p_home"]
    if lgbm_probs is None:
        lgbm_a, lgbm_d, lgbm_h = dc_a, dc_d, dc_h
    else:
        lgbm_a, lgbm_d, lgbm_h = float(lgbm_probs[0]), float(lgbm_probs[1]), float(lgbm_probs[2])
    if shin_probs is None:
        shin_h, shin_d, shin_a = 1.0 / 3, 1.0 / 3, 1.0 / 3
    else:
        shin_h, shin_d, shin_a = shin_probs

    return np.array([
        dc_a, dc_d, dc_h,
        lgbm_a, lgbm_d, lgbm_h,
        shin_a, shin_d, shin_h,
        1.0 if is_knockout else 0.0,
        1.0 if is_neutral else 0.0,
        dc_h - shin_h, dc_d - shin_d, dc_a - shin_a,
        dc_h - lgbm_h, dc_d - lgbm_d, dc_a - lgbm_a,
    ], dtype=np.float64)


@dataclass
class Stacker:
    """Logistic-regression meta-classifier on top of DC + LGBM + Market.

    fit() expects:
      X: (N, len(_FEATURE_COLUMNS)) feature matrix built by build_stacker_features
      y: (N,) int array, values ∈ {0=away, 1=draw, 2=home}

    predict_proba() returns (N, 3) array [p_away, p_draw, p_home] summing to 1.
    """
    model: "LogisticRegression | None" = None
    feature_columns: list[str] = field(default_factory=lambda: list(_FEATURE_COLUMNS))
    n_training_samples: int = 0
    fit_metadata: dict = field(default_factory=dict)

    def fit(self, X: np.ndarray, y: np.ndarray,
            C: float = 1.0, max_iter: int = 1000) -> "Stacker":
        """Fits multinomial logistic regression.

        Uses lbfgs solver (multinomial loss, no class_weight). When the held-out
        backtest has imbalanced outcomes (typical: draws under-represented), C
        can be lowered for stronger L2 shrinkage.
        """
        if X.ndim != 2 or X.shape[1] != len(self.feature_columns):
            raise ValueError(
                f"X must be 2D with {len(self.feature_columns)} cols, got {X.shape}"
            )
        # sklearn ≥1.5 picks multinomial automatically when n_classes > 2;
        # we pass only solver + C explicitly.
        self.model = LogisticRegression(
            solver="lbfgs",
            C=C,
            max_iter=max_iter,
        )
        self.model.fit(X, y)
        self.n_training_samples = int(len(X))
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("Stacker has not been fit yet")
        if X.ndim == 1:
            X = X.reshape(1, -1)
        proba = self.model.predict_proba(X)  # shape (N, k)
        # sklearn orders classes ascending; we need [p_away=0, p_draw=1, p_home=2]
        classes = list(self.model.classes_)
        out = np.zeros((proba.shape[0], 3))
        for col, cls in enumerate(classes):
            if cls in (0, 1, 2):
                out[:, int(cls)] = proba[:, col]
        # Normalise (defensive — sklearn already returns rows summing to 1).
        row_sums = out.sum(axis=1, keepdims=True)
        row_sums[row_sums < 1e-12] = 1.0
        return out / row_sums

    def predict_one(
        self,
        dc_probs: dict[str, float],
        lgbm_probs: "np.ndarray | None" = None,
        shin_probs: "tuple[float, float, float] | None" = None,
        is_knockout: bool = False,
        is_neutral: bool = False,
    ) -> dict[str, float]:
        """Convenience wrapper: returns {p_home, p_draw, p_away} for a single match."""
        x = build_stacker_features(dc_probs, lgbm_probs, shin_probs,
                                    is_knockout=is_knockout, is_neutral=is_neutral)
        p = self.predict_proba(x.reshape(1, -1))[0]
        return {"p_away": float(p[0]), "p_draw": float(p[1]), "p_home": float(p[2])}

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls, path: Path) -> "Stacker":
        with open(path, "rb") as f:
            obj = pickle.load(f)
        if not isinstance(obj, cls):
            raise TypeError(f"Loaded object is {type(obj).__name__}, expected Stacker")
        return obj


def feature_columns() -> list[str]:
    return list(_FEATURE_COLUMNS)
