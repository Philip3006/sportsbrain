"""2.BL Auto-Settlement via ESPN — analog zu tennis_settle.py.

Holt abgeschlossene BL2-Matches von ESPN (da football-data.co.uk am
Saisonstart oft 24-48h verzögert ist) und settled offene BL2-Bets im Ledger.

Filter BL2-Bets:
  - league == 'bl2'  (primär)
  - ODER market aus BL2-typischen Märkten (home/away/draw/o/u…/btts_yes/ah…)
    UND Heimteam-Name in BL2-Teamliste

Usage:
  python3 scripts/bundesliga2_settle.py             # settle
  python3 scripts/bundesliga2_settle.py --dry-run   # nur anzeigen
  python3 scripts/bundesliga2_settle.py --user philip
  python3 scripts/bundesliga2_settle.py --days 10   # ESPN-Fenster in Tagen (default 7)
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scripts.settle_bets import settle_market
from src.config import DEFAULT_USER, ledger_path_for
from src.football.backfill_helpers import (
    BL2_2627_TEAMS,
    fetch_bl2_window,
    espn_to_signal_name,
)

LEAGUE_SHORT = "bl2"
BL2_MARKETS = {
    "home", "away", "draw",
    "btts_yes", "btts_no",
    "o/u1.5_over", "o/u1.5_under",
    "o/u2.5_over", "o/u2.5_under",
    "o/u3.5_over", "o/u3.5_under",
    "ah+0.5_away", "ah-0.5_home",
    "ah+0.5_home", "ah-0.5_away",
}


def _pnl(result: str, odds: float, stake: float) -> float:
    if result == "won":
        return round((odds - 1) * stake, 2)
    if result == "lost":
        return round(-stake, 2)
    return 0.0


def _looks_bl2(bet: dict) -> bool:
    if bet.get("league", "") == LEAGUE_SHORT:
        return True
    home = espn_to_signal_name(bet.get("home", ""))
    if home in BL2_2627_TEAMS and bet.get("market", "") in BL2_MARKETS:
        return True
    return False


def _settle_user_ledger(
    user: str,
    scores: dict,
    dry_run: bool,
    no_push: bool = False,
) -> int:
    ledger = ledger_path_for(user)
    if not ledger.exists():
        return 0
    rows = list(csv.DictReader(ledger.open()))
    if not rows:
        return 0

    open_bets = [r for r in rows if r.get("status", "").lower() == "open"]
    bl2_open = [r for r in open_bets if _looks_bl2(r)]
    if not bl2_open:
        print(f"[{user}] Keine offenen BL2-Bets")
        return 0

    print(f"[{user}] Offene BL2-Bets: {len(bl2_open)}")
    settled_count = 0

    for r in bl2_open:
        home = r.get("home", "")
        away = r.get("away", "")
        match_key = f"{home} vs {away}"
        sc = scores.get(match_key) or scores.get(f"{away} vs {home}")
        if not sc:
            continue

        market = r["market"]
        result = settle_market(market, sc["home_score"], sc["away_score"])
        if result is None:
            print(f"  ⚠️  Unbekannter Markt: {market} — skip")
            continue

        odds = float(r["decimal_odds"])
        stake = float(r["stake_amount"])
        pnl_val = _pnl(result, odds, stake)

        clv_str = ""
        try:
            closing = float(r.get("closing_odds") or 0)
            if 1.0 < closing < odds * 3.0:
                clv = max(-0.99, min(2.00, odds / closing - 1.0))
                clv_str = f"{clv:.4f}"
        except (ValueError, TypeError):
            pass

        clv_info = f" | CLV {clv_str}" if clv_str else ""
        print(
            f"  {home} vs {away} | {market} @ {odds} "
            f"| {result.upper()} → {pnl_val:+.2f} €{clv_info}"
        )

        if not dry_run:
            r["status"] = result
            r["pnl"] = f"{pnl_val:.2f}"
            if clv_str:
                r["clv"] = clv_str
        settled_count += 1

    if not dry_run and settled_count > 0:
        fieldnames = list(rows[0].keys())
        with ledger.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[{user}] {settled_count} Bets gesettled → Ledger aktualisiert")

        import os as _os
        if no_push:
            print(f"[{user}] --no-push aktiv, {settled_count} settlements ohne Notification")
        elif not _os.getenv("PYTEST_CURRENT_TEST"):
            try:
                from src.betting.ledger import ledger_summary
                from src.notifications.web_push import send_settlement_alert
                summary = ledger_summary(ledger)
                for r in bl2_open:
                    if r.get("status", "").lower() in ("won", "lost", "push"):
                        try:
                            send_settlement_alert(r, summary)
                        except Exception as e:
                            print(f"  [push] {r.get('home')}: {e}")
            except Exception as e:
                print(f"[{user}] Push-Init failed: {e}")

    return settled_count


def main() -> int:
    ap = argparse.ArgumentParser(description="2.BL Settlement via ESPN")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--user", default=None)
    ap.add_argument("--days", type=int, default=7, help="ESPN-Fenster in Tagen (default 7)")
    ap.add_argument("--no-push", action="store_true", dest="no_push")
    args = ap.parse_args()

    # ESPN-Fenster: heute − args.days bis heute
    today_iso = date.today().isoformat()
    scan_dates = [today_iso]
    scores, n_dates = fetch_bl2_window(scan_dates, {}, window_days=args.days)
    bl2_count = sum(1 for v in scores.values() if isinstance(v, dict) and v.get("source") == "espn_bl2")
    print(f"[bl2_settle] ESPN: {bl2_count // 2} abgeschlossene Matches ({n_dates} Tage geladen)")

    if args.user:
        users = [args.user]
    else:
        ledger_dir = ledger_path_for(DEFAULT_USER).parent
        users = sorted({
            p.stem.replace("ledger_", "")
            for p in ledger_dir.glob("ledger_*.csv")
            if "backfill" not in p.stem and "backup" not in p.stem and "bak" not in p.stem.split("_")
        }) or [DEFAULT_USER]

    total = 0
    for u in users:
        try:
            total += _settle_user_ledger(u, scores, args.dry_run, no_push=args.no_push)
        except Exception as e:
            print(f"[{u}] Settle-Fehler: {e}")

    if args.dry_run:
        print(f"\n[DRY-RUN] {total} Bets wären gesettled worden")
    else:
        print(f"\nGesamt: {total} BL2-Bets gesettled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
