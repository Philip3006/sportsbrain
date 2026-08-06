"""Phase B — B2b: identifiziert die aktuellen 18 Vereine der 2.BL-Saison.

Primär: pullt anstehende Fixtures via TheOddsAPI (`soccer_germany_bundesliga2`),
extrahiert Heim/Auswärts-Teams, kreuzt mit Universe.

Fallback: nutzt Teams der letzten Saison (2526) aus dem lokalen Match-Cache
wenn TheOddsAPI noch keine Fixtures für die neue Saison liefert (Off-Season-Fenster).

Erzeugt:
  data/cache/bundesliga2_current_teams.json — {"teams": [...], "source": "odds_api|last_season", "captured_at": iso}
"""
from __future__ import annotations

import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DATA_CACHE, canonical_name


def _from_odds_api() -> set[str]:
    try:
        from src.data.odds_api import fetch_upcoming_matches
        matches = fetch_upcoming_matches(
            sport="soccer_germany_bundesliga2",
            markets="h2h",
            force=True,
        )
    except Exception as exc:
        print(f"  odds_api fetch failed: {exc}")
        return set()
    teams: set[str] = set()
    for m in matches or []:
        teams.add(canonical_name(str(m.get("home_team", ""))))
        teams.add(canonical_name(str(m.get("away_team", ""))))
    teams.discard("")
    return teams


def _from_last_season() -> set[str]:
    path = DATA_CACHE / "bundesliga2_matches.pkl"
    if not path.exists():
        return set()
    with open(path, "rb") as f:
        df = pickle.load(f)
    last = df[df["season"] == df["season"].max()]
    teams: set[str] = set()
    for col in ("home_team", "away_team"):
        for t in last[col].dropna().unique():
            teams.add(canonical_name(str(t)))
    return teams


def main() -> None:
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    odds_teams = _from_odds_api()
    if len(odds_teams) >= 14:
        source = "odds_api"
        teams = odds_teams
    else:
        print(f"  odds_api lieferte nur {len(odds_teams)} Teams → Fallback letzte Saison")
        teams = _from_last_season()
        source = "last_season"

    universe_path = DATA_CACHE / "bundesliga2_universe.json"
    universe = set()
    if universe_path.exists():
        universe = set(json.loads(universe_path.read_text()).keys())
    unknown = sorted(teams - universe) if universe else []

    out = {
        "teams": sorted(teams),
        "n": len(teams),
        "source": source,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "unknown_vs_universe": unknown,
    }
    path = DATA_CACHE / "bundesliga2_current_teams.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"OK    {len(teams)} aktuelle 2.BL-Teams → {path}")
    if unknown:
        print(f"WARN  {len(unknown)} Teams NICHT im Universe: {unknown}")
        print("       → build_bundesliga2_universe.py erneut ausführen oder TEAM_NAME_MAP ergänzen")


if __name__ == "__main__":
    main()
