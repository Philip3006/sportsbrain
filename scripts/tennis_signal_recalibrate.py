"""
Tennis Signal Recalibrator — täglicher Kalibrierungs-Update aus signal_history.jsonl.

Logik:
  1. Liest alle settled Tennis-Signale mit sport="tennis".
  2. Match-Winner-Signale (market="home"/"away"): (model_prob, actual) → Kalibrierung.
  3. Berechnet Brier-Score, Log-Loss, ECE (5 Bins) und schreibt Stats.
  4. Ab MIN_SAMPLES Match-Winner-Signale: fitted IsotonicRegression-Meta-Calibrator
     und speichert models/tennis_lgbm/meta_calibrator.pkl.
     Dieser wird dann im Ensemble als finaler Korrektiv-Layer angewendet.

CLI:
  python3 scripts/tennis_signal_recalibrate.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SIGNAL_HISTORY = ROOT / "data" / "cache" / "signal_history.jsonl"
META_CAL_PATH = ROOT / "models" / "tennis_lgbm" / "meta_calibrator.pkl"
CAL_STATS_PATH = ROOT / "results" / "tennis_cal_stats.json"

# Isotonic braucht mindestens diese Anzahl Punkte um sinnvoll zu sein
MIN_SAMPLES = 50


def _load_settled_tennis() -> list[dict]:
    if not SIGNAL_HISTORY.exists():
        return []
    rows = []
    for line in SIGNAL_HISTORY.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("sport") != "tennis":
            continue
        if r.get("outcome") not in ("won", "lost"):
            continue
        rows.append(r)
    return rows


def _match_winner_pairs(rows: list[dict]) -> tuple[list[float], list[int]]:
    """Nur home/away Signale — model_prob = P(diese Seite gewinnt)."""
    probs, actuals = [], []
    for r in rows:
        if r.get("market") not in ("home", "away"):
            continue
        p = r.get("model_prob")
        if p is None or not (0.0 < p < 1.0):
            continue
        actual = 1 if r["outcome"] == "won" else 0
        probs.append(p)
        actuals.append(actual)
    return probs, actuals


def _all_market_pairs(rows: list[dict]) -> tuple[list[float], list[int]]:
    """Alle Signale — model_prob = P(Event eintritt), actual = 1 wenn gewonnen."""
    probs, actuals = [], []
    for r in rows:
        p = r.get("model_prob")
        if p is None or not (0.0 < p < 1.0):
            continue
        actual = 1 if r["outcome"] == "won" else 0
        probs.append(p)
        actuals.append(actual)
    return probs, actuals


def _brier(probs: list[float], actuals: list[int]) -> float:
    return float(np.mean([(p - a) ** 2 for p, a in zip(probs, actuals)]))


def _logloss(probs: list[float], actuals: list[int]) -> float:
    eps = 1e-7
    return float(-np.mean([
        a * math.log(max(p, eps)) + (1 - a) * math.log(max(1 - p, eps))
        for p, a in zip(probs, actuals)
    ]))


def _ece(probs: list[float], actuals: list[int], n_bins: int = 5) -> float:
    """Expected Calibration Error (uniform bins)."""
    bins = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    n = len(probs)
    probs_arr = np.array(probs)
    actuals_arr = np.array(actuals)
    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (probs_arr >= lo) & (probs_arr < hi)
        if mask.sum() == 0:
            continue
        frac = mask.sum() / n
        acc = actuals_arr[mask].mean()
        conf = probs_arr[mask].mean()
        ece += frac * abs(acc - conf)
    return float(ece)


def _reliability_curve(probs: list[float], actuals: list[int], n_bins: int = 5) -> list[dict]:
    bins = np.linspace(0, 1, n_bins + 1)
    curve = []
    probs_arr = np.array(probs)
    actuals_arr = np.array(actuals)
    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (probs_arr >= lo) & (probs_arr < hi)
        n = int(mask.sum())
        if n == 0:
            continue
        curve.append({
            "bin_lo": round(float(lo), 2),
            "bin_hi": round(float(hi), 2),
            "n": n,
            "mean_pred": round(float(probs_arr[mask].mean()), 4),
            "mean_actual": round(float(actuals_arr[mask].mean()), 4),
        })
    return curve


def recalibrate(dry_run: bool = False) -> dict:
    settled = _load_settled_tennis()
    print(f"[recal] {len(settled)} settled Tennis-Signale geladen")

    # --- Match-Winner (Kernkalibrierung) ---
    mw_probs, mw_actuals = _match_winner_pairs(settled)
    # --- Alle Märkte ---
    all_probs, all_actuals = _all_market_pairs(settled)

    stats: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_settled_total": len(settled),
        "match_winner": {},
        "all_markets": {},
        "meta_calibrator": {"fitted": False, "n_samples": 0},
    }

    if all_probs:
        stats["all_markets"] = {
            "n": len(all_probs),
            "brier": round(_brier(all_probs, all_actuals), 5),
            "log_loss": round(_logloss(all_probs, all_actuals), 5),
            "ece": round(_ece(all_probs, all_actuals), 5),
            "win_rate": round(sum(all_actuals) / len(all_actuals), 4),
            "reliability_curve": _reliability_curve(all_probs, all_actuals),
        }
        s = stats["all_markets"]
        print(f"[recal] Alle Märkte  n={s['n']}  Brier={s['brier']:.4f}  "
              f"LogLoss={s['log_loss']:.4f}  ECE={s['ece']:.4f}  WR={s['win_rate']:.1%}")

    if mw_probs:
        stats["match_winner"] = {
            "n": len(mw_probs),
            "brier": round(_brier(mw_probs, mw_actuals), 5),
            "log_loss": round(_logloss(mw_probs, mw_actuals), 5),
            "ece": round(_ece(mw_probs, mw_actuals), 5),
            "win_rate": round(sum(mw_actuals) / len(mw_actuals), 4),
            "reliability_curve": _reliability_curve(mw_probs, mw_actuals),
        }
        s = stats["match_winner"]
        print(f"[recal] Match-Winner n={s['n']}  Brier={s['brier']:.4f}  "
              f"LogLoss={s['log_loss']:.4f}  ECE={s['ece']:.4f}  WR={s['win_rate']:.1%}")

    # --- Meta-Calibrator (ab MIN_SAMPLES) ---
    if len(mw_probs) >= MIN_SAMPLES:
        from sklearn.isotonic import IsotonicRegression
        probs_arr = np.array(mw_probs)
        actuals_arr = np.array(mw_actuals)
        cal = IsotonicRegression(out_of_bounds="clip")
        cal.fit(probs_arr, actuals_arr)

        stats["meta_calibrator"] = {
            "fitted": True,
            "n_samples": len(mw_probs),
            "brier_before": round(_brier(mw_probs, mw_actuals), 5),
            "brier_after": round(
                float(np.mean((cal.predict(probs_arr) - actuals_arr) ** 2)), 5
            ),
        }
        print(f"[recal] Meta-Calibrator fitted — "
              f"Brier vorher: {stats['meta_calibrator']['brier_before']:.4f} → "
              f"nachher: {stats['meta_calibrator']['brier_after']:.4f}")

        if not dry_run:
            META_CAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with META_CAL_PATH.open("wb") as f:
                pickle.dump({"calibrator": cal, "n_samples": len(mw_probs),
                             "generated_at": stats["generated_at"]}, f)
            print(f"[recal] {META_CAL_PATH.name} gespeichert")
    else:
        needed = MIN_SAMPLES - len(mw_probs)
        print(f"[recal] {len(mw_probs)} Match-Winner-Signale — noch {needed} bis Meta-Calibrator")

    if not dry_run:
        CAL_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CAL_STATS_PATH.write_text(json.dumps(stats, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"[recal] {CAL_STATS_PATH.name} geschrieben")

    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    recalibrate(dry_run=args.dry_run)
