"""FLAGSHIP-BL1 — Matched-sample pre-closing vs closing market comparison + 2526 coverage audit.

CEO BL1 PARTITION CLOSURE:

1. Matched-sample paired bootstrap:
   Recompute pre-closing market (per-fold selected source, from
   oof_m5_preclose_dev.csv) vs closing benchmark (Bookmaker-avg × basic)
   on EXACTLY the same 1,224 outer-fold matches. Both series predict
   the same match; ΔBrier is paired match-level bootstrapped with
   1,000 replicates.

2. 2526 market-source availability audit (schema only, NO outcome eval).
   For each closing and pre-closing source, report coverage on the
   sealed 2526 partition. Uses `load_holdout_schema_only()` so no y
   or score column is available to this script by construction.

Outputs:
  research/bl1/results/matched_preclose_vs_close.csv
  research/bl1/results/holdout_2526_market_coverage.csv
"""
from __future__ import annotations

import importlib.util
import pickle
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

spec = importlib.util.spec_from_file_location("bl1_partitions", ROOT / "research/bl1/scripts/09_partitions.py")
partitions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(partitions)


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
    # ---- 1. Matched-sample paired comparison ----
    # M5 pre-closing OOF (already per-fold-selected, dev outer 2021-2324)
    m5 = pd.read_csv(RES / "oof_m5_preclose_dev.csv", dtype={"season": str})
    m5["date"] = pd.to_datetime(m5["date"])
    m5 = m5.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)

    # Enrich with closing bookmaker-avg columns from full dataset (dev-legal,
    # since these are dev outer folds — outcomes on 2021-2324 are allowed).
    with open(FULL_PKL, "rb") as f:
        full = pickle.load(f)
    full["season"] = full["season"].astype(str)
    full["date"] = pd.to_datetime(full["date"])
    merged = m5.merge(
        full[["date", "home_team", "away_team", "AvgCH", "AvgCD", "AvgCA"]],
        on=["date", "home_team", "away_team"], how="left",
    )
    # Keep only rows with both signals available
    mask = merged[["AvgCH", "AvgCD", "AvgCA"]].notna().all(axis=1)
    matched = merged[mask].reset_index(drop=True)
    y = matched["y"].to_numpy()
    p_preclose = matched[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()

    # Bookmaker-avg closing basic-normalized
    p_close_rows = []
    for _, r in matched.iterrows():
        p = _devig_basic(r["AvgCH"], r["AvgCD"], r["AvgCA"])
        # p is [home, draw, away] → reorder [away, draw, home]
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
        "verdict": ("pre-closing wins" if not ci_covers_zero and delta < 0
                    else ("closing wins" if not ci_covers_zero and delta > 0
                          else "indistinguishable")),
    }])
    result.to_csv(RES / "matched_preclose_vs_close.csv", index=False)
    print("Matched paired M5 pre-closing vs Bookmaker-avg closing (basic):", flush=True)
    print(result.to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)

    # ---- 2. 2526 market-source availability audit ----
    # Load via schema-only partition loader: outcomes are structurally absent.
    holdout = partitions.load_holdout_schema_only(FULL_PKL)
    # Confirm no outcome columns
    outcome_leaks = [c for c in holdout.columns
                     if any(tok in c.lower() for tok in ("score", "goal", "result", "outcome", "pnl"))]
    assert not outcome_leaks, f"HOLDOUT_2526 leaked outcome-named columns: {outcome_leaks}"

    coverage_rows = []
    source_pairs = [
        ("Pinnacle_preclose", ("PSH", "PSD", "PSA")),
        ("Pinnacle_closing", ("PSCH", "PSCD", "PSCA")),
        ("Bookmaker_avg_preclose", ("AvgH", "AvgD", "AvgA")),
        ("Bookmaker_avg_closing", ("AvgCH", "AvgCD", "AvgCA")),
        ("Bookmaker_max_preclose", ("MaxH", "MaxD", "MaxA")),
        ("Bookmaker_max_closing", ("MaxCH", "MaxCD", "MaxCA")),
        ("Bet365_preclose", ("B365H", "B365D", "B365A")),
        ("Bet365_closing", ("B365CH", "B365CD", "B365CA")),
    ]
    n_2526 = len(holdout)
    for name, cols in source_pairs:
        present = all(c in holdout.columns for c in cols)
        if not present:
            coverage_rows.append({"source": name, "columns_present": False, "coverage": 0.0, "n_covered": 0, "n_total": n_2526})
            continue
        n_covered = int(holdout[list(cols)].dropna().shape[0])
        coverage_rows.append({
            "source": name, "columns_present": True,
            "coverage": n_covered / max(n_2526, 1),
            "n_covered": n_covered, "n_total": n_2526,
        })
    cov = pd.DataFrame(coverage_rows).sort_values("coverage", ascending=False)
    cov.to_csv(RES / "holdout_2526_market_coverage.csv", index=False)
    print("\n2526 market-source availability (SCHEMA ONLY — no outcomes accessed):", flush=True)
    print(cov.to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)


if __name__ == "__main__":
    main()
