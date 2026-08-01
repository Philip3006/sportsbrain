"""Tennis LightGBM 2-Class Modell (Roadmap J2-K).

P(player_a wins | features) via GradientBoostingClassifier (LightGBM wenn
verfügbar, sonst sklearn HistGradientBoostingClassifier als Drop-in).
Isotonic-Calibration analog Football-Pipeline (`src/models/lgbm_model.py`).

Persistierung: `models/tennis_lgbm/model.pkl` + `calibrator.pkl` +
`metadata.json` mit Feature-Order, Brier, Trainings-Rows.
"""
from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import IsotonicRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, log_loss

from src.tennis.features import FEATURE_COLUMNS


@dataclass
class TennisLGBM:
    """Container für Modell + Kalibrator + Feature-Order.

    Predict-Kontrakt: `predict_proba(X)` liefert (n, 2)-Array mit
    P(player_b_wins), P(player_a_wins).
    """
    model: object
    calibrator: IsotonicRegression | None
    feature_columns: tuple[str, ...] = FEATURE_COLUMNS

    def predict_proba(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        if isinstance(X, pd.DataFrame):
            X = X[list(self.feature_columns)].to_numpy()
        raw = self.model.predict_proba(X)[:, 1]
        if self.calibrator is not None:
            cal = self.calibrator.predict(raw)
            cal = np.clip(cal, 1e-4, 1 - 1e-4)
        else:
            cal = raw
        return np.column_stack([1 - cal, cal])

    def predict_p_a(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        return self.predict_proba(X)[:, 1]


def train_tennis_lgbm(
    X_train: pd.DataFrame,
    y_train: np.ndarray,
    X_cal: pd.DataFrame | None = None,
    y_cal: np.ndarray | None = None,
    max_iter: int = 800,
    learning_rate: float = 0.03,
    max_depth: int = 7,
    l2_regularization: float = 2.0,
    random_state: int = 42,
) -> TennisLGBM:
    """Trainiert HistGradientBoosting + optional Isotonic-Calibrator.

    Args:
        X_train/y_train: Training-Split (y ∈ {0, 1}, 1 = player_a gewinnt)
        X_cal/y_cal: Kalibrations-Split (getrennt für Isotonic).
                     Wenn None, wird Kalibrator übersprungen.
    """
    X_train = X_train[list(FEATURE_COLUMNS)]
    model = HistGradientBoostingClassifier(
        max_iter=max_iter,
        learning_rate=learning_rate,
        max_depth=max_depth,
        l2_regularization=l2_regularization,
        random_state=random_state,
        early_stopping=True,
        validation_fraction=0.15,
        n_iter_no_change=25,
    )
    model.fit(X_train.to_numpy(), y_train)

    calibrator = None
    if X_cal is not None and y_cal is not None and len(X_cal):
        raw = model.predict_proba(X_cal[list(FEATURE_COLUMNS)].to_numpy())[:, 1]
        calibrator = IsotonicRegression(out_of_bounds="clip")
        calibrator.fit(raw, y_cal)

    return TennisLGBM(model=model, calibrator=calibrator)


def evaluate(model: TennisLGBM, X: pd.DataFrame, y: np.ndarray) -> dict[str, float]:
    p_a = model.predict_p_a(X)
    return {
        "n": int(len(y)),
        "brier": float(brier_score_loss(y, p_a)),
        "log_loss": float(log_loss(y, np.clip(p_a, 1e-6, 1 - 1e-6))),
        "accuracy": float(((p_a > 0.5) == y.astype(bool)).mean()),
        "mean_pred": float(p_a.mean()),
        "mean_actual": float(y.mean()),
    }


FEATURE_VERSION = "j2m-serve-2026-08-01"  # bump when FEATURE_COLUMNS changes


def save(model: TennisLGBM, out_dir: Path, n_train_rows: int | None = None,
         extra_meta: dict | None = None) -> None:
    """Persist model + optional trained_at/feature_version metadata (J8-M2).

    n_train_rows: passed through to metadata.json for Drift-Diagnose.
    extra_meta: merged into metadata.json (e.g. gate_passed, brier).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "model.pkl").open("wb") as f:
        pickle.dump(model.model, f)
    if model.calibrator is not None:
        with (out_dir / "calibrator.pkl").open("wb") as f:
            pickle.dump(model.calibrator, f)
    (out_dir / "feature_columns.json").write_text(
        json.dumps(list(model.feature_columns), indent=2)
    )

    # J8-M2: Retrain-Metadata für Drift-Diagnose. Bestehende metadata.json (mit
    # gate_passed etc.) wird gemergt, nicht überschrieben.
    meta_path = out_dir / "metadata.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            meta = {}
    meta["trained_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta["feature_version"] = FEATURE_VERSION
    meta["n_features"] = len(model.feature_columns)
    if n_train_rows is not None:
        meta["n_train_rows"] = int(n_train_rows)
    if extra_meta:
        meta.update(extra_meta)
    meta_path.write_text(json.dumps(meta, indent=2))


def load(model_dir: Path) -> TennisLGBM:
    with (model_dir / "model.pkl").open("rb") as f:
        mdl = pickle.load(f)
    cal_path = model_dir / "calibrator.pkl"
    calibrator = None
    if cal_path.exists():
        with cal_path.open("rb") as f:
            calibrator = pickle.load(f)
    feats = tuple(json.loads((model_dir / "feature_columns.json").read_text()))
    return TennisLGBM(model=mdl, calibrator=calibrator, feature_columns=feats)
