"""Tests für src/models/tennis_lgbm.py (J2-K Phase 2)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.models import tennis_lgbm as tlgbm
from src.tennis.features import FEATURE_COLUMNS


def _synthetic_dataset(n: int = 400, seed: int = 42) -> tuple[pd.DataFrame, np.ndarray]:
    """Baut synthetischen Datensatz mit klarer elo_diff→y-Beziehung."""
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        elo_diff = rng.normal(0, 100)
        p = 1 / (1 + 10 ** (-elo_diff / 400))
        y = int(rng.random() < p)
        row = {c: 0.0 for c in FEATURE_COLUMNS}
        row["elo_diff"] = elo_diff
        row["elo_surface_diff"] = elo_diff * 0.9
        row["rank_diff"] = -elo_diff / 5  # negative-korreliert
        rows.append((row, y))
    X = pd.DataFrame([r[0] for r in rows])
    y_arr = np.array([r[1] for r in rows])
    return X, y_arr


def test_train_predict_shape():
    X, y = _synthetic_dataset(300)
    model = tlgbm.train_tennis_lgbm(X, y, max_iter=50, learning_rate=0.1)
    probs = model.predict_proba(X)
    assert probs.shape == (300, 2)
    assert np.allclose(probs.sum(axis=1), 1.0, atol=1e-5)


def test_predict_p_a_learns_signal():
    X, y = _synthetic_dataset(600)
    train_X = X.iloc[:400]
    train_y = y[:400]
    test_X = X.iloc[400:]
    test_y = y[400:]
    model = tlgbm.train_tennis_lgbm(train_X, train_y, max_iter=100)
    p_a = model.predict_p_a(test_X)
    # Predictions correlate mit elo_diff
    corr = np.corrcoef(test_X["elo_diff"].to_numpy(), p_a)[0, 1]
    assert corr > 0.5, f"Model failed to learn elo_diff signal: corr={corr}"


def test_calibrator_used_when_provided():
    X, y = _synthetic_dataset(600)
    train_X = X.iloc[:300]
    train_y = y[:300]
    cal_X = X.iloc[300:450]
    cal_y = y[300:450]
    model = tlgbm.train_tennis_lgbm(train_X, train_y, cal_X, cal_y, max_iter=50)
    assert model.calibrator is not None


def test_save_load_roundtrip(tmp_path):
    X, y = _synthetic_dataset(200)
    model = tlgbm.train_tennis_lgbm(X, y, max_iter=30)
    tlgbm.save(model, tmp_path)
    loaded = tlgbm.load(tmp_path)
    assert list(loaded.feature_columns) == list(model.feature_columns)
    orig = model.predict_p_a(X)
    reloaded = loaded.predict_p_a(X)
    np.testing.assert_allclose(orig, reloaded)


def test_evaluate_returns_metrics():
    X, y = _synthetic_dataset(400)
    model = tlgbm.train_tennis_lgbm(X, y, max_iter=50)
    metrics = tlgbm.evaluate(model, X, y)
    assert metrics["n"] == 400
    assert 0.0 < metrics["brier"] < 0.5
    assert 0.0 < metrics["accuracy"] < 1.0
