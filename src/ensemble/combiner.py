import numpy as np
import pandas as pd


def blend(
    dc_probs: dict[str, float],
    lgbm_probs: np.ndarray,
    dc_weight: float = 0.5,
) -> np.ndarray:
    """
    Linear blend of Dixon-Coles and LightGBM probabilities.
    dc_probs: {p_home, p_draw, p_away}
    lgbm_probs: (3,) array [p_away, p_draw, p_home]
    Returns (3,) blended array [p_away, p_draw, p_home], sums to 1.
    """
    dc_vec = np.array([
        dc_probs["p_away"],
        dc_probs["p_draw"],
        dc_probs["p_home"],
    ])
    blended = dc_weight * dc_vec + (1.0 - dc_weight) * lgbm_probs
    total = blended.sum()
    if total > 0:
        blended /= total
    return blended


def find_optimal_weight(
    dc_probs_list: list[dict[str, float]],
    lgbm_probs_array: np.ndarray,
    true_outcomes: np.ndarray,
    weight_grid: np.ndarray | None = None,
) -> float:
    """
    Grid-searches dc_weight in [0.1, 0.9] minimizing multiclass Brier score.
    true_outcomes: (N,) int array, values 0=away, 1=draw, 2=home.
    Returns optimal dc_weight.
    """
    from src.ensemble.calibration import brier_score_multiclass

    if weight_grid is None:
        weight_grid = np.arange(0.1, 1.0, 0.1)

    best_w, best_score = 0.5, float("inf")
    for w in weight_grid:
        blended = np.array([
            blend(dc, lgbm, dc_weight=w)
            for dc, lgbm in zip(dc_probs_list, lgbm_probs_array)
        ])
        score = brier_score_multiclass(blended, true_outcomes)
        if score < best_score:
            best_score = score
            best_w = float(w)

    return best_w
