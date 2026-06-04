"""
Analyses walk-forward backtest predictions without needing market odds.
Computes Brier score, calibration, and accuracy per tournament.
Run: python scripts/analyse_backtest.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ensemble.calibration import brier_score_multiclass, expected_calibration_error
from src.config import RESULTS_DIR


def main():
    csv = RESULTS_DIR / "backtests" / "walkforward_results.csv"
    if not csv.exists():
        print("No backtest results found. Run: python scripts/run_backtest.py")
        return

    df = pd.read_csv(csv)
    print(f"Loaded {len(df)} predictions across {df['event'].nunique()} tournaments\n")

    # Build probability matrix [p_away, p_draw, p_home]
    probs = df[["p_away", "p_draw", "p_home"]].values
    outcomes = df["actual_outcome"].values  # 0=away, 1=draw, 2=home

    # Overall metrics
    brier = brier_score_multiclass(probs, outcomes)
    ece = expected_calibration_error(probs, outcomes)
    accuracy = (probs.argmax(axis=1) == outcomes).mean()

    # Baseline: uniform prediction (0.333, 0.333, 0.333)
    uniform = np.full_like(probs, 1/3)
    brier_baseline = brier_score_multiclass(uniform, outcomes)

    # Baseline: always predict most common outcome
    most_common = np.bincount(outcomes).argmax()
    naive = np.zeros_like(probs)
    naive[:, most_common] = 1.0
    brier_naive = brier_score_multiclass(naive, outcomes)

    print("=== Overall Model Quality ===")
    print(f"  Predictions:        {len(df)}")
    print(f"  Brier score:        {brier:.4f}  (lower = better)")
    print(f"  Brier baseline:     {brier_baseline:.4f}  (uniform 1/3)")
    print(f"  Brier naive:        {brier_naive:.4f}  (always predict home/draw/away)")
    print(f"  Brier skill score:  {1 - brier/brier_baseline:.4f}  (positive = beats baseline)")
    print(f"  ECE:                {ece:.4f}  (target < 0.05)")
    print(f"  Accuracy:           {accuracy:.3f}  (argmax prediction)")
    print()

    print("=== Per Tournament ===")
    for event in df["event"].unique():
        ev = df[df["event"] == event]
        p = ev[["p_away", "p_draw", "p_home"]].values
        o = ev["actual_outcome"].values
        b = brier_score_multiclass(p, o)
        acc = (p.argmax(axis=1) == o).mean()
        # Outcome distribution
        hw = (o == 2).sum()
        dr = (o == 1).sum()
        aw = (o == 0).sum()
        print(f"  {event:10s}  n={len(ev):3d}  Brier={b:.4f}  Acc={acc:.3f}  "
              f"(H:{hw} D:{dr} A:{aw})")

    print()
    print("=== Calibration Check (model prob vs actual frequency) ===")
    # Bin predictions by p_home into deciles and compare to actual home-win rate
    p_home = probs[:, 2]
    actual_home = (outcomes == 2).astype(float)
    bins = np.linspace(0, 1, 11)
    print(f"  {'Bin':12s}  {'Model avg%':>10s}  {'Actual%':>8s}  {'N':>5s}  {'Diff':>7s}")
    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (p_home >= lo) & (p_home < hi)
        if mask.sum() == 0:
            continue
        model_avg = p_home[mask].mean()
        actual_avg = actual_home[mask].mean()
        print(f"  [{lo:.1f}-{hi:.1f}]      "
              f"{model_avg*100:>8.1f}%   "
              f"{actual_avg*100:>6.1f}%   "
              f"{mask.sum():>5d}   "
              f"{(model_avg-actual_avg)*100:>+6.1f}%")

    print()
    note = "✅" if brier < brier_baseline else "⚠️"
    print(f"{note}  Model {'beats' if brier < brier_baseline else 'does NOT beat'} "
          f"uniform baseline (skill score: {1 - brier/brier_baseline:+.3f})")
    print("   CLV backtest requires historical market odds — "
          "add Pinnacle closing lines via football-data.co.uk or TheOddsAPI historical.")


if __name__ == "__main__":
    main()
