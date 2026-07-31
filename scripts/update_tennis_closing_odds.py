"""Update Closing-Odds fuer offene Tennis-Bets (Roadmap TENNIS P1.2).

Laeuft alle 30 Min via Cron. Fuer jeden offenen Tennis-Bet:
  1. Match-Startzeit ermitteln (aus TheOddsAPI /events oder Ledger-match_date)
  2. Wenn Start in <= 45 Min: aktuelle Marktquote fetchen
  3. Als closing_odds in Ledger schreiben

CLV wird spaeter in scripts/tennis_settle.py als odds/closing_odds-1 berechnet.

Usage:
  python3 scripts/update_tennis_closing_odds.py
  python3 scripts/update_tennis_closing_odds.py --dry-run
  python3 scripts/update_tennis_closing_odds.py --user philip --window-min 45
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.tennis_settlement import is_tennis_market
from src.config import DEFAULT_USER, ledger_path_for
from src.tennis.discovery import discover_active_tournaments


def _api_key() -> str:
    key = os.getenv("ODDS_API_KEY", "")
    if not key:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if "ODDS_API_KEY" in line:
                    key = line.split("=", 1)[1].strip().strip('"')
                    break
    return key


# ---------- Odds-Fetching -------------------------------------------------- #

def _fetch_events_and_odds(sport_key: str, api_key: str) -> list[dict]:
    """Fetch /odds fuer einen Sport-Key. Returns event-list."""
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": api_key,
        "regions": "eu,us,uk",
        "markets": "h2h",  # Match Winner ist immer verfuegbar; Set-Markets separat
        "oddsFormat": "decimal",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return []
        return r.json()
    except Exception as e:
        print(f"[closing_odds] {sport_key}: {e}")
        return []


def _extract_h2h(event: dict) -> tuple[float, float] | None:
    """Extrahiere (odds_a, odds_b) aus event.bookmakers[].markets[]."""
    home = event.get("home_team")
    away = event.get("away_team")
    if not home or not away:
        return None
    a_prices: list[float] = []
    b_prices: list[float] = []
    for bm in event.get("bookmakers", []):
        for mk in bm.get("markets", []):
            if mk.get("key") != "h2h":
                continue
            for outcome in mk.get("outcomes", []):
                name = outcome.get("name")
                price = outcome.get("price")
                if price is None:
                    continue
                if name == home:
                    a_prices.append(float(price))
                elif name == away:
                    b_prices.append(float(price))
    if not a_prices or not b_prices:
        return None
    # Best available price (Consumer perspective)
    return max(a_prices), max(b_prices)


# ---------- Ledger-Update -------------------------------------------------- #

def _looks_tennis(bet: dict) -> bool:
    if is_tennis_market(bet.get("market", "")):
        return True
    src = (bet.get("source") or "").lower()
    reason = (bet.get("stake_reason") or "").lower()
    if "tennis" in src or "tennis" in reason:
        return True
    return False


def _match_key(home: str, away: str) -> str:
    return f"{home.lower().strip()} vs {away.lower().strip()}"


def _parse_kickoff(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _closing_for_bet(bet: dict, odds_map: dict[str, tuple[float, float, datetime | None]]) -> float | None:
    """Given odds_map keyed by 'a vs b' → (odds_a, odds_b, commence),
    return the closing odds relevant to this bet's market.

    P1.2 supports h2h (home/away) only. Set/AH/O_U closing odds require
    more market data — P3 refinement.
    """
    market = bet.get("market", "")
    home, away = bet.get("home", ""), bet.get("away", "")
    key = _match_key(home, away)
    entry = odds_map.get(key)
    if not entry:
        return None
    odds_a, odds_b, _ = entry
    if market == "home":
        return odds_a
    if market == "away":
        return odds_b
    # first_set_a/b: approximiere mit Match-Odds - Set-Betting hat eigene Preise,
    # aber h2h ist besserer Proxy als 'kein CLV'. Fuer P3 richtig implementieren.
    if market in ("first_set_a", "first_set_b"):
        return None  # skip fuer P1.2 - schuetzt vor Fake-CLV
    # AH und O/U und score_*: unklar aus h2h, skip.
    return None


def _update_user_ledger(user: str, odds_map: dict, window_min: int, dry_run: bool) -> int:
    ledger = ledger_path_for(user)
    if not ledger.exists():
        return 0
    rows = list(csv.DictReader(ledger.open()))
    if not rows:
        return 0

    open_tennis = [
        r for r in rows
        if r.get("status", "").lower() == "open" and _looks_tennis(r)
    ]
    if not open_tennis:
        print(f"[{user}] Keine offenen Tennis-Bets fuer Closing-Odds")
        return 0

    now = datetime.now(timezone.utc)
    updated = 0
    for r in open_tennis:
        home, away = r.get("home", ""), r.get("away", "")
        key = _match_key(home, away)
        entry = odds_map.get(key)
        if not entry:
            continue
        _, _, kickoff = entry
        # Nur nahe am Start (Match Winner-Odds werden bis zum Kickoff volatil)
        if kickoff:
            mins_to_start = (kickoff - now).total_seconds() / 60.0
            if mins_to_start > window_min or mins_to_start < -60:
                continue

        closing = _closing_for_bet(r, odds_map)
        if closing is None or closing <= 1.0:
            continue

        old = r.get("closing_odds", "").strip()
        if old and float(old) > 1.0:
            # Bereits gesetzt (koennte T-30 vs T-15 sein - immer neueste Zahl behalten)
            pass

        print(f"  [{user}] {home} vs {away} | {r['market']} @ {r['decimal_odds']} → closing {closing:.2f}")
        if not dry_run:
            r["closing_odds"] = f"{closing:.4f}"
        updated += 1

    if not dry_run and updated:
        fieldnames = list(rows[0].keys())
        with ledger.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[{user}] {updated} closing_odds aktualisiert")
    return updated


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--user", default=None)
    ap.add_argument("--window-min", type=int, default=45,
                    help="Nur Bets updaten deren Match in <=N Minuten startet (Default 45)")
    args = ap.parse_args()

    key = _api_key()
    if not key:
        print("[closing_odds] Kein ODDS_API_KEY - Abbruch")
        return 1

    active = discover_active_tournaments()
    sport_keys = list({sk for t in active for sk in (t.sport_keys or [])})
    print(f"[closing_odds] {len(sport_keys)} aktive Tennis-Sport-Keys")

    odds_map: dict[str, tuple[float, float, datetime | None]] = {}
    for sk in sport_keys:
        events = _fetch_events_and_odds(sk, key)
        for ev in events:
            h2h = _extract_h2h(ev)
            if h2h is None:
                continue
            home, away = ev.get("home_team", ""), ev.get("away_team", "")
            commence = _parse_kickoff(ev.get("commence_time", ""))
            odds_map[_match_key(home, away)] = (h2h[0], h2h[1], commence)
        time.sleep(0.25)
    print(f"[closing_odds] Odds geladen fuer {len(odds_map)} Matches")

    if args.user:
        users = [args.user]
    else:
        ledger_dir = ledger_path_for(DEFAULT_USER).parent
        users = sorted({
            p.stem.replace("ledger_", "")
            for p in ledger_dir.glob("ledger_*.csv")
            if "backfill" not in p.stem and "backup" not in p.stem
        }) or [DEFAULT_USER]

    total = 0
    for u in users:
        try:
            total += _update_user_ledger(u, odds_map, args.window_min, args.dry_run)
        except Exception as e:
            print(f"[{u}] Update-Fehler: {e}")

    print(f"\n{'[DRY-RUN] ' if args.dry_run else ''}Insgesamt {total} closing_odds updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
