"""FLAGSHIP-BL1 continuation — Full edge-threshold sweep.

Per CEO expansion:
- Every serious calibrated candidate (M1, M2, M3, M4, plus P4 promoted policy overlay)
- For each: unrestricted vs one-selection-per-match
- Report class distribution (home/draw/away) but do NOT tune per-class thresholds
- Use CROSS-FITTED PLATT calibration (locked global method)
- Entry price = Pinnacle pre-closing (PSH/PSD/PSA)  [audit doc: not closing]
- Primary CLV benchmark = Bookmaker-avg closing × basic-normalization
    (locked market hierarchy in 60_market_hierarchy.py; falls back to Pinnacle)
- Secondary Odds CLV = entry/closing
- Diagnostic model-vs-close = p_model / p_close_novig − 1

Root-cause decomposition (Task 17) also produced here:
- ROI/CLV split by class (home/draw/away)
- ROI/CLV split by odds bucket (short/med/long)
- ROI/CLV split by promoted-team subset
- ROI/CLV split by edge magnitude bucket
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402

RES = ROOT / "research" / "bl1" / "results"
FULL_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw_full.pkl"
FOLDS = ["2021", "2122", "2223", "2324"]
CLASSES = ["away", "draw", "home"]
THRESHOLDS = [0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10]


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
    row_sum = out.sum(axis=1, keepdims=True)
    row_sum[row_sum == 0] = 1.0
    return out / row_sum


def _cross_fit_platt(df: pd.DataFrame, prefix: str) -> np.ndarray:
    p_cols = [f"{prefix}_p_away", f"{prefix}_p_draw", f"{prefix}_p_home"]
    out = np.zeros((len(df), 3))
    for held_out in FOLDS:
        train_mask = df["season"] != held_out
        eval_mask = df["season"] == held_out
        cals = _fit_platt(df[train_mask][p_cols].to_numpy(), df[train_mask]["y"].to_numpy())
        out[eval_mask.values] = _apply_platt(df[eval_mask][p_cols].to_numpy(), cals)
    return out


def _devig_basic(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1 / oh, 1 / od, 1 / oa])
    return inv / inv.sum()


def _no_vig_avg_or_pin(row):
    """Use bookmaker-avg closing first; fall back to Pinnacle."""
    for prefix in ("Avg", "Max", "PS"):
        oh = row.get(f"{prefix}CH")
        od = row.get(f"{prefix}CD")
        oa = row.get(f"{prefix}CA")
        p = _devig_basic(oh, od, oa)
        if p is not None:
            return p, prefix  # returns [home,draw,away] order per _devig_basic convention
    return None, None


def _bootstrap_ci(vals, fn, n_boot=1000, seed=42):
    rng = np.random.default_rng(seed)
    n = len(vals)
    if n == 0:
        return np.nan, np.nan
    boot = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        boot.append(fn(vals[idx]))
    return float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))


def _max_drawdown(pnls):
    if len(pnls) == 0:
        return 0.0
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    return float((peak - cum).max())


def _load_oof_with_market(prefix: str, filename: str) -> pd.DataFrame:
    """Loads a per-model OOF file and enriches with full-market closing odds."""
    df = pd.read_csv(RES / filename, dtype={"season": str})
    # Enrich with full-market columns from bl1_raw_full.pkl
    with open(FULL_PKL, "rb") as f:
        full = pickle.load(f)
    full["season"] = full["season"].astype(str)
    full["date"] = pd.to_datetime(full["date"])
    df["date"] = pd.to_datetime(df["date"])
    merged = df.merge(
        full[["date", "home_team", "away_team", "AvgCH", "AvgCD", "AvgCA",
              "MaxCH", "MaxCD", "MaxCA", "PSH", "PSD", "PSA", "PSCH", "PSCD", "PSCA"]],
        on=["date", "home_team", "away_team"], how="left", suffixes=("", "_full"),
    )
    # Prefer ps_* columns already in the OOF file where present
    for c in ("PSH", "PSD", "PSA", "PSCH", "PSCD", "PSCA"):
        if c not in merged.columns:
            continue
    return merged


def _build_signals(oof: pd.DataFrame, calibrated: np.ndarray, prefix: str) -> pd.DataFrame:
    rows = []
    for i, r in oof.reset_index(drop=True).iterrows():
        p_cal = calibrated[i]
        # entry = Pinnacle pre-closing (PSH/PSD/PSA) — from full market join
        oh_e, od_e, oa_e = r.get("PSH"), r.get("PSD"), r.get("PSA")
        # closing = Pinnacle closing for secondary CLV
        oh_c, od_c, oa_c = r.get("PSCH"), r.get("PSCD"), r.get("PSCA")
        # PRIMARY market close for CLV = bookmaker-avg first, Pinnacle fallback
        p_close, src = _no_vig_avg_or_pin(r)

        open_odds = [oa_e, od_e, oh_e]
        close_odds = [oa_c, od_c, oh_c]
        for k, cls in enumerate(CLASSES):
            o_entry = open_odds[k]
            o_close = close_odds[k]
            if pd.isna(o_entry) or o_entry <= 1.0:
                continue
            p = p_cal[k]
            edge = p * o_entry - 1.0
            outcome = 1.0 if r["y"] == k else 0.0
            pnl = (o_entry - 1) if outcome == 1.0 else -1.0
            # p_close in order (home, draw, away)
            if p_close is not None:
                p_close_ordered = [p_close[2], p_close[1], p_close[0]]
                pc = p_close_ordered[k]
                closing_price_edge = o_entry * pc - 1.0
                model_vs_close = (p / pc - 1.0) if pc > 0 else np.nan
            else:
                pc = np.nan
                closing_price_edge = np.nan
                model_vs_close = np.nan
            odds_clv = (o_entry / o_close - 1.0) if (pd.notna(o_close) and o_close > 1.0) else np.nan

            rows.append({
                "match_id": f"{r['date']}|{r['home_team']}|{r['away_team']}",
                "season": r["season"], "date": r["date"],
                "home_team": r["home_team"], "away_team": r["away_team"],
                "class": cls, "p_model": p, "edge": edge, "o_entry": o_entry, "o_close": o_close,
                "p_close_novig": pc, "close_source": src,
                "outcome": outcome, "pnl": pnl,
                "closing_price_edge": closing_price_edge,
                "odds_clv": odds_clv, "model_vs_close": model_vs_close,
            })
    return pd.DataFrame(rows)


def _sweep(signals: pd.DataFrame, label: str, one_per_match: bool) -> pd.DataFrame:
    rows = []
    for thr in THRESHOLDS:
        fire = signals[signals["edge"] >= thr].copy()
        if one_per_match and not fire.empty:
            fire = fire.sort_values(["match_id", "edge"], ascending=[True, False]).drop_duplicates("match_id", keep="first")
        n = len(fire)
        if n == 0:
            rows.append({"model": label, "policy": "one_per" if one_per_match else "all",
                         "threshold": thr, "signal_count": 0})
            continue
        pnls = fire["pnl"].to_numpy()
        roi = pnls.mean()
        ci_lo, ci_hi = _bootstrap_ci(pnls, lambda a: a.mean(), n_boot=1000)
        rows.append({
            "model": label, "policy": "one_per" if one_per_match else "all",
            "threshold": thr, "signal_count": n,
            "roi": roi, "roi_ci_lo": ci_lo, "roi_ci_hi": ci_hi,
            "avg_odds": fire["o_entry"].mean(),
            "avg_edge": fire["edge"].mean(),
            "closing_price_edge": fire["closing_price_edge"].mean(skipna=True),
            "odds_clv": fire["odds_clv"].mean(skipna=True),
            "model_vs_close": fire["model_vs_close"].mean(skipna=True),
            "max_drawdown": _max_drawdown(pnls),
            "p_mean_signal": fire["p_model"].mean(),
            "y_mean_realized": fire["outcome"].mean(),
            "calibration_gap": abs(fire["p_model"].mean() - fire["outcome"].mean()),
            "share_home": float(fire["class"].eq("home").mean()),
            "share_draw": float(fire["class"].eq("draw").mean()),
            "share_away": float(fire["class"].eq("away").mean()),
        })
    return pd.DataFrame(rows)


def _decompose(signals: pd.DataFrame, thr: float, one_per_match: bool = False) -> pd.DataFrame:
    fire = signals[signals["edge"] >= thr].copy()
    if one_per_match and not fire.empty:
        fire = fire.sort_values(["match_id", "edge"], ascending=[True, False]).drop_duplicates("match_id", keep="first")
    if fire.empty:
        return pd.DataFrame()
    fire["odds_bucket"] = pd.cut(fire["o_entry"], bins=[1.0, 2.0, 3.5, 6.0, 100.0],
                                   labels=["1.0-2.0", "2.0-3.5", "3.5-6.0", "6.0+"])
    fire["edge_bucket"] = pd.cut(fire["edge"], bins=[-1, 0.05, 0.10, 0.20, 999],
                                   labels=["<=0.05", "0.05-0.10", "0.10-0.20", ">0.20"])
    rows = []
    for subset_label, sub in [("all", fire),
                              ("home_only", fire[fire["class"] == "home"]),
                              ("draw_only", fire[fire["class"] == "draw"]),
                              ("away_only", fire[fire["class"] == "away"])]:
        if sub.empty:
            continue
        rows.append({
            "cut": "class", "value": subset_label, "n": len(sub),
            "roi": sub["pnl"].mean(), "closing_price_edge": sub["closing_price_edge"].mean(),
            "avg_odds": sub["o_entry"].mean(),
            "avg_edge": sub["edge"].mean(),
            "calib_gap": abs(sub["p_model"].mean() - sub["outcome"].mean()),
        })
    for b in fire["odds_bucket"].dropna().unique():
        sub = fire[fire["odds_bucket"] == b]
        rows.append({
            "cut": "odds_bucket", "value": str(b), "n": len(sub),
            "roi": sub["pnl"].mean(), "closing_price_edge": sub["closing_price_edge"].mean(),
            "avg_odds": sub["o_entry"].mean(), "avg_edge": sub["edge"].mean(),
            "calib_gap": abs(sub["p_model"].mean() - sub["outcome"].mean()),
        })
    for b in fire["edge_bucket"].dropna().unique():
        sub = fire[fire["edge_bucket"] == b]
        rows.append({
            "cut": "edge_bucket", "value": str(b), "n": len(sub),
            "roi": sub["pnl"].mean(), "closing_price_edge": sub["closing_price_edge"].mean(),
            "avg_odds": sub["o_entry"].mean(), "avg_edge": sub["edge"].mean(),
            "calib_gap": abs(sub["p_model"].mean() - sub["outcome"].mean()),
        })
    return pd.DataFrame(rows)


def main() -> None:
    # Load per-model OOF
    m1 = _load_oof_with_market("dc", "oof_dev.csv").rename(columns={
        "dc_p_away": "m1_p_away", "dc_p_draw": "m1_p_draw", "dc_p_home": "m1_p_home"})
    m2 = _load_oof_with_market("elo", "oof_dev.csv").rename(columns={
        "elo_p_away": "m2_p_away", "elo_p_draw": "m2_p_draw", "elo_p_home": "m2_p_home"})
    m3 = _load_oof_with_market("m3", "oof_m3_dev.csv")
    m4 = _load_oof_with_market("m4", "oof_m4_dev.csv")

    all_signals = {}
    for name, df, prefix in [("M1_DC", m1, "m1"), ("M2_Elo", m2, "m2"),
                              ("M3_LGBM_midweek", m3, "m3"), ("M4_LGBM", m4, "m4")]:
        cal = _cross_fit_platt(df, prefix)
        sig = _build_signals(df, cal, prefix)
        all_signals[name] = sig
        print(f"[{name}] built {len(sig)} candidate signals", flush=True)

    # Sweep all models × {unrestricted, one-per-match}
    sweep_rows = []
    for name, sig in all_signals.items():
        sweep_rows.append(_sweep(sig, name, one_per_match=False))
        sweep_rows.append(_sweep(sig, name, one_per_match=True))
    sweep = pd.concat(sweep_rows, ignore_index=True)
    sweep.to_csv(RES / "edge_sweep_all_models.csv", index=False)

    print("\nEdge sweep (compact, showing model × threshold × policy):", flush=True)
    print(sweep[["model", "policy", "threshold", "signal_count", "roi", "closing_price_edge",
                 "max_drawdown", "calibration_gap", "share_draw"]].to_string(
        index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)

    # CLV / ROI decomposition at threshold 0.04 for best-Brier model (M2 Elo)
    print("\n--- Decomposition @ threshold 0.04, one_per_match=True ---", flush=True)
    decomps = {}
    for name in ("M1_DC", "M2_Elo", "M3_LGBM_midweek", "M4_LGBM"):
        d = _decompose(all_signals[name], thr=0.04, one_per_match=True)
        d["model"] = name
        decomps[name] = d
    dec = pd.concat(decomps.values(), ignore_index=True)
    dec.to_csv(RES / "clv_decomposition.csv", index=False)
    print(dec.to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)


if __name__ == "__main__":
    main()
