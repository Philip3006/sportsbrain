"""FLAGSHIP-BL1 CORRECTED — Market benchmark on DEV data only.

CHANGES vs 60_market_hierarchy.py:
- Selection metric computed on DEVELOPMENT seasons (1617-2324) ONLY.
  2425 outcomes are NOT used for source / de-vig choice.
- 2526 coverage inspection (schema only) retained for reporting.

Sources: PS, Avg, Max, B365 (closing).
De-vig methods: basic, Shin, power, logodds.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.optimize import brentq  # noqa: E402

FULL_PKL = ROOT / "research" / "bl1" / "dataset" / "bl1_raw_full.pkl"
RES = ROOT / "research" / "bl1" / "results"
DEV_SEASONS = ["1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324"]
CALIB = "2425"
HOLDOUT = "2526"


def _label(row) -> int:
    return 2 if row["home_score"] > row["away_score"] else (1 if row["home_score"] == row["away_score"] else 0)


def _brier(y, p):
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


def devig_basic(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1 / oh, 1 / od, 1 / oa])
    return inv / inv.sum()


def devig_power(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    invs = np.array([1 / oh, 1 / od, 1 / oa])
    def f(a):
        return (invs ** a).sum() - 1.0
    try:
        a = brentq(f, 0.5, 2.0, maxiter=100)
    except ValueError:
        return devig_basic(oh, od, oa)
    return invs ** a


def devig_logodds(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    q = np.array([1 / oh, 1 / od, 1 / oa])
    logit_q = np.log(q / (1 - q))
    def f(b):
        return (1.0 / (1.0 + np.exp(-(logit_q + b)))).sum() - 1.0
    try:
        b = brentq(f, -5.0, 5.0, maxiter=100)
    except ValueError:
        return devig_basic(oh, od, oa)
    return 1.0 / (1.0 + np.exp(-(logit_q + b)))


def devig_shin(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    invs = np.array([1 / oh, 1 / od, 1 / oa])
    S = invs.sum()
    if S <= 1.0:
        return devig_basic(oh, od, oa)
    z = 0.05
    for _ in range(200):
        p_i = (np.sqrt(z * z + 4 * (1 - z) * (invs ** 2) / S) - z) / (2 * (1 - z))
        z_new = np.clip(z + 0.3 * (1.0 - p_i.sum()), 1e-6, 0.99)
        if abs(z_new - z) < 1e-8:
            z = z_new
            break
        z = z_new
    p_i = (np.sqrt(z * z + 4 * (1 - z) * (invs ** 2) / S) - z) / (2 * (1 - z))
    return p_i / p_i.sum()


SOURCES = {"Pinnacle": "PS", "Bookmaker_avg": "Avg", "Bookmaker_max": "Max", "Bet365": "B365"}
METHODS = {"basic": devig_basic, "shin": devig_shin, "power": devig_power, "logodds": devig_logodds}


def main() -> None:
    with open(FULL_PKL, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    raw = raw.dropna(subset=["home_score", "away_score"]).copy()
    raw["y"] = raw.apply(_label, axis=1)

    dev = raw[raw["season"].isin(DEV_SEASONS)].copy()
    # 2526 coverage — schema-only, no outcome eval
    holdout = raw[raw["season"] == HOLDOUT]
    hcov = {
        "Pinnacle_closing": holdout[["PSCH", "PSCD", "PSCA"]].dropna().shape[0] / max(len(holdout), 1),
        "Bookmaker_avg_closing": holdout[["AvgCH", "AvgCD", "AvgCA"]].dropna().shape[0] / max(len(holdout), 1) if "AvgCH" in holdout.columns else np.nan,
        "Bookmaker_max_closing": holdout[["MaxCH", "MaxCD", "MaxCA"]].dropna().shape[0] / max(len(holdout), 1) if "MaxCH" in holdout.columns else np.nan,
        "Bet365_closing": holdout[["B365CH", "B365CD", "B365CA"]].dropna().shape[0] / max(len(holdout), 1) if "B365CH" in holdout.columns else np.nan,
    }
    print(f"2526 CLOSING coverage (schema-only): {hcov}", flush=True)

    rows = []
    for src_name, src_prefix in SOURCES.items():
        hc, dc, ac = f"{src_prefix}CH", f"{src_prefix}CD", f"{src_prefix}CA"
        if not all(c in dev.columns for c in (hc, dc, ac)):
            continue
        for method_name, fn in METHODS.items():
            p_arr, y_arr = [], []
            for _, r in dev.iterrows():
                p = fn(r.get(hc), r.get(dc), r.get(ac))
                if p is None:
                    continue
                p_arr.append([p[2], p[1], p[0]])   # convert [home,draw,away] → [away,draw,home]
                y_arr.append(int(r["y"]))
            if len(y_arr) < 10:
                continue
            rows.append({
                "source": src_name, "method": method_name,
                "n": len(y_arr), "coverage": len(y_arr) / len(dev),
                "brier": _brier(np.array(y_arr), np.array(p_arr)),
            })

    result = pd.DataFrame(rows).sort_values("brier")
    result.to_csv(RES / "market_hierarchy_devig_v2.csv", index=False)
    print("\nMarket hierarchy — DEV ONLY (1617-2324) selection:", flush=True)
    print(result.to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)

    print(f"\n2526 outcomes used for selection: NO. Selection ran on dev only.")
    top = result.iloc[0]
    print(f"Provisional primary: {top['source']} / {top['method']} — dev Brier {top['brier']:.4f}", flush=True)


if __name__ == "__main__":
    main()
