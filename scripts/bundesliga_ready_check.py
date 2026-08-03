"""N2: Bundesliga Warm-up readiness check. Run once before Spieltag 1 (2026-08-15)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.config import FOOTBALL_LEAGUES_WHITELIST, HOST_BOOST_ENABLED
from src.data.football_discovery import discover_leagues


def main() -> None:
    ok = True

    # 1. HOST_BOOST must be disabled for club football
    if HOST_BOOST_ENABLED:
        print("FAIL  HOST_BOOST_ENABLED=True — should be False for club football")
        ok = False
    else:
        print("OK    HOST_BOOST_ENABLED=False")

    # 2. Bundesliga keys in whitelist
    required = {"soccer_germany_bundesliga", "soccer_germany_2_bundesliga"}
    missing = required - FOOTBALL_LEAGUES_WHITELIST
    if missing:
        print(f"FAIL  Missing from whitelist: {missing}")
        ok = False
    else:
        print(f"OK    Bundesliga keys in FOOTBALL_LEAGUES_WHITELIST")

    # 3. Live odds available via TheOddsAPI
    try:
        leagues = discover_leagues()
        buli = [l for l in leagues if "bundesliga" in l.lower()]
        if buli:
            print(f"OK    Discovery finds: {buli}")
        else:
            print("WARN  No Bundesliga leagues returned by discovery (fixtures may not be live yet)")
    except Exception as exc:
        print(f"WARN  Discovery error (API may be down): {exc}")

    # 4. Elo data for Bundesliga teams
    elo_path = ROOT / "data" / "cache" / "elo_ratings.json"
    if elo_path.exists():
        elo = json.loads(elo_path.read_text())
        sample_teams = ["Bayern Munich", "Borussia Dortmund", "Bayer Leverkusen"]
        found = [t for t in sample_teams if t in elo]
        if found:
            print(f"OK    Elo data for: {found}")
        else:
            print(f"WARN  No Elo data for Bundesliga sample teams — run tennis_scan to trigger Elo refresh")
    else:
        print("WARN  elo_ratings.json not found — Elo not yet populated")

    print()
    print("GO" if ok else "NO-GO — fix FAIL items before Spieltag 1")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
