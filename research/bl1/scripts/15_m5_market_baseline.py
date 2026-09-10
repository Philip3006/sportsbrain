"""FLAGSHIP-BL1 — M5 pre-closing market baseline (v6 STRUCTURAL CORRECTION).

CHANGES vs v5 (CEO BL1 V6 §1, §4, §7, §8):

  §1 No direct raw-pickle load. All dataset access routes through
     `09_partitions.py::load_development_with_market()`.
  §4 Unified missing-market policy via `canonical_market.apply_policy()`
     shared with M6 and M7 so the three models evaluate on the same rows.
  §7 Canonical source language framed as CANONICAL RESEARCH MARKET
     BASELINE, not a locked operational source. BL1 has no production
     signal-time contract.
  §8 Terminology: no "opening" / "M5_market_open" — canonical / pre-closing.

Outputs:
  research/bl1/results/m5_preclose_baseline_summary.csv
  research/bl1/results/oof_m5_preclose_dev.csv
  research/bl1/results/m5_source_selection_by_fold.csv
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
CALIB_SEED_FOLDS = ["1819", "1920"]
OUTER_FOLDS = ["2021", "2122", "2223", "2324"]


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


partitions = _load("bl1_partitions", ROOT / "research/bl1/scripts/09_partitions.py")
market = _load("bl1_canonical_market", ROOT / "research/bl1/scripts/canonical_market.py")


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


RESEARCH_SOURCES = {
    "Pinnacle_preclose": ("PSH", "PSD", "PSA"),
    "Bookmaker_avg_preclose": ("AvgH", "AvgD", "AvgA"),
    "Bookmaker_max_preclose": ("MaxH", "MaxD", "MaxA"),
    "Bet365_preclose": ("B365H", "B365D", "B365A"),
}


def _devig_basic(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1 / oh, 1 / od, 1 / oa])
    return inv / inv.sum()


def _source_probs(df: pd.DataFrame, cols: tuple[str, str, str]):
    h, d, a = cols
    if not all(c in df.columns for c in cols):
        return None
    p_arr, y_arr = [], []
    for _, r in df.reset_index(drop=True).iterrows():
        p = _devig_basic(r.get(h), r.get(d), r.get(a))
        if p is None:
            continue
        p_arr.append([p[2], p[1], p[0]])
        y_arr.append(int(r["y"]))
    if not y_arr:
        return None
    return np.array(y_arr), np.array(p_arr)


def main() -> None:
    # ---- Load DEV+market via canonical partition helper ----
    dev = partitions.load_development_with_market(RAW_PKL, FULL_PKL, include_closing=False)
    dev["y"] = dev.apply(_label, axis=1)
    print(f"[15_m5] DEV+market: n={len(dev)}", flush=True)

    # ---- RESEARCH BENCHMARK: per-fold chronological source selection -----
    all_folds = CALIB_SEED_FOLDS + OUTER_FOLDS
    per_source_by_fold: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = {}
    for src, cols in RESEARCH_SOURCES.items():
        per_source_by_fold[src] = {}
        for season in all_folds:
            fold_df = dev[dev["season"] == season].sort_values(["date", "home_team"], kind="stable")
            r = _source_probs(fold_df, cols)
            if r is None:
                continue
            per_source_by_fold[src][season] = r

    selection_rows = []
    for i, outer in enumerate(OUTER_FOLDS):
        earlier = CALIB_SEED_FOLDS + OUTER_FOLDS[:i]
        src_scores = {}
        for src in RESEARCH_SOURCES:
            ys, ps = [], []
            for s in earlier:
                if s in per_source_by_fold[src]:
                    ys.append(per_source_by_fold[src][s][0])
                    ps.append(per_source_by_fold[src][s][1])
            if not ys:
                continue
            y = np.concatenate(ys); p = np.concatenate(ps, axis=0)
            src_scores[src] = _brier(y, p)
        best_src = min(src_scores, key=src_scores.get)
        selection_rows.append({
            "outer_fold": outer, "earlier_seeds": ",".join(earlier),
            **{f"brier_{s}": v for s, v in src_scores.items()},
            "best_dev_brier_source": best_src,
            "best_dev_brier_value": src_scores[best_src],
            "canonical_research_baseline_source": market.CANONICAL_PRECLOSE_SOURCE,
            "baseline_matches_best_dev_brier": (best_src == market.CANONICAL_PRECLOSE_SOURCE),
        })
    sel_df = pd.DataFrame(selection_rows)
    sel_df.to_csv(RES / "m5_source_selection_by_fold.csv", index=False)
    print("\nRESEARCH BENCHMARK — per-fold source Brier:", flush=True)
    print(sel_df.to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)

    # ---- M5: canonical policy over outer folds via unified apply_policy() ----
    oof_rows = []
    for outer in OUTER_FOLDS:
        fold_df = dev[dev["season"] == outer].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        kept, probs = market.apply_policy(fold_df)
        for j, (_, row) in enumerate(kept.iterrows()):
            oof_rows.append({
                "season": outer, "date": row["date"],
                "home_team": row["home_team"], "away_team": row["away_team"],
                "y": int(row["y"]),
                "m5_p_away": probs[j, 0], "m5_p_draw": probs[j, 1], "m5_p_home": probs[j, 2],
                "m5_source": market.CANONICAL_PRECLOSE_SOURCE,
            })
    oof = pd.DataFrame(oof_rows)
    for col in ("m5_p_away", "m5_p_draw", "m5_p_home"):
        oof[col] = oof[col].round(12)
    oof.to_csv(RES / "oof_m5_preclose_dev.csv", index=False)

    y_all = oof["y"].to_numpy()
    p_all = oof[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()
    b_all = _brier(y_all, p_all); lo, hi = _boot_ci(y_all, p_all, _brier, 1000)
    l_all = _logloss(y_all, p_all); e_all = _ece(y_all, p_all)

    fold_summary_rows = []
    for outer in OUTER_FOLDS:
        sub = oof[oof["season"] == outer]
        y = sub["y"].to_numpy()
        p = sub[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()
        fold_summary_rows.append({
            "outer_fold": outer, "source": market.CANONICAL_PRECLOSE_SOURCE,
            "n": len(sub), "brier": round(_brier(y, p), 12),
            "logloss": round(_logloss(y, p), 12),
        })
    fold_summary_df = pd.DataFrame(fold_summary_rows)

    summary = pd.DataFrame([{
        "model": "M5_market_preclose",
        "canonical_source": market.CANONICAL_PRECLOSE_SOURCE,
        "n": len(y_all),
        "brier": round(b_all, 12), "brier_ci_lo": round(lo, 12), "brier_ci_hi": round(hi, 12),
        "logloss": round(l_all, 12), "ece": round(e_all, 12),
    }])
    summary.to_csv(RES / "m5_preclose_baseline_summary.csv", index=False)

    print("\nM5 (canonical research baseline):", flush=True)
    print(summary.to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)
    print("\nFold-by-fold:", flush=True)
    print(fold_summary_df.to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)


if __name__ == "__main__":
    main()
