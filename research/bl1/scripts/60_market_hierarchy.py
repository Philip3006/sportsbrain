"""FLAGSHIP-BL1 continuation — Market-benchmark hierarchy + de-vig comparison.

Uses the FULL raw dataset with all bookmaker columns.

For each of the four de-vig methods, computes closing no-vig probabilities
on the DEVELOPMENT + 2425 slice, then measures Brier of "market-implied
prediction" against actual outcomes.

De-vig methods:
  1. Basic normalization: p_k = (1/o_k) / sum(1/o_j)
  2. Shin (1993, iterative): accounts for insider-trading skew Z
  3. Power (overround^alpha with alpha calibrated):
        p_k = (1/o_k)^alpha, alpha solved so sum=1
  4. Log-odds (Buchdahl): p_k = e^(gamma*log(1/o_k)) / sum(...)

Market sources compared:
  - Pinnacle closing (PSCH/PSCD/PSCA)
  - Bookmaker-average closing (AvgCH/AvgCD/AvgCA)
  - Bookmaker-max closing (MaxCH/MaxCD/MaxCA)

Reports Brier per (market source × de-vig method) on the 2425 calibration
slice ONLY (holdout 2526 remains sealed for market-methodology selection).

Locks a primary + secondary benchmark hierarchy.
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


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    onehot = np.eye(3)[y]
    return float(np.mean(np.sum((p - onehot) ** 2, axis=1)))


# ---- De-vig methods -----------------------------------------------------

def devig_basic(oh, od, oa):
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    inv = np.array([1 / oh, 1 / od, 1 / oa])
    return inv / inv.sum()


def devig_power(oh, od, oa):
    """Find alpha so that sum (1/o_k)^alpha = 1. p_k = (1/o_k)^alpha."""
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    invs = np.array([1 / oh, 1 / od, 1 / oa])
    def f(a):
        return (invs ** a).sum() - 1.0
    try:
        a = brentq(f, 0.5, 2.0, maxiter=100)
    except ValueError:
        # Overround is negative (arbitrage) or very tight — fall back to basic
        return devig_basic(oh, od, oa)
    return invs ** a


def devig_logodds(oh, od, oa):
    """p_k = e^(gamma * logit(1/o_k)) style. Buchdahl variant. Solve gamma."""
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    q = np.array([1 / oh, 1 / od, 1 / oa])
    # Fair prob p_k with logit(p_k) = logit(q_k) + b, and sum p_k = 1
    logit_q = np.log(q / (1 - q))
    def f(b):
        logit_p = logit_q + b
        p = 1.0 / (1.0 + np.exp(-logit_p))
        return p.sum() - 1.0
    try:
        b = brentq(f, -5.0, 5.0, maxiter=100)
    except ValueError:
        return devig_basic(oh, od, oa)
    logit_p = logit_q + b
    return 1.0 / (1.0 + np.exp(-logit_p))


def devig_shin(oh, od, oa):
    """Shin (1993). Solve for Z ∈ [0,1] iteratively. p_k = (sqrt(z^2 + 4(1-z)*(1/o_k)^2 * Sum(1/o_j)) - z) / (2(1-z))."""
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    invs = np.array([1 / oh, 1 / od, 1 / oa])
    S = invs.sum()
    if S <= 1.0:
        # Non-overround market — Shin doesn't apply; fall back to basic
        return devig_basic(oh, od, oa)
    # Iterate to find z
    z = 0.05
    for _ in range(200):
        num = np.sqrt(z * z + 4 * (1 - z) * (invs ** 2) / S) - z
        den = 2 * (1 - z)
        p = num / den
        # New z from constraint sum(p) = 1
        diff = 1.0 - p.sum()
        z_new = np.clip(z + 0.3 * diff, 1e-6, 0.99)
        if abs(z_new - z) < 1e-8:
            z = z_new
            break
        z = z_new
    # Recompute p with converged z
    num = np.sqrt(z * z + 4 * (1 - z) * (invs ** 2) / S) - z
    den = 2 * (1 - z)
    p = num / den
    return p / p.sum()  # normalize residual


# ---- Runner -------------------------------------------------------------

def _source_columns(prefix: str) -> tuple[str, str, str]:
    """Returns (home, draw, away) column names for a given closing source prefix."""
    return f"{prefix}CH", f"{prefix}CD", f"{prefix}CA"


def main() -> None:
    with open(FULL_PKL, "rb") as f:
        raw = pickle.load(f)
    raw["season"] = raw["season"].astype(str)
    raw = raw.dropna(subset=["home_score", "away_score"]).copy()
    raw["y"] = raw.apply(_label, axis=1)

    # Constrain to development + 2425 (holdout 2526 excluded)
    slice_df = raw[raw["season"].isin(DEV_SEASONS + [CALIB])].copy()

    # 2526 coverage inspection (rows only) — reported per CEO's flag
    holdout_df = raw[raw["season"] == HOLDOUT]
    holdout_coverage = {
        "PS": holdout_df[["PSCH", "PSCD", "PSCA"]].dropna().shape[0] / max(len(holdout_df), 1),
        "Avg": holdout_df[["AvgCH", "AvgCD", "AvgCA"]].dropna().shape[0] / max(len(holdout_df), 1) if "AvgCH" in holdout_df.columns else np.nan,
        "Max": holdout_df[["MaxCH", "MaxCD", "MaxCA"]].dropna().shape[0] / max(len(holdout_df), 1) if "MaxCH" in holdout_df.columns else np.nan,
    }
    print(f"2526 CLOSING coverage (schema-only, no outcome eval): {holdout_coverage}", flush=True)

    sources = {
        "Pinnacle": "PS",
        "Bookmaker_avg": "Avg",
        "Bookmaker_max": "Max",
        "Bet365": "B365",
    }
    methods = {
        "basic": devig_basic,
        "shin": devig_shin,
        "power": devig_power,
        "logodds": devig_logodds,
    }

    # DEV metric on dev seasons only
    dev_slice = slice_df[slice_df["season"].isin(DEV_SEASONS)].copy()
    calib_slice = slice_df[slice_df["season"] == CALIB].copy()

    rows = []
    for src_name, src_prefix in sources.items():
        hc, dc, ac = _source_columns(src_prefix)
        if not all(c in slice_df.columns for c in (hc, dc, ac)):
            print(f"  {src_name}: columns missing, skip", flush=True)
            continue
        for method_name, fn in methods.items():
            for label, subset in [("dev", dev_slice), ("2425", calib_slice)]:
                p_arr = []
                y_arr = []
                covered = 0
                for _, r in subset.iterrows():
                    p = fn(r.get(hc), r.get(dc), r.get(ac))
                    if p is None:
                        continue
                    # Order in scripts: array = [home, draw, away] BUT for Brier convention
                    # (0=away, 1=draw, 2=home), we need to REORDER to [away, draw, home].
                    p_arr.append([p[2], p[1], p[0]])
                    y_arr.append(int(r["y"]))
                    covered += 1
                if len(y_arr) < 10:
                    continue
                b = _brier(np.array(y_arr), np.array(p_arr))
                rows.append({
                    "source": src_name, "method": method_name, "slice": label,
                    "n": len(y_arr), "coverage": covered / len(subset),
                    "brier": b,
                })

    result = pd.DataFrame(rows).sort_values(["slice", "brier"])
    result.to_csv(RES / "market_hierarchy_devig.csv", index=False)

    print("\nMarket hierarchy comparison (Brier of implied 1X2 vs actual):", flush=True)
    print(result.to_string(index=False, float_format=lambda x: f"{x:.4f}"), flush=True)

    # Recommend primary + secondary from the best 2425-slice options
    calib_view = result[result["slice"] == "2425"].sort_values("brier")
    if not calib_view.empty:
        top = calib_view.iloc[0]
        print(f"\nProvisional primary: {top['source']} / {top['method']} — 2425 Brier {top['brier']:.4f}", flush=True)


if __name__ == "__main__":
    main()
