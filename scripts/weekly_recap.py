"""Wöchentlicher Recap-Push (Sonntag 20 UTC).

Aggregiert die letzten 7 Tage aus dem Ledger und sendet einen
Web Push mit Gesamt-P&L, Hit-Rate, Best/Worst-Bet, ROI.

Wird via .github/workflows/weekly_recap.yml ausgelöst.
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

LEDGER = ROOT / "results" / "ledger.csv"


def main() -> int:
    if not LEDGER.exists():
        print("No ledger.csv — nothing to recap.")
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    cutoff_date = cutoff.strftime("%Y-%m-%d")

    rows: list[dict] = []
    try:
        with open(LEDGER, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("status") not in ("won", "lost", "push"):
                    continue
                date = (r.get("match_date") or "")[:10]
                if not date or date < cutoff_date:
                    continue
                try:
                    r["_pnl"] = float(r.get("pnl") or 0)
                    r["_stake"] = float(r.get("stake_amount") or 0)
                except ValueError:
                    continue
                rows.append(r)
    except Exception as e:
        print(f"Failed to read ledger: {e}")
        return 1

    if not rows:
        print("No settled bets in last 7 days — skipping recap.")
        return 0

    n = len(rows)
    pnl = sum(r["_pnl"] for r in rows)
    staked = sum(r["_stake"] for r in rows)
    won = sum(1 for r in rows if r.get("status") == "won")
    hit_rate = (won / n * 100) if n > 0 else 0.0
    roi = (pnl / staked * 100) if staked > 0 else 0.0

    best = max(rows, key=lambda r: r["_pnl"])
    worst = min(rows, key=lambda r: r["_pnl"])

    pnl_sign = "+" if pnl >= 0 else ""
    roi_sign = "+" if roi >= 0 else ""

    title_emoji = "📈" if pnl > 0 else "📉" if pnl < 0 else "➡️"
    title = f"{title_emoji} Woche: {pnl_sign}€{pnl:.2f} ({roi_sign}{roi:.1f}% ROI)"

    lines = [
        f"{n} Wetten · {won} gewonnen ({hit_rate:.0f}%)",
        f"Beste: {best.get('home','?')} vs {best.get('away','?')} +€{best['_pnl']:.2f}",
    ]
    if worst["_pnl"] < 0:
        lines.append(f"Schlechteste: {worst.get('home','?')} vs {worst.get('away','?')} €{worst['_pnl']:.2f}")
    body = "\n".join(lines)

    from src.notifications.web_push import _send_notification
    sent = _send_notification(
        title=title,
        body=body,
        url="/sportsbrain/#journal",
        kind="recap",
        tag=f"recap-{datetime.now(timezone.utc).strftime('%Y-W%U')}",
        require=False,
    )
    print(f"Weekly recap sent to {sent} subscribers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
