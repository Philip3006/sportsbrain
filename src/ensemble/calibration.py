import numpy as np
import pickle
from pathlib import Path
from sklearn.isotonic import IsotonicRegression


def fit_isotonic(
    probs: np.ndarray,
    outcomes: np.ndarray,
    outcome_idx: int,
) -> IsotonicRegression:
    """
    Fits one isotonic regressor for outcome_idx (one-vs-rest).
    probs: (N, 3) array. outcomes: (N,) int array.
    """
    y_binary = (outcomes == outcome_idx).astype(float)
    p_class = probs[:, outcome_idx]
    order = np.argsort(p_class)
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(p_class[order], y_binary[order])
    return iso


def calibrate(
    raw_probs: np.ndarray,
    calibrators: list[IsotonicRegression],
) -> np.ndarray:
    """
    Applies three isotonic calibrators (one per outcome class), renormalizes rows.
    raw_probs: (N, 3). Returns calibrated (N, 3).
    """
    calibrated = np.column_stack([
        calibrators[i].predict(raw_probs[:, i]) for i in range(3)
    ])
    calibrated = np.clip(calibrated, 0.0, None)
    row_sums = calibrated.sum(axis=1, keepdims=True)
    degenerate = (row_sums < 1e-6).flatten()
    if degenerate.any():
        calibrated[degenerate] = np.array([1/3, 1/3, 1/3])
        row_sums = calibrated.sum(axis=1, keepdims=True)
    return calibrated / row_sums


def brier_score_multiclass(
    probs: np.ndarray,
    outcomes: np.ndarray,
) -> float:
    """
    Multiclass Brier score: (1/N) * sum_i sum_j (p_ij - o_ij)^2
    Lower is better. Perfect = 0.
    """
    n, k = probs.shape
    one_hot = np.zeros((n, k))
    one_hot[np.arange(n), outcomes] = 1.0
    return float(np.mean(np.sum((probs - one_hot) ** 2, axis=1)))


def expected_calibration_error(
    probs: np.ndarray,
    outcomes: np.ndarray,
    n_bins: int = 10,
) -> float:
    """
    ECE averaged over all outcome classes.
    Groups predictions into n_bins by probability, measures |mean_prob - mean_outcome|.
    Target: ECE < 0.05 before live deployment.
    """
    k = probs.shape[1]
    ece_per_class = []
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)

    for cls in range(k):
        p_cls = probs[:, cls]
        y_cls = (outcomes == cls).astype(float)
        total_weight = 0.0
        weighted_error = 0.0

        for lo, hi in zip(bin_edges[:-1], bin_edges[1:]):
            mask = (p_cls >= lo) & (p_cls < hi)
            if cls == k - 1:
                mask |= (p_cls == 1.0)
            if mask.sum() == 0:
                continue
            avg_p = p_cls[mask].mean()
            avg_y = y_cls[mask].mean()
            weight = mask.sum() / len(p_cls)
            weighted_error += weight * abs(avg_p - avg_y)
            total_weight += weight

        ece_per_class.append(weighted_error)

    return float(np.mean(ece_per_class))


def save_calibrators(calibrators: list[IsotonicRegression], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(calibrators, f)


def load_calibrators(path: Path) -> list[IsotonicRegression]:
    with open(path, "rb") as f:
        return pickle.load(f)
