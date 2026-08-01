"""J8-I10: Wöchentliche Auto-Recalibration per Surface.

Liest Signal-Archive (`signal_history.jsonl` mit outcomes) + Ledger-Bets,
fittet je Surface einen Isotonic-Calibrator auf `(model_prob, outcome)` und
persistiert das Ergebnis als `models/tennis_calibrators/{surface}.pkl`.

Wird die Live-Ensemble später an eine `apply_calibrator()`-Post-Hook hooken.
Diese Version bereitet nur die Kalibratoren vor.

Skript ist idempotent: re-runs überschreiben. Bei zu wenigen Samples
(< MIN_N pro Surface) wird der Slot gesäubert (Fallback = identity).

Usage:
    python3 scripts/tennis_recalibrate.py
    python3 scripts/tennis_recalibrate.py --window-days 90 --min-n 40
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CAL_DIR = ROOT / "models" / "tennis_calibrators"
SIGNAL_HISTORY = ROOT / "data" / "cache" / "signal_history.jsonl"

_SURFACES = ("hard", "clay", "grass")
_DEFAULT_MIN_N = 30
_DEFAULT_WINDOW = 60


def _load_signals(window_days: int) -> list[dict]:
    if not SIGNAL_HISTORY.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    out: list[dict] = []
    for line in SIGNAL_HISTORY.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("sport") != "tennis":
            continue
        if row.get("outcome") not in ("correct", "wrong"):
            continue
        ts = row.get("scan_ts") or ""
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < cutoff:
            continue
        out.append(row)
    return out


def _fit_surface(signals: list[dict], surface: str, min_n: int) -> tuple[object | None, int]:
    """Fit isotonic on (model_prob, y). Return (calibrator, n)."""
    xs = []
    ys = []
    for r in signals:
        if r.get("surface", "").lower() != surface:
            continue
        p = r.get("model_prob")
        outcome = r.get("outcome")
        if p is None or outcome not in ("correct", "wrong"):
            continue
        xs.append(float(p))
        ys.append(1.0 if outcome == "correct" else 0.0)
    if len(xs) < min_n:
        return None, len(xs)
    try:
        from sklearn.isotonic import IsotonicRegression
    except ImportError:
        return None, len(xs)
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(xs, ys)
    return iso, len(xs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=_DEFAULT_WINDOW)
    ap.add_argument("--min-n", type=int, default=_DEFAULT_MIN_N)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    CAL_DIR.mkdir(parents=True, exist_ok=True)

    signals = _load_signals(args.window_days)
    print(f"[recalibrate] {len(signals)} Tennis-Signale in den letzten {args.window_days}d mit Outcome")

    summary: dict[str, dict] = {}
    for surf in _SURFACES:
        cal, n = _fit_surface(signals, surf, args.min_n)
        summary[surf] = {"n": n, "status": "fitted" if cal else "insufficient"}
        if cal is None:
            print(f"  {surf:8s} n={n} — n < min_n={args.min_n}, Slot geleert")
            slot = CAL_DIR / f"{surf}.pkl"
            if slot.exists() and not args.dry_run:
                slot.unlink()
            continue
        print(f"  {surf:8s} n={n} — Isotonic gefittet")
        if not args.dry_run:
            with (CAL_DIR / f"{surf}.pkl").open("wb") as f:
                pickle.dump(cal, f)

    if not args.dry_run:
        (CAL_DIR / "recalibrate_meta.json").write_text(json.dumps({
            "recalibrated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window_days": args.window_days,
            "min_n": args.min_n,
            "surfaces": summary,
        }, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
