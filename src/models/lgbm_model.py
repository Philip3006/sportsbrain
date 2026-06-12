"""
Gradient boosted trees via sklearn HistGradientBoostingClassifier.
Same algorithm as LightGBM (histogram-based GBT), no external lib dependencies.
API kept identical so scanner/ensemble code needs no changes.
"""
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance

GBT_PARAMS = {
    "max_iter": 500,
    "learning_rate": 0.05,
    "max_leaf_nodes": 31,
    "min_samples_leaf": 20,
    "l2_regularization": 1.0,
    "random_state": 42,
    "early_stopping": True,
    "validation_fraction": 0.1,
    "n_iter_no_change": 50,
    "verbose": 0,
}


def train(
    X: pd.DataFrame,
    y: pd.Series,
    params: dict | None = None,
    eval_set: tuple | None = None,
    early_stopping_rounds: int = 50,
    sample_weight: np.ndarray | pd.Series | None = None,
) -> HistGradientBoostingClassifier:
    """
    Trains HistGradientBoostingClassifier (multiclass).
    eval_set ignored — sklearn uses internal validation_fraction for early stopping.
    y: 0=away_win, 1=draw, 2=home_win.
    sample_weight: optional per-row weights (e.g. up-weight tournament finals).
    """
    merged = {**GBT_PARAMS, **(params or {})}
    model = HistGradientBoostingClassifier(**merged)
    if sample_weight is not None:
        model.fit(X, y, sample_weight=np.asarray(sample_weight))
    else:
        model.fit(X, y)
    return model


def predict_proba(model: HistGradientBoostingClassifier, X: pd.DataFrame) -> np.ndarray:
    """Returns (N, 3) array: [p_away, p_draw, p_home]."""
    return model.predict_proba(X)


def shap_explain(
    model: HistGradientBoostingClassifier,
    X: pd.DataFrame,
    max_display: int = 15,
) -> pd.DataFrame:
    """
    Returns feature importance via sklearn's built-in gain-based importance.
    (HistGBM doesn't support TreeExplainer; permutation importance is too slow here.)
    """
    # Use mean decrease in impurity (gain) across all trees
    importances = np.zeros(X.shape[1])
    for est in model._predictors:
        for tree in est:
            for node in tree.nodes:
                if node["is_leaf"]:
                    continue
                feat_idx = node["feature_idx"]
                if 0 <= feat_idx < len(importances):
                    importances[feat_idx] += node["gain"]

    if importances.sum() > 0:
        importances /= importances.sum()

    df = pd.DataFrame({"feature": X.columns, "mean_abs_shap": importances})
    return df.sort_values("mean_abs_shap", ascending=False).head(max_display).reset_index(drop=True)


def top_features(
    model: HistGradientBoostingClassifier,
    X: pd.DataFrame,
    n: int = 3,
) -> list[str]:
    """Returns top-n feature names by importance."""
    df = shap_explain(model, X, max_display=n)
    return df["feature"].tolist()


def save_model(model: HistGradientBoostingClassifier, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(model, f)


def load_model(path: Path) -> HistGradientBoostingClassifier:
    with open(path, "rb") as f:
        return pickle.load(f)
