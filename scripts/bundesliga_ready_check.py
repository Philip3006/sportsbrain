"""N2: Bundesliga Warm-up readiness check. Run once before Spieltag 1 (2026-08-15)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.config import FOOTBALL_LEAGUES_WHITELIST, HOST_BOOST_ENABLED
from src.data.football_discovery import discover_active_leagues


def main() -> None:
    ok = True

    # 1. HOST_BOOST must be disabled for club football
    if HOST_BOOST_ENABLED:
        print("FAIL  HOST_BOOST_ENABLED=True — should be False for club football")
        ok = False
    else:
        print("OK    HOST_BOOST_ENABLED=False")

    # 2. Bundesliga keys in whitelist
    required = {"soccer_germany_bundesliga", "soccer_germany_bundesliga2"}
    missing = required - FOOTBALL_LEAGUES_WHITELIST
    if missing:
        print(f"FAIL  Missing from whitelist: {missing}")
        ok = False
    else:
        print(f"OK    Bundesliga keys in FOOTBALL_LEAGUES_WHITELIST")

    # 3. Live odds available via TheOddsAPI
    try:
        leagues = discover_active_leagues()
        buli = [l for l in leagues if "bundesliga" in l.lower()]
        if buli:
            print(f"OK    Discovery finds: {buli}")
        else:
            print("WARN  No Bundesliga leagues returned by discovery (fixtures may not be live yet)")
    except Exception as exc:
        print(f"WARN  Discovery error (API may be down): {exc}")

    # 4. Elo data for Bundesliga teams (1.BL + 2.BL)
    elo_path = ROOT / "data" / "cache" / "elo_ratings.json"
    if elo_path.exists():
        elo = json.loads(elo_path.read_text())
        bl1_sample = ["Bayern Munich", "Borussia Dortmund", "Bayer Leverkusen"]
        bl2_sample = ["Hamburg", "Schalke 04", "St Pauli", "Hertha"]
        bl1_found = [t for t in bl1_sample if t in elo]
        bl2_found = [t for t in bl2_sample if t in elo]
        if bl1_found:
            print(f"OK    1.BL Elo: {bl1_found}")
        else:
            print("WARN  Keine 1.BL-Elos (run scripts/train_elo_bundesliga.py wenn Bundesliga geplant)")
        if bl2_found:
            print(f"OK    2.BL Elo: {bl2_found}")
        else:
            print("WARN  Keine 2.BL-Elos — run scripts/train_elo_bundesliga2.py")
    else:
        print("WARN  elo_ratings.json fehlt")

    print()
    print("GO" if ok else "NO-GO — fix FAIL items before Spieltag 1")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
