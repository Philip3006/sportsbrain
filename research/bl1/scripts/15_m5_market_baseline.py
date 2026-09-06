"""FLAGSHIP-BL1 — M5 opening market baseline (CEO Correction Section 6).

Establishes the pure signal-time market baseline before further feature
work. Uses ONLY pre-closing (opening) 1X2 odds; closing prices remain
benchmark-only.

Sources tested:
  - PSH / PSD / PSA  (Pinnacle pre-closing)
  - AvgH / AvgD / AvgA  (bookmaker-average pre-closing)
  - MaxH / MaxD / MaxA  (bookmaker-max pre-closing)
  - B365H / B365D / B365A  (Bet365 pre-closing)

De-vig: basic normalization (locked as primary by 61_market_hierarchy_dev.py
using dev-only data; Shin over-corrects on BL1).

Outputs:
  research/bl1/results/m5_market_baseline_summary.csv
  research/bl1/results/oof_m5_dev_v3.csv  (dev OOF 2021-2324)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pickle  # noqa: E402

RES = ROOT / "research" / "bl1" / "results"
FULL_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw_full.pkl"
DEV_SEASONS = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324"]
OUTER_FOLDS = ["2021", "2122", "2223", "2324"]
CLOSING_BENCH_BRIER_DEV = 0.5799  # from 61_market_hierarchy_dev.py (bookmaker-avg × basic)


def _label(row) -> int:
    return 2 if row["home_score"] > row["away_score"] else (1 if row["home_score"] == row["away_score"] else 0)


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


def _devig_basic(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1 / oh, 1 / od, 1 / oa])
    return inv / inv.sum()


SOURCES = {"Pinnacle_open": ("PSH", "PSD", "PSA"),
            "Bookmaker_avg_open": ("AvgH", "AvgD", "AvgA"),
            "Bookmaker_max_open": ("MaxH", "MaxD", "MaxA"),
            "Bet365_open": ("B365H", "B365D", "B365A")}


def main() -> None:
    with open(FULL_PKL, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    raw = raw.dropna(subset=["home_score", "away_score"]).copy()
    raw["y"] = raw.apply(_label, axis=1)

    dev = raw[raw["season"].isin(DEV_SEASONS)].copy()
    outer_slice = raw[raw["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)

    # Full development coverage table
    rows = []
    for name, (h, d, a) in SOURCES.items():
        if not all(c in dev.columns for c in (h, d, a)):
            print(f"  {name}: columns missing", flush=True)
            continue
        p_arr, y_arr = [], []
        for _, r in dev.iterrows():
            p = _devig_basic(r.get(h), r.get(d), r.get(a))
            if p is None:
                continue
            # p is [home, draw, away] — reorder to [away, draw, home]
            p_arr.append([p[2], p[1], p[0]])
            y_arr.append(int(r["y"]))
        y_arr = np.array(y_arr)
        p_arr = np.array(p_arr)
        lo_b, hi_b = _boot_ci(y_arr, p_arr, _brier, 1000)
        rows.append({
            "source": name, "slice": "dev_1617_2324",
            "n": len(y_arr), "coverage": len(y_arr) / len(dev),
            "brier": _brier(y_arr, p_arr), "brier_ci_lo": lo_b, "brier_ci_hi": hi_b,
            "logloss": _logloss(y_arr, p_arr),
            "ece": _ece(y_arr, p_arr),
        })
    summary_dev = pd.DataFrame(rows).sort_values("brier")

    # Also compute on outer folds only (for pooling comparability with M1-M4)
    rows_outer = []
    outer_probs = {}
    for name, (h, d, a) in SOURCES.items():
        if not all(c in outer_slice.columns for c in (h, d, a)):
            continue
        p_arr, y_arr, dates, homes, aways, seasons = [], [], [], [], [], []
        for _, r in outer_slice.iterrows():
            p = _devig_basic(r.get(h), r.get(d), r.get(a))
            if p is None:
                continue
            p_arr.append([p[2], p[1], p[0]])
            y_arr.append(int(r["y"]))
            dates.append(r["date"])
            homes.append(r["home_team"])
            aways.append(r["away_team"])
            seasons.append(r["season"])
        y_arr = np.array(y_arr)
        p_arr = np.array(p_arr)
        lo_b, hi_b = _boot_ci(y_arr, p_arr, _brier, 1000)
        rows_outer.append({
            "source": name, "slice": "outer_2021_2324",
            "n": len(y_arr), "coverage": len(y_arr) / len(outer_slice),
            "brier": _brier(y_arr, p_arr), "brier_ci_lo": lo_b, "brier_ci_hi": hi_b,
            "logloss": _logloss(y_arr, p_arr),
            "ece": _ece(y_arr, p_arr),
        })
        outer_probs[name] = pd.DataFrame({
            "season": seasons, "date": dates, "home_team": homes, "away_team": aways,
            "y": y_arr,
            "m5_p_away": p_arr[:, 0], "m5_p_draw": p_arr[:, 1], "m5_p_home": p_arr[:, 2],
        })

    summary = pd.concat([summary_dev, pd.DataFrame(rows_outer)], ignore_index=True).sort_values(["slice", "brier"])
    summary["gap_vs_closing_bench"] = summary["brier"] - CLOSING_BENCH_BRIER_DEV
    summary.to_csv(RES / "m5_market_baseline_summary.csv", index=False)

    # Persist best source's OOF on the outer folds as M5 OOF
    best_outer = pd.DataFrame(rows_outer).sort_values("brier").iloc[0]
    best_name = best_outer["source"]
    outer_probs[best_name].to_csv(RES / "oof_m5_dev_v3.csv", index=False)

    print("\nM5 opening-market baseline — DEV & OUTER slices, basic normalization:", flush=True)
    print(summary.to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)
    print(f"\nBest M5 source (outer 2021-2324): {best_name} — Brier {best_outer['brier']:.4f}", flush=True)
    print(f"Gap to closing benchmark (dev Bookmaker-avg × basic = 0.5799): "
          f"{best_outer['brier'] - CLOSING_BENCH_BRIER_DEV:+.4f}", flush=True)


if __name__ == "__main__":
    main()
