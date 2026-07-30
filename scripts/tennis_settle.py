"""Tennis Auto-Settlement (Roadmap TENNIS P1.1).

Iteriert offene Tennis-Bets im Ledger, holt Match-Scores (TheOddsAPI + ESPN),
settled ueber src.betting.tennis_settlement.settle_tennis_market().

Filter Tennis-Bets:
  - Market matched Tennis-Prefix (ah-1.5_, first_set_, o/u_sets_, etc.)
  - ODER match_id in aktiver Tennis-Discovery
  - ODER stake_reason contains 'tennis'

Usage:
  python3 scripts/tennis_settle.py            # settle
  python3 scripts/tennis_settle.py --dry-run  # show only
  python3 scripts/tennis_settle.py --user philip
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.tennis_settlement import (
    is_tennis_market,
    settle_tennis_market,
)
from src.config import DEFAULT_USER, ledger_path_for
from src.data.tennis_scores import fetch_tennis_scores, LAST_TENNIS_SCORES_SOURCE
from src.tennis.discovery import discover_active_tournaments


def _pnl(result: str, odds: float, stake: float) -> float:
    if result == "won":
        return round((odds - 1) * stake, 2)
    if result == "lost":
        return round(-stake, 2)
    return 0.0  # push


def _looks_tennis(bet: dict, tennis_match_ids: set[str]) -> bool:
    """True wenn Bet vermutlich Tennis ist."""
    market = bet.get("market", "")
    if is_tennis_market(market):
        return True
    if bet.get("match_id", "") in tennis_match_ids:
        return True
    reason = (bet.get("stake_reason") or "").lower()
    if "tennis" in reason:
        return True
    # source column may contain tennis hint
    src = (bet.get("source") or "").lower()
    if "tennis" in src:
        return True
    return False


def _settle_user_ledger(user: str, scores: dict, tennis_match_ids: set[str], dry_run: bool) -> int:
    """Returns count of settled bets for the given user."""
    ledger = ledger_path_for(user)
    if not ledger.exists():
        return 0
    rows = list(csv.DictReader(ledger.open()))
    if not rows:
        return 0

    open_bets = [r for r in rows if r.get("status", "").lower() == "open"]
    tennis_open = [r for r in open_bets if _looks_tennis(r, tennis_match_ids)]
    if not tennis_open:
        print(f"[{user}] Keine offenen Tennis-Bets")
        return 0

    print(f"[{user}] Offene Tennis-Bets: {len(tennis_open)}")
    settled_count = 0

    for r in tennis_open:
        mid = r.get("match_id", "")
        home = r.get("home", "")
        away = r.get("away", "")
        match_key = f"{home} vs {away}"

        sc = scores.get(mid) or scores.get(match_key)
        if not sc:
            continue

        market = r["market"]
        result = settle_tennis_market(market, sc)
        if result is None:
            print(f"  ⚠️  Unbekannter Tennis-Markt: {market} — skipping")
            continue
        if result == "pending":
            continue

        odds = float(r["decimal_odds"])
        stake = float(r["stake_amount"])
        pnl_val = _pnl(result, odds, stake)

        print(f"  {home} vs {away} | {market} @ {odds} | {result.upper()} → {pnl_val:+.2f} €")

        if not dry_run:
            r["status"] = result
            r["pnl"] = f"{pnl_val:.2f}"
        settled_count += 1

    if not dry_run and settled_count > 0:
        # Ledger komplett neu schreiben (open + settled)
        fieldnames = list(rows[0].keys())
        with ledger.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[{user}] {settled_count} Bets gesettled → Ledger aktualisiert")
    return settled_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Zeige nur was gesettled werden wuerde")
    ap.add_argument("--user", default=None, help="Nur diesen User settlen (Default: alle)")
    args = ap.parse_args()

    # Aktive Turniere ermitteln
    try:
        active = discover_active_tournaments()
        sport_keys = list({sk for t in active for sk in (t.sport_keys or [])})
    except Exception as e:
        print(f"[tennis_settle] Discovery failed: {e}")
        active = []
        sport_keys = []

    print(f"[tennis_settle] Aktive Tennis-Sport-Keys: {len(sport_keys)}")
    if sport_keys:
        print(f"                {', '.join(sport_keys[:5])}{'...' if len(sport_keys) > 5 else ''}")

    # Scores fetchen (Odds-API + ESPN Fallback)
    scores = fetch_tennis_scores(sport_keys)
    print(f"[tennis_settle] Scores geladen: {len(scores)} (Quelle: {LAST_TENNIS_SCORES_SOURCE})")

    # Tennis-Match-IDs sammeln fuer Ledger-Filter
    tennis_match_ids: set[str] = set()
    for k, v in scores.items():
        if isinstance(v, dict) and v.get("source") in ("odds_api", "espn"):
            tennis_match_ids.add(k)

    # Iteriere ueber alle User mit existierendem Ledger (Discovery via Filename)
    if args.user:
        users = [args.user]
    else:
        ledger_dir = ledger_path_for(DEFAULT_USER).parent
        users = sorted({
            p.stem.replace("ledger_", "")
            for p in ledger_dir.glob("ledger_*.csv")
            if "backfill" not in p.stem and "backup" not in p.stem and "bak" not in p.stem.split("_")
        })
        if not users:
            users = [DEFAULT_USER]
    total = 0
    for u in users:
        try:
            total += _settle_user_ledger(u, scores, tennis_match_ids, args.dry_run)
        except Exception as e:
            print(f"[{u}] Settle-Fehler: {e}")

    if args.dry_run:
        print(f"\n[DRY-RUN] Insgesamt {total} Bets waeren gesettled worden")
    else:
        print(f"\nInsgesamt {total} Bets gesettled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
