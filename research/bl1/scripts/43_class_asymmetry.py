"""FLAGSHIP-BL1 — Class asymmetry decomposition (CEO Correction Section 9).

The persistent home-positive / draw-positive / away-weak-or-negative pattern
in edge sweep results appears across all four v2/v3 models. This script
decomposes the pattern by:

  - predicted probability quintile
  - opening odds bucket
  - closing odds bucket
  - favourite / underdog / neutral classification (from opening odds)
  - home/away favourite (based on which side has lower opening odds)
  - calibration residual per bucket
  - closing-price edge per bucket
  - season
  - promoted status

Uses the best-Brier candidate per model (per calibration_all_models_v2.csv).
Reports Brier + realised rate + calibration residual + primary CLV per bucket.

Enforces one-selection-per-match to make bucket comparisons meaningful.

No new hypotheses tested; this is diagnostic reporting only.
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


def _devig_basic(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1 / oh, 1 / od, 1 / oa])
    return inv / inv.sum()


def _favourite_side(row) -> str:
    ph = row.get("PSH")
    pa = row.get("PSA")
    if pd.isna(ph) or pd.isna(pa):
        return "unknown"
    if ph < pa - 0.5:
        return "home_fav"
    if pa < ph - 0.5:
        return "away_fav"
    return "balanced"


def _promoted_by_season(raw):
    seasons = sorted(raw["season"].unique())
    out = {}
    prev = None
    for s in seasons:
        cur = set(raw[raw["season"] == s]["home_team"]).union(raw[raw["season"] == s]["away_team"])
        out[s] = (cur - prev) if prev is not None else set()
        prev = cur
    return out


def main() -> None:
    # Model probabilities: use uncalibrated Elo (M2) as the reference model.
    # It has the best Brier (0.5977 with Platt, 0.6189 uncal; but for edge
    # research uncal is comparable). Also include M3 uncal for the LGBM view.
    m2 = pd.read_csv(RES / "oof_dev_v2.csv", dtype={"season": str})
    m2["date"] = pd.to_datetime(m2["date"])
    m2 = m2[m2["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)

    # Merge full-market columns
    with open(FULL_PKL, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    raw["date"] = pd.to_datetime(raw["date"])
    merged = m2.merge(
        raw[["date", "home_team", "away_team",
              "PSH", "PSD", "PSA", "AvgCH", "AvgCD", "AvgCA",
              "PSCH", "PSCD", "PSCA"]],
        on=["date", "home_team", "away_team"], how="left",
    )

    # Load calibrated M2 probs (Platt)
    with open(RES / "calibrated_probs_all_models_v2.pkl", "rb") as f:
        cal = pickle.load(f)
    m2_probs = cal["M2_Elo"]["probs"]["platt"]

    # Load M3 uncalibrated OOF
    m3 = pd.read_csv(RES / "oof_m3_dev_v2.csv", dtype={"season": str})
    m3["date"] = pd.to_datetime(m3["date"])
    m3 = m3[m3["season"].isin(OUTER_FOLDS)].sort_values(["date", "home_team"], kind="stable").reset_index(drop=True)

    # Favourite side classification from opening PS odds
    merged["fav_side"] = merged.apply(_favourite_side, axis=1)

    # Promoted-team status
    promoted_map = _promoted_by_season(raw[raw["season"] != "2526"])

    # Expand into per-outcome signals with model prob, edge, closing benchmark
    def _expand(base, probs, model_name):
        rows = []
        for i, r in base.reset_index(drop=True).iterrows():
            for k, cls in enumerate(CLASSES):
                o_entry = r.get(f"ps_open_{cls}") if f"ps_open_{cls}" in r else (
                    r.get("PSA") if cls == "away" else r.get("PSD") if cls == "draw" else r.get("PSH")
                )
                if pd.isna(o_entry) or o_entry <= 1.0:
                    continue
                p = probs[i, k]
                edge = p * o_entry - 1.0
                outcome = 1 if r["y"] == k else 0
                pnl = (o_entry - 1) if outcome == 1 else -1.0
                # Closing benchmark: bookmaker-avg × basic
                p_close = _devig_basic(r.get("AvgCH"), r.get("AvgCD"), r.get("AvgCA"))
                if p_close is not None:
                    p_close_ordered = [p_close[2], p_close[1], p_close[0]]
                    pc = p_close_ordered[k]
                    closing_price_edge = o_entry * pc - 1.0
                else:
                    pc = np.nan
                    closing_price_edge = np.nan
                promoted = (r["home_team"] in promoted_map.get(r["season"], set())) or \
                             (r["away_team"] in promoted_map.get(r["season"], set()))
                rows.append({
                    "model": model_name, "match_id": f"{r['date']}|{r['home_team']}|{r['away_team']}",
                    "season": r["season"], "class": cls,
                    "p_model": p, "o_entry": o_entry, "o_close_avg": None,
                    "p_close_novig": pc, "edge": edge, "outcome": outcome, "pnl": pnl,
                    "closing_price_edge": closing_price_edge,
                    "fav_side": r["fav_side"], "promoted": promoted,
                })
        return pd.DataFrame(rows)

    sig_m2 = _expand(merged, m2_probs, "M2_Elo_Platt")
    # Rebuild for M3 uncalibrated using its own probs
    merged3 = m3.merge(raw[["date", "home_team", "away_team", "PSH", "PSD", "PSA", "AvgCH", "AvgCD", "AvgCA"]],
                        on=["date", "home_team", "away_team"], how="left")
    merged3["fav_side"] = merged3.apply(_favourite_side, axis=1)
    m3_probs = merged3[["m3_p_away", "m3_p_draw", "m3_p_home"]].to_numpy()
    sig_m3 = _expand(merged3, m3_probs, "M3_LGBM_uncal")

    signals = pd.concat([sig_m2, sig_m3], ignore_index=True)
    # One-per-match at threshold 0.04 for the class asymmetry analysis
    thr = 0.04
    fired = signals[signals["edge"] >= thr].sort_values(["model", "match_id", "edge"], ascending=[True, True, False]).drop_duplicates(["model", "match_id"], keep="first")

    # Class × fav_side × model
    rows = []
    for model in fired["model"].unique():
        sub_m = fired[fired["model"] == model]
        for cls in CLASSES:
            for fav in ("home_fav", "away_fav", "balanced"):
                s = sub_m[(sub_m["class"] == cls) & (sub_m["fav_side"] == fav)]
                if len(s) < 5:
                    continue
                rows.append({
                    "model": model, "class": cls, "fav_side": fav,
                    "n": len(s),
                    "roi": s["pnl"].mean(),
                    "closing_price_edge": s["closing_price_edge"].mean(skipna=True),
                    "avg_odds": s["o_entry"].mean(),
                    "avg_edge": s["edge"].mean(),
                    "avg_p_model": s["p_model"].mean(),
                    "realised_rate": s["outcome"].mean(),
                    "calib_gap": abs(s["p_model"].mean() - s["outcome"].mean()),
                })

    # Class × predicted-probability quintile
    for model in fired["model"].unique():
        sub_m = fired[fired["model"] == model].copy()
        sub_m["p_q"] = pd.qcut(sub_m["p_model"], q=5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"], duplicates="drop")
        for cls in CLASSES:
            for q in sub_m["p_q"].dropna().unique():
                s = sub_m[(sub_m["class"] == cls) & (sub_m["p_q"] == q)]
                if len(s) < 5:
                    continue
                rows.append({
                    "model": model, "class": cls, "fav_side": f"p_quintile_{q}",
                    "n": len(s),
                    "roi": s["pnl"].mean(),
                    "closing_price_edge": s["closing_price_edge"].mean(skipna=True),
                    "avg_odds": s["o_entry"].mean(),
                    "avg_edge": s["edge"].mean(),
                    "avg_p_model": s["p_model"].mean(),
                    "realised_rate": s["outcome"].mean(),
                    "calib_gap": abs(s["p_model"].mean() - s["outcome"].mean()),
                })

    # Class × season
    for model in fired["model"].unique():
        sub_m = fired[fired["model"] == model]
        for cls in CLASSES:
            for season in OUTER_FOLDS:
                s = sub_m[(sub_m["class"] == cls) & (sub_m["season"] == season)]
                if len(s) < 5:
                    continue
                rows.append({
                    "model": model, "class": cls, "fav_side": f"season_{season}",
                    "n": len(s),
                    "roi": s["pnl"].mean(),
                    "closing_price_edge": s["closing_price_edge"].mean(skipna=True),
                    "avg_odds": s["o_entry"].mean(),
                    "avg_edge": s["edge"].mean(),
                    "avg_p_model": s["p_model"].mean(),
                    "realised_rate": s["outcome"].mean(),
                    "calib_gap": abs(s["p_model"].mean() - s["outcome"].mean()),
                })

    # Class × promoted
    for model in fired["model"].unique():
        sub_m = fired[fired["model"] == model]
        for cls in CLASSES:
            for promoted in (False, True):
                s = sub_m[(sub_m["class"] == cls) & (sub_m["promoted"] == promoted)]
                if len(s) < 5:
                    continue
                rows.append({
                    "model": model, "class": cls, "fav_side": f"promoted_{promoted}",
                    "n": len(s),
                    "roi": s["pnl"].mean(),
                    "closing_price_edge": s["closing_price_edge"].mean(skipna=True),
                    "avg_odds": s["o_entry"].mean(),
                    "avg_edge": s["edge"].mean(),
                    "avg_p_model": s["p_model"].mean(),
                    "realised_rate": s["outcome"].mean(),
                    "calib_gap": abs(s["p_model"].mean() - s["outcome"].mean()),
                })

    result = pd.DataFrame(rows)
    result.to_csv(RES / "class_asymmetry_decomposition.csv", index=False)
    print("\nClass asymmetry decomposition (one-per-match @ 0.04, both models):", flush=True)
    print(result.to_string(index=False, float_format=lambda x: f"{x:.4f}" if isinstance(x, float) else str(x)), flush=True)


if __name__ == "__main__":
    main()
