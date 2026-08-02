#!/usr/bin/env python3
"""Snapshot aktueller Tennis-Odds für alle laufenden Turniere (Hebel 4).

Cron-Setup (empfohlen):
  # T-24h vor typischen Match-Startzeiten
  0 */6 * * * python3 scripts/tennis_odds_snapshot.py --label auto

Alle laufenden Matches aus Discovery holen, Odds via Merger-Chain, in
`data/odds_history/tennis/` speichern. Timestamps stapeln sich pro match_id.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.tennis.discovery import discover_active_tournaments
from src.tennis.odds_snapshot import save_snapshot


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="auto", help="Snapshot-Label z.B. 'T-24h', 'T-0'")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"[snapshot] label={args.label}")
    try:
        tournaments = discover_active_tournaments()
    except Exception as e:
        print(f"[snapshot] Discovery-Fehler: {e}", file=sys.stderr)
        return 1

    n_saved = 0
    for t in tournaments:
        # Odds via TheOddsAPI (bekannt aus tennis_scan) — hier vereinfachte Route.
        # Vollständige Merger-Chain ist im Scanner; für Snapshot reicht Primärquelle.
        try:
            import os
            api_key = os.environ.get("ODDS_API_KEY")
            if not api_key:
                print("[snapshot] ODDS_API_KEY fehlt — skip")
                return 1
            sport_key = t.sport_keys[0] if t.sport_keys else None
            if not sport_key:
                continue
            import requests
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
            r = requests.get(url, params={
                "apiKey": api_key, "regions": "eu", "markets": "h2h",
                "oddsFormat": "decimal",
            }, timeout=10)
            if not r.ok:
                continue
            events = r.json()
        except Exception as e:
            print(f"[snapshot] {t.slug}: fetch error {e}")
            continue

        for ev in events:
            book = (ev.get("bookmakers") or [{}])[0]
            h2h = next((m for m in book.get("markets", []) if m.get("key") == "h2h"), None)
            if not h2h or len(h2h.get("outcomes", [])) < 2:
                continue
            oc = h2h["outcomes"]
            match_id = ev.get("id", "")
            if not match_id:
                continue
            if args.dry_run:
                print(f"  DRY {match_id}: {oc[0]['price']} / {oc[1]['price']}")
                continue
            save_snapshot(
                match_id,
                odds_a=oc[0]["price"], odds_b=oc[1]["price"],
                source=book.get("key", "unknown"),
                label=args.label,
                tournament_slug=t.slug,
                player_a=oc[0]["name"], player_b=oc[1]["name"],
            )
            n_saved += 1

    print(f"[snapshot] {n_saved} Match-Snapshots geschrieben.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
