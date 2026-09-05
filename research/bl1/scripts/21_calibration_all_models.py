"""FLAGSHIP-BL1 continuation — Per-model calibration for M1/M2/M3/M4.

Cross-fitted uncalibrated / Platt / isotonic comparison on strict
DEVELOPMENT OOF for each of the four candidates:

  M1 = DC baseline (from oof_dev.csv)
  M2 = Elo baseline (from oof_dev.csv)
  M3 = DC+LGBM+midweek (from oof_m3_dev.csv)
  M4 = DC+LGBM (from oof_m4_dev.csv)

Then select ONE final calibration method to lock (per CEO Correction 1).

Output:
  research/bl1/results/calibration_all_models.csv
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

RES = ROOT / "research" / "bl1" / "results"
FOLDS = ["2021", "2122", "2223", "2324"]


def _brier(y, p):
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def _logloss(y, p):
    p = np.clip(p, 1e-12, 1.0)
    onehot = np.eye(3)[y]
    return float(-np.mean(np.sum(onehot * np.log(p), axis=1)))


def _ece(y, p, n_bins=10, min_bin=20):
    eces = []
    for k in range(3):
        labels = (y == k).astype(int)
        bins = np.linspace(0, 1, n_bins + 1)
        idx = np.clip(np.digitize(p[:, k], bins) - 1, 0, n_bins - 1)
        total_used = 0
        weighted_gap = 0.0
        for b in range(n_bins):
            mask = idx == b
            cnt = int(mask.sum())
            if cnt < min_bin:
                continue
            p_mean = float(p[mask, k].mean())
            y_mean = float(labels[mask].mean())
            weighted_gap += abs(p_mean - y_mean) * cnt
            total_used += cnt
        if total_used > 0:
            eces.append(weighted_gap / total_used)
    return float(np.mean(eces)) if eces else np.nan


def _boot_ci(y, p, fn, n_boot=1000, seed=42):
    rng = np.random.default_rng(seed)
    n = len(y)
    vals = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        vals.append(fn(y[idx], p[idx]))
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def _fit_platt(p_train, y_train):
    cals = []
    for k in range(3):
        clf = LogisticRegression(max_iter=1000)
        clf.fit(p_train[:, k].reshape(-1, 1), (y_train == k).astype(int))
        cals.append(clf)
    return cals


def _apply_platt(p, cals):
    out = np.zeros_like(p)
    for k, clf in enumerate(cals):
        out[:, k] = clf.predict_proba(p[:, k].reshape(-1, 1))[:, 1]
    row_sum = out.sum(axis=1, keepdims=True)
    row_sum[row_sum == 0] = 1.0
    return out / row_sum


def _fit_iso(p_train, y_train):
    cals = []
    for k in range(3):
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(p_train[:, k], (y_train == k).astype(int))
        cals.append(iso)
    return cals


def _apply_iso(p, cals):
    out = np.zeros_like(p)
    for k, iso in enumerate(cals):
        out[:, k] = iso.predict(p[:, k])
    row_sum = out.sum(axis=1, keepdims=True)
    row_sum[row_sum == 0] = 1.0
    return out / row_sum


def _load_oof(path: Path, prefix: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"season": str})
    cols = [f"{prefix}_p_away", f"{prefix}_p_draw", f"{prefix}_p_home"]
    # Sanity: rows have valid probs
    return df[["season", "date", "home_team", "away_team", "y"] + cols].copy()


def _cross_fitted(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """Returns rows with columns: model, method, brier, ece, logloss, cis."""
    p_cols = [f"{prefix}_p_away", f"{prefix}_p_draw", f"{prefix}_p_home"]
    pooled = {}
    for method in ("uncalibrated", "platt", "isotonic"):
        pooled_p = np.zeros((len(df), 3))
        pooled_y = df["y"].to_numpy()
        for held_out in FOLDS:
            train_mask = df["season"] != held_out
            eval_mask = df["season"] == held_out
            p_train = df[train_mask][p_cols].to_numpy()
            y_train = df[train_mask]["y"].to_numpy()
            p_eval = df[eval_mask][p_cols].to_numpy()
            if method == "uncalibrated":
                pooled_p[eval_mask.values] = p_eval
            elif method == "platt":
                cals = _fit_platt(p_train, y_train)
                pooled_p[eval_mask.values] = _apply_platt(p_eval, cals)
            else:
                cals = _fit_iso(p_train, y_train)
                pooled_p[eval_mask.values] = _apply_iso(p_eval, cals)
        pooled[method] = pooled_p
    rows = []
    for method in ("uncalibrated", "platt", "isotonic"):
        p = pooled[method]
        y = df["y"].to_numpy()
        lo_b, hi_b = _boot_ci(y, p, _brier, 1000)
        lo_l, hi_l = _boot_ci(y, p, _logloss, 1000)
        rows.append({
            "model": prefix, "method": method, "n": len(df),
            "brier": _brier(y, p), "brier_ci_lo": lo_b, "brier_ci_hi": hi_b,
            "logloss": _logloss(y, p), "logloss_ci_lo": lo_l, "logloss_ci_hi": hi_l,
            "ece": _ece(y, p),
        })
    return pd.DataFrame(rows), pooled


def main() -> None:
    # Load per-model OOF
    m1 = _load_oof(RES / "oof_dev.csv", "dc")     # M1 = DC
    m2 = _load_oof(RES / "oof_dev.csv", "elo")    # M2 = Elo
    m3 = _load_oof(RES / "oof_m3_dev.csv", "m3")
    m4 = _load_oof(RES / "oof_m4_dev.csv", "m4")

    print(f"Row counts: M1={len(m1)} M2={len(m2)} M3={len(m3)} M4={len(m4)}", flush=True)

    all_rows = []
    calibrated_probs = {}
    for name, df, prefix in [("M1_DC", m1, "dc"), ("M2_Elo", m2, "elo"),
                              ("M3_LGBM_midweek", m3, "m3"), ("M4_LGBM", m4, "m4")]:
        res, pooled = _cross_fitted(df, prefix)
        res["model_full"] = name
        all_rows.append(res)
        calibrated_probs[name] = pooled

    result = pd.concat(all_rows, ignore_index=True)
    result.to_csv(RES / "calibration_all_models.csv", index=False)
    # Save cross-fitted calibrated probabilities for downstream edge sweep
    import pickle
    with open(RES / "calibrated_probs_all_models.pkl", "wb") as f:
        pickle.dump(calibrated_probs, f)

    print("\nCalibration comparison across all 4 candidates:", flush=True)
    print(result[["model_full", "method", "n", "brier", "brier_ci_lo", "brier_ci_hi",
                  "logloss", "ece"]].to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)


if __name__ == "__main__":
    main()
