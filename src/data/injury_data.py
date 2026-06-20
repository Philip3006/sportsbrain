"""
Covers.com World Cup 2026 injury scraper.
Primary source for national team injury/availability data.

Status weights:
  "out" / "ruled out" → 0.0 (unavailable)
  "doubtful"          → 0.5
  "questionable"      → 0.7
  "probable"          → 0.9
  "expected to play"  → 0.95
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

from scripts._http_retry import retry_request
from src.config import DATA_CACHE

_CACHE_DIR = DATA_CACHE / "injuries"
_CACHE_TTL_HOURS = 1.0  # injury status changes during tournament
_COVERS_URL = "https://www.covers.com/world-cup/injury-report-2026"

_STATUS_WEIGHT: dict[str, float] = {
    "ruled out":        0.0,
    "out":              0.0,
    "torn":             0.0,   # "torn ACL" etc.
    "doubtful":         0.5,
    "questionable":     0.7,
    "probable":         0.9,
    "expected to play": 0.95,
}

# Static fallback: current WM 2026 injuries as of 2026-06-12
# Source: covers.com, ESPN injury tracker, Reuters
_STATIC_INJURIES: list[dict] = [
    # --- Ruled Out ---
    {"player": "Rodrygo",           "team": "Brazil",        "availability": 0.0, "status": "out"},
    {"player": "Estevao",           "team": "Brazil",        "availability": 0.0, "status": "out"},
    {"player": "Wesley",            "team": "Brazil",        "availability": 0.0, "status": "out"},
    {"player": "Hugo Ekitike",      "team": "France",        "availability": 0.0, "status": "out"},
    {"player": "Serge Gnabry",      "team": "Germany",       "availability": 0.0, "status": "out"},
    {"player": "Lennart Karl",      "team": "Germany",       "availability": 0.0, "status": "out"},
    {"player": "Kaoru Mitoma",      "team": "Japan",         "availability": 0.0, "status": "out"},
    {"player": "Takumi Minamino",   "team": "Japan",         "availability": 0.0, "status": "out"},
    {"player": "Wataru Endo",       "team": "Japan",         "availability": 0.0, "status": "out"},
    {"player": "Jurrien Timber",    "team": "Netherlands",   "availability": 0.0, "status": "out"},
    {"player": "Xavi Simons",       "team": "Netherlands",   "availability": 0.0, "status": "out"},
    {"player": "Matthijs de Ligt",  "team": "Netherlands",   "availability": 0.0, "status": "out"},
    {"player": "Fermin Lopez",      "team": "Spain",         "availability": 0.0, "status": "out"},
    {"player": "Johnny Cardoso",    "team": "United States", "availability": 0.0, "status": "out"},
    {"player": "Patrick Agyemang",  "team": "United States", "availability": 0.0, "status": "out"},
    {"player": "Mohammed Kudus",    "team": "Ghana",         "availability": 0.0, "status": "out"},
    {"player": "Marcelo Flores",    "team": "Canada",        "availability": 0.0, "status": "out"},
    {"player": "Christoph Baumgartner", "team": "Austria",   "availability": 0.0, "status": "out"},
    {"player": "Leonardo Balerdi",  "team": "Argentina",     "availability": 0.0, "status": "out"},
    {"player": "Billy Gilmour",     "team": "Scotland",      "availability": 0.0, "status": "out"},
    {"player": "Riley McGree",      "team": "Australia",     "availability": 0.0, "status": "out"},
    # --- Questionable / Doubtful ---
    {"player": "Neymar",            "team": "Brazil",        "availability": 0.7, "status": "questionable"},
    {"player": "Alphonso Davies",   "team": "Canada",        "availability": 0.7, "status": "questionable"},
    {"player": "Jose Gimenez",      "team": "Uruguay",       "availability": 0.7, "status": "questionable"},
    {"player": "Julio Enciso",      "team": "Paraguay",      "availability": 0.7, "status": "questionable"},
    {"player": "Abde Ezzalzouli",   "team": "Morocco",       "availability": 0.7, "status": "questionable"},
    {"player": "Chris Richards",    "team": "United States", "availability": 0.9, "status": "probable"},
    {"player": "Lamine Yamal",      "team": "Spain",         "availability": 0.9, "status": "probable"},
]

# Team name aliases for matching covers.com names to our canonical names
_TEAM_ALIASES: dict[str, str] = {
    "usa":           "United States",
    "u.s.":          "United States",
    "u.s.a.":        "United States",
    "cote d'ivoire": "Ivory Coast",
    "côte d'ivoire": "Ivory Coast",
    "czechia":       "Czech Republic",
}


def _normalize_team(raw: str) -> str:
    lower = raw.strip().lower()
    return _TEAM_ALIASES.get(lower, raw.strip().title())


def _status_to_availability(status_text: str) -> float:
    lower = status_text.lower()
    for key, weight in _STATUS_WEIGHT.items():
        if key in lower:
            return weight
    return 0.5  # unknown → treat as doubtful


def _fetch_covers_injuries() -> list[dict]:
    """
    Scrapes covers.com injury report.
    Page uses h3/p/li structure (not tables).
    Returns list of {player, team, status, availability}.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  [injury] beautifulsoup4 not installed — using static fallback")
        return []

    try:
        resp = retry_request("GET",
            _COVERS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=15,
        )
        time.sleep(0.3)
    except Exception as exc:
        print(f"  [injury] covers.com request failed: {exc}")
        return []

    if resp.status_code != 200:
        print(f"  [injury] covers.com HTTP {resp.status_code}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    injuries: list[dict] = []
    current_status = "out"

    # Try table-based structure first
    # covers.com: the only table on this page is "Players ruled out" → all entries are out
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        has_player = any("player" in h or "name" in h for h in headers)
        has_country = any("country" in h or "team" in h or "nation" in h for h in headers)
        if not (has_player or has_country):
            continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            player = cells[0].get_text(strip=True)
            team = cells[1].get_text(strip=True)
            injury_type = cells[-1].get_text(strip=True) if len(cells) > 2 else "injury"
            if player and team:
                # All rows in this table are confirmed "ruled out" players
                injuries.append({
                    "player": player,
                    "team": _normalize_team(team),
                    "status": "out",
                    "availability": 0.0,
                })

    if injuries:
        return injuries

    # Fallback: h3/section-based structure
    # Pattern: section heading → player entries with "(Country)" or team listed separately
    _section_keywords = {
        "ruled out": 0.0, "season-ending": 0.0,
        "questionable": 0.7, "doubtful": 0.5,
        "probable": 0.9, "expected to play": 0.95,
    }

    for el in soup.find_all(["h2", "h3", "h4"]):
        text = el.get_text(strip=True).lower()
        for kw, avail in _section_keywords.items():
            if kw in text:
                current_status = kw
                current_avail = avail
                break

        # Extract player entries that follow this header
        sibling = el.find_next_sibling()
        while sibling and sibling.name not in ("h2", "h3", "h4"):
            entry_text = sibling.get_text(separator=" ", strip=True)
            # Pattern: "Player Name (Country)" or "Player Name | Country"
            m = re.search(r"^([A-Z][a-zA-Zéàü\-'\s]+?)\s*[|(]\s*([A-Z][a-zA-Zéàü\s]+?)[\s|)]", entry_text)
            if m:
                player = m.group(1).strip()
                team = _normalize_team(m.group(2).strip())
                injuries.append({
                    "player": player,
                    "team": team,
                    "status": current_status,
                    "availability": current_avail,
                })
            sibling = sibling.find_next_sibling()

    return injuries


def fetch_injuries(force: bool = False) -> list[dict]:
    """
    Returns current WM 2026 injury list.
    Tries covers.com first, falls back to static data.
    Cached for 1 hour.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _CACHE_DIR / "covers_injuries.json"

    if not force and _cache_fresh(cache_file):
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass

    live = _fetch_covers_injuries()
    if live:
        cache_file.write_text(json.dumps(live, ensure_ascii=False))
        print(f"  [injury] covers.com: {len(live)} injury records cached")
        return live

    print("  [injury] using static injury fallback")
    cache_file.write_text(json.dumps(_STATIC_INJURIES, ensure_ascii=False))
    return _STATIC_INJURIES


def get_team_injuries(team: str, force: bool = False) -> list[dict]:
    """Returns injury records for a specific team."""
    all_injuries = fetch_injuries(force=force)
    t_lower = team.lower()
    return [
        i for i in all_injuries
        if i["team"].lower() == t_lower
        or i["team"].lower() in (t_lower,)
        or t_lower in (i["team"].lower(),)
    ]


def get_team_availability_score(team: str) -> float:
    """
    Returns 0.0–1.0 availability score based on current injuries.
    Each missing player reduces score by 1/26 (squad size = 26).
    Floors at 0.5 to avoid extreme adjustments.
    """
    injuries = get_team_injuries(team)
    if not injuries:
        return 1.0
    squad_size = 26.0
    missing = sum(1.0 - i["availability"] for i in injuries)
    return max(0.5, 1.0 - missing / squad_size)


def _cache_fresh(path: Path) -> bool:
    import time as _time
    if not path.exists():
        return False
    return (_time.time() - path.stat().st_mtime) / 3600 < _CACHE_TTL_HOURS
