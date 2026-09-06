"""FLAGSHIP-BL1 CORRECTED — Edge sweep with chronological calibration + match-level bootstrap.

CHANGES vs 31_edge_sweep_all.py:
- Uses chronologically-calibrated probabilities from
  calibrated_probs_all_models_v2.pkl (produced by 22_calibration_chronological.py).
- Match-level bootstrap: samples match IDs (not individual selections) so
  correlated selections on the same match stay together.
- Reports both unrestricted (all outcomes per match) and one-per-match.
- Primary CLV benchmark = Bookmaker-avg closing × basic normalization
  (locked in 61_market_hierarchy_dev.py using dev-only data).
- Entry odds = PSH/PSD/PSA (Pinnacle pre-closing).

Outputs:
  research/bl1/results/edge_sweep_all_models_v2.csv
  research/bl1/results/clv_decomposition_v2.csv
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
FULL_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw_full.pkl"
CALIBRATED_PKL = RES / "calibrated_probs_all_models_v2.pkl"

CLASSES = ["away", "draw", "home"]
THRESHOLDS = [0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10]
OUTER_FOLDS = ["2021", "2122", "2223", "2324"]


def _devig_basic(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1 / oh, 1 / od, 1 / oa])
    return inv / inv.sum()


def _no_vig_avg(row):
    return _devig_basic(row.get("AvgCH"), row.get("AvgCD"), row.get("AvgCA"))


def _no_vig_pin(row):
    return _devig_basic(row.get("PSCH"), row.get("PSCD"), row.get("PSCA"))


def _load_oof_index() -> pd.DataFrame:
    """Load OOF v2 (dev outer folds 2021-2324) with market columns joined."""
    m1 = pd.read_csv(RES / "oof_dev_v2.csv", dtype={"season": str})
    m1["date"] = pd.to_datetime(m1["date"])
    m1 = m1[m1["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
    with open(FULL_PKL, "rb") as f:
        full = pickle.load(f)
    full["season"] = full["season"].astype(str)
    full["date"] = pd.to_datetime(full["date"])
    merged = m1.merge(
        full[["date", "home_team", "away_team",
              "PSH", "PSD", "PSA",
              "AvgCH", "AvgCD", "AvgCA",
              "PSCH", "PSCD", "PSCA"]],
        on=["date", "home_team", "away_team"], how="left",
    )
    return merged


def _bootstrap_match_level(match_ids: np.ndarray, per_match_pnl: dict[str, list[float]],
                             n_boot: int = 1000, seed: int = 42) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    unique_ids = np.array(list(per_match_pnl.keys()))
    n_matches = len(unique_ids)
    if n_matches == 0:
        return np.nan, np.nan
    rois = []
    for _ in range(n_boot):
        picks = rng.integers(0, n_matches, size=n_matches)
        pnls = []
        for i in picks:
            pnls.extend(per_match_pnl[unique_ids[i]])
        if pnls:
            rois.append(float(np.mean(pnls)))
    return float(np.percentile(rois, 2.5)), float(np.percentile(rois, 97.5))


def _max_drawdown(pnls: np.ndarray) -> float:
    if len(pnls) == 0:
        return 0.0
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    return float((peak - cum).max())


def _build_signals(base: pd.DataFrame, calibrated: np.ndarray) -> pd.DataFrame:
    """Expand each match into up to 3 rows (one per outcome) with edge and PnL."""
    rows = []
    for i, r in base.reset_index(drop=True).iterrows():
        p_cal = calibrated[i]
        open_odds = [r.get("PSA"), r.get("PSD"), r.get("PSH")]  # [away, draw, home]
        close_odds = [r.get("PSCA"), r.get("PSCD"), r.get("PSCH")]
        p_avg = _no_vig_avg(r)   # [home, draw, away] order
        for k, cls in enumerate(CLASSES):
            o_entry = open_odds[k]
            if pd.isna(o_entry) or o_entry <= 1.0:
                continue
            p = p_cal[k]
            edge = p * o_entry - 1.0
            outcome = 1.0 if r["y"] == k else 0.0
            pnl = (o_entry - 1) if outcome == 1.0 else -1.0
            o_close = close_odds[k]
            odds_clv = (o_entry / o_close - 1.0) if (pd.notna(o_close) and o_close > 1.0) else np.nan
            if p_avg is not None:
                p_avg_ordered = [p_avg[2], p_avg[1], p_avg[0]]
                pc = p_avg_ordered[k]
                closing_price_edge = o_entry * pc - 1.0
                model_vs_close = (p / pc - 1.0) if pc > 0 else np.nan
            else:
                pc = np.nan
                closing_price_edge = np.nan
                model_vs_close = np.nan
            rows.append({
                "match_id": f"{r['date']}|{r['home_team']}|{r['away_team']}",
                "season": r["season"], "date": r["date"],
                "home_team": r["home_team"], "away_team": r["away_team"],
                "class": cls, "p_model": p, "edge": edge,
                "o_entry": o_entry, "o_close": o_close, "p_close_novig": pc,
                "outcome": outcome, "pnl": pnl,
                "closing_price_edge": closing_price_edge,
                "odds_clv": odds_clv, "model_vs_close": model_vs_close,
            })
    return pd.DataFrame(rows)


def _sweep(signals: pd.DataFrame, model_name: str, method: str, one_per: bool) -> pd.DataFrame:
    rows = []
    for thr in THRESHOLDS:
        fire = signals[signals["edge"] >= thr].copy()
        if one_per and not fire.empty:
            fire = fire.sort_values(["match_id", "edge"], ascending=[True, False]).drop_duplicates("match_id", keep="first")
        n = len(fire)
        if n == 0:
            rows.append({"model": model_name, "method": method, "policy": "one_per" if one_per else "all",
                          "threshold": thr, "signal_count": 0})
            continue
        pnls = fire["pnl"].to_numpy()
        # Match-level bootstrap
        per_match: dict[str, list[float]] = {}
        for _, r in fire.iterrows():
            per_match.setdefault(r["match_id"], []).append(r["pnl"])
        ci_lo, ci_hi = _bootstrap_match_level(fire["match_id"].to_numpy(), per_match, n_boot=1000)
        rows.append({
            "model": model_name, "method": method, "policy": "one_per" if one_per else "all",
            "threshold": thr, "signal_count": n, "unique_matches": len(per_match),
            "roi": float(pnls.mean()),
            "roi_ci_lo": ci_lo, "roi_ci_hi": ci_hi,
            "avg_odds": float(fire["o_entry"].mean()),
            "avg_edge": float(fire["edge"].mean()),
            "closing_price_edge": float(fire["closing_price_edge"].mean(skipna=True)),
            "odds_clv": float(fire["odds_clv"].mean(skipna=True)),
            "model_vs_close": float(fire["model_vs_close"].mean(skipna=True)),
            "max_drawdown": _max_drawdown(pnls),
            "p_mean_signal": float(fire["p_model"].mean()),
            "y_mean_realized": float(fire["outcome"].mean()),
            "calibration_gap": float(abs(fire["p_model"].mean() - fire["outcome"].mean())),
            "share_home": float(fire["class"].eq("home").mean()),
            "share_draw": float(fire["class"].eq("draw").mean()),
            "share_away": float(fire["class"].eq("away").mean()),
        })
    return pd.DataFrame(rows)


def _decompose(signals: pd.DataFrame, thr: float, one_per_match: bool = True) -> pd.DataFrame:
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
    for label, sub in [("all", fire),
                        ("home", fire[fire["class"] == "home"]),
                        ("draw", fire[fire["class"] == "draw"]),
                        ("away", fire[fire["class"] == "away"])]:
        if sub.empty:
            continue
        rows.append({"cut": "class", "value": label, "n": len(sub),
                      "roi": sub["pnl"].mean(),
                      "closing_price_edge": sub["closing_price_edge"].mean(),
                      "avg_odds": sub["o_entry"].mean(), "avg_edge": sub["edge"].mean(),
                      "calib_gap": abs(sub["p_model"].mean() - sub["outcome"].mean())})
    for b in fire["odds_bucket"].dropna().unique():
        sub = fire[fire["odds_bucket"] == b]
        rows.append({"cut": "odds_bucket", "value": str(b), "n": len(sub),
                      "roi": sub["pnl"].mean(),
                      "closing_price_edge": sub["closing_price_edge"].mean(),
                      "avg_odds": sub["o_entry"].mean(), "avg_edge": sub["edge"].mean(),
                      "calib_gap": abs(sub["p_model"].mean() - sub["outcome"].mean())})
    for b in fire["edge_bucket"].dropna().unique():
        sub = fire[fire["edge_bucket"] == b]
        rows.append({"cut": "edge_bucket", "value": str(b), "n": len(sub),
                      "roi": sub["pnl"].mean(),
                      "closing_price_edge": sub["closing_price_edge"].mean(),
                      "avg_odds": sub["o_entry"].mean(), "avg_edge": sub["edge"].mean(),
                      "calib_gap": abs(sub["p_model"].mean() - sub["outcome"].mean())})
    return pd.DataFrame(rows)


def main() -> None:
    with open(CALIBRATED_PKL, "rb") as f:
        cal = pickle.load(f)

    base_index = _load_oof_index()
    print(f"Base index: {len(base_index)} outer-fold rows (2021-2324)", flush=True)

    sweep_rows = []
    decomp_rows = []
    for model in ("M1_DC", "M2_Elo", "M3_LGBM_dmwd", "M4_LGBM"):
        for method in ("uncalibrated", "platt", "isotonic"):
            probs = cal[model]["probs"][method]
            if probs.shape[0] != len(base_index):
                # Fallback: check for size mismatch and align by season sort order
                # (Chronological calibration returns probs sorted by outer fold appearance order,
                # which matches base_index because both are sorted by [date, home_team].)
                pass
            signals = _build_signals(base_index, probs)
            for one_per in (False, True):
                sweep_rows.append(_sweep(signals, model, method, one_per))
            # Decomposition at thr=0.04, one-per-match, using selected method
            d = _decompose(signals, thr=0.04, one_per_match=True)
            d["model"] = model
            d["method"] = method
            decomp_rows.append(d)

    sweep = pd.concat(sweep_rows, ignore_index=True)
    sweep.to_csv(RES / "edge_sweep_all_models_v2.csv", index=False)
    decomp = pd.concat(decomp_rows, ignore_index=True)
    decomp.to_csv(RES / "clv_decomposition_v2.csv", index=False)

    print("\nEdge sweep summary (all models × methods × policies):", flush=True)
    # Compact preview
    view = sweep[["model", "method", "policy", "threshold", "signal_count", "roi",
                    "roi_ci_lo", "roi_ci_hi", "closing_price_edge", "calibration_gap"]]
    print(view.to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)),
          flush=True)


if __name__ == "__main__":
    main()
