"""FLAGSHIP-BL1 — Matched-sample pre-closing vs closing + 2526 closing coverage.

CEO BL1 V6 §1, §2, §6:

  §1 No direct raw-pickle load. DEV closing prices come via
     `partitions.load_development_closing_prices()`. 2526 closing coverage
     comes via `partitions.holdout_closing_coverage_diagnostics()`.
  §2 The 2526 coverage helper returns diagnostics only (source, coverage,
     counts). No 2526 closing-price values are surfaced to this script.
  §6 Interpretation stays strictly within observed evidence — no monotonic
     Brier-trajectory claims across intermediate times, no "lower bound"
     or "upper limit" phrasing about hypothetical future signal-time
     snapshots.

Outputs:
  research/bl1/results/matched_preclose_vs_close.csv
  research/bl1/results/holdout_2526_market_coverage.csv
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

RES = ROOT / "research" / "bl1" / "results"
RAW_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw.pkl"
FULL_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw_full.pkl"
OUTER_FOLDS = ["2021", "2122", "2223", "2324"]


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


partitions = _load("bl1_partitions", ROOT / "research/bl1/scripts/09_partitions.py")


def _brier(y, p):
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def _devig_basic(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1 / oh, 1 / od, 1 / oa])
    return inv / inv.sum()


def _paired_bootstrap(y, p_a, p_b, n_boot=1000, seed=42):
    rng = np.random.default_rng(seed)
    n = len(y)
    deltas = []
    a_wins = 0
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        ba = _brier(y[idx], p_a[idx])
        bb = _brier(y[idx], p_b[idx])
        deltas.append(ba - bb)
        if ba < bb:
            a_wins += 1
    point = _brier(y, p_a) - _brier(y, p_b)
    lo = float(np.percentile(deltas, 2.5))
    hi = float(np.percentile(deltas, 97.5))
    return point, lo, hi, a_wins / n_boot


def main() -> None:
    # ---- 1. Matched-sample paired comparison (DEV closing is allowed) ----
    m5 = pd.read_csv(RES / "oof_m5_preclose_dev.csv", dtype={"season": str})
    m5["date"] = pd.to_datetime(m5["date"])
    m5 = m5.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)

    # DEV closing prices via partition helper (dev is unsealed).
    dev_close = partitions.load_development_closing_prices(RAW_PKL, FULL_PKL)
    dev_close["date"] = pd.to_datetime(dev_close["date"])
    merged = m5.merge(
        dev_close[["date", "home_team", "away_team", "AvgCH", "AvgCD", "AvgCA"]],
        on=["date", "home_team", "away_team"], how="left",
    )
    mask = merged[["AvgCH", "AvgCD", "AvgCA"]].notna().all(axis=1)
    matched = merged[mask].reset_index(drop=True)
    y = matched["y"].to_numpy()
    p_preclose = matched[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()

    p_close_rows = []
    for _, r in matched.iterrows():
        p = _devig_basic(r["AvgCH"], r["AvgCD"], r["AvgCA"])
        p_close_rows.append([p[2], p[1], p[0]])
    p_close = np.array(p_close_rows)

    b_preclose = _brier(y, p_preclose)
    b_close = _brier(y, p_close)
    delta, lo, hi, preclose_win_frac = _paired_bootstrap(y, p_preclose, p_close, n_boot=1000)
    ci_covers_zero = lo <= 0.0 <= hi

    result = pd.DataFrame([{
        "n_matched": len(matched),
        "brier_M5_preclose_per_fold": b_preclose,
        "brier_bookmaker_avg_closing_basic": b_close,
        "delta_brier_point_preclose_minus_close": delta,
        "delta_ci_lo_95": lo,
        "delta_ci_hi_95": hi,
        "ci_covers_zero": ci_covers_zero,
        "preclose_win_fraction": preclose_win_frac,
        "verdict": ("pre-closing wins observed sample"
                    if not ci_covers_zero and delta < 0
                    else ("closing wins observed sample"
                          if not ci_covers_zero and delta > 0
                          else "indistinguishable")),
    }])
    result.to_csv(RES / "matched_preclose_vs_close.csv", index=False)
    print("Matched paired M5 pre-closing vs Bookmaker-avg closing (basic):", flush=True)
    print(result.to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)
    print("\nInterpretation: on this DEV sample the closing snapshot is "
          "statistically better than the pre-closing snapshot. This does NOT "
          "prove a monotonic Brier trajectory for intermediate T-N prices, "
          "and does NOT bound the Brier of any hypothetical future "
          "SportsBrain BL1 signal-time snapshot.", flush=True)

    # ---- 2. 2526 closing coverage via dedicated diagnostics helper ----
    # The helper returns counts only — no price values are exposed to this
    # script. See 09_partitions.holdout_closing_coverage_diagnostics.
    cov = partitions.holdout_closing_coverage_diagnostics(FULL_PKL)
    cov.to_csv(RES / "holdout_2526_market_coverage.csv", index=False)
    print("\n2526 market-source availability (DIAGNOSTICS ONLY — no price values):", flush=True)
    print(cov.to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)


if __name__ == "__main__":
    main()
