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
    sport: str = "soccer_fifa_world_cup_2026",
    regions: str = "eu",
    markets: str = "h2h,totals,spreads,btts",
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
        try:
            from src.notifications.telegram import send_quota_alert
            send_quota_alert(remaining)
        except Exception:
            pass

    data = resp.json()
    return _parse_matches(data)


_PREFERRED_BM = "bet365"


def _parse_markets(bm: dict, home: str, away: str, store: dict) -> None:
    """Extracts h2h/totals/spreads/btts odds from one bookmaker into store dict."""
    for market in bm.get("markets", []):
        mkt = market.get("key")
        if mkt == "h2h":
            for o in market.get("outcomes", []):
                name, price = o["name"], o["price"]
                if price > store.get(name, 0):
                    store[name] = price
        elif mkt == "totals":
            for o in market.get("outcomes", []):
                if abs(o.get("point", 0) - 2.5) < 0.1:
                    key = f"{o['name']}_2.5"
                    if o["price"] > store.get(key, 0):
                        store[key] = o["price"]
        elif mkt == "spreads":
            for o in market.get("outcomes", []):
                pt = o.get("point", 0)
                name = o["name"]
                if abs(abs(pt) - 0.5) < 0.1:
                    key = f"ah_{name}"
                elif abs(abs(pt) - 1.0) < 0.1:
                    key = f"ah1_{name}"
                elif abs(abs(pt) - 1.5) < 0.1:
                    key = f"ah15_{name}"
                else:
                    continue
                if o["price"] > store.get(key, 0):
                    store[key] = o["price"]
        elif mkt == "btts":
            for o in market.get("outcomes", []):
                key = f"btts_{o['name'].lower()}"  # "btts_yes" or "btts_no"
                if o["price"] > store.get(key, 0):
                    store[key] = o["price"]


def _parse_matches(raw: list[dict]) -> list[dict]:
    """
    Returns one entry per match with:
    - Best odds across ALL bookmakers (used for EV calculation)
    - Bet365-specific odds (shown in report/Telegram — what user actually gets)
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

        best: dict[str, float] = {}
        b365: dict[str, float] = {}
        best_bm: dict[str, str] = {}

        for bm in bookmakers:
            bm_key = bm["key"]
            prev = {k: v for k, v in best.items()}
            _parse_markets(bm, home, away, best)
            # Track which bookmaker provides each best price
            for k in best:
                if best[k] != prev.get(k, 0):
                    best_bm[k] = bm_key
            # Capture Bet365 separately
            if bm_key == _PREFERRED_BM:
                _parse_markets(bm, home, away, b365)

        if not best.get(home):
            continue

        match_dict: dict = {
            "match_id":       match_id,
            "commence_time":  commence,
            "home_team":      home,
            "away_team":      away,
            "tournament":     "FIFA World Cup",
            # Best-market odds (for EV/model comparison)
            "home_odds":      best.get(home, 0.0),
            "draw_odds":      best.get("Draw", 0.0),
            "away_odds":      best.get(away, 0.0),
            "over_odds":      best.get("Over_2.5", 0.0),
            "under_odds":     best.get("Under_2.5", 0.0),
            "ah_home_odds":   best.get(f"ah_{home}", 0.0),
            "ah_away_odds":   best.get(f"ah_{away}", 0.0),
            "ah1_home_odds":  best.get(f"ah1_{home}", 0.0),
            "ah1_away_odds":  best.get(f"ah1_{away}", 0.0),
            "ah15_home_odds": best.get(f"ah15_{home}", 0.0),
            "ah15_away_odds": best.get(f"ah15_{away}", 0.0),
            "btts_yes_odds":  best.get("btts_yes", 0.0),
            "btts_no_odds":   best.get("btts_no", 0.0),
            "best_home_bm":   best_bm.get(home, ""),
            "best_draw_bm":   best_bm.get("Draw", ""),
            "best_away_bm":   best_bm.get(away, ""),
            # Bet365-specific odds (shown to user for placing)
            "b365_home":      b365.get(home, 0.0),
            "b365_draw":      b365.get("Draw", 0.0),
            "b365_away":      b365.get(away, 0.0),
            "b365_over":      b365.get("Over_2.5", 0.0),
            "b365_under":     b365.get("Under_2.5", 0.0),
            "b365_ah_home":   b365.get(f"ah_{home}", 0.0),
            "b365_ah_away":   b365.get(f"ah_{away}", 0.0),
            "b365_ah1_home":  b365.get(f"ah1_{home}", 0.0),
            "b365_ah1_away":  b365.get(f"ah1_{away}", 0.0),
            "b365_ah15_home": b365.get(f"ah15_{home}", 0.0),
            "b365_ah15_away": b365.get(f"ah15_{away}", 0.0),
            "b365_btts_yes":  b365.get("btts_yes", 0.0),
            "b365_btts_no":   b365.get("btts_no", 0.0),
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
            "ah1_home_odds": 2.05,
            "ah1_away_odds": 1.80,
            "btts_yes_odds": 1.72,
            "btts_no_odds": 2.05,
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
            "ah1_home_odds": 2.05,
            "ah1_away_odds": 1.80,
            "btts_yes_odds": 1.72,
            "btts_no_odds": 2.05,
            "best_home_bm": "mock",
            "best_draw_bm": "mock",
            "best_away_bm": "mock",
        },
    ]
