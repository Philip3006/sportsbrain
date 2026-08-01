"""J8-M1: Model-Correctness-Assertions.

Bisher: `test_lgbm.py` prüft nur Shape/Save-Load, nicht dass die trainierte
Klassifikation die Elo-→-Winner-Monotonie erhält. Dieser Test bildet einen
synthetischen Datensatz mit klarer elo_diff→y-Korrelation und asserted die
Monotonie in den Vorhersagen.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.models import tennis_lgbm as tlgbm
from src.tennis.features import FEATURE_COLUMNS


def _synthetic_dataset(n: int = 1000, seed: int = 42) -> tuple[pd.DataFrame, np.ndarray]:
    """Baue synthetischen Trainingsdatensatz mit klarer elo_diff→y-Beziehung.

    - elo_diff aus U(-400, +400)
    - p(y=1) = sigmoid(elo_diff / 200)  (starke Monotonie)
    - alle anderen Features Rauschen
    """
    rng = np.random.default_rng(seed)
    rows = []
    ys = []
    for _ in range(n):
        elo_diff = rng.uniform(-400, 400)
        p = 1.0 / (1.0 + np.exp(-elo_diff / 200))
        y = int(rng.random() < p)
        row = {c: rng.normal(0, 0.5) for c in FEATURE_COLUMNS}
        # Elo strong signal
        row["elo_a"] = 1500 + elo_diff / 2
        row["elo_b"] = 1500 - elo_diff / 2
        row["elo_diff"] = elo_diff
        row["elo_surface_diff"] = elo_diff * 0.8
        row["rank_a"] = 100
        row["rank_b"] = 100
        rows.append(row)
        ys.append(y)
    return pd.DataFrame(rows), np.array(ys)


def test_ensemble_predictions_monotone_in_elo_diff():
    """Nach Training: höheres elo_diff → höhere P(A gewinnt)."""
    X, y = _synthetic_dataset(n=800)
    model = tlgbm.train_tennis_lgbm(X_train=X, y_train=y, X_cal=None, y_cal=None)

    # Probe-Grid: variiere nur elo_diff, alles andere neutral
    grid_rows = []
    for elo_diff in (-300, -150, 0, 150, 300):
        row = {c: 0.0 for c in FEATURE_COLUMNS}
        row["elo_a"] = 1500 + elo_diff / 2
        row["elo_b"] = 1500 - elo_diff / 2
        row["elo_diff"] = elo_diff
        row["elo_surface_diff"] = elo_diff * 0.8
        row["rank_a"] = 100
        row["rank_b"] = 100
        grid_rows.append(row)
    probes = pd.DataFrame(grid_rows)
    p_a = model.predict_p_a(probes)

    # Monotonie: p_a monoton steigend in elo_diff
    for i in range(len(p_a) - 1):
        assert p_a[i] < p_a[i + 1] + 0.01, \
            f"Monotonie verletzt: p_a[{i}]={p_a[i]:.3f} nicht ≤ p_a[{i+1}]={p_a[i+1]:.3f}"

    # Absolute Sanity: massiv positives Elo → p_a > 0.6
    assert p_a[-1] > 0.60, f"elo_diff=+300 lieferte p_a={p_a[-1]:.3f}, erwartet > 0.6"
    assert p_a[0] < 0.40, f"elo_diff=-300 lieferte p_a={p_a[0]:.3f}, erwartet < 0.4"


def test_symmetric_features_give_symmetric_p():
    """elo_diff=0 (Spieler gleichstark) → p_a nahe 0.5."""
    X, y = _synthetic_dataset(n=800)
    model = tlgbm.train_tennis_lgbm(X_train=X, y_train=y, X_cal=None, y_cal=None)
    row = {c: 0.0 for c in FEATURE_COLUMNS}
    row["elo_a"] = 1500; row["elo_b"] = 1500
    row["rank_a"] = 100; row["rank_b"] = 100
    probes = pd.DataFrame([row])
    p_a = float(model.predict_p_a(probes)[0])
    # Toleranter Range: neutrale Rauschen-Features liegen ausserhalb der
    # Trainingsverteilung; Modell kann ±0.2 vom Symmetrie-Prior abweichen.
    assert 0.25 < p_a < 0.75, f"p_a bei elo_diff=0 sollte grob um 0.5 liegen, war {p_a:.3f}"
