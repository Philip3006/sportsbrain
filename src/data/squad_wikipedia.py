"""Wikipedia squad scrapers: WC squads consolidated page + per-team pages."""
from __future__ import annotations

import re
import time

import pandas as pd

from scripts._http_retry import retry_request
from .squad_models import (
    PlayerStatus, _HEADERS,
    _wiki_cache_path, _cache_fresh, _save_cache, _load_cache,
)

# All 48 WM 2026 teams → Wikipedia section index on "2026_FIFA_World_Cup_squads"
_WC_SECTION_MAP: dict[str, int] = {
    "Czech Republic": 2, "Mexico": 3, "South Africa": 4, "South Korea": 5,
    "Bosnia and Herzegovina": 7, "Bosnia & Herzegovina": 7,
    "Canada": 8, "Qatar": 9, "Switzerland": 10,
    "Brazil": 12, "Haiti": 13, "Morocco": 14, "Scotland": 15,
    "Australia": 17, "Paraguay": 18, "Turkey": 19, "United States": 20,
    "Curacao": 22, "Ecuador": 23, "Germany": 24,
    "Ivory Coast": 25, "Cote d'Ivoire": 25,
    "Japan": 27, "Netherlands": 28, "Sweden": 29, "Tunisia": 30,
    "Belgium": 32, "Egypt": 33, "Iran": 34, "New Zealand": 35,
    "Cape Verde": 37, "Saudi Arabia": 38, "Spain": 39, "Uruguay": 40,
    "France": 42, "Iraq": 43, "Norway": 44, "Senegal": 45,
    "Algeria": 47, "Argentina": 48, "Austria": 49, "Jordan": 50,
    "Colombia": 52, "DR Congo": 53, "Portugal": 54, "Uzbekistan": 55,
    "Croatia": 57, "England": 58, "Ghana": 59, "Panama": 60,
    # Aliases
    "USA": 20, "Czechia": 2,
}

_WC_PAGE = "2026_FIFA_World_Cup_squads"
_WIKI_API = "https://en.wikipedia.org/w/api.php"

# Wikitext player template pattern: |name=[[Link|Display Name]] or |name=[[Name]]
_WT_NAME_RE = re.compile(r"\|name=\[\[(?:[^\|\]]+\|)?([^\]]+)\]\]")
_WT_POS_RE  = re.compile(r"\|pos=(GK|DF|MF|FW)")

# Wikipedia position abbreviations → internal position codes
_WIKI_POS_MAP = {
    "GK": "GK",
    "DF": "DEF",
    "MF": "MID",
    "FW": "FWD",
    # some pages spell it out
    "Goalkeeper": "GK",
    "Defender": "DEF",
    "Midfielder": "MID",
    "Forward": "FWD",
}

# Map team canonical name to Wikipedia URL slug (overrides default underscore logic)
_WIKI_SLUG_OVERRIDES: dict[str, str] = {
    "United States": "United_States",
    "South Korea": "South_Korea",
    "DR Congo": "DR_Congo",
    "Cote d'Ivoire": "Ivory_Coast",
    "Ivory Coast": "Ivory_Coast",
    "Czech Republic": "Czech_Republic",
    "Saudi Arabia": "Saudi_Arabia",
    "New Zealand": "New_Zealand",
    "New Caledonia": "New_Caledonia",
    "El Salvador": "El_Salvador",
    "Costa Rica": "Costa_Rica",
    "South Africa": "South_Africa",
}


def _parse_wikitext_squad(wikitext: str) -> list[PlayerStatus]:
    """Extract players from Wikipedia wikitext squad template format."""
    players: list[PlayerStatus] = []
    lines = wikitext.split("\n")
    for line in lines:
        name_m = _WT_NAME_RE.search(line)
        pos_m  = _WT_POS_RE.search(line)
        if not name_m:
            continue
        raw_name = name_m.group(1).strip()
        # Strip disambiguation suffixes like "(soccer)" or "(footballer)"
        raw_name = re.sub(r"\s*\([^)]*\)\s*$", "", raw_name).strip()
        pos_raw = pos_m.group(1) if pos_m else "MF"
        position = _WIKI_POS_MAP.get(pos_raw, "MID")
        players.append(PlayerStatus(
            name=raw_name, position=position,
            availability=1.0, status="fit",
            key_player=True, p_plays=1.0,
        ))
    return players


