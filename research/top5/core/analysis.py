"""Top-5 — Cross-model analysis: edge sweep, paired bootstrap, CLV.

Matches BL1 v7 analysis scripts (34_edge_sweep_v3.py, 33_paired_bootstrap.py).
All analyses use DEVELOPMENT outer folds only. No calibration/holdout data.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import metrics
from . import partitions
from ..config import LeagueConfig

THRESHOLDS = [0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10]
CLASSES = ["away", "draw", "home"]
N_BOOT = 1000


def _devig_basic(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1 / oh, 1 / od, 1 / oa])
    return inv / inv.sum()


def _load_model_oof(res: Path, config: LeagueConfig) -> dict[str, tuple[pd.DataFrame, np.ndarray]]:
    """Load all available model OOF files. Returns {model_name: (df, probs)}."""
    out: dict = {}
    candidates = [
        ("M1_DC", res / "oof_m1_dev.csv",
         ["m1_p_away", "m1_p_draw", "m1_p_home"]),
        ("M2_Elo", res / "oof_m2_dev.csv",
         ["m2_p_away", "m2_p_draw", "m2_p_home"]),
        ("M3_LGBM_dmwd", res / "oof_m3_dev_v2.csv",
         ["m3_p_away", "m3_p_draw", "m3_p_home"]),
        ("M4_LGBM", res / "oof_m4_dev_v2.csv",
         ["m4_p_away", "m4_p_draw", "m4_p_home"]),
        ("M5_market_preclose", res / "oof_m5_preclose_dev.csv",
         ["m5_p_away", "m5_p_draw", "m5_p_home"]),
        ("M6_market_elo_blend", res / "oof_m6_dev_v3.csv",
         ["m6_p_away", "m6_p_draw", "m6_p_home"]),
        ("M7_market_residual", res / "oof_m7_dev_v3.csv",
         ["m7_p_away", "m7_p_draw", "m7_p_home"]),
    ]
    for name, path, cols in candidates:
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype={"season": str})
        df = df[df["season"].isin(config.outer_folds)].sort_values(
            ["date", "home_team"], kind="stable").reset_index(drop=True)
        avail_cols = [c for c in cols if c in df.columns]
        if not avail_cols or "y" not in df.columns:
            continue
        probs = df[avail_cols].to_numpy()
        out[name] = (df, probs)
    return out


def run_paired_bootstrap(config: LeagueConfig, top5_root: Path) -> dict:
    """Paired match-level bootstrap for all model pairs vs M5 baseline.

    Writes: results/paired_bootstrap.csv
    """
    res = config.results_dir(top5_root)
    models = _load_model_oof(res, config)
    if "M5_market_preclose" not in models:
        print(f"[bootstrap/{config.key}] M5 not available", flush=True)
        return {}

    df_m5, p_m5 = models["M5_market_preclose"]
    y_m5 = df_m5["y"].to_numpy()
    m5_prob_cols = ["m5_p_away", "m5_p_draw", "m5_p_home"]
    rows = []
    for name, (df, probs) in models.items():
        if name == "M5_market_preclose":
            continue
        # If lengths match on identical y ordering, use fast path
        if len(df) == len(df_m5):
            y = df["y"].to_numpy()
            if np.array_equal(y, y_m5):
                b_model = metrics.brier(y, probs)
                b_m5 = metrics.brier(y_m5, p_m5)
                delta, lo, hi, frac = metrics.paired_bootstrap(
                    y, probs, p_m5, N_BOOT)
                rows.append({
                    "model_a": name, "model_b": "M5_market_preclose",
                    "n": len(y), "n_matched": len(y),
                    "brier_a": b_model, "brier_b": b_m5,
                    "delta_brier_a_minus_b": delta,
                    "ci_lo_95": lo, "ci_hi_95": hi,
                    "frac_a_wins": frac,
                    "ci_covers_zero": lo <= 0.0 <= hi,
                    "match_mode": "identical_index",
                })
                print(f"[bootstrap/{config.key}] {name} vs M5: "
                      f"delta_brier={delta:.4f} CI=[{lo:.4f},{hi:.4f}]",
                      flush=True)
                continue

        # Mismatch: construct matched population on (date, home, away)
        df_a = df.copy()
        df_b = df_m5.copy()
        for _d in (df_a, df_b):
            _d["date"] = pd.to_datetime(_d["date"])
        prob_cols = [c for c in df_a.columns
                     if c.startswith(("m1_p_", "m2_p_", "m3_p_", "m4_p_",
                                       "m6_p_", "m7_p_"))]
        if not prob_cols:
            print(f"[bootstrap/{config.key}] {name}: no prob cols — skip",
                  flush=True)
            continue
        keys = ["date", "home_team", "away_team"]
        merged = df_a[keys + ["y"] + prob_cols].merge(
            df_b[keys + ["y"] + m5_prob_cols],
            on=keys, how="inner", suffixes=("", "_m5"))
        if len(merged) < 10:
            print(f"[bootstrap/{config.key}] {name}: matched pop too small "
                  f"(n={len(merged)}) — skip", flush=True)
            continue
        # y must match on both sides (both derived from same scoreboard)
        if not np.array_equal(merged["y"].to_numpy(),
                              merged["y_m5"].to_numpy()):
            print(f"[bootstrap/{config.key}] {name}: y mismatch on matched "
                  f"population — skip", flush=True)
            continue
        y = merged["y"].to_numpy()
        p_a = merged[prob_cols].to_numpy()
        p_b = merged[m5_prob_cols].to_numpy()
        b_a = metrics.brier(y, p_a)
        b_b = metrics.brier(y, p_b)
        delta, lo, hi, frac = metrics.paired_bootstrap(y, p_a, p_b, N_BOOT)
        rows.append({
            "model_a": name, "model_b": "M5_market_preclose",
            "n": len(df_a), "n_matched": len(merged),
            "brier_a": b_a, "brier_b": b_b,
            "delta_brier_a_minus_b": delta,
            "ci_lo_95": lo, "ci_hi_95": hi,
            "frac_a_wins": frac,
            "ci_covers_zero": lo <= 0.0 <= hi,
            "match_mode": "inner_join_dhaway",
        })
        print(f"[bootstrap/{config.key}] {name} vs M5 (matched n={len(merged)}): "
              f"delta_brier={delta:.4f} CI=[{lo:.4f},{hi:.4f}]", flush=True)

    # All non-M5 model pairs
    model_names = list(models.keys())
    for i, na in enumerate(model_names):
        for nb in model_names[i + 1:]:
            if na == "M5_market_preclose" or nb == "M5_market_preclose":
                continue
            dfa, pa = models[na]
            dfb, pb = models[nb]
            if len(dfa) != len(dfb):
                continue
            ya, yb = dfa["y"].to_numpy(), dfb["y"].to_numpy()
            if not np.array_equal(ya, yb):
                continue
            delta, lo, hi, frac = metrics.paired_bootstrap(ya, pa, pb, N_BOOT)
            rows.append({
                "model_a": na, "model_b": nb,
                "n": len(ya),
                "brier_a": metrics.brier(ya, pa), "brier_b": metrics.brier(yb, pb),
                "delta_brier_a_minus_b": delta,
                "ci_lo_95": lo, "ci_hi_95": hi,
                "frac_a_wins": frac,
                "ci_covers_zero": lo <= 0.0 <= hi,
            })

    out = pd.DataFrame(rows)
    out.to_csv(res / "paired_bootstrap.csv", index=False)
    return {"league": config.key, "n_pairs": len(rows)}


def run_edge_sweep(config: LeagueConfig, top5_root: Path) -> dict:
    """One-per-match edge analysis across all models × thresholds.

    Entry odds = Bookmaker-avg pre-close (AvgH/D/A).
    CLV = entry vs Bookmaker-avg closing (AvgCH/D/A).
    Writes: results/edge_sweep_v3_one_per_match.csv
    """
    res = config.results_dir(top5_root)
    models = _load_model_oof(res, config)
    if not models:
        print(f"[edge/{config.key}] no models available", flush=True)
        return {}

    dev_close = partitions.load_development_closing_prices(
        config.raw_pkl(top5_root), config.full_pkl(top5_root), config.dev_seasons)
    dev_close["date"] = pd.to_datetime(dev_close["date"])

    all_rows: list[dict] = []
    for model_name, (df, probs) in models.items():
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        merged = df.merge(
            dev_close[["date", "home_team", "away_team",
                        "AvgH", "AvgD", "AvgA",
                        "AvgCH", "AvgCD", "AvgCA"]],
            on=["date", "home_team", "away_team"], how="left")
        for thresh in THRESHOLDS:
            match_rows: list[dict] = []
            for i, r in merged.reset_index(drop=True).iterrows():
                p_model = probs[i]
                entry_odds = [r.get("AvgA"), r.get("AvgD"), r.get("AvgH")]
                close_odds = [r.get("AvgCA"), r.get("AvgCD"), r.get("AvgCH")]
                p_close_dv = _devig_basic(r.get("AvgCH"), r.get("AvgCD"), r.get("AvgCA"))

                best_k, best_edge = -1, -999.0
                for k in range(3):
                    o = entry_odds[k]
                    if pd.isna(o) or o <= 1.0:
                        continue
                    edge = p_model[k] * o - 1.0
                    if edge > best_edge:
                        best_edge = edge; best_k = k
                if best_k < 0 or best_edge < thresh:
                    continue

                o_entry = entry_odds[best_k]
                outcome = 1.0 if int(r["y"]) == best_k else 0.0
                pnl = (o_entry - 1) if outcome == 1.0 else -1.0
                o_close = close_odds[best_k]
                odds_clv = (o_entry / o_close - 1.0) if (pd.notna(o_close) and o_close > 1.0) else np.nan
                if p_close_dv is not None:
                    pc_k = [p_close_dv[2], p_close_dv[1], p_close_dv[0]][best_k]
                    price_clv = o_entry * pc_k - 1.0
                else:
                    pc_k = np.nan; price_clv = np.nan
                match_rows.append({
                    "model": model_name, "threshold": thresh,
                    "season": r.get("season"), "date": r.get("date"),
                    "class": CLASSES[best_k], "edge": best_edge,
                    "o_entry": o_entry, "p_model": p_model[best_k],
                    "outcome": outcome, "pnl": pnl,
                    "price_clv": price_clv, "odds_clv": odds_clv,
                })
            if not match_rows:
                continue
            mdf = pd.DataFrame(match_rows)
            pnl_arr = mdf["pnl"].to_numpy()
            roi = float(pnl_arr.mean())
            lo, hi = _boot_roi_ci(pnl_arr, N_BOOT)
            all_rows.append({
                "model": model_name, "threshold": thresh,
                "n_signals": len(mdf), "roi": roi,
                "roi_ci_lo_95": lo, "roi_ci_hi_95": hi,
                "ci_covers_zero": lo <= 0.0 <= hi,
                "mean_price_clv": float(mdf["price_clv"].mean()) if mdf["price_clv"].notna().any() else np.nan,
                "mean_odds_clv": float(mdf["odds_clv"].mean()) if mdf["odds_clv"].notna().any() else np.nan,
            })

    out = pd.DataFrame(all_rows)
    out.to_csv(res / "edge_sweep_v3_one_per_match.csv", index=False)
    return {"league": config.key, "n_rows": len(out)}


def _boot_roi_ci(pnl: np.ndarray, n_boot: int = 1000) -> tuple[float, float]:
    rng = np.random.RandomState(42)
    boot = [float(rng.choice(pnl, size=len(pnl), replace=True).mean())
            for _ in range(n_boot)]
    return float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))


def run_summary(config: LeagueConfig, top5_root: Path) -> dict:
    """Consolidated per-league model summary."""
    res = config.results_dir(top5_root)
    models = _load_model_oof(res, config)
    summary: dict[str, dict] = {}
    for name, (df, probs) in models.items():
        y = df["y"].to_numpy()
        b = metrics.brier(y, probs)
        l = metrics.logloss(y, probs)
        e = metrics.ece(y, probs)
        lo, hi = metrics.boot_ci(y, probs, metrics.brier, N_BOOT)
        summary[name] = {"n": len(y), "brier": b, "brier_ci_lo": lo,
                         "brier_ci_hi": hi, "logloss": l, "ece": e}
    pd.DataFrame(summary).T.to_csv(res / "model_summary.csv")
    return summary
