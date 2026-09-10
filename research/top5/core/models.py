"""Top-5 — Generic model orchestrator for M5, M6, M7.

Consumes a LeagueConfig. Writes canonical outputs to the league's results/
directory. Behaviour and formulae match BL1 v7 (frozen reference
569741b4ad571a38492e4d7cfd014cf82daec396) — this module is the parameterized
port used for parity verification and Top-5 rollout.

M1–M4 are not orchestrated here — they require league-specific precomputed
DC snapshots + LGBM feature builders which are league-agnostic in principle
but require substantial per-league artefacts. This module focuses on the
market-anchored family (M5/M6/M7) plus matched preclose-vs-close, which
depend only on the raw dataset + canonical market policy + an in-line Elo
series. That is the CEO-critical path for the reuse-rate demonstration.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import canonical_market as market
from . import elo as elo_mod
from . import metrics
from . import partitions
from ..config import LeagueConfig


ALPHA_GRID = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]

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


def _apply_policy_and_elo(fold_df: pd.DataFrame, elo_series: pd.DataFrame):
    kept, p_mkt = market.apply_policy(fold_df.sort_values(["date", "home_team"], kind="stable"))
    elo_lookup = elo_series.set_index(["date", "home_team", "away_team"])
    ys, pelo = [], []
    for _, r in kept.iterrows():
        date = pd.Timestamp(r["date"]); home = r["home_team"]; away = r["away_team"]
        try:
            elo_row = elo_lookup.loc[(date, home, away)]
            eh = float(elo_row["elo_home_pre"]); ea = float(elo_row["elo_away_pre"])
        except Exception:  # noqa: BLE001
            eh = ea = elo_mod.ELO_DEFAULT
        ph, pd_, pa = elo_mod.elo_win_probability(eh, ea, neutral=False)
        pelo.append([pa, pd_, ph])
        ys.append(int(r["y"]))
    pelo_arr = np.array(pelo).reshape(-1, 3) if pelo else np.empty((0, 3))
    return kept, p_mkt, pelo_arr, np.array(ys, dtype=np.int64)


def run_m5(config: LeagueConfig, top5_root: Path) -> dict:
    """Run M5 canonical baseline for a league. Writes:
      - {results}/oof_m5_preclose_dev.csv
      - {results}/m5_preclose_baseline_summary.csv
      - {results}/m5_source_selection_by_fold.csv
    """
    res = config.results_dir(top5_root); res.mkdir(parents=True, exist_ok=True)
    dev = partitions.load_development_with_market(
        config.raw_pkl(top5_root), config.full_pkl(top5_root),
        config.dev_seasons, include_closing=False)
    dev["y"] = dev.apply(lambda r: metrics.label_from_scores(r["home_score"], r["away_score"]),
                          axis=1)
    print(f"[M5/{config.key}] DEV+market: n={len(dev)}", flush=True)

    # RESEARCH BENCHMARK per-fold source table
    all_folds = list(config.calib_seed_folds) + list(config.outer_folds)
    per_source: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = {}
    for src, cols in RESEARCH_SOURCES.items():
        per_source[src] = {}
        for season in all_folds:
            fold_df = dev[dev["season"] == season].sort_values(["date", "home_team"], kind="stable")
            r = _source_probs(fold_df, cols)
            if r is None:
                continue
            per_source[src][season] = r

    sel_rows = []
    for i, outer in enumerate(config.outer_folds):
        earlier = list(config.calib_seed_folds) + list(config.outer_folds[:i])
        scores = {}
        for src in RESEARCH_SOURCES:
            ys, ps = [], []
            for s in earlier:
                if s in per_source[src]:
                    ys.append(per_source[src][s][0])
                    ps.append(per_source[src][s][1])
            if not ys:
                continue
            y = np.concatenate(ys); p = np.concatenate(ps, axis=0)
            scores[src] = metrics.brier(y, p)
        if not scores:
            continue
        best_src = min(scores, key=scores.get)
        sel_rows.append({
            "outer_fold": outer, "earlier_seeds": ",".join(earlier),
            **{f"brier_{s}": v for s, v in scores.items()},
            "best_dev_brier_source": best_src,
            "best_dev_brier_value": scores[best_src],
            "canonical_research_baseline_source": market.CANONICAL_PRECLOSE_SOURCE,
        })
    pd.DataFrame(sel_rows).to_csv(res / "m5_source_selection_by_fold.csv", index=False)

    # OPERATIONAL M5 via apply_policy
    oof_rows = []
    for outer in config.outer_folds:
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
    oof.to_csv(res / "oof_m5_preclose_dev.csv", index=False)

    y_all = oof["y"].to_numpy()
    p_all = oof[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()
    b_all = metrics.brier(y_all, p_all)
    lo, hi = metrics.boot_ci(y_all, p_all, metrics.brier, 1000)
    l_all = metrics.logloss(y_all, p_all)
    e_all = metrics.ece(y_all, p_all)

    fold_rows = []
    for outer in config.outer_folds:
        sub = oof[oof["season"] == outer]
        y = sub["y"].to_numpy(); p = sub[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()
        fold_rows.append({"outer_fold": outer, "n": len(sub),
                          "brier": round(metrics.brier(y, p), 12),
                          "logloss": round(metrics.logloss(y, p), 12)})

    summary = pd.DataFrame([{
        "model": "M5_market_preclose",
        "canonical_source": market.CANONICAL_PRECLOSE_SOURCE,
        "n": len(y_all),
        "brier": round(b_all, 12),
        "brier_ci_lo": round(lo, 12), "brier_ci_hi": round(hi, 12),
        "logloss": round(l_all, 12), "ece": round(e_all, 12),
    }])
    summary.to_csv(res / "m5_preclose_baseline_summary.csv", index=False)
    return {
        "league": config.key,
        "n": int(len(y_all)),
        "brier": b_all, "brier_ci_lo": lo, "brier_ci_hi": hi,
        "logloss": l_all, "ece": e_all,
        "fold_briers": fold_rows,
    }


def run_m6(config: LeagueConfig, top5_root: Path) -> dict:
    """Run M6 market+Elo blend with chronological OOF alpha selection.
    Requires an Elo series precomputed on the league's DEV rows — we build
    it in-line here (cumulative walk-forward Elo across all DEV seasons)."""
    res = config.results_dir(top5_root); res.mkdir(parents=True, exist_ok=True)
    dev = partitions.load_development_with_market(
        config.raw_pkl(top5_root), config.full_pkl(top5_root),
        config.dev_seasons, include_closing=False)
    dev["y"] = dev.apply(lambda r: metrics.label_from_scores(r["home_score"], r["away_score"]),
                          axis=1)
    dev["date"] = pd.to_datetime(dev["date"])
    dev = dev.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)

    # Elo series over ALL dev matches (used as pre-match feature for M6).
    elo_series = elo_mod.compute_elo_series(dev[["date", "home_team", "away_team",
                                                   "home_score", "away_score"]])

    # Apply unified missing-market policy at DEV level.
    dev, _ = market.apply_policy(dev)

    # Per-fold market + Elo probability grids.
    fold_data = {}
    all_folds = list(config.outer_folds) + list(config.calib_seed_folds)
    for outer in all_folds:
        fold_df = dev[dev["season"] == outer].copy()
        kept, p_mkt, p_elo, ys = _apply_policy_and_elo(fold_df, elo_series)
        fold_data[outer] = {
            "y": ys, "p_mkt": p_mkt, "p_elo": p_elo,
            "date": kept["date"].tolist(),
            "home_team": kept["home_team"].tolist(),
            "away_team": kept["away_team"].tolist(),
            "season": [outer] * len(kept),
        }

    m6_outer_rows = []
    alpha_selection = []
    for i, outer in enumerate(config.outer_folds):
        earlier = list(config.calib_seed_folds) + list(config.outer_folds[:i])
        earlier_avail = [e for e in earlier if len(fold_data[e]["y"]) > 0]
        if not earlier_avail:
            best_alpha = 1.0
        else:
            y_train = np.concatenate([fold_data[e]["y"] for e in earlier_avail])
            pmkt_train = np.concatenate([fold_data[e]["p_mkt"] for e in earlier_avail], axis=0)
            pelo_train = np.concatenate([fold_data[e]["p_elo"] for e in earlier_avail], axis=0)
            best_alpha, best_brier = 0.0, np.inf
            for a in ALPHA_GRID:
                p_blend = a * pmkt_train + (1 - a) * pelo_train
                b = metrics.brier(y_train, p_blend)
                alpha_selection.append({"outer": outer, "alpha": a, "train_brier": b})
                if b < best_brier:
                    best_brier = b; best_alpha = a
        p_val = best_alpha * fold_data[outer]["p_mkt"] + (1 - best_alpha) * fold_data[outer]["p_elo"]
        y_val = fold_data[outer]["y"]
        val_brier = metrics.brier(y_val, p_val) if len(y_val) else float("nan")
        print(f"  M6 {config.key} outer={outer}: alpha={best_alpha:.1f} val_brier={val_brier:.4f}", flush=True)
        m6_outer_rows.append(pd.DataFrame({
            "season": fold_data[outer]["season"],
            "date": fold_data[outer]["date"],
            "home_team": fold_data[outer]["home_team"],
            "away_team": fold_data[outer]["away_team"],
            "y": y_val,
            "m6_p_away": p_val[:, 0] if len(p_val) else np.array([]),
            "m6_p_draw": p_val[:, 1] if len(p_val) else np.array([]),
            "m6_p_home": p_val[:, 2] if len(p_val) else np.array([]),
            "selected_alpha": best_alpha,
        }))

    m6_oof = pd.concat(m6_outer_rows, ignore_index=True)
    for col in ("m6_p_away", "m6_p_draw", "m6_p_home"):
        m6_oof[col] = m6_oof[col].round(12)
    m6_oof.to_csv(res / "oof_m6_dev_v3.csv", index=False)
    _alpha = pd.DataFrame(alpha_selection)
    if len(_alpha):
        _alpha["train_brier"] = _alpha["train_brier"].round(12)
    _alpha.to_csv(res / "m6_alpha_sweep.csv", index=False)

    y6 = m6_oof["y"].to_numpy()
    p6 = m6_oof[["m6_p_away", "m6_p_draw", "m6_p_home"]].to_numpy()
    b6 = metrics.brier(y6, p6)
    l6 = metrics.logloss(y6, p6)
    return {"league": config.key, "n": int(len(y6)), "brier": b6, "logloss": l6}


def run_matched_preclose_vs_close(config: LeagueConfig, top5_root: Path) -> dict:
    """Matched-sample paired M5 pre-close vs Bookmaker-avg closing on DEV."""
    res = config.results_dir(top5_root); res.mkdir(parents=True, exist_ok=True)
    m5 = pd.read_csv(res / "oof_m5_preclose_dev.csv", dtype={"season": str})
    m5["date"] = pd.to_datetime(m5["date"])
    m5 = m5.sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    dev_close = partitions.load_development_closing_prices(
        config.raw_pkl(top5_root), config.full_pkl(top5_root), config.dev_seasons)
    dev_close["date"] = pd.to_datetime(dev_close["date"])
    merged = m5.merge(
        dev_close[["date", "home_team", "away_team", "AvgCH", "AvgCD", "AvgCA"]],
        on=["date", "home_team", "away_team"], how="left")
    mask = merged[["AvgCH", "AvgCD", "AvgCA"]].notna().all(axis=1)
    matched = merged[mask].reset_index(drop=True)
    y = matched["y"].to_numpy()
    p_pre = matched[["m5_p_away", "m5_p_draw", "m5_p_home"]].to_numpy()
    p_close = []
    for _, r in matched.iterrows():
        p = _devig_basic(r["AvgCH"], r["AvgCD"], r["AvgCA"])
        p_close.append([p[2], p[1], p[0]])
    p_close = np.array(p_close)
    b_pre = metrics.brier(y, p_pre); b_cl = metrics.brier(y, p_close)
    delta, lo, hi, pre_win = metrics.paired_bootstrap(y, p_pre, p_close, n_boot=1000)
    covers_zero = lo <= 0.0 <= hi
    out = pd.DataFrame([{
        "n_matched": len(matched),
        "brier_M5_preclose": b_pre,
        "brier_bookmaker_avg_closing_basic": b_cl,
        "delta_brier_point_preclose_minus_close": delta,
        "delta_ci_lo_95": lo, "delta_ci_hi_95": hi,
        "ci_covers_zero": covers_zero,
        "preclose_win_fraction": pre_win,
        "verdict": ("pre-closing wins observed sample" if not covers_zero and delta < 0
                    else ("closing wins observed sample" if not covers_zero and delta > 0
                          else "indistinguishable")),
    }])
    out.to_csv(res / "matched_preclose_vs_close.csv", index=False)
    return {"league": config.key, **out.iloc[0].to_dict()}


def run_all(config: LeagueConfig, top5_root: Path) -> dict:
    m5r = run_m5(config, top5_root)
    m6r = run_m6(config, top5_root)
    mr = run_matched_preclose_vs_close(config, top5_root)
    return {"m5": m5r, "m6": m6r, "matched_preclose_close": mr}