def _fetch_wc_squads_page(team: str, match_date: pd.Timestamp) -> list[PlayerStatus]:
    """
    Fetches squad from the Wikipedia WC 2026 squads consolidated page using
    the MediaWiki parse API (no HTML scraping — no bot blocking).
    Caches result to data/cache/squad/{team}_wiki.json.
    """
    cache_file = _wiki_cache_path(team)
    if _cache_fresh(cache_file):
        return _load_cache(cache_file)

    section_idx = _WC_SECTION_MAP.get(team)
    if section_idx is None:
        return []

    _wiki_headers = {
        "User-Agent": "SportsBrain/1.0 (WM 2026 squad fetcher; +https://github.com/sportsbrain)",
        "Accept": "application/json",
    }
    try:
        resp = retry_request("GET",
            _WIKI_API,
            params={
                "action": "parse",
                "page": _WC_PAGE,
                "prop": "wikitext",
                "section": section_idx,
                "format": "json",
            },
            headers=_wiki_headers,
            timeout=15,
        )
        time.sleep(0.3)
    except Exception as exc:
        print(f"  [wiki-wc] {team}: request failed — {exc}")
        return []

    if resp.status_code != 200:
        print(f"  [wiki-wc] {team}: HTTP {resp.status_code}")
        return []

    try:
        wikitext = resp.json()["parse"]["wikitext"]["*"]
    except (KeyError, ValueError):
        return []

    players = _parse_wikitext_squad(wikitext)
    if players:
        _save_cache(cache_file, players)
        print(f"  [wiki-wc] {team}: {len(players)} players (WC squads page)")
    else:
        print(f"  [wiki-wc] {team}: no players parsed from wikitext")
    return players


def _fetch_wikipedia_squad(
    team: str,
    match_date: pd.Timestamp,
) -> list[PlayerStatus]:
    """
    Fetches the squad list from Wikipedia's WM 2026 team page.
    URL pattern: https://en.wikipedia.org/wiki/{Team}_at_the_2026_FIFA_World_Cup

    Uses requests + BeautifulSoup (no Playwright/JS needed).
    Caches to data/cache/squad/{team}_wiki.json with 24h TTL.
    Returns [] on any error (404, parse failure, bs4 missing, etc.).
    """
    cache_file = _wiki_cache_path(team)
    if _cache_fresh(cache_file):
        return _load_cache(cache_file)

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  [wiki] beautifulsoup4 not installed — run: pip3 install beautifulsoup4")
        return []

    slug = _WIKI_SLUG_OVERRIDES.get(team, team.replace(" ", "_"))
    url = f"https://en.wikipedia.org/wiki/{slug}_at_the_2026_FIFA_World_Cup"

    try:
        resp = retry_request("GET",
            url,
            headers=_HEADERS,
            timeout=15,
        )
        time.sleep(0.5)  # rate limit
    except Exception as exc:
        print(f"  [wiki] {team}: request failed — {exc}")
        return []

    if resp.status_code == 404:
        print(f"  [wiki] {team}: Wikipedia page not found (404) — {url}")
        return []
    if resp.status_code != 200:
        print(f"  [wiki] {team}: HTTP {resp.status_code} — {url}")
        return []

    players = _parse_wikipedia_squad_html(resp.text, team)
    if players:
        _save_cache(cache_file, players)
        print(f"  [wiki] {team}: {len(players)} players parsed from Wikipedia")
    else:
        print(f"  [wiki] {team}: page found but no squad table parsed — {url}")
    return players


def _parse_wikipedia_squad_html(html: str, team: str) -> list[PlayerStatus]:
    """
    Parses the squad table from a Wikipedia WM 2026 team page.
    Wikipedia squad tables have columns like: No., Pos., Name, DOB (Age), Caps, Club.
    Returns list[PlayerStatus] with availability=1.0 (Wikipedia has no injury data).
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    soup = BeautifulSoup(html, "html.parser")
    players: list[PlayerStatus] = []

    # Find the squad section — look for a wikitable that contains position column
    # Wikipedia uses class="wikitable" for squad tables
    for table in soup.find_all("table", class_="wikitable"):
        # Check if this table has a position-like column header
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        has_pos = any("pos" in h for h in headers)
        has_name = any("name" in h or "player" in h for h in headers)
        if not (has_pos and has_name):
            continue

        # Determine column indices from the header row
        header_row = table.find("tr")
        if not header_row:
            continue
        cols = [th.get_text(strip=True).lower() for th in header_row.find_all("th")]

        pos_idx = next((i for i, c in enumerate(cols) if "pos" in c), None)
        name_idx = next(
            (i for i, c in enumerate(cols) if "name" in c or "player" in c), None
        )
        dob_idx = next(
            (i for i, c in enumerate(cols) if "dob" in c or "birth" in c or "age" in c),
            None,
        )

        if pos_idx is None or name_idx is None:
            continue

        for row in table.find_all("tr")[1:]:  # skip header
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(pos_idx, name_idx):
                continue

            pos_raw = cells[pos_idx].get_text(strip=True)
            name_raw = cells[name_idx].get_text(strip=True)

            # Clean name: remove footnote references like [1] or (c) captain markers
            name = re.sub(r"\[.*?\]|\(c\)", "", name_raw).strip()
            if not name:
                continue

            position = _WIKI_POS_MAP.get(pos_raw, "unknown")

            players.append(PlayerStatus(
                name=name,
                position=position,
                availability=1.0,   # Wikipedia has no injury data
                status="fit",
                key_player=True,    # all squad members are potential starters
                p_plays=1.0,
            ))

        if players:
            break  # found and parsed the main squad table

    return players
