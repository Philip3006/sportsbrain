"""
TheOddsAPI wrapper. Free tier: 500 requests/month.
Caches responses for 1 hour to preserve quota.
Set ODDS_API_KEY in .env or pass directly.
"""
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from src.config import DATA_CACHE, ODDS_API_URL
from src.data.cache import disk_cache

load_dotenv()

_USAGE_LOG = DATA_CACHE / "api_usage.json"


def _log_usage(requests_used: int, requests_remaining: int) -> None:
    usage = {"requests_used": requests_used, "requests_remaining": requests_remaining}
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    with open(_USAGE_LOG, "w") as f:
        json.dump(usage, f)


def get_api_key(api_key: str | None = None) -> str:
    key = api_key or os.getenv("ODDS_API_KEY", "")
    if not key:
        raise EnvironmentError(
            "ODDS_API_KEY not set. Add it to .env or pass api_key= directly."
        )
    return key


@disk_cache("odds_api_upcoming", max_age_hours=1.0)
def fetch_upcoming_matches(
    sport: str = "soccer_fifa_world_cup",
    regions: str = "eu",
    markets: str = "h2h,totals,spreads",
    api_key: str | None = None,
    force: bool = False,
) -> list[dict]:
    """
    Fetches upcoming matches with odds from TheOddsAPI.
    Returns list of parsed match dicts with bookmaker odds.
    """
    key = get_api_key(api_key)
    url = f"{ODDS_API_URL}/sports/{sport}/odds"
    params = {
        "apiKey": key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "decimal",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()

    # Log usage from response headers
    used = int(resp.headers.get("x-requests-used", 0))
    remaining = int(resp.headers.get("x-requests-remaining", 0))
    _log_usage(used, remaining)

    if remaining < 20:
        print(f"WARNING: Only {remaining} API requests remaining this month.")

    data = resp.json()
    return _parse_matches(data)


def _parse_matches(raw: list[dict]) -> list[dict]:
    """
    Returns one entry per match with BEST odds per outcome across ALL bookmakers.
    Also records which bookmaker provided each best price.
    Captures h2h (1X2), totals (O/U 2.5), and spreads (AH -0.5).
    """
    matches = []
    for event in raw:
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        commence = event.get("commence_time", "")
        match_id = event.get("id", f"{home}_vs_{away}")

        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            continue

        # Best price per outcome across all bookmakers
        best: dict[str, float] = {}
        best_bm: dict[str, str] = {}

        for bm in bookmakers:
            bm_key = bm["key"]
            for market in bm.get("markets", []):
                mkt = market.get("key")
                if mkt == "h2h":
                    for o in market.get("outcomes", []):
                        name, price = o["name"], o["price"]
                        if price > best.get(name, 0):
                            best[name] = price
                            best_bm[name] = bm_key
                elif mkt == "totals":
                    for o in market.get("outcomes", []):
                        if abs(o.get("point", 0) - 2.5) < 0.1:
                            key = f"{o['name']}_2.5"
                            if o["price"] > best.get(key, 0):
                                best[key] = o["price"]
                                best_bm[key] = bm_key
                elif mkt == "spreads":
                    for o in market.get("outcomes", []):
                        if abs(o.get("point", 0) + 0.5) < 0.1:  # AH -0.5 home
                            key = f"ah_{o['name']}"
                            if o["price"] > best.get(key, 0):
                                best[key] = o["price"]
                                best_bm[key] = bm_key

        if not best.get(home):
            continue  # no h2h odds found

        match_dict: dict = {
            "match_id":      match_id,
            "commence_time": commence,
            "home_team":     home,
            "away_team":     away,
            "home_odds":     best.get(home, 0.0),
            "draw_odds":     best.get("Draw", 0.0),
            "away_odds":     best.get(away, 0.0),
            "over_odds":     best.get("Over_2.5", 0.0),
            "under_odds":    best.get("Under_2.5", 0.0),
            "ah_home_odds":  best.get(f"ah_{home}", 0.0),
            "ah_away_odds":  best.get(f"ah_{away}", 0.0),
            "best_home_bm":  best_bm.get(home, ""),
            "best_draw_bm":  best_bm.get("Draw", ""),
            "best_away_bm":  best_bm.get(away, ""),
        }
        matches.append(match_dict)

    return matches


def mock_upcoming_matches() -> list[dict]:
    """Returns synthetic upcoming matches for dry-run testing (no API call)."""
    return [
        {
            "match_id": "mock_USA_vs_Mexico",
            "commence_time": "2026-06-15T20:00:00Z",
            "home_team": "United States",
            "away_team": "Mexico",
            "home_odds": 2.30,
            "draw_odds": 3.20,
            "away_odds": 3.00,
            "over_odds": 1.90,
            "under_odds": 1.95,
            "ah_home_odds": 1.85,
            "ah_away_odds": 2.00,
            "best_home_bm": "mock",
            "best_draw_bm": "mock",
            "best_away_bm": "mock",
        },
        {
            "match_id": "mock_Brazil_vs_Argentina",
            "commence_time": "2026-06-16T17:00:00Z",
            "home_team": "Brazil",
            "away_team": "Argentina",
            "home_odds": 2.10,
            "draw_odds": 3.40,
            "away_odds": 3.50,
            "over_odds": 1.85,
            "under_odds": 2.00,
            "ah_home_odds": 1.90,
            "ah_away_odds": 1.95,
            "best_home_bm": "mock",
            "best_draw_bm": "mock",
            "best_away_bm": "mock",
        },
    ]
