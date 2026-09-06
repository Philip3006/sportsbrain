"""FLAGSHIP-BL1 — Paired match-level bootstrap for model comparison.

CEO Correction Section 5.

For every pair of models predicting the SAME 1,224 dev-outer matches:

  ΔBrier = Brier(model A) - Brier(model B)

with 1,000 paired match-level bootstrap replications:
  - sample match indices with replacement
  - for each replicate compute Brier(A) and Brier(B) on the same indices
  - store the difference

Report:
  - point ΔBrier
  - 95% paired CI
  - fraction of bootstrap draws where model A wins (Brier_A < Brier_B)
  - fraction where the difference is not distinguishable from zero
    (paired CI covers 0)

Uses UNCALIBRATED probabilities for the primary comparison per model
(each model's best-Brier method — see calibration_all_models_v2.csv).

Pairs required:
  M2 vs M1
  M2 vs M3
  M2 vs M4
  M3 vs M4

Also compared: M2 vs M5, M2 vs M6, M2 vs M7 (once M5-M7 exist).
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

RES = ROOT / "research" / "bl1" / "results"
OUTER_FOLDS = ["2021", "2122", "2223", "2324"]
N_BOOT = 1000

# Per-model best-method probabilities (from calibration_all_models_v2.csv).
BEST_METHOD = {
    "M1_DC": "uncalibrated",
    "M2_Elo": "platt",
    "M3_LGBM_dmwd": "uncalibrated",
    "M4_LGBM": "uncalibrated",
    "M5_market_open": "uncalibrated",     # M5 is intrinsically market-derived
    "M6_market_elo_blend": "uncalibrated",
    "M7_market_residual": "uncalibrated",
}


def _brier(y, p):
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def _load_pooled(model: str) -> tuple[np.ndarray, np.ndarray, list]:
    """Return (y, p, match_ids) for the specified model, restricted to
    outer folds 2021-2324 (n=1,224)."""
    if model in ("M1_DC", "M2_Elo", "M3_LGBM_dmwd", "M4_LGBM"):
        with open(RES / "calibrated_probs_all_models_v2.pkl", "rb") as f:
            cal = pickle.load(f)
        best_method = BEST_METHOD[model]
        probs = cal[model]["probs"][best_method]
        y = cal[model]["y"]
        # The corresponding date/team labels come from oof_dev_v2 or oof_m*.
        # Rebuild match_ids by re-loading the source.
        if model == "M1_DC":
            df = pd.read_csv(RES / "oof_dev_v2.csv", dtype={"season": str})
            df = df[df["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        elif model == "M2_Elo":
            df = pd.read_csv(RES / "oof_dev_v2.csv", dtype={"season": str})
            df = df[df["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        elif model == "M3_LGBM_dmwd":
            df = pd.read_csv(RES / "oof_m3_dev_v2.csv", dtype={"season": str})
            df = df[df["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        else:
            df = pd.read_csv(RES / "oof_m4_dev_v2.csv", dtype={"season": str})
            df = df[df["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        assert len(df) == len(y), f"{model}: len mismatch {len(df)} vs {len(y)}"
        match_ids = [f"{d}|{h}|{a}" for d, h, a in zip(df["date"], df["home_team"], df["away_team"])]
        return y, probs, match_ids
    elif model == "M5_market_open":
        df = pd.read_csv(RES / "oof_m5_dev_v3.csv", dtype={"season": str})
        df = df[df["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        y = df["y"].to_numpy()
        p = df[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()
        match_ids = [f"{d}|{h}|{a}" for d, h, a in zip(df["date"], df["home_team"], df["away_team"])]
        return y, p, match_ids
    elif model == "M6_market_elo_blend":
        df = pd.read_csv(RES / "oof_m6_dev_v3.csv", dtype={"season": str})
        df = df[df["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        y = df["y"].to_numpy()
        p = df[["m6_p_away", "m6_p_draw", "m6_p_home"]].to_numpy()
        match_ids = [f"{d}|{h}|{a}" for d, h, a in zip(df["date"], df["home_team"], df["away_team"])]
        return y, p, match_ids
    elif model == "M7_market_residual":
        df = pd.read_csv(RES / "oof_m7_dev_v3.csv", dtype={"season": str})
        df = df[df["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        y = df["y"].to_numpy()
        p = df[["m7_p_away", "m7_p_draw", "m7_p_home"]].to_numpy()
        match_ids = [f"{d}|{h}|{a}" for d, h, a in zip(df["date"], df["home_team"], df["away_team"])]
        return y, p, match_ids
    else:
        raise ValueError(model)


def _paired(y_a, p_a, y_b, p_b, seed: int = 42):
    """Point ΔBrier, 95% paired CI, win fraction for A (Brier_A < Brier_B)."""
    assert np.array_equal(y_a, y_b), "y mismatch — models must predict same matches"
    n = len(y_a)
    rng = np.random.default_rng(seed)
    deltas = []
    a_wins = 0
    for _ in range(N_BOOT):
        idx = rng.integers(0, n, size=n)
        ba = _brier(y_a[idx], p_a[idx])
        bb = _brier(y_b[idx], p_b[idx])
        d = ba - bb
        deltas.append(d)
        if ba < bb:
            a_wins += 1
    point = _brier(y_a, p_a) - _brier(y_b, p_b)
    lo = float(np.percentile(deltas, 2.5))
    hi = float(np.percentile(deltas, 97.5))
    ci_covers_zero = lo <= 0.0 <= hi
    return point, lo, hi, a_wins / N_BOOT, ci_covers_zero


def main() -> None:
    available_models = []
    for m in ("M1_DC", "M2_Elo", "M3_LGBM_dmwd", "M4_LGBM",
              "M5_market_open", "M6_market_elo_blend", "M7_market_residual"):
        try:
            _load_pooled(m)
            available_models.append(m)
        except Exception as e:
            print(f"  {m}: not available ({e})", flush=True)

    print(f"Available models: {available_models}", flush=True)

    pairs = [
        ("M2_Elo", "M1_DC"),
        ("M2_Elo", "M3_LGBM_dmwd"),
        ("M2_Elo", "M4_LGBM"),
        ("M3_LGBM_dmwd", "M4_LGBM"),
    ]
    for m5 in ("M5_market_open", "M6_market_elo_blend", "M7_market_residual"):
        if m5 in available_models:
            for opponent in ("M1_DC", "M2_Elo", "M3_LGBM_dmwd", "M4_LGBM"):
                pairs.append((m5, opponent))

    rows = []
    for a, b in pairs:
        if a not in available_models or b not in available_models:
            continue
        y_a, p_a, _ = _load_pooled(a)
        y_b, p_b, _ = _load_pooled(b)
        assert np.array_equal(y_a, y_b), f"y mismatch {a} vs {b}"
        point, lo, hi, a_wins, covers_zero = _paired(y_a, p_a, y_b, p_b)
        rows.append({
            "model_a": a, "model_b": b, "n_matches": len(y_a),
            "delta_brier_point": point, "ci_lo_95": lo, "ci_hi_95": hi,
            "ci_covers_zero": covers_zero,
            "a_win_fraction": a_wins,
            "verdict": ("A wins" if not covers_zero and point < 0
                        else ("B wins" if not covers_zero and point > 0 else "indistinguishable")),
        })

    df = pd.DataFrame(rows)
    df.to_csv(RES / "paired_bootstrap_v3.csv", index=False)
    print("\nPaired match-level bootstrap results (ΔBrier = A - B; negative = A better):", flush=True)
    print(df.to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)


if __name__ == "__main__":
    main()
