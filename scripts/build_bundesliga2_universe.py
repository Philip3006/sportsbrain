"""Phase B — B2: sammelt alle Teams die in den letzten 10 Saisons in 2.BL gespielt haben.

Erzeugt:
  data/cache/bundesliga2_universe.json  — {team: {seasons_played, first_season, last_season}}
  data/cache/bundesliga2_matches.pkl    — kombinierter Match-Stream (chronologisch)
  data/cache/bundesliga2_unmapped_teams.txt — Teams die canonical_name() nicht kennt (0 = OK)

Aufsteiger-/Absteiger-Rotation: 2.BL rotiert ~5 Teams pro Saison. In 10 Saisons
sammeln sich ~35-40 Vereine. Das Universum ist die Basis für Elo-Training über
alle historisch relevanten Teams (nicht nur aktuelle 18).
"""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from src.config import DATA_CACHE, canonical_name
from src.data.football_data import fetch_season

SEASONS = ["1617", "1718", "1819", "1920", "2021",
           "2122", "2223", "2324", "2425", "2526"]


def main() -> None:
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    universe: dict[str, dict] = {}
    unmapped: set[str] = set()
    frames: list[pd.DataFrame] = []

    for season in SEASONS:
        print(f"Fetching D2 season {season}...", flush=True)
        df = fetch_season("D2", season)
        if df is None or df.empty:
            print(f"  WARN: keine Daten für {season}")
            continue
        df = df.copy()
        df["season"] = season
        frames.append(df)
        for team_col in ("home_team", "away_team"):
            for raw in df[team_col].dropna().unique():
                canon = canonical_name(str(raw))
                if canon != raw and canon not in universe:
                    # Team wurde umbenannt → beide Formen erfassen für Debugging
                    unmapped.discard(canon)
                info = universe.setdefault(canon, {"seasons_played": [], "raw_names": set()})
                if season not in info["seasons_played"]:
                    info["seasons_played"].append(season)
                info["raw_names"].add(str(raw))
                # Mojibake-Detektor: nur wenn CSV kaputt dekodiert wurde (Ã-Sequenzen)
                if any(seq in str(raw) for seq in ("Ã", "�")):
                    unmapped.add(str(raw))

    # Persist matches
    matches_path = DATA_CACHE / "bundesliga2_matches.pkl"
    if frames:
        merged = pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)
        with open(matches_path, "wb") as f:
            pickle.dump(merged, f)
        print(f"Wrote {len(merged)} matches → {matches_path}")

    # Universe JSON (raw_names Set → List für JSON)
    universe_out = {
        team: {
            "seasons_played": sorted(info["seasons_played"]),
            "first_season": min(info["seasons_played"]) if info["seasons_played"] else None,
            "last_season": max(info["seasons_played"]) if info["seasons_played"] else None,
            "raw_names": sorted(info["raw_names"]),
        }
        for team, info in universe.items()
    }
    (DATA_CACHE / "bundesliga2_universe.json").write_text(
        json.dumps(universe_out, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Universe: {len(universe_out)} teams (erwartet ~35-40)")

    # Unmapped
    unmapped_path = DATA_CACHE / "bundesliga2_unmapped_teams.txt"
    if unmapped:
        unmapped_path.write_text("\n".join(sorted(unmapped)) + "\n", encoding="utf-8")
        print(f"WARN  {len(unmapped)} unmapped teams (siehe {unmapped_path}) — TEAM_NAME_MAP erweitern")
    else:
        unmapped_path.write_text("", encoding="utf-8")
        print("OK    keine unmapped teams")


if __name__ == "__main__":
    main()
