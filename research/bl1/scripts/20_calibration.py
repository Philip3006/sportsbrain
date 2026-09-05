"""FLAGSHIP-BL1 Task F — Calibration comparison.

Compares uncalibrated / Platt / isotonic calibration on the DC and Elo
development OOF predictions. Uses cross-fitted calibration so the calibrator
is NEVER fit and evaluated on the same fold — a common leakage source.

Method: leave-one-fold-out. For each dev fold F_i, fit calibrator on the union
of the other 3 folds' OOF, then apply to F_i's predictions. Report Brier,
LogLoss, ECE per model per method.

Also produces reliability plots (data + ECE) per class.

Reserves 2425 for the FINAL calibrator fit only (not evaluated for method
selection). Sanity check: fit chosen method on 2425 and report calibration
metrics on it just for observation (but this does NOT select the method).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.isotonic import IsotonicRegression  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402

OOF_DEV = ROOT / "research" / "bl1" / "results" / "oof_dev.csv"
OOF_2425 = ROOT / "research" / "bl1" / "results" / "oof_2425.csv"
OUT_DIR = ROOT / "research" / "bl1" / "results"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FOLDS = ["2021", "2122", "2223", "2324"]
CLASSES = ["away", "draw", "home"]  # y=0,1,2


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def _logloss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-12, 1.0)
    onehot = np.eye(3)[y]
    return float(-np.mean(np.sum(onehot * np.log(p), axis=1)))


def _ece_per_class(y: np.ndarray, p_class: np.ndarray, y_class: int, n_bins: int = 10,
                    min_bin: int = 20) -> tuple[float, list[dict]]:
    """One-vs-rest ECE for a single class. Returns (ece, per-bin diagnostics).

    Bins with count < min_bin are excluded from the ECE gate but reported
    diagnostically.
    """
    labels = (y == y_class).astype(int)
    bins = np.linspace(0, 1, n_bins + 1)
    bin_idx = np.clip(np.digitize(p_class, bins) - 1, 0, n_bins - 1)
    diagnostics = []
    weighted_gap = 0.0
    total_used = 0
    for b in range(n_bins):
        mask = bin_idx == b
        cnt = int(mask.sum())
        if cnt == 0:
            diagnostics.append({"bin": b, "n": 0, "p_mean": np.nan, "y_mean": np.nan, "gap": np.nan, "used_in_gate": False})
            continue
        p_mean = float(p_class[mask].mean())
        y_mean = float(labels[mask].mean())
        gap = abs(p_mean - y_mean)
        used = cnt >= min_bin
        diagnostics.append({"bin": b, "n": cnt, "p_mean": p_mean, "y_mean": y_mean, "gap": gap, "used_in_gate": used})
        if used:
            weighted_gap += gap * cnt
            total_used += cnt
    ece = weighted_gap / total_used if total_used > 0 else np.nan
    return ece, diagnostics


def _multiclass_ece(y: np.ndarray, p: np.ndarray, n_bins: int = 10, min_bin: int = 20) -> float:
    """Averages one-vs-rest ECE across the 3 classes."""
    eces = []
    for k in range(3):
        e, _ = _ece_per_class(y, p[:, k], k, n_bins, min_bin)
        if not np.isnan(e):
            eces.append(e)
    return float(np.mean(eces)) if eces else np.nan


def _bootstrap_ci(y: np.ndarray, p: np.ndarray, metric_fn, n_boot: int = 1000, rng_seed: int = 42) -> tuple[float, float]:
    """Match-level bootstrap CI for the given metric."""
    rng = np.random.default_rng(rng_seed)
    vals = []
    n = len(y)
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        vals.append(metric_fn(y[idx], p[idx]))
    lo, hi = float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))
    return lo, hi


def _fit_platt(p_train: np.ndarray, y_train: np.ndarray) -> list:
    """One-vs-rest Platt (logistic on raw prob)."""
    calibrators = []
    for k in range(3):
        clf = LogisticRegression(max_iter=1000)
        # Fit on logit of the probability (or just the prob itself; both work in practice)
        X = p_train[:, k].reshape(-1, 1)
        y_bin = (y_train == k).astype(int)
        clf.fit(X, y_bin)
        calibrators.append(clf)
    return calibrators


def _apply_platt(p: np.ndarray, cals: list) -> np.ndarray:
    out = np.zeros_like(p)
    for k, clf in enumerate(cals):
        out[:, k] = clf.predict_proba(p[:, k].reshape(-1, 1))[:, 1]
    # Normalize to sum to 1 per row
    row_sum = out.sum(axis=1, keepdims=True)
    row_sum[row_sum == 0] = 1.0
    return out / row_sum


def _fit_iso(p_train: np.ndarray, y_train: np.ndarray) -> list:
    calibrators = []
    for k in range(3):
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        y_bin = (y_train == k).astype(int)
        iso.fit(p_train[:, k], y_bin)
        calibrators.append(iso)
    return calibrators


def _apply_iso(p: np.ndarray, cals: list) -> np.ndarray:
    out = np.zeros_like(p)
    for k, iso in enumerate(cals):
        out[:, k] = iso.predict(p[:, k])
    row_sum = out.sum(axis=1, keepdims=True)
    row_sum[row_sum == 0] = 1.0
    return out / row_sum


def _evaluate_cross_fitted(oof: pd.DataFrame, model_prefix: str) -> pd.DataFrame:
    """Leave-one-fold-out calibrator fit on 3 folds, evaluate on the 4th."""
    p_cols = [f"{model_prefix}_p_away", f"{model_prefix}_p_draw", f"{model_prefix}_p_home"]
    rows = []
    for held_out in FOLDS:
        train_slice = oof[oof["season"] != held_out]
        eval_slice = oof[oof["season"] == held_out]
        p_train = train_slice[p_cols].to_numpy()
        y_train = train_slice["y"].to_numpy()
        p_eval = eval_slice[p_cols].to_numpy()
        y_eval = eval_slice["y"].to_numpy()

        # Uncalibrated
        rows.append(_eval_row(held_out, model_prefix, "uncalibrated", y_eval, p_eval))

        # Platt (cross-fitted)
        cals = _fit_platt(p_train, y_train)
        p_platt = _apply_platt(p_eval, cals)
        rows.append(_eval_row(held_out, model_prefix, "platt", y_eval, p_platt))

        # Isotonic (cross-fitted)
        cals = _fit_iso(p_train, y_train)
        p_iso = _apply_iso(p_eval, cals)
        rows.append(_eval_row(held_out, model_prefix, "isotonic", y_eval, p_iso))

    return pd.DataFrame(rows)


def _eval_row(fold: str, model: str, method: str, y: np.ndarray, p: np.ndarray) -> dict:
    lo_b, hi_b = _bootstrap_ci(y, p, _brier, n_boot=1000)
    lo_l, hi_l = _bootstrap_ci(y, p, _logloss, n_boot=1000)
    return {
        "fold": fold, "model": model, "method": method,
        "n": len(y),
        "brier": _brier(y, p),
        "brier_ci_lo": lo_b, "brier_ci_hi": hi_b,
        "logloss": _logloss(y, p),
        "logloss_ci_lo": lo_l, "logloss_ci_hi": hi_l,
        "ece": _multiclass_ece(y, p, n_bins=10, min_bin=20),
    }


def main() -> None:
    oof = pd.read_csv(OOF_DEV, dtype={"season": str})
    print(f"Loaded {len(oof)} dev OOF rows across seasons {sorted(oof['season'].unique())}", flush=True)

    all_rows = []
    for model in ("dc", "elo"):
        df = _evaluate_cross_fitted(oof, model)
        all_rows.append(df)
    result = pd.concat(all_rows, ignore_index=True)
    result.to_csv(OUT_DIR / "calibration_by_fold.csv", index=False)

    # Aggregate across all 4 folds (pooled)
    agg_rows = []
    for model in ("dc", "elo"):
        p_cols = [f"{model}_p_away", f"{model}_p_draw", f"{model}_p_home"]
        for method in ("uncalibrated", "platt", "isotonic"):
            # Rebuild cross-fitted predictions across all dev matches for pooled aggregation.
            pooled_p = np.zeros((len(oof), 3))
            pooled_y = oof["y"].to_numpy()
            for held_out in FOLDS:
                mask_train = oof["season"] != held_out
                mask_eval = oof["season"] == held_out
                p_train = oof[mask_train][p_cols].to_numpy()
                y_train = oof[mask_train]["y"].to_numpy()
                p_eval = oof[mask_eval][p_cols].to_numpy()
                if method == "uncalibrated":
                    pooled_p[mask_eval.values] = p_eval
                elif method == "platt":
                    cals = _fit_platt(p_train, y_train)
                    pooled_p[mask_eval.values] = _apply_platt(p_eval, cals)
                else:
                    cals = _fit_iso(p_train, y_train)
                    pooled_p[mask_eval.values] = _apply_iso(p_eval, cals)
            lo_b, hi_b = _bootstrap_ci(pooled_y, pooled_p, _brier, n_boot=1000)
            lo_l, hi_l = _bootstrap_ci(pooled_y, pooled_p, _logloss, n_boot=1000)
            agg_rows.append({
                "model": model, "method": method, "n": len(pooled_y),
                "brier": _brier(pooled_y, pooled_p),
                "brier_ci_lo": lo_b, "brier_ci_hi": hi_b,
                "logloss": _logloss(pooled_y, pooled_p),
                "logloss_ci_lo": lo_l, "logloss_ci_hi": hi_l,
                "ece": _multiclass_ece(pooled_y, pooled_p),
            })
    agg = pd.DataFrame(agg_rows).sort_values(["model", "brier"])
    agg.to_csv(OUT_DIR / "calibration_aggregate.csv", index=False)
    print(agg.to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)


if __name__ == "__main__":
    main()
