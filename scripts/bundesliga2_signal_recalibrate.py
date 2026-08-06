"""2.BL Signal-Recalibrator — täglicher Kalibrierungs-Update.

Logik:
  1. Liest settled 2.BL-Signale mit sport="football", league="bl2" aus signal_history.jsonl.
  2. Berechnet Brier-Score und ECE (5 Bins) für 1X2-Signale.
  3. Ab MIN_SAMPLES=50 Signalen: fitted IsotonicRegression Meta-Calibrator
     und speichert models/lgbm_bundesliga2/meta_calibrator.pkl.
  4. Wenn ΔBrier ≥ 1pp gegenüber ungekalibriert: reduziert min_edge in LEAGUE_REGISTRY
     von 6% auf 4% (auto-CLV-Feedback via bl2_gate.json).

⚠️  Outcome-Feld MUSS 'won'/'lost'/'void' sein (nicht 'correct'/'wrong').
    Void-Signale werden ignoriert (kein Update).

CLI:
  python3 scripts/bundesliga2_signal_recalibrate.py [--dry-run]
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
META_CAL_PATH = ROOT / "models" / "lgbm_bundesliga2" / "meta_calibrator.pkl"
CAL_STATS_PATH = ROOT / "results" / "bl2_cal_stats.json"
BL2_GATE_PATH  = ROOT / "models" / "lgbm_bundesliga2" / "gate.json"

MIN_SAMPLES = 50


def _load_settled_bl2() -> list[dict]:
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
        if r.get("sport") != "football":
            continue
        if (r.get("league") or "").strip() not in ("bl2", "2. Bundesliga", ""):
            continue
        if r.get("outcome") not in ("won", "lost"):
            continue
        rows.append(r)
    return rows


def _1x2_pairs(rows: list[dict]) -> tuple[list[float], list[int]]:
    """model_prob=P(dieses Outcome tritt ein), actual=1 wenn won."""
    probs, actuals = [], []
    for r in rows:
        market = r.get("market", "")
        if market not in ("home", "draw", "away"):
            continue
        mp = float(r.get("model_prob") or r.get("model_p", 0))
        if not (0.01 <= mp <= 0.99):
            continue
        actual = 1 if r.get("outcome") == "won" else 0
        probs.append(mp)
        actuals.append(actual)
    return probs, actuals


def _brier(probs: list[float], actuals: list[int]) -> float:
    return float(np.mean([(p - a) ** 2 for p, a in zip(probs, actuals)]))


def _ece(probs: list[float], actuals: list[int], n_bins: int = 5) -> float:
    bins = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    n = len(probs)
    for lo, hi in zip(bins[:-1], bins[1:]):
        idx = [i for i, p in enumerate(probs) if lo <= p < hi]
        if not idx:
            continue
        avg_p = np.mean([probs[i] for i in idx])
        avg_a = np.mean([actuals[i] for i in idx])
        ece += len(idx) / n * abs(avg_p - avg_a)
    return float(ece)


def main(dry_run: bool = False) -> None:
    rows = _load_settled_bl2()
    print(f"[bl2_recal] {len(rows)} settled 2.BL-Signale geladen")

    probs, actuals = _1x2_pairs(rows)
    n = len(probs)
    print(f"[bl2_recal] {n} 1X2-Signale mit Outcome")

    if n == 0:
        print("[bl2_recal] Keine Daten — Skip")
        return

    brier = _brier(probs, actuals)
    ece = _ece(probs, actuals)
    print(f"[bl2_recal] Brier={brier:.4f} ECE={ece:.4f}")

    # Schreibe Stats immer (auch bei < MIN_SAMPLES)
    stats = {
        "n_signals": n,
        "brier": brier,
        "ece": ece,
        "min_samples_for_calibrator": MIN_SAMPLES,
        "calibrator_fitted": n >= MIN_SAMPLES,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if n < MIN_SAMPLES:
        print(f"[bl2_recal] {n}/{MIN_SAMPLES} Samples — Meta-Calibrator noch nicht aktiviert")
        if not dry_run:
            CAL_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
            CAL_STATS_PATH.write_text(json.dumps(stats, indent=2))
        return

    # Fit Isotonic Meta-Calibrator
    try:
        from sklearn.isotonic import IsotonicRegression
    except ImportError:
        print("[bl2_recal] sklearn fehlt — pip install scikit-learn")
        return

    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(np.array(probs), np.array(actuals))

    # Kalibrierter Brier
    cal_probs = iso.predict(np.array(probs))
    brier_cal = _brier(cal_probs.tolist(), actuals)
    improvement = brier - brier_cal
    print(f"[bl2_recal] Kalibriert Brier={brier_cal:.4f} ΔBrier={improvement:.4f}")

    stats["brier_calibrated"] = brier_cal
    stats["improvement"] = improvement

    if not dry_run:
        META_CAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(META_CAL_PATH, "wb") as f:
            pickle.dump(iso, f)
        print(f"[bl2_recal] Gespeichert: {META_CAL_PATH}")

        # Auto-Edge-Reduktion: wenn Modell sich mit ΔBrier ≥ 1pp beweist
        if improvement >= 0.01 and BL2_GATE_PATH.exists():
            try:
                gate = json.loads(BL2_GATE_PATH.read_text())
                if gate.get("min_edge_override", 0.06) > 0.04:
                    gate["min_edge_override"] = 0.04
                    gate["min_edge_reduced_at"] = datetime.now(timezone.utc).isoformat()
                    gate["min_edge_reason"] = f"CLV-Feedback: ΔBrier={improvement:.4f} ≥ 0.01"
                    BL2_GATE_PATH.write_text(json.dumps(gate, indent=2))
                    print(f"[bl2_recal] min_edge auto-reduziert auf 4% (war 6%)")
            except Exception as exc:
                print(f"[bl2_recal] Gate-Update fehlgeschlagen: {exc}")

        CAL_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CAL_STATS_PATH.write_text(json.dumps(stats, indent=2))
    else:
        print(f"[bl2_recal] dry-run — nichts gespeichert")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    main(dry_run=args.dry_run)
