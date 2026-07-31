"""WM 2026 Post-Tournament Persistence-Snapshot (Roadmap I1).

Schreibt data/snapshots/wm2026_final.json mit:
- Meta (n_matches, date_range, generated_at)
- Alle Matches (date, home, away, home_score, away_score, neutral)
- Team-Aggregate (Spiele, W/D/L, GF/GA, GD, xGF/xGA falls verfügbar)
- Confederation-Summary (n_teams, avg_gd, avg_pts, best_team)

Basis für Bundesliga-Start (2026-08-15) — I3-Retrain nutzt diesen Snapshot
als kanonische WM-Datenquelle. Keine externen API-Calls; alle Daten aus
`data/cache/international_results.pkl` (bereits vom Scanner gepflegt).

Usage:
    python3 scripts/build_post_wm_snapshot.py
    python3 scripts/build_post_wm_snapshot.py --out data/snapshots/wm2026_final.json
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

_RESULTS_CACHE = ROOT / "data" / "cache" / "international_results.pkl"
_DEFAULT_OUT = ROOT / "data" / "snapshots" / "wm2026_final.json"

# Confederation-Zuordnung (kuratiert für WM2026-Teilnehmer).
# Nur für Summary-Aggregate; kein Impact auf Modelle.
_CONFED_MAP: dict[str, str] = {
    # UEFA
    "England": "UEFA", "France": "UEFA", "Spain": "UEFA", "Germany": "UEFA",
    "Italy": "UEFA", "Portugal": "UEFA", "Netherlands": "UEFA", "Belgium": "UEFA",
    "Switzerland": "UEFA", "Croatia": "UEFA", "Denmark": "UEFA", "Poland": "UEFA",
    "Serbia": "UEFA", "Austria": "UEFA", "Turkey": "UEFA", "Norway": "UEFA",
    "Ukraine": "UEFA", "Czechia": "UEFA", "Scotland": "UEFA", "Wales": "UEFA",
    "Bosnia and Herzegovina": "UEFA", "Slovakia": "UEFA",
    # CONMEBOL
    "Argentina": "CONMEBOL", "Brazil": "CONMEBOL", "Uruguay": "CONMEBOL",
    "Colombia": "CONMEBOL", "Ecuador": "CONMEBOL", "Paraguay": "CONMEBOL",
    "Chile": "CONMEBOL", "Bolivia": "CONMEBOL",
    # CONCACAF
    "United States": "CONCACAF", "Mexico": "CONCACAF", "Canada": "CONCACAF",
    "Costa Rica": "CONCACAF", "Panama": "CONCACAF", "Jamaica": "CONCACAF",
    "Haiti": "CONCACAF",
    # AFC
    "Japan": "AFC", "South Korea": "AFC", "Australia": "AFC", "Iran": "AFC",
    "Saudi Arabia": "AFC", "Qatar": "AFC", "Iraq": "AFC", "Uzbekistan": "AFC",
    "Jordan": "AFC",
    # CAF
    "Morocco": "CAF", "Egypt": "CAF", "Senegal": "CAF", "Tunisia": "CAF",
    "Algeria": "CAF", "Nigeria": "CAF", "Ghana": "CAF", "Cameroon": "CAF",
    "Ivory Coast": "CAF", "South Africa": "CAF",
    # OFC
    "New Zealand": "OFC",
}


def _load_wm_matches() -> list[dict]:
    if not _RESULTS_CACHE.exists():
        raise FileNotFoundError(f"international_results cache missing: {_RESULTS_CACHE}")
    df = pickle.load(_RESULTS_CACHE.open("rb"))
    wm = df[(df["tournament"] == "FIFA World Cup") & (df["date"] >= "2026-06-01")].copy()
    wm = wm.sort_values("date").reset_index(drop=True)

    matches: list[dict] = []
    for _, row in wm.iterrows():
        matches.append({
            "date": row["date"].strftime("%Y-%m-%d"),
            "home": row["home_team"],
            "away": row["away_team"],
            "home_score": int(row["home_score"]),
            "away_score": int(row["away_score"]),
            "neutral": bool(row["neutral"]),
        })
    return matches


def _team_aggregates(matches: list[dict]) -> dict[str, dict]:
    """Für jedes Team: Spiele, W/D/L, GF, GA, GD, Pts."""
    agg: dict[str, dict] = defaultdict(lambda: {
        "matches": 0, "wins": 0, "draws": 0, "losses": 0,
        "goals_for": 0, "goals_against": 0, "goal_diff": 0, "points": 0,
        "confederation": "",
    })

    for m in matches:
        h, a = m["home"], m["away"]
        hs, as_ = m["home_score"], m["away_score"]
        for team in (h, a):
            agg[team]["matches"] += 1
            agg[team]["confederation"] = _CONFED_MAP.get(team, "UNKNOWN")

        agg[h]["goals_for"] += hs
        agg[h]["goals_against"] += as_
        agg[a]["goals_for"] += as_
        agg[a]["goals_against"] += hs

        if hs > as_:
            agg[h]["wins"] += 1
            agg[h]["points"] += 3
            agg[a]["losses"] += 1
        elif hs < as_:
            agg[a]["wins"] += 1
            agg[a]["points"] += 3
            agg[h]["losses"] += 1
        else:
            agg[h]["draws"] += 1
            agg[a]["draws"] += 1
            agg[h]["points"] += 1
            agg[a]["points"] += 1

    for team, d in agg.items():
        d["goal_diff"] = d["goals_for"] - d["goals_against"]

    return dict(agg)


def _confederation_summary(team_agg: dict[str, dict]) -> dict[str, dict]:
    by_confed: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for team, d in team_agg.items():
        by_confed[d["confederation"]].append((team, d))

    summary: dict[str, dict] = {}
    for confed, entries in by_confed.items():
        n = len(entries)
        avg_gd = sum(d["goal_diff"] for _, d in entries) / n
        avg_pts = sum(d["points"] for _, d in entries) / n
        best_team, best_d = max(entries, key=lambda kv: (kv[1]["points"], kv[1]["goal_diff"]))
        summary[confed] = {
            "n_teams": n,
            "avg_goal_diff": round(avg_gd, 2),
            "avg_points": round(avg_pts, 2),
            "best_team": best_team,
            "best_team_points": best_d["points"],
        }
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(_DEFAULT_OUT), help="Ziel-Pfad für Snapshot")
    args = ap.parse_args()

    matches = _load_wm_matches()
    if not matches:
        print("Keine WM2026-Matches im Cache — beende.")
        return 1

    team_agg = _team_aggregates(matches)
    confed = _confederation_summary(team_agg)

    payload = {
        "meta": {
            "n_matches": len(matches),
            "n_teams": len(team_agg),
            "date_first": matches[0]["date"],
            "date_last": matches[-1]["date"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "data/cache/international_results.pkl",
        },
        "matches": matches,
        "team_aggregates": team_agg,
        "confederation_summary": confed,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Snapshot geschrieben: {out}")
    print(f"  {len(matches)} Matches, {len(team_agg)} Teams, {len(confed)} Konföderationen")
    top_teams = sorted(team_agg.items(),
                       key=lambda kv: (kv[1]["points"], kv[1]["goal_diff"]), reverse=True)[:5]
    print("  Top-5 Teams (Punkte):")
    for team, d in top_teams:
        print(f"    {team:30s} {d['matches']}Sp {d['points']:2d}P {d['goal_diff']:+d}GD ({d['confederation']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
