"""FLAGSHIP-BL1 — Edge sweep v3 for M1–M7.

CEO Correction Section 10. Rerun one-selection-per-match edge analysis
across all seven candidates using match-level bootstrap.

Uses each model's best-Brier method (see calibration_all_models_v2.csv):
  M1 uncalibrated
  M2 Platt
  M3 uncalibrated
  M4 uncalibrated
  M5 M6 M7: uncalibrated (M5 is intrinsically market-derived; M6 is a blend;
  M7 already uses market probs as features so re-calibration adds noise —
  verified downstream if needed).

Reports for each (model × threshold):
  - signal count, unique matches
  - ROI
  - match-level bootstrap CI 95%
  - primary CLV (Bookmaker-avg closing × basic)
  - odds CLV (secondary)
  - max drawdown
  - calibration gap on firing subset
  - class share
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
OUTER_FOLDS = ["2021", "2122", "2223", "2324"]
CLASSES = ["away", "draw", "home"]
THRESHOLDS = [0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10]


def _devig_basic(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1 / oh, 1 / od, 1 / oa])
    return inv / inv.sum()


def _load_model_probs():
    """Returns dict[str, tuple(oof_df, probs (n,3))] where oof_df has season/date/home/away/y."""
    out = {}
    # M1, M2 via calibrated pickle
    with open(RES / "calibrated_probs_all_models_v2.pkl", "rb") as f:
        cal = pickle.load(f)
    for name, method in (("M1_DC_uncal", "uncalibrated"), ("M2_Elo_Platt", "platt"),
                          ("M3_LGBM_uncal", "uncalibrated"), ("M4_LGBM_uncal", "uncalibrated")):
        key = {"M1_DC_uncal": "M1_DC", "M2_Elo_Platt": "M2_Elo",
                "M3_LGBM_uncal": "M3_LGBM_dmwd", "M4_LGBM_uncal": "M4_LGBM"}[name]
        src_file = "oof_dev_v2.csv" if key in ("M1_DC", "M2_Elo") else {
            "M3_LGBM_dmwd": "oof_m3_dev_v2.csv", "M4_LGBM": "oof_m4_dev_v2.csv"}[key]
        df = pd.read_csv(RES / src_file, dtype={"season": str})
        df = df[df["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        out[name] = (df, cal[key]["probs"][method])
    # M5, M6, M7 from their own OOF CSVs
    for name, path, cols in [
        ("M5_market_open", "oof_m5_dev_v3.csv", ("m5_p_away", "m5_p_draw", "m5_p_home")),
        ("M6_market_elo_blend", "oof_m6_dev_v3.csv", ("m6_p_away", "m6_p_draw", "m6_p_home")),
        ("M7_market_residual", "oof_m7_dev_v3.csv", ("m7_p_away", "m7_p_draw", "m7_p_home")),
    ]:
        p = RES / path
        if not p.exists():
            continue
        df = pd.read_csv(p, dtype={"season": str})
        df = df[df["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)
        probs = df[list(cols)].to_numpy()
        out[name] = (df, probs)
    return out


def _build_signals(base: pd.DataFrame, probs: np.ndarray) -> pd.DataFrame:
    # Merge full-market columns
    with open(FULL_PKL, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    raw["date"] = pd.to_datetime(raw["date"])
    base = base.copy()
    base["date"] = pd.to_datetime(base["date"])
    merged = base.merge(
        raw[["date", "home_team", "away_team",
              "PSH", "PSD", "PSA", "PSCH", "PSCD", "PSCA",
              "AvgCH", "AvgCD", "AvgCA"]],
        on=["date", "home_team", "away_team"], how="left",
    )
    rows = []
    for i, r in merged.reset_index(drop=True).iterrows():
        p_cal = probs[i]
        open_odds = [r.get("PSA"), r.get("PSD"), r.get("PSH")]
        close_odds = [r.get("PSCA"), r.get("PSCD"), r.get("PSCH")]
        p_close = _devig_basic(r.get("AvgCH"), r.get("AvgCD"), r.get("AvgCA"))
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
            if p_close is not None:
                pc = [p_close[2], p_close[1], p_close[0]][k]
                closing_price_edge = o_entry * pc - 1.0
            else:
                pc = np.nan; closing_price_edge = np.nan
            rows.append({
                "match_id": f"{r['date']}|{r['home_team']}|{r['away_team']}",
                "season": r["season"], "date": r["date"],
                "class": cls, "p_model": p, "edge": edge, "o_entry": o_entry,
                "o_close": o_close, "p_close_novig": pc,
                "outcome": outcome, "pnl": pnl,
                "closing_price_edge": closing_price_edge, "odds_clv": odds_clv,
            })
    return pd.DataFrame(rows)


def _bootstrap_match_level(per_match_pnl: dict, n_boot: int = 1000, seed: int = 42):
    rng = np.random.default_rng(seed)
    unique_ids = np.array(list(per_match_pnl.keys()))
    if len(unique_ids) == 0:
        return np.nan, np.nan
    rois = []
    for _ in range(n_boot):
        picks = rng.integers(0, len(unique_ids), size=len(unique_ids))
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


def _sweep_one_per(signals: pd.DataFrame, model_name: str) -> pd.DataFrame:
    rows = []
    for thr in THRESHOLDS:
        fire = signals[signals["edge"] >= thr].copy()
        if fire.empty:
            rows.append({"model": model_name, "threshold": thr, "signal_count": 0})
            continue
        fire = fire.sort_values(["match_id", "edge"], ascending=[True, False]).drop_duplicates("match_id", keep="first")
        pnls = fire["pnl"].to_numpy()
        per_match = {r["match_id"]: [r["pnl"]] for _, r in fire.iterrows()}
        lo, hi = _bootstrap_match_level(per_match, n_boot=1000)
        rows.append({
            "model": model_name, "threshold": thr,
            "signal_count": len(fire), "unique_matches": len(fire),
            "roi": float(pnls.mean()), "roi_ci_lo": lo, "roi_ci_hi": hi,
            "avg_odds": float(fire["o_entry"].mean()),
            "avg_edge": float(fire["edge"].mean()),
            "closing_price_edge": float(fire["closing_price_edge"].mean(skipna=True)),
            "odds_clv": float(fire["odds_clv"].mean(skipna=True)),
            "max_drawdown": _max_drawdown(pnls),
            "p_mean_signal": float(fire["p_model"].mean()),
            "y_mean_realized": float(fire["outcome"].mean()),
            "calibration_gap": float(abs(fire["p_model"].mean() - fire["outcome"].mean())),
            "share_home": float(fire["class"].eq("home").mean()),
            "share_draw": float(fire["class"].eq("draw").mean()),
            "share_away": float(fire["class"].eq("away").mean()),
        })
    return pd.DataFrame(rows)


def main() -> None:
    all_models = _load_model_probs()
    print(f"Available models: {list(all_models.keys())}", flush=True)

    all_rows = []
    for name, (df, probs) in all_models.items():
        signals = _build_signals(df, probs)
        all_rows.append(_sweep_one_per(signals, name))

    sweep = pd.concat(all_rows, ignore_index=True)
    sweep.to_csv(RES / "edge_sweep_v3_one_per_match.csv", index=False)
    print("\nOne-per-match edge sweep across M1–M7 (match-level bootstrap):", flush=True)
    print(sweep[["model", "threshold", "signal_count", "roi", "roi_ci_lo", "roi_ci_hi",
                  "closing_price_edge", "calibration_gap"]]
          .to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)


if __name__ == "__main__":
    main()
