"""Match-Reminder Push: 25-35 Min vor Anpfiff einer offenen Wette.

Wird via prematch_scan.yml Watchdog alle 30 Min ausgelöst. Mit dem 30-Min-
Window (25-35 vor KO) ist garantiert genau ein Hit pro Spiel.

Liest open bets aus dem Ledger + Kickoff aus docs/data/signals.json
(schedule oder wm_results) und sendet pro offener Wette einen Push.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.config import ledger_path_for, DEFAULT_USER
LEDGER = ledger_path_for(DEFAULT_USER)
SIGNALS = ROOT / "docs" / "data" / "signals.json"


def _norm(s: str) -> str:
    return (s or "").lower().strip()


def main() -> int:
    if not LEDGER.exists() or not SIGNALS.exists():
        return 0

    # Open bets
    open_bets: list[dict] = []
    with open(LEDGER, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("status") == "open":
                open_bets.append(r)
    if not open_bets:
        return 0

    # Schedule (Kickoff-Map aus signals.json)
    try:
        data = json.loads(SIGNALS.read_text())
    except Exception:
        return 0
    schedule = data.get("schedule", [])
    ko_map: dict[tuple[str, str], str] = {}
    for g in schedule:
        h = _norm(g.get("home", ""))
        a = _norm(g.get("away", ""))
        ko = g.get("kickoff", "")
        if h and a and ko:
            ko_map[(h, a)] = ko

    now = datetime.now(timezone.utc)
    sent_count = 0

    from src.notifications.web_push import _send_notification

    for b in open_bets:
        home = b.get("home", "")
        away = b.get("away", "")
        market = b.get("market", "")
        odds = float(b.get("decimal_odds", 0) or 0)
        stake = float(b.get("stake_amount", 0) or 0)
        ko_iso = ko_map.get((_norm(home), _norm(away)))
        if not ko_iso:
            continue
        try:
            ko_dt = datetime.fromisoformat(ko_iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        minutes_to_ko = (ko_dt - now).total_seconds() / 60.0
        # 25-35 Min Window (30-Min-Cron + 5 Min Slack auf jeder Seite → genau ein Treffer)
        if not (25 <= minutes_to_ko < 35):
            continue

        title = f"⏰ Anpfiff in {int(minutes_to_ko)} Min"
        body = f"{home} vs {away}\n{market.upper()} @ {odds:.2f} · €{stake:.0f}"
        ok = _send_notification(
            title=title,
            body=body,
            url="/sportsbrain/#bets",
            kind="reminder",
            tag=f"reminder-{home}-{away}-{market}",
            require=False,
        )
        if ok:
            sent_count += 1
            print(f"  Reminder sent: {home} vs {away} ({int(minutes_to_ko)} min)")
    if sent_count:
        print(f"Sent {sent_count} match-reminder push(es).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
