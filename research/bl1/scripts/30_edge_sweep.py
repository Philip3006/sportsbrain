"""FLAGSHIP-BL1 Task G — Edge-threshold research on DEVELOPMENT OOF.

Sweeps EV thresholds {0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10} on the
CALIBRATED DC OOF predictions. Uses cross-fitted Platt (see 20_calibration.py
selection). Entry odds = Pinnacle OPENING (proxy for our entry price).
Closing no-vig probability computed via basic normalization for the primary
CLV metric.

For each threshold reports:
    - signal count / bet count
    - ROI at flat unit stake
    - Bootstrap CI on ROI
    - Closing price edge (PRIMARY CLV): entry_odds × p_close_novig − 1
    - Odds CLV (secondary): entry_odds / closing_odds − 1
    - Model-vs-close (diagnostic): p_model / p_close_novig − 1
    - Max drawdown (flat stakes)
    - Average odds
    - Outcome distribution
    - Average model edge (mean EV of firing signals)
    - Calibration of selected subset (ECE within firing signals)

Explicitly excludes 2425 (calibration) and 2526 (holdout).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402

OOF_DEV = ROOT / "research" / "bl1" / "results" / "oof_dev.csv"
OUT_DIR = ROOT / "research" / "bl1" / "results"

FOLDS = ["2021", "2122", "2223", "2324"]
CLASSES = ["away", "draw", "home"]
THRESHOLDS = [0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10]


def _no_vig(oh: float, od: float, oa: float) -> tuple[float, float, float] | None:
    if any(pd.isna(x) or x <= 1.0 for x in (oh, od, oa)):
        return None
    ih, id_, ia = 1.0 / oh, 1.0 / od, 1.0 / oa
    total = ih + id_ + ia
    return ih / total, id_ / total, ia / total


def _fit_platt(p_train: np.ndarray, y_train: np.ndarray) -> list:
    calibrators = []
    for k in range(3):
        clf = LogisticRegression(max_iter=1000)
        clf.fit(p_train[:, k].reshape(-1, 1), (y_train == k).astype(int))
        calibrators.append(clf)
    return calibrators


def _apply_platt(p: np.ndarray, cals: list) -> np.ndarray:
    out = np.zeros_like(p)
    for k, clf in enumerate(cals):
        out[:, k] = clf.predict_proba(p[:, k].reshape(-1, 1))[:, 1]
    row_sum = out.sum(axis=1, keepdims=True)
    row_sum[row_sum == 0] = 1.0
    return out / row_sum


def _cross_fitted_platt(oof: pd.DataFrame, prefix: str = "dc") -> np.ndarray:
    """Produces cross-fitted calibrated probabilities for the full dev set."""
    p_cols = [f"{prefix}_p_away", f"{prefix}_p_draw", f"{prefix}_p_home"]
    out = np.zeros((len(oof), 3))
    for held_out in FOLDS:
        mask_train = oof["season"] != held_out
        mask_eval = oof["season"] == held_out
        cals = _fit_platt(oof[mask_train][p_cols].to_numpy(),
                          oof[mask_train]["y"].to_numpy())
        out[mask_eval.values] = _apply_platt(oof[mask_eval][p_cols].to_numpy(), cals)
    return out


def _build_signals(oof: pd.DataFrame, calibrated: np.ndarray) -> pd.DataFrame:
    """Expands each match into 3 rows (one per outcome class). For each,
    computes EV using Pinnacle OPENING odds as the entry price. Also computes
    closing no-vig probability per outcome for the CLV metric.
    """
    rows = []
    for i, (_, r) in enumerate(oof.iterrows()):
        p_cal = calibrated[i]
        # entry odds = opening Pinnacle
        open_odds = [r.get("ps_open_away"), r.get("ps_open_draw"), r.get("ps_open_home")]
        close_odds = [r.get("ps_close_away"), r.get("ps_close_draw"), r.get("ps_close_home")]
        nv_close = _no_vig(r.get("ps_close_home"), r.get("ps_close_draw"), r.get("ps_close_away"))
        for k, cls in enumerate(CLASSES):
            o_entry = open_odds[k]
            o_close = close_odds[k]
            if pd.isna(o_entry) or o_entry <= 1.0:
                continue
            p = p_cal[k]
            ev = p * (o_entry - 1) - (1 - p)  # profit/loss expected on a 1-unit stake
            edge = p * o_entry - 1.0  # equivalent formulation, same threshold as ev/stake
            outcome = 1.0 if r["y"] == k else 0.0
            pnl = (o_entry - 1) if outcome == 1.0 else -1.0

            if nv_close is not None:
                # nv_close order is (home, draw, away) from _no_vig
                p_close = nv_close[2 if cls == "away" else (1 if cls == "draw" else 0)]
                close_price_edge = o_entry * p_close - 1.0  # PRIMARY CLV
                model_vs_close_diag = (p / p_close - 1.0) if p_close > 0 else np.nan
            else:
                p_close = np.nan
                close_price_edge = np.nan
                model_vs_close_diag = np.nan
            odds_clv = (o_entry / o_close - 1.0) if (pd.notna(o_close) and o_close > 1.0) else np.nan

            rows.append({
                "season": r["season"], "date": r["date"],
                "home_team": r["home_team"], "away_team": r["away_team"],
                "class": cls, "p_model": p, "ev": ev, "edge": edge,
                "o_entry": o_entry, "o_close": o_close,
                "p_close_novig": p_close,
                "outcome": outcome, "pnl": pnl,
                "close_price_edge": close_price_edge,
                "odds_clv": odds_clv,
                "model_vs_close": model_vs_close_diag,
            })
    return pd.DataFrame(rows)


def _bootstrap_roi(pnls: np.ndarray, n_boot: int = 1000, seed: int = 42) -> tuple[float, float]:
    if len(pnls) == 0:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    vals = [pnls[rng.integers(0, len(pnls), size=len(pnls))].mean() for _ in range(n_boot)]
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def _max_drawdown(pnls: np.ndarray) -> float:
    if len(pnls) == 0:
        return 0.0
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    return float(dd.max())


def main() -> None:
    oof = pd.read_csv(OOF_DEV, dtype={"season": str})
    print(f"Loaded {len(oof)} dev OOF rows.", flush=True)

    # Calibrated DC probabilities via cross-fitted Platt.
    p_cal = _cross_fitted_platt(oof, "dc")
    signals = _build_signals(oof, p_cal)
    print(f"Built {len(signals)} candidate signals (3 per match).", flush=True)

    # Coverage stats for entry odds availability
    print(f"  entry-odds coverage: {signals['o_entry'].notna().mean():.2%}", flush=True)
    print(f"  closing no-vig coverage: {signals['p_close_novig'].notna().mean():.2%}", flush=True)

    rows = []
    for thr in THRESHOLDS:
        fire = signals[signals["edge"] >= thr].copy()
        n = len(fire)
        if n == 0:
            rows.append({"threshold": thr, "signal_count": 0})
            continue
        pnls = fire["pnl"].to_numpy()
        roi = pnls.mean() if n > 0 else np.nan
        ci_lo, ci_hi = _bootstrap_roi(pnls, n_boot=1000)
        mdd = _max_drawdown(pnls)
        # CLV
        cpe = fire["close_price_edge"].mean(skipna=True)
        oclv = fire["odds_clv"].mean(skipna=True)
        mvc = fire["model_vs_close"].mean(skipna=True)
        # Calibration of firing set (mean predicted p vs mean realized outcome)
        p_mean = fire["p_model"].mean()
        y_mean = fire["outcome"].mean()
        # Outcome distribution
        home_wr = float(fire["class"].eq("home").mean())
        draw_wr = float(fire["class"].eq("draw").mean())
        away_wr = float(fire["class"].eq("away").mean())
        rows.append({
            "threshold": thr,
            "signal_count": n,
            "roi": roi, "roi_ci_lo": ci_lo, "roi_ci_hi": ci_hi,
            "avg_odds": fire["o_entry"].mean(),
            "avg_edge": fire["edge"].mean(),
            "closing_price_edge_mean": cpe,
            "odds_clv_mean": oclv,
            "model_vs_close_mean": mvc,
            "max_drawdown_units": mdd,
            "signal_pmean": p_mean,
            "realized_ymean": y_mean,
            "calibration_gap": abs(p_mean - y_mean),
            "share_home": home_wr, "share_draw": draw_wr, "share_away": away_wr,
        })
    result = pd.DataFrame(rows)
    result.to_csv(OUT_DIR / "edge_sweep_dev.csv", index=False)
    print("\nEdge sweep (calibrated DC, dev OOF only):", flush=True)
    fmt = {c: (lambda x: f"{x:.4f}" if isinstance(x, (int, float)) and not pd.isna(x) else "-") for c in result.columns}
    print(result.to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
