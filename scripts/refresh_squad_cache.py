"""
Force-refreshes Transfermarkt squad/injury data for all WM 2026 teams.
Run this the day before the tournament starts (2026-06-11) and again on match days.

Usage:
  python scripts/refresh_squad_cache.py              # all teams
  python scripts/refresh_squad_cache.py --team Germany France  # specific teams
"""
import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.squad_availability import _TM_TEAMS, fetch_transfermarkt_squad

_DELAY_BETWEEN_TEAMS = 2.5  # seconds — polite scraping


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--team", nargs="+", default=None,
        help="Specific teams to refresh (default: all WM 2026 teams)"
    )
    parser.add_argument(
        "--match-date", default="2026-06-11",
        help="Match date for injury recovery check (default: 2026-06-11)"
    )
    args = parser.parse_args()

    match_date = pd.Timestamp(args.match_date)
    teams = args.team if args.team else list(_TM_TEAMS.keys())

    print(f"Refreshing squad cache for {len(teams)} teams (match date: {match_date.date()})")
    print("This takes ~2-3 minutes due to polite rate limiting.\n")

    results = []
    for i, team in enumerate(teams, 1):
        if team not in _TM_TEAMS:
            print(f"  [{i:2d}/{len(teams)}] {team:20s} — not in team map, skipping")
            continue

        players = fetch_transfermarkt_squad(team, match_date, force=True)
        out = [p for p in players if p.status != "fit"]
        score = sum(p.availability for p in players) / len(players) if players else 1.0
        ampel = "🟢" if score >= 0.95 else ("🟡" if score >= 0.80 else "🔴")

        status_str = f"{len(players):3d} players | {len(out):2d} out {ampel}"
        print(f"  [{i:2d}/{len(teams)}] {team:20s} {status_str}")

        if out:
            for p in out:
                print(f"             ❌ {p.name} ({p.position})")

        results.append({"team": team, "players": len(players), "out": len(out), "score": score})

        if i < len(teams):
            time.sleep(_DELAY_BETWEEN_TEAMS)

    print(f"\nDone. {len([r for r in results if r['players'] > 0])}/{len(teams)} teams scraped successfully.")
    print("Cache valid for 24h — re-run on match days for latest injury updates.")

    # Summary of key absences
    key_absences = [(r["team"], r["out"]) for r in results if r["out"] > 0]
    if key_absences:
        print("\n=== Teams with absences ===")
        for team, n in sorted(key_absences, key=lambda x: -x[1]):
            print(f"  {team}: {n} player(s) out")


if __name__ == "__main__":
    main()
