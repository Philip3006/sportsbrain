"""FLAGSHIP-BL1 CORRECTED — Chronological calibration.

CHANGES vs 21_calibration_all_models.py:
- For outer fold F, calibrator is fit ONLY on OOF predictions from folds
  strictly earlier than F. Never uses future folds.
- Outer folds: 2021, 2122, 2223, 2324 (each has 1..3 earlier folds available:
  {1819}, {1819, 1920}, {1819, 1920, 2021}, {1819, 1920, 2021, 2122}
  respectively).
- Fold 1819 has no earlier fold available → uncalibrated only reported;
  never used for aggregate calibration comparison (but its predictions ARE
  used as calibrator training data for later folds).

Compares uncalibrated / Platt / isotonic across M1 DC, M2 Elo, M3 LGBM+dmwd,
M4 LGBM. Match-level bootstrap CI 1,000 resamples.

Outputs:
  research/bl1/results/calibration_all_models_v2.csv
  research/bl1/results/calibrated_probs_all_models_v2.pkl
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.isotonic import IsotonicRegression  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402

RES = ROOT / "research" / "bl1" / "results"

CALIB_TRAIN_FOLDS = ["1819", "1920"]  # seeds only
OUTER_FOLDS = ["2021", "2122", "2223", "2324"]  # reported


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
        used, gap = 0, 0.0
        for b in range(n_bins):
            mask = idx == b
            cnt = int(mask.sum())
            if cnt < min_bin:
                continue
            gap += abs(float(p[mask, k].mean()) - float(labels[mask].mean())) * cnt
            used += cnt
        if used > 0:
            eces.append(gap / used)
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
    s = out.sum(axis=1, keepdims=True)
    s[s == 0] = 1.0
    return out / s


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
    s = out.sum(axis=1, keepdims=True)
    s[s == 0] = 1.0
    return out / s


def _load_oof(path: Path, prefix: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"season": str})
    df["date"] = pd.to_datetime(df["date"])
    cols = ["season", "date", "home_team", "away_team", "y",
             f"{prefix}_p_away", f"{prefix}_p_draw", f"{prefix}_p_home"]
    return df[cols].copy()


def _chronological_calibrated(df: pd.DataFrame, prefix: str) -> dict[str, np.ndarray]:
    """For each outer fold F ∈ OUTER_FOLDS, use OOF preds from all folds < F
    to fit the calibrator, then apply to fold F. Returns pooled (n_all,3) probs
    across all outer folds for {uncalibrated, platt, isotonic}.

    Where "n_all" = number of rows in OUTER_FOLDS (excludes calibrator-seed folds).
    """
    p_cols = [f"{prefix}_p_away", f"{prefix}_p_draw", f"{prefix}_p_home"]
    all_seasons = CALIB_TRAIN_FOLDS + OUTER_FOLDS
    pooled = {m: [] for m in ("uncalibrated", "platt", "isotonic")}
    y_pooled = []
    season_pooled = []

    for i, outer in enumerate(OUTER_FOLDS):
        # Chronological training set = seed folds + any outer folds before this one
        earlier = CALIB_TRAIN_FOLDS + OUTER_FOLDS[:i]
        train_slice = df[df["season"].isin(earlier)]
        eval_slice = df[df["season"] == outer]
        p_train = train_slice[p_cols].to_numpy()
        y_train = train_slice["y"].to_numpy()
        p_eval = eval_slice[p_cols].to_numpy()
        y_eval = eval_slice["y"].to_numpy()

        pooled["uncalibrated"].append(p_eval)
        pooled["platt"].append(_apply_platt(p_eval, _fit_platt(p_train, y_train)))
        pooled["isotonic"].append(_apply_iso(p_eval, _fit_iso(p_train, y_train)))
        y_pooled.append(y_eval)
        season_pooled.extend([outer] * len(eval_slice))

    return {m: np.concatenate(v, axis=0) for m, v in pooled.items()}, np.concatenate(y_pooled), np.array(season_pooled)


def main() -> None:
    # Load all four models' OOF (with dev-fold seasons 1819..2324)
    m1 = _load_oof(RES / "oof_dev_v2.csv", "dc")
    m2 = _load_oof(RES / "oof_dev_v2.csv", "elo")
    m3 = _load_oof(RES / "oof_m3_dev_v2.csv", "m3")
    m4 = _load_oof(RES / "oof_m4_dev_v2.csv", "m4")

    print(f"Row counts (dev v2 across all outer folds 1819..2324):")
    print(f"  M1 DC / M2 Elo: {len(m1)} rows (six-fold set)")
    print(f"  M3 / M4: {len(m3)} rows (four outer folds only 2021..2324)", flush=True)

    all_rows = []
    all_calibrated = {}

    # M1 and M2 need to be restricted to outer 2021-2324 for pooling comparability with M3/M4
    for name, df, prefix in [("M1_DC", m1, "dc"), ("M2_Elo", m2, "elo"),
                              ("M3_LGBM_dmwd", m3, "m3"), ("M4_LGBM", m4, "m4")]:
        pooled, y_pooled, season_pooled = _chronological_calibrated(df, prefix)
        all_calibrated[name] = {
            "probs": pooled,
            "y": y_pooled, "seasons": season_pooled,
        }
        for method in ("uncalibrated", "platt", "isotonic"):
            p = pooled[method]
            lo_b, hi_b = _boot_ci(y_pooled, p, _brier, 1000)
            lo_l, hi_l = _boot_ci(y_pooled, p, _logloss, 1000)
            all_rows.append({
                "model": name, "method": method, "n": len(y_pooled),
                "brier": _brier(y_pooled, p),
                "brier_ci_lo": lo_b, "brier_ci_hi": hi_b,
                "logloss": _logloss(y_pooled, p),
                "logloss_ci_lo": lo_l, "logloss_ci_hi": hi_l,
                "ece": _ece(y_pooled, p),
            })

    df_out = pd.DataFrame(all_rows).sort_values(["model", "brier"])
    df_out.to_csv(RES / "calibration_all_models_v2.csv", index=False)
    with open(RES / "calibrated_probs_all_models_v2.pkl", "wb") as f:
        pickle.dump(all_calibrated, f)

    print("\nChronological calibration comparison (pooled 2021-2324, n=1224):", flush=True)
    print(df_out[["model", "method", "n", "brier", "brier_ci_lo", "brier_ci_hi",
                    "logloss", "ece"]].to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)


if __name__ == "__main__":
    main()
