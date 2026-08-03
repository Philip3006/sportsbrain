"""N8: Model-Drift Brier Monitor — wöchentlicher Brier-Score-Vergleich vs. Baseline.

Liest settled Bets der letzten 28 Tage mit model_prob-Feld.
Berechnet Brier-Score: mean((model_prob - outcome)²).
Alert: WARN wenn Δ Brier > 0.01, CRITICAL wenn > 0.03 vs. Baseline.

Usage:
    python3 scripts/monitor_model_drift.py
    python3 scripts/monitor_model_drift.py --window-days 28 --push
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DEFAULT_USER, RESULTS_DIR, ledger_path_for

# Baseline Brier from walk-forward backtest (conservative estimate)
_BRIER_BASELINE = 0.24
_WARN_DELTA = 0.01
_CRIT_DELTA = 0.03
_MIN_BETS = 10


def _load_settled_rows(ledger_path: Path, window_days: int) -> list[dict]:
    if not ledger_path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    rows = []
    with open(ledger_path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") not in ("won", "lost"):
                continue
            prob_str = r.get("model_prob", "")
            if not prob_str:
                continue
            try:
                float(prob_str)
            except ValueError:
                continue
            date_str = r.get("match_date") or r.get("placed_date") or ""
            if date_str:
                try:
                    dt = datetime.fromisoformat(date_str[:10])
                    if dt.replace(tzinfo=timezone.utc) < cutoff:
                        continue
                except ValueError:
                    pass
            rows.append(r)
    return rows


def compute_brier(rows: list[dict]) -> float | None:
    scores = []
    for r in rows:
        try:
            prob = float(r["model_prob"])
            outcome = 1.0 if r["status"] == "won" else 0.0
            scores.append((prob - outcome) ** 2)
        except (ValueError, KeyError):
            pass
    if not scores:
        return None
    return sum(scores) / len(scores)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-days", type=int, default=28)
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--user", default=DEFAULT_USER)
    args = parser.parse_args()

    lp = ledger_path_for(args.user)
    rows = _load_settled_rows(lp, args.window_days)

    if len(rows) < _MIN_BETS:
        print(f"[drift] Only {len(rows)} settled bets with model_prob — need {_MIN_BETS}. Skipping.")
        return 0

    brier = compute_brier(rows)
    if brier is None:
        print("[drift] Could not compute Brier — no valid rows.")
        return 0

    delta = brier - _BRIER_BASELINE
    level = "OK"
    if delta > _CRIT_DELTA:
        level = "CRITICAL"
    elif delta > _WARN_DELTA:
        level = "WARN"

    print(f"[drift] Brier={brier:.4f} (baseline={_BRIER_BASELINE:.4f}, Δ={delta:+.4f}) n={len(rows)} → {level}")

    report = {
        "level": level,
        "brier": round(brier, 6),
        "baseline": _BRIER_BASELINE,
        "delta": round(delta, 6),
        "n_bets": len(rows),
        "window_days": args.window_days,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out_dir = ROOT / "results" / "audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (out_dir / f"model_drift_{ts}.json").write_text(json.dumps(report, indent=2))

    if level in ("WARN", "CRITICAL") and args.push:
        try:
            from src.notifications.web_push import _send_notification
            _send_notification(
                title=f"🔴 Model-Drift {level}: Brier +{delta:.3f}",
                body=f"Brier={brier:.3f} vs. Baseline={_BRIER_BASELINE:.3f} ({len(rows)} Bets)",
                url="/sportsbrain/#journal",
                kind="alert",
                tag=f"model-drift-{ts}",
                require=False,
            )
        except Exception as exc:
            print(f"[drift] Push failed: {exc}")

    return 1 if level == "CRITICAL" else 0


if __name__ == "__main__":
    sys.exit(main())
