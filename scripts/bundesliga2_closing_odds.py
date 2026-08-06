"""Snapshot der Closing-Odds für 2.BL — 5 Min vor Kickoff.

Holt aktuelle 1X2-Quoten via TheOddsAPI für alle 2.BL-Matches die in den
nächsten 90 Minuten beginnen, schreibt sie in data/cache/bundesliga2_closing_odds.json.
Basis für CLV-Berechnung nach Settlement.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DATA_CACHE, canonical_name
from src.data.odds_api import fetch_upcoming_matches

SPORT_KEY = "soccer_germany_bundesliga2"
CACHE_PATH = DATA_CACHE / "bundesliga2_closing_odds.json"
WINDOW_MINUTES = 90


def main() -> None:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=WINDOW_MINUTES)
    print(f"[bl2_closing] Snapshot {now.isoformat()} — Fenster bis {cutoff.isoformat()}")

    try:
        matches = fetch_upcoming_matches(sport=SPORT_KEY, markets="h2h") or []
    except Exception as exc:
        print(f"  API-Fehler: {exc}")
        matches = []

    snapshots: dict[str, dict] = {}
    if CACHE_PATH.exists():
        try:
            snapshots = json.loads(CACHE_PATH.read_text())
        except Exception:
            snapshots = {}

    added = 0
    for m in matches:
        commence_raw = m.get("commence_time", "")
        if not commence_raw:
            continue
        try:
            commence = datetime.fromisoformat(commence_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if commence > cutoff:
            continue  # zu weit in Zukunft

        home = canonical_name(str(m.get("home_team", "")))
        away = canonical_name(str(m.get("away_team", "")))
        match_key = f"{home}_vs_{away}_{commence.strftime('%Y%m%d')}"

        if match_key in snapshots:
            continue  # bereits gesnapshottet

        # Konsens-Quote (Median)
        h_prices, d_prices, a_prices = [], [], []
        for bm in m.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for o in mkt.get("outcomes", []):
                    n, p = o.get("name", ""), float(o.get("price", 0))
                    if p <= 1.0:
                        continue
                    if canonical_name(n) == home:
                        h_prices.append(p)
                    elif canonical_name(n) == away:
                        a_prices.append(p)
                    elif "draw" in n.lower():
                        d_prices.append(p)

        import statistics as _st
        snapshots[match_key] = {
            "home": home, "away": away,
            "commence_time": commence_raw,
            "snapshot_at": now.isoformat(),
            "n_bookies": len(h_prices),
            "closing_home": round(_st.median(h_prices), 3) if h_prices else None,
            "closing_draw": round(_st.median(d_prices), 3) if d_prices else None,
            "closing_away": round(_st.median(a_prices), 3) if a_prices else None,
        }
        added += 1
        print(f"  Snapshot: {home} vs {away} | H={snapshots[match_key]['closing_home']} "
              f"D={snapshots[match_key]['closing_draw']} A={snapshots[match_key]['closing_away']}")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(snapshots, indent=2, ensure_ascii=False))
    print(f"[bl2_closing] {added} neue Snapshots | {len(snapshots)} total → {CACHE_PATH}")


if __name__ == "__main__":
    main()
