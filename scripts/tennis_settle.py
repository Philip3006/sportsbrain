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
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.tennis_settlement import (
    is_tennis_market,
    settle_tennis_market,
)
from src.config import DEFAULT_USER, ledger_path_for
from src.data.tennis_scores import (
    LAST_TENNIS_SCORES_SOURCE,
    canonical_match_key,
    fetch_tennis_scores,
)
from src.tennis.discovery import discover_active_tournaments


def _write_suspended_cache(scores: dict) -> None:
    """Schreibt suspended/postponed Matches nach data/cache/tennis_suspended.json."""
    seen: set[str] = set()
    matches = []
    for v in scores.values():
        if not isinstance(v, dict) or v.get("status") not in ("suspended", "postponed"):
            continue
        ck = canonical_match_key(v.get("player_a", ""), v.get("player_b", ""))
        if ck in seen:
            continue
        seen.add(ck)
        matches.append({
            "match_key": ck,
            "home": v.get("player_a", ""),
            "away": v.get("player_b", ""),
            "status": v.get("status"),
            "sets": v.get("sets", []),
            "resume_time": v.get("resume_time"),
        })
    cache_path = ROOT / "data" / "cache" / "tennis_suspended.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "matches": matches,
    }, indent=2))
    if matches:
        print(f"[tennis_settle] {len(matches)} unterbrochene Matches gecacht: "
              + ", ".join(f"{m['home']} vs {m['away']} ({m['status']})" for m in matches))


def _pnl(result: str, odds: float, stake: float) -> float:
    if result == "won":
        return round((odds - 1) * stake, 2)
    if result == "lost":
        return round(-stake, 2)
    return 0.0  # push


def _looks_tennis(bet: dict, tennis_match_ids: set[str], scores: dict | None = None) -> bool:
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
    # Fallback: match appears in tennis scores dict (covers home/away bets on tennis matches)
    if scores:
        home, away = bet.get("home", ""), bet.get("away", "")
        match_key = f"{home} vs {away}"
        if match_key in scores:
            return True
    return False


def _settle_user_ledger(user: str, scores: dict, tennis_match_ids: set[str], dry_run: bool, no_push: bool = False) -> int:
    """Returns count of settled bets for the given user."""
    ledger = ledger_path_for(user)
    if not ledger.exists():
        return 0
    rows = list(csv.DictReader(ledger.open()))
    if not rows:
        return 0

    open_bets = [r for r in rows if r.get("status", "").lower() == "open"]
    tennis_open = [r for r in open_bets if _looks_tennis(r, tennis_match_ids, scores)]
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

        # J8-B3: canonical-Fallback verhindert Whitespace/Unicode/Cap-Drift zwischen
        # Score-Fetcher-Namen und Ledger-Namen ('Bosnia-Analog' für Tennis).
        canon_key = canonical_match_key(home, away)
        sc = scores.get(mid) or scores.get(match_key) or scores.get(canon_key)
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

        # CLV berechnen wenn closing_odds vorhanden (P1.2)
        clv_str = ""
        try:
            closing = float(r.get("closing_odds") or 0)
            if 1.0 < closing < odds * 3.0:
                clv = max(-0.99, min(2.00, odds / closing - 1.0))
                clv_str = f"{clv:.4f}"
        except (ValueError, TypeError):
            pass

        clv_info = f" | CLV {clv_str}" if clv_str else ""
        print(f"  {home} vs {away} | {market} @ {odds} | {result.upper()} → {pnl_val:+.2f} €{clv_info}")

        if not dry_run:
            r["status"] = result
            r["pnl"] = f"{pnl_val:.2f}"
            if clv_str:
                r["clv"] = clv_str
        settled_count += 1

    if not dry_run and settled_count > 0:
        # Ledger komplett neu schreiben (open + settled)
        fieldnames = list(rows[0].keys())
        with ledger.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[{user}] {settled_count} Bets gesettled → Ledger aktualisiert")

        # P1.4 Post-Match-Push: sende Notification fuer jeden neu-gesettelten Bet
        import os as _os
        if no_push:
            print(f"[{user}] --no-push aktiv, {settled_count} settlements ohne Notification")
        elif not _os.getenv("PYTEST_CURRENT_TEST"):
            try:
                from src.betting.ledger import ledger_summary
                from src.notifications.web_push import send_settlement_alert
                summary = ledger_summary(ledger)
                for r in tennis_open:
                    if r.get("status", "").lower() in ("won", "lost", "push"):
                        try:
                            send_settlement_alert(r, summary)
                        except Exception as e:
                            print(f"  [push] {r.get('home')}: {e}")
            except Exception as e:
                print(f"[{user}] Push-Init failed: {e}")
    return settled_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Zeige nur was gesettled werden wuerde")
    ap.add_argument("--user", default=None, help="Nur diesen User settlen (Default: alle)")
    ap.add_argument("--no-push", action="store_true", dest="no_push",
                    help="Skip Post-Match-Push (nützlich für Batch-Settlement, verhindert Push-Spam)")
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
            total += _settle_user_ledger(u, scores, tennis_match_ids, args.dry_run, no_push=args.no_push)
        except Exception as e:
            print(f"[{u}] Settle-Fehler: {e}")

    if args.dry_run:
        print(f"\n[DRY-RUN] Insgesamt {total} Bets waeren gesettled worden")
    else:
        print(f"\nInsgesamt {total} Bets gesettled")

    # Suspended/postponed Matches in Cache schreiben (fuer Dashboard-Anreicherung)
    _write_suspended_cache(scores)
    return 0


if __name__ == "__main__":
    sys.exit(main())
