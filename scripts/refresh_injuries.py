"""
Daily injury & suspension refresh for WM 2026.
Searches DuckDuckGo News for each team, extracts player names mentioned
alongside injury keywords, merges into data/suspensions.json and
docs/data/squads.json.

Usage:
  python3 scripts/refresh_injuries.py           # full refresh, all 48 teams
  python3 scripts/refresh_injuries.py --teams "Germany" "Spain"  # specific teams
  python3 scripts/refresh_injuries.py --dry-run  # show findings, don't write
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

SUSPENSIONS_FILE = ROOT / "data" / "suspensions.json"
SQUADS_FILE = ROOT / "docs" / "data" / "squads.json"

# All 48 WM teams + search query hints
WM_TEAMS: list[tuple[str, str]] = [
    ("Czech Republic",         "Czech Republic OR Czechia injury World Cup 2026"),
    ("Mexico",                 "Mexico injury World Cup 2026"),
    ("South Africa",           "South Africa injury World Cup 2026"),
    ("South Korea",            "South Korea injury World Cup 2026"),
    ("Bosnia and Herzegovina", "Bosnia Herzegovina injury World Cup 2026"),
    ("Canada",                 "Canada injury World Cup 2026"),
    ("Qatar",                  "Qatar injury World Cup 2026"),
    ("Switzerland",            "Switzerland injury World Cup 2026"),
    ("Brazil",                 "Brazil injury World Cup 2026"),
    ("Haiti",                  "Haiti injury World Cup 2026"),
    ("Morocco",                "Morocco injury World Cup 2026"),
    ("Scotland",               "Scotland injury World Cup 2026"),
    ("Australia",              "Australia injury World Cup 2026"),
    ("Paraguay",               "Paraguay injury World Cup 2026"),
    ("Turkey",                 "Turkey injury World Cup 2026"),
    ("United States",          "USMNT injury World Cup 2026"),
    ("Curacao",                "Curacao injury World Cup 2026"),
    ("Ecuador",                "Ecuador injury World Cup 2026"),
    ("Germany",                "Germany injury World Cup 2026"),
    ("Ivory Coast",            "Ivory Coast Cote d'Ivoire injury World Cup 2026"),
    ("Japan",                  "Japan injury World Cup 2026"),
    ("Netherlands",            "Netherlands injury World Cup 2026"),
    ("Sweden",                 "Sweden injury World Cup 2026"),
    ("Tunisia",                "Tunisia injury World Cup 2026"),
    ("Belgium",                "Belgium injury World Cup 2026"),
    ("Egypt",                  "Egypt injury World Cup 2026"),
    ("Iran",                   "Iran injury World Cup 2026"),
    ("New Zealand",            "New Zealand injury World Cup 2026"),
    ("Cape Verde",             "Cape Verde injury World Cup 2026"),
    ("Saudi Arabia",           "Saudi Arabia injury World Cup 2026"),
    ("Spain",                  "Spain injury World Cup 2026"),
    ("Uruguay",                "Uruguay injury World Cup 2026"),
    ("France",                 "France injury World Cup 2026"),
    ("Iraq",                   "Iraq injury World Cup 2026"),
    ("Norway",                 "Norway injury World Cup 2026"),
    ("Senegal",                "Senegal injury World Cup 2026"),
    ("Algeria",                "Algeria injury World Cup 2026"),
    ("Argentina",              "Argentina injury World Cup 2026"),
    ("Austria",                "Austria injury World Cup 2026"),
    ("Jordan",                 "Jordan injury World Cup 2026"),
    ("Colombia",               "Colombia injury World Cup 2026"),
    ("DR Congo",               "DR Congo injury World Cup 2026"),
    ("Portugal",               "Portugal injury World Cup 2026"),
    ("Uzbekistan",             "Uzbekistan injury World Cup 2026"),
    ("Croatia",                "Croatia injury World Cup 2026"),
    ("England",                "England injury World Cup 2026"),
    ("Ghana",                  "Ghana injury World Cup 2026"),
    ("Panama",                 "Panama injury World Cup 2026"),
]

_INJURY_KEYWORDS = re.compile(
    r'\b(injur|ruled out|miss|out for|absent|withdrawn|unavailabl|doubtful|'
    r'doubt|hamstring|achilles|acl|torn|fractur|sprain|suspended|suspension)\b',
    re.I,
)

_FIT_KEYWORDS = re.compile(
    r'\b(fit|recovered|returns?|available|cleared|back in|back to training|'
    r'ready|no longer|fully fit|named in squad|included)\b',
    re.I,
)

# Known players per team (loaded from squads.json for name matching)
_squad_players: dict[str, list[str]] = {}


def _load_squad_players() -> None:
    if not SQUADS_FILE.exists():
        return
    try:
        data = json.loads(SQUADS_FILE.read_text())
        for team, d in data.get("teams", {}).items():
            _squad_players[team] = [p["name"] for p in d.get("players", [])]
    except Exception:
        pass


def _find_mentioned_players(text: str, team: str) -> tuple[list[str], list[str]]:
    """
    Returns (injured_players, fit_players) based on proximity to keywords.
    A player is 'fit' if mentioned near fit-keywords but NOT near injury-keywords.
    """
    players = _squad_players.get(team, [])
    injured, fit = [], []
    for name in players:
        near_injury = False
        near_fit = False
        for m in re.finditer(re.escape(name), text, re.I):
            start = max(0, m.start() - 150)
            end = min(len(text), m.end() + 150)
            context = text[start:end]
            if _INJURY_KEYWORDS.search(context):
                near_injury = True
            if _FIT_KEYWORDS.search(context):
                near_fit = True
        if near_injury and name not in injured:
            injured.append(name)
        elif near_fit and not near_injury and name not in fit:
            fit.append(name)
    return injured, fit


def search_team_injuries(
    team: str, query: str, n: int = 6
) -> tuple[list[str], list[str], list[str]]:
    """
    Searches DDG news for injury/fitness news about team.
    Returns (injured_players, fit_players, raw_headlines).
    """
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            print("  [injury] ddgs not installed — run: pip3 install ddgs")
            return [], [], []

    headlines = []
    combined_text = ""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.news(query, max_results=n, timelimit="w"))
        for r in results:
            title = r.get("title", "")
            body = r.get("body", "")
            headlines.append(title)
            combined_text += f" {title} {body}"
    except Exception as e:
        print(f"  [injury] DDG search failed for {team}: {e}")
        return [], [], []

    injured, fit = _find_mentioned_players(combined_text, team)
    return injured, fit, headlines


def _load_suspensions() -> dict:
    if not SUSPENSIONS_FILE.exists():
        return {}
    try:
        return json.loads(SUSPENSIONS_FILE.read_text())
    except Exception:
        return {}


def _save_suspensions(data: dict) -> None:
    SUSPENSIONS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _update_squads_json(suspensions: dict) -> int:
    if not SQUADS_FILE.exists():
        return 0
    try:
        squads = json.loads(SQUADS_FILE.read_text())
    except Exception:
        return 0

    updated = 0
    for team, inj_list in suspensions.items():
        if team.startswith("_") or team not in squads.get("teams", {}):
            continue
        squads["teams"][team]["suspended"] = inj_list
        for p in squads["teams"][team].get("players", []):
            was_injured = p.get("status") == "injured"
            is_injured = any(
                inj.lower() in p["name"].lower() or p["name"].lower() in inj.lower()
                for inj in inj_list
            )
            if is_injured and not was_injured:
                p["status"] = "injured"
                updated += 1
            elif not is_injured and was_injured:
                p["status"] = "fit"

    from datetime import date
    squads["updated"] = str(date.today())
    SQUADS_FILE.write_text(json.dumps(squads, ensure_ascii=False, indent=2))
    return updated


def run(teams_filter: list[str] | None = None, dry_run: bool = False) -> None:
    _load_squad_players()
    suspensions = _load_suspensions()

    teams = WM_TEAMS
    if teams_filter:
        teams = [(t, q) for t, q in WM_TEAMS if t in teams_filter]

    total_new = 0
    total_removed = 0
    for i, (team, query) in enumerate(teams):
        print(f"  [{i+1:2d}/{len(teams)}] {team:<30s}", end="", flush=True)
        injured, fit, _ = search_team_injuries(team, query)

        existing = [p for p in suspensions.get(team, []) if not p.startswith("_")]
        new_players = [p for p in injured if p not in existing]
        # Remove players explicitly mentioned as fit/recovered/returning
        removed = [p for p in existing if p in fit]

        parts = []
        if new_players:
            parts.append(f"🆕 {new_players}")
            total_new += len(new_players)
        if removed:
            parts.append(f"✅ back: {removed}")
            total_removed += len(removed)
        if not parts:
            parts.append("(no change)")
        print("  " + " | ".join(parts))

        if not dry_run:
            suspensions.setdefault(team, [])
            updated = list(dict.fromkeys(existing + new_players))
            updated = [p for p in updated if p not in removed]
            suspensions[team] = updated

        time.sleep(0.8)  # polite rate limit

    if not dry_run:
        from datetime import date
        suspensions["_injuries_last_updated"] = str(date.today())
        _save_suspensions(suspensions)
        squad_updates = _update_squads_json(suspensions)
        print(f"\n✓ suspensions.json — {total_new} neu, {total_removed} wieder fit")
        print(f"✓ squads.json — {squad_updates} Spielerstatus geändert")
    else:
        print(f"\n[dry-run] {total_new} neu, {total_removed} wieder fit")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily WM 2026 injury refresh")
    parser.add_argument("--teams", nargs="+", default=None, help="Only refresh these teams")
    parser.add_argument("--dry-run", action="store_true", help="Don't write files")
    args = parser.parse_args()
    print(f"=== WM 2026 Injury Refresh ({'dry-run' if args.dry_run else 'live'}) ===\n")
    run(teams_filter=args.teams, dry_run=args.dry_run)
