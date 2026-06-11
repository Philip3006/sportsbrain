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
    markets: str = "h2h,totals,spreads,double_chance",
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
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 422 and "double_chance" in markets:
            print("  WARN: double_chance market unavailable for this sport — retrying without it.")
            params["markets"] = ",".join(m for m in markets.split(",") if m != "double_chance")
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
        else:
            raise

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


_PREFERRED_BM = "pinnacle"


def _round_quarter(x: float) -> float:
    """Round to nearest 0.25."""
    return round(x * 4) / 4


def _parse_markets(bm: dict, home: str, away: str, store: dict, dynamic: dict) -> None:
    """Extracts h2h/totals/spreads/btts odds from one bookmaker.
    store: flat dict for h2h and btts.
    dynamic: nested dict with keys 'spreads' and 'totals' for all lines.
    """
    for market in bm.get("markets", []):
        mkt = market.get("key")
        if mkt == "h2h":
            for o in market.get("outcomes", []):
                name, price = o["name"], o["price"]
                if price > store.get(name, 0):
                    store[name] = price
        elif mkt in ("totals", "alternate_totals"):
            for o in market.get("outcomes", []):
                pt = _round_quarter(o.get("point", 0))
                side = o["name"].lower()  # "over" or "under"
                if pt not in dynamic["totals"]:
                    dynamic["totals"][pt] = {}
                if o["price"] > dynamic["totals"][pt].get(side, 0):
                    dynamic["totals"][pt][side] = o["price"]
        elif mkt in ("spreads", "alternate_spreads"):
            for o in market.get("outcomes", []):
                pt = _round_quarter(o.get("point", 0))
                # Normalize to home's line (negative when home favored)
                team = o["name"]
                if team == home:
                    home_line = pt
                else:
                    home_line = -pt  # away's line flipped to home perspective
                home_line = _round_quarter(home_line)
                side = "home" if team == home else "away"
                if home_line not in dynamic["spreads"]:
                    dynamic["spreads"][home_line] = {}
                if o["price"] > dynamic["spreads"][home_line].get(side, 0):
                    dynamic["spreads"][home_line][side] = o["price"]
        elif mkt == "btts":
            for o in market.get("outcomes", []):
                key = f"btts_{o['name'].lower()}"  # "btts_yes" or "btts_no"
                if o["price"] > store.get(key, 0):
                    store[key] = o["price"]
        elif mkt == "double_chance":
            for o in market.get("outcomes", []):
                name, price = o["name"], o["price"]
                if "Home" in name and "Draw" in name:
                    key = "dc_1x"
                elif "Draw" in name and "Away" in name:
                    key = "dc_x2"
                else:
                    key = "dc_12"
                if price > store.get(key, 0):
                    store[key] = o["price"]


