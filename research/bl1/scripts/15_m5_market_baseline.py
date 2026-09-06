"""FLAGSHIP-BL1 — M5 pre-closing market baseline (v5).

CHANGES vs v3 15_m5_market_baseline.py (CEO BL1 PARTITION CLOSURE):
- Terminology: "pre-closing", NOT "opening". Football-Data.co.uk's PSH/PSD/PSA
  are snapshots captured typically a few days before kickoff — not true
  opening lines. See research/bl1/results/signal_time_contract.md.
- Routes development access through canonical partition loader.
- Source-selection methodology corrected: source chosen per outer fold
  using strictly earlier chronological dev seasons only (no leakage from
  the fold being evaluated).
- Output file names updated to encode "preclose" semantics.

Sources tested:
  PSH/PSD/PSA (Pinnacle pre-closing), AvgH/AvgD/AvgA, MaxH/MaxD/MaxA,
  B365H/B365D/B365A. De-vig: basic normalization.

DOES NOT touch 2425 or 2526 outcomes.
DOES NOT emit outcome-labelled 2526 or 2425 predictions.

Outputs:
  research/bl1/results/m5_preclose_baseline_summary.csv
  research/bl1/results/oof_m5_preclose_dev.csv
  research/bl1/results/m5_source_selection_by_fold.csv
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
CALIB_SEED_FOLDS = ["1819", "1920"]
OUTER_FOLDS = ["2021", "2122", "2223", "2324"]

# Load partition loader dynamically (its module filename starts with a digit).
spec = importlib.util.spec_from_file_location("bl1_partitions", ROOT / "research/bl1/scripts/09_partitions.py")
partitions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(partitions)


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


PRE_CLOSE_SOURCES = {
    "Pinnacle_preclose": ("PSH", "PSD", "PSA"),
    "Bookmaker_avg_preclose": ("AvgH", "AvgD", "AvgA"),
    "Bookmaker_max_preclose": ("MaxH", "MaxD", "MaxA"),
    "Bet365_preclose": ("B365H", "B365D", "B365A"),
}


def _source_probs(df: pd.DataFrame, cols: tuple[str, str, str]):
    """Returns aligned (y, p, kept_index_mask) for a source over the input df."""
    h, d, a = cols
    if not all(c in df.columns for c in cols):
        return None
    p_arr, y_arr, kept = [], [], []
    for i, r in df.reset_index(drop=True).iterrows():
        p = _devig_basic(r.get(h), r.get(d), r.get(a))
        if p is None:
            continue
        # p is [home, draw, away] — reorder to [away, draw, home]
        p_arr.append([p[2], p[1], p[0]])
        y_arr.append(int(r["y"]))
        kept.append(i)
    if not y_arr:
        return None
    return np.array(y_arr), np.array(p_arr), np.array(kept)


def main() -> None:
    # ---- Load DEVELOPMENT via canonical partition loader --------------
    dev_labelled = partitions.load_development(RAW_PKL)
    dev_labelled["y"] = dev_labelled.apply(_label, axis=1)
    # Enrich with all bookmaker columns from bl1_raw_full for source coverage
    with open(FULL_PKL, "rb") as f:
        full = pickle.load(f)
    full["season"] = full["season"].astype(str)
    full["date"] = pd.to_datetime(full["date"])
    dev_labelled["date"] = pd.to_datetime(dev_labelled["date"])
    dev_market_cols = [c for cols in PRE_CLOSE_SOURCES.values() for c in cols]
    dev = dev_labelled.merge(
        full[["date", "home_team", "away_team"] + dev_market_cols],
        on=["date", "home_team", "away_team"], how="left", suffixes=("", "_full"),
    )
    print(f"[15_m5] DEV enriched: n={len(dev)}", flush=True)

    # ---- Per-fold chronological source selection ----------------------
    # For each outer fold F, select the pre-closing source with the best Brier
    # on OOF predictions from all folds strictly earlier than F.
    all_folds = CALIB_SEED_FOLDS + OUTER_FOLDS
    per_source_by_fold: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = {}
    for src, cols in PRE_CLOSE_SOURCES.items():
        per_source_by_fold[src] = {}
        for season in all_folds:
            fold_df = dev[dev["season"] == season].sort_values(["date", "home_team"], kind="stable")
            r = _source_probs(fold_df, cols)
            if r is None:
                continue
            y, p, _ = r
            per_source_by_fold[src][season] = (y, p)

    selection_rows = []
    for i, outer in enumerate(OUTER_FOLDS):
        earlier = CALIB_SEED_FOLDS + OUTER_FOLDS[:i]
        # For each source, pool earlier folds and compute Brier
        src_scores = {}
        for src in PRE_CLOSE_SOURCES:
            ys, ps = [], []
            for s in earlier:
                if s in per_source_by_fold[src]:
                    ys.append(per_source_by_fold[src][s][0])
                    ps.append(per_source_by_fold[src][s][1])
            if not ys:
                continue
            y = np.concatenate(ys); p = np.concatenate(ps, axis=0)
            src_scores[src] = _brier(y, p)
        # Pick best
        best_src = min(src_scores, key=src_scores.get)
        selection_rows.append({
            "outer_fold": outer, "earlier_seeds": ",".join(earlier),
            **{f"brier_{s}": v for s, v in src_scores.items()},
            "selected_source": best_src, "selected_train_brier": src_scores[best_src],
        })
    sel_df = pd.DataFrame(selection_rows)
    sel_df.to_csv(RES / "m5_source_selection_by_fold.csv", index=False)
    print("\nPer-fold M5 source selection (strictly-earlier chronological):", flush=True)
    print(sel_df.to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)

    # ---- Apply per-fold selected source to build final M5 dev OOF -----
    oof_rows = []
    per_source_brier_check = {}
    for i, outer in enumerate(OUTER_FOLDS):
        best_src = sel_df.iloc[i]["selected_source"]
        cols = PRE_CLOSE_SOURCES[best_src]
        fold_df = dev[dev["season"] == outer].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        r = _source_probs(fold_df, cols)
        if r is None:
            continue
        y, p, kept_idx = r
        kept_df = fold_df.iloc[kept_idx].reset_index(drop=True)
        for j, (_, row) in enumerate(kept_df.iterrows()):
            oof_rows.append({
                "season": outer, "date": row["date"],
                "home_team": row["home_team"], "away_team": row["away_team"],
                "y": int(y[j]),
                "m5_p_away": p[j, 0], "m5_p_draw": p[j, 1], "m5_p_home": p[j, 2],
                "m5_source": best_src,
            })

    oof = pd.DataFrame(oof_rows)
    oof.to_csv(RES / "oof_m5_preclose_dev.csv", index=False)

    y_all = oof["y"].to_numpy()
    p_all = oof[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()
    b_all = _brier(y_all, p_all); lo, hi = _boot_ci(y_all, p_all, _brier, 1000)
    l_all = _logloss(y_all, p_all); e_all = _ece(y_all, p_all)
    # Per-fold summary
    fold_summary_rows = []
    for outer in OUTER_FOLDS:
        sub = oof[oof["season"] == outer]
        y = sub["y"].to_numpy()
        p = sub[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()
        fold_summary_rows.append({
            "outer_fold": outer, "selected_source": sub["m5_source"].iloc[0],
            "n": len(sub), "brier": _brier(y, p), "logloss": _logloss(y, p),
        })
    fold_summary_df = pd.DataFrame(fold_summary_rows)

    summary = pd.DataFrame([{
        "model": "M5_preclose_per_fold_selected",
        "n": len(y_all), "brier": b_all, "brier_ci_lo": lo, "brier_ci_hi": hi,
        "logloss": l_all, "ece": e_all,
    }])
    summary.to_csv(RES / "m5_preclose_baseline_summary.csv", index=False)

    print("\nM5 pooled outer-fold summary (per-fold source, chronological selection):", flush=True)
    print(summary.to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)
    print("\nFold-by-fold:", flush=True)
    print(fold_summary_df.to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)


if __name__ == "__main__":
    main()
