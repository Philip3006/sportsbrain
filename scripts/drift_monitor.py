"""Phase 4.1 — Drift Monitor.

Checks whether the live model is drifting relative to:
  1. Closing-line value (CLV): bet_odds / closing_odds - 1.
     Positive = beat the close; consistent negative = model has lost edge.
  2. Empirical calibration: for each market bucket (home/draw/away/over/under),
     compare model_prob to actual win rate over settled bets.
  3. DC param sanity: re-run validate_params on the current snapshot and
     flag any team that has drifted > 0.5 from its walk-forward baseline.

Triggers a Telegram alert when:
  • CLV mean < -5% on ≥ 5 bets with closing-odds data (model behind the line).
  • Win rate deviates > 20pp from model_prob expectation on ≥ 10 bets.
  • DC bounds hit on current snapshot.

Usage:
  python3 scripts/drift_monitor.py
  python3 scripts/drift_monitor.py --no-alert   # report only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import MODELS_DIR, RESULTS_DIR

_CLV_ALERT_THRESHOLD = -0.05   # mean CLV below -5% → alert
_CALIB_ALERT_PP = 0.20          # abs(win_rate - mean_model_prob) > 20pp → alert
_MIN_BETS_CLV = 5
_MIN_BETS_CALIB = 10


def _load_ledger() -> pd.DataFrame:
    path = RESULTS_DIR / "ledger.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _market_bucket(market: str) -> str:
    if market in ("home", "draw", "away"):
        return market
    if "over" in market:
        return "over"
    if "under" in market:
        return "under"
    if market.startswith("ah"):
        return "asian_handicap"
    if market.startswith("dc_"):
        return "double_chance"
    if "btts" in market:
        return "btts"
    return "other"


def analyse_clv(df: pd.DataFrame) -> dict:
    """Compute CLV stats from ledger rows where closing_odds is populated."""
    clv_df = df[df["clv"].notna() & (df["status"].isin(["won", "lost"]))].copy()
    if clv_df.empty:
        return {"n": 0, "mean_clv": None, "pct_positive": None, "alert": False}
    clvs = clv_df["clv"].astype(float).values
    mean_clv = float(clvs.mean())
    pct_pos = float((clvs > 0).mean())
    alert = len(clvs) >= _MIN_BETS_CLV and mean_clv < _CLV_ALERT_THRESHOLD
    return {
        "n": int(len(clvs)),
        "mean_clv": mean_clv,
        "pct_positive": pct_pos,
        "alert": alert,
    }


def analyse_pinnacle_clv(df: pd.DataFrame) -> dict:
    """Opening CLV vs Pinnacle reference odds stored at bet placement time.

    opening_clv = decimal_odds / pinnacle_ref_odds - 1
    Positive = we got better price than Pinnacle's opening line.
    """
    if "pinnacle_ref_odds" not in df.columns:
        return {"n": 0, "mean_opening_clv": None, "pct_positive": None}

    pin_df = df[
        df["pinnacle_ref_odds"].notna()
        & (df["pinnacle_ref_odds"] != "")
        & (df["status"].isin(["won", "lost", "open"]))
    ].copy()
    pin_df = pin_df[pd.to_numeric(pin_df["pinnacle_ref_odds"], errors="coerce") > 1.0].copy()
    if pin_df.empty:
        return {"n": 0, "mean_opening_clv": None, "pct_positive": None}

    dec_odds = pd.to_numeric(pin_df["decimal_odds"], errors="coerce")
    pin_odds = pd.to_numeric(pin_df["pinnacle_ref_odds"], errors="coerce")
    opening_clv = (dec_odds / pin_odds - 1.0).dropna().values
    mean_oclv = float(opening_clv.mean())
    pct_pos = float((opening_clv > 0).mean())
    alert = len(opening_clv) >= _MIN_BETS_CLV and mean_oclv < _CLV_ALERT_THRESHOLD
    return {
        "n": int(len(opening_clv)),
        "mean_opening_clv": mean_oclv,
        "pct_positive": pct_pos,
        "alert": alert,
    }


def analyse_calibration(df: pd.DataFrame) -> dict:
    """Compare model_prob expectations vs actual win rate per market bucket."""
    settled = df[df["status"].isin(["won", "lost"])].copy()
    if settled.empty:
        return {}

    # model_prob is the probability at bet placement (stored in ledger)
    # We use decimal_odds as a proxy when model_prob not explicitly stored:
    # fair_prob = 1/(decimal_odds * (1 + edge)) but we don't have it here.
    # Instead we read it from decimal_odds and stake_pct to approximate edge.
    # If model_prob column exists, use it directly; otherwise skip.
    if "model_prob" not in settled.columns:
        return {}

    settled["bucket"] = settled["market"].apply(_market_bucket)
    results = {}
    for bucket, grp in settled.groupby("bucket"):
        n = len(grp)
        if n < 3:
            continue
        mean_mp = float(grp["model_prob"].mean())
        win_rate = float((grp["status"] == "won").mean())
        deviation = abs(win_rate - mean_mp)
        results[bucket] = {
            "n": n,
            "mean_model_prob": round(mean_mp, 3),
            "empirical_win_rate": round(win_rate, 3),
            "deviation_pp": round(deviation * 100, 1),
            "alert": n >= _MIN_BETS_CALIB and deviation > _CALIB_ALERT_PP,
        }
    return results


def analyse_dc_params() -> dict:
    """Load current DC snapshot, run validate_params, check for outliers."""
    snap_dir = MODELS_DIR / "dixon_coles"
    if not snap_dir.exists():
        return {"status": "no_model"}
    files = sorted(snap_dir.glob("params_*.pkl"))
    if not files:
        return {"status": "no_model"}

    try:
        from src.models.dixon_coles import load, validate_params
        params = load(files[-1])
        issues = validate_params(params)
        return {
            "snapshot": files[-1].name,
            "n_teams": len(params.attack),
            "fit_date": str(params.fit_date.date()),
            "n_issues": len(issues),
            "issues": issues[:5],  # first 5
            "alert": len(issues) > 0,
        }
    except Exception as e:
        return {"status": f"error: {e}", "alert": False}


def _send_alert(msg: str, no_alert: bool) -> None:
    # Drift-Alerts wurden früher via Telegram verschickt. Telegram-Bot ist seit Roadmap B6
    # retired (PWA-Push ist primärer Kanal). Drift-Output bleibt im stdout-Log + Markdown-Report.
    if no_alert:
        return
    print(f"[drift-alert] {msg}")


def main(no_alert: bool = False) -> int:
    print("=" * 55)
    print("Drift Monitor — SportsBrain Phase 4.1")
    print("=" * 55)

    df = _load_ledger()
    if df.empty:
        print("Ledger empty or missing — nothing to analyse.")
        return 0

    total = len(df)
    settled_n = int((df["status"].isin(["won", "lost"])).sum()) if "status" in df.columns else 0
    print(f"\nLedger: {total} bets total, {settled_n} settled\n")

    # 1. CLV analysis (closing-line)
    clv = analyse_clv(df)
    print("── CLV Analysis (closing line) ───────────────────")
    if clv["n"] == 0:
        print("  No closing odds data yet.")
    else:
        print(f"  n={clv['n']}  mean CLV={clv['mean_clv']:+.3f}  "
              f"pct_positive={clv['pct_positive']:.0%}")
        if clv["alert"]:
            msg = f"⚠️ DRIFT ALERT: mean CLV={clv['mean_clv']:+.3f} < {_CLV_ALERT_THRESHOLD} on {clv['n']} bets"
            print(f"  {msg}")
            _send_alert(msg, no_alert)

    # 1b. Opening CLV vs Pinnacle reference (only when PINNACLE_CLV_ENABLED)
    from src.config import PINNACLE_CLV_ENABLED
    if PINNACLE_CLV_ENABLED:
        print("\n── Opening CLV vs Pinnacle ───────────────────────")
        pclv = analyse_pinnacle_clv(df)
        if pclv["n"] == 0:
            print("  No Pinnacle reference odds stored yet "
                  "(bets placed before this feature was enabled).")
        else:
            print(f"  n={pclv['n']}  mean opening CLV={pclv['mean_opening_clv']:+.3f}  "
                  f"pct_positive={pclv['pct_positive']:.0%}")
            if pclv.get("alert"):
                msg = (f"⚠️ PINNACLE CLV ALERT: opening CLV={pclv['mean_opening_clv']:+.3f} "
                       f"< {_CLV_ALERT_THRESHOLD}")
                print(f"  {msg}")
                _send_alert(msg, no_alert)

    # 2. Calibration
    print("\n── Calibration (model_prob vs win rate) ──────────")
    calib = analyse_calibration(df)
    if not calib:
        print("  model_prob column missing or insufficient data.")
    else:
        any_alert = False
        for bucket, stats in sorted(calib.items()):
            flag = " ⚠️" if stats["alert"] else ""
            print(f"  {bucket:<18} n={stats['n']:3d}  "
                  f"model={stats['mean_model_prob']:.3f}  "
                  f"actual={stats['empirical_win_rate']:.3f}  "
                  f"Δ={stats['deviation_pp']:+.1f}pp{flag}")
            if stats["alert"]:
                any_alert = True
        if any_alert:
            msg = "⚠️ DRIFT ALERT: empirical win rate deviates >20pp from model_prob"
            _send_alert(msg, no_alert)

    # 3. DC params
    print("\n── DC Parameter Sanity ────────────────────────────")
    dc = analyse_dc_params()
    if "status" in dc:
        print(f"  {dc['status']}")
    else:
        print(f"  Snapshot: {dc['snapshot']}  "
              f"Teams: {dc['n_teams']}  "
              f"Fit: {dc['fit_date']}")
        if dc["n_issues"] == 0:
            print("  All params within valid bounds ✅")
        else:
            print(f"  ⚠️ {dc['n_issues']} param issue(s): {dc['issues']}")
            msg = f"⚠️ DRIFT ALERT: DC params have {dc['n_issues']} out-of-bound values"
            _send_alert(msg, no_alert)

    # 4. ROI summary
    if settled_n >= 3 and "pnl" in df.columns and "stake_amount" in df.columns:
        s = df[df["status"].isin(["won", "lost"])]
        total_staked = s["stake_amount"].sum()
        total_pnl = s["pnl"].sum()
        roi = total_pnl / total_staked if total_staked > 0 else 0.0
        print(f"\n── P&L Summary ────────────────────────────────────")
        print(f"  Staked: €{total_staked:.2f}  PNL: €{total_pnl:+.2f}  ROI: {roi:+.1%}")

    print("\n" + "=" * 55)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-alert", action="store_true", help="Suppress Telegram alerts")
    args = parser.parse_args()
    sys.exit(main(no_alert=args.no_alert))