def _parse_matches(raw: list[dict]) -> list[dict]:
    """
    Returns one entry per match with:
    - Best odds across ALL bookmakers (used for EV calculation)
    - Pinnacle-specific odds (sharp reference line)
    - `spreads`: {home_line: {"home": odds, "away": odds}} for all AH lines
    - `totals_lines`: {line: {"over": odds, "under": odds}} for all O/U lines
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
        pin: dict[str, float] = {}
        pin_dynamic: dict = {"spreads": {}, "totals": {}}
        best_dynamic: dict = {"spreads": {}, "totals": {}}
        best_bm: dict[str, str] = {}

        for bm in bookmakers:
            bm_key = bm["key"]
            prev = {k: v for k, v in best.items()}
            _parse_markets(bm, home, away, best, best_dynamic)
            # Track which bookmaker provides each best price
            for k in best:
                if best[k] != prev.get(k, 0):
                    best_bm[k] = bm_key
            # Capture Pinnacle separately (sharp reference line)
            if bm_key == _PREFERRED_BM:
                _parse_markets(bm, home, away, pin, pin_dynamic)

        if not best.get(home):
            continue

        # Build backward-compat h2h flat keys from best dict
        match_dict: dict = {
            "match_id":       match_id,
            "commence_time":  commence,
            "home_team":      home,
            "away_team":      away,
            "tournament":     "FIFA World Cup",
            # Best h2h odds (for EV/model comparison)
            "home_odds":      best.get(home, 0.0),
            "draw_odds":      best.get("Draw", 0.0),
            "away_odds":      best.get(away, 0.0),
            # Backward compat O/U 2.5 (best across bookmakers)
            "over_odds":      best_dynamic["totals"].get(2.5, {}).get("over", 0.0),
            "under_odds":     best_dynamic["totals"].get(2.5, {}).get("under", 0.0),
            "over15_odds":    best_dynamic["totals"].get(1.5, {}).get("over", 0.0),
            "under15_odds":   best_dynamic["totals"].get(1.5, {}).get("under", 0.0),
            "over35_odds":    best_dynamic["totals"].get(3.5, {}).get("over", 0.0),
            "under35_odds":   best_dynamic["totals"].get(3.5, {}).get("under", 0.0),
            # Backward compat AH (best across bookmakers)
            "ah_home_odds":   best_dynamic["spreads"].get(-0.5, {}).get("home", 0.0),
            "ah_away_odds":   best_dynamic["spreads"].get(-0.5, {}).get("away", 0.0),
            "ah1_home_odds":  best_dynamic["spreads"].get(-1.0, {}).get("home", 0.0),
            "ah1_away_odds":  best_dynamic["spreads"].get(-1.0, {}).get("away", 0.0),
            "ah15_home_odds": best_dynamic["spreads"].get(-1.5, {}).get("home", 0.0),
            "ah15_away_odds": best_dynamic["spreads"].get(-1.5, {}).get("away", 0.0),
            "ah2_home_odds":  best_dynamic["spreads"].get(-2.0, {}).get("home", 0.0),
            "ah2_away_odds":  best_dynamic["spreads"].get(-2.0, {}).get("away", 0.0),
            "ah25_home_odds": best_dynamic["spreads"].get(-2.5, {}).get("home", 0.0),
            "ah25_away_odds": best_dynamic["spreads"].get(-2.5, {}).get("away", 0.0),
            "btts_yes_odds":  best.get("btts_yes", 0.0),
            "btts_no_odds":   best.get("btts_no", 0.0),
            "ftts_home_odds": 0.0,
            "ftts_away_odds": 0.0,
            "best_home_bm":   best_bm.get(home, ""),
            "best_draw_bm":   best_bm.get("Draw", ""),
            "best_away_bm":   best_bm.get(away, ""),
            # Pinnacle odds (sharp reference line — used for display and CLV)
            "pin_home":      pin.get(home, 0.0),
            "pin_draw":      pin.get("Draw", 0.0),
            "pin_away":      pin.get(away, 0.0),
            "pin_over":      pin_dynamic["totals"].get(2.5, {}).get("over", 0.0),
            "pin_under":     pin_dynamic["totals"].get(2.5, {}).get("under", 0.0),
            "pin_ah_home":   pin_dynamic["spreads"].get(-0.5, {}).get("home", 0.0),
            "pin_ah_away":   pin_dynamic["spreads"].get(-0.5, {}).get("away", 0.0),
            "pin_ah1_home":  pin_dynamic["spreads"].get(-1.0, {}).get("home", 0.0),
            "pin_ah1_away":  pin_dynamic["spreads"].get(-1.0, {}).get("away", 0.0),
            "pin_ah15_home": pin_dynamic["spreads"].get(-1.5, {}).get("home", 0.0),
            "pin_ah15_away": pin_dynamic["spreads"].get(-1.5, {}).get("away", 0.0),
            "pin_btts_yes":  pin.get("btts_yes", 0.0),
            "pin_btts_no":   pin.get("btts_no", 0.0),
            # Double Chance (best across bookmakers)
            "dc_1x_odds":    best.get("dc_1x", 0.0),   # Home or Draw
            "dc_x2_odds":    best.get("dc_x2", 0.0),   # Draw or Away
            "dc_12_odds":    best.get("dc_12", 0.0),   # Home or Away
            "pin_dc_1x":     pin.get("dc_1x", 0.0),
            "pin_dc_x2":     pin.get("dc_x2", 0.0),
            "pin_dc_12":     pin.get("dc_12", 0.0),
            # Dynamic all-lines dicts — used by scanner for comprehensive coverage
            "spreads":        best_dynamic["spreads"],   # {home_line: {"home": odds, "away": odds}}
            "totals_lines":   best_dynamic["totals"],    # {line: {"over": odds, "under": odds}}
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
            "btts_yes_odds": 1.72,
            "btts_no_odds": 2.05,
            "ftts_home_odds": 1.90,
            "ftts_away_odds": 2.05,
            "best_home_bm": "mock",
            "best_draw_bm": "mock",
            "best_away_bm": "mock",
            # Dynamic dicts used by scanner's dynamic loops
            "totals_lines": {
                1.5: {"over": 1.35, "under": 3.10},
                2.25: {"over": 1.93, "under": 1.97},
                2.5: {"over": 1.90, "under": 1.95},
                3.5: {"over": 3.20, "under": 1.38},
            },
            "spreads": {
                -0.5: {"home": 1.85, "away": 2.00},
                -1.0: {"home": 2.05, "away": 1.80},
            },
            # Backward-compat (still used for pin_* fields in b365_map)
            "over_odds": 1.90, "under_odds": 1.95,
            "over15_odds": 1.35, "under15_odds": 3.10,
            "over35_odds": 3.20, "under35_odds": 1.38,
            "ah_home_odds": 1.85, "ah_away_odds": 2.00,
            "ah1_home_odds": 2.05, "ah1_away_odds": 1.80,
            "ah15_home_odds": 0.0, "ah15_away_odds": 0.0,
            "pin_home": 2.28, "pin_draw": 3.15, "pin_away": 2.97,
            "pin_over": 1.90, "pin_under": 1.95,
            "pin_ah_home": 1.83, "pin_ah_away": 1.98,
            "pin_ah1_home": 2.02, "pin_ah1_away": 1.78,
            "pin_ah15_home": 0.0, "pin_ah15_away": 0.0,
            "pin_btts_yes": 1.70, "pin_btts_no": 2.03,
            "dc_1x_odds": 1.35, "dc_x2_odds": 1.72, "dc_12_odds": 1.22,
            "pin_dc_1x": 1.33, "pin_dc_x2": 1.70, "pin_dc_12": 1.20,
        },
        {
            "match_id": "mock_Brazil_vs_Argentina",
            "commence_time": "2026-06-16T17:00:00Z",
            "home_team": "Brazil",
            "away_team": "Argentina",
            "home_odds": 2.10,
            "draw_odds": 3.40,
            "away_odds": 3.50,
            "btts_yes_odds": 1.72,
            "btts_no_odds": 2.05,
            "ftts_home_odds": 1.85,
            "ftts_away_odds": 2.10,
            "best_home_bm": "mock",
            "best_draw_bm": "mock",
            "best_away_bm": "mock",
            # Dynamic dicts
            "totals_lines": {
                1.5: {"over": 1.30, "under": 3.40},
                2.25: {"over": 1.87, "under": 2.02},
                2.5: {"over": 1.85, "under": 2.00},
                3.5: {"over": 3.40, "under": 1.35},
            },
            "spreads": {
                -0.5: {"home": 1.90, "away": 1.95},
                -0.75: {"home": 2.05, "away": 1.85},
                -1.0: {"home": 2.20, "away": 1.72},
            },
            # Backward-compat
            "over_odds": 1.85, "under_odds": 2.00,
            "over15_odds": 1.30, "under15_odds": 3.40,
            "over35_odds": 3.40, "under35_odds": 1.35,
            "ah_home_odds": 1.90, "ah_away_odds": 1.95,
            "ah1_home_odds": 2.20, "ah1_away_odds": 1.72,
            "ah15_home_odds": 0.0, "ah15_away_odds": 0.0,
            "pin_home": 2.08, "pin_draw": 3.38, "pin_away": 3.47,
            "pin_over": 1.83, "pin_under": 1.97,
            "pin_ah_home": 1.88, "pin_ah_away": 1.93,
            "pin_ah1_home": 2.18, "pin_ah1_away": 1.70,
            "pin_ah15_home": 0.0, "pin_ah15_away": 0.0,
            "pin_btts_yes": 1.70, "pin_btts_no": 2.03,
            "dc_1x_odds": 1.30, "dc_x2_odds": 1.88, "dc_12_odds": 1.18,
            "pin_dc_1x": 1.28, "pin_dc_x2": 1.86, "pin_dc_12": 1.16,
        },
    ]
