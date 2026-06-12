"""
Fetches BTTS (Both Teams to Score) odds from Bet365 via api-football (RapidAPI).

Setup:
1. Create free account at https://rapidapi.com
2. Subscribe to "API-Football" (free tier, 100 req/day)
3. Add to .env:  API_FOOTBALL_KEY=your_rapidapi_key

Free tier covers ~14 date-requests for the full WM2026 group stage.
"""
import os
from datetime import datetime, timezone

import requests

from src.data.cache import disk_cache
from src.config import canonical_name

_HOST = "api-football-v1.p.rapidapi.com"
_LEAGUE_ID = 1     # FIFA World Cup
_SEASON    = 2026
_BM_BET365 = 6
_BET_BTTS  = 8     # "Both Teams Score" bet ID in api-football


def _api_key() -> str | None:
    return os.getenv("API_FOOTBALL_KEY")


def _norm(name: str) -> str:
    try:
        return canonical_name(name).lower().strip()
    except Exception:
        return name.lower().strip()


@disk_cache("btts_odds_bet365", max_age_hours=4.0)
def fetch_btts_odds(matches: list[dict] | None = None, force: bool = False) -> dict[str, dict]:
    """
    Returns {match_key: {"yes": float, "no": float}} for upcoming WM2026 matches.

    match_key format: "Home vs Away" (canonical names, same as TheOddsAPI)
    Requests one api-football call per unique match-date → ~1-4 calls/run.
    Returns {} if API_FOOTBALL_KEY is not set.
    """
    key = _api_key()
    if not key:
        return {}

    headers = {
        "x-rapidapi-key": key,
        "x-rapidapi-host": _HOST,
    }

    # Collect unique fixture dates from caller's match list
    dates: list[str] = []
    if matches:
        seen: set[str] = set()
        for m in matches:
            ct = m.get("commence_time", "")
            if ct:
                try:
                    d = datetime.fromisoformat(ct.replace("Z", "+00:00")).strftime("%Y-%m-%d")
                    if d not in seen:
                        seen.add(d)
                        dates.append(d)
                except ValueError:
                    pass

    # Fall back to today if no dates supplied
    if not dates:
        dates = [datetime.now(timezone.utc).strftime("%Y-%m-%d")]

    result: dict[str, dict] = {}

    for date_str in dates:
        try:
            resp = requests.get(
                f"https://{_HOST}/v3/odds",
                headers=headers,
                params={
                    "league":     _LEAGUE_ID,
                    "season":     _SEASON,
                    "date":       date_str,
                    "bookmaker":  _BM_BET365,
                    "bet":        _BET_BTTS,
                },
                timeout=15,
            )
        except Exception as e:
            print(f"  [btts] api-football error for {date_str}: {e}")
            continue

        if resp.status_code == 401:
            print("  [btts] API_FOOTBALL_KEY invalid or quota exceeded.")
            break
        if resp.status_code != 200:
            print(f"  [btts] api-football {resp.status_code} for {date_str}: {resp.text[:120]}")
            continue

        for entry in resp.json().get("response", []):
            fix = entry.get("fixture", {})
            home_raw = fix.get("teams", {}).get("home", {}).get("name", "")
            away_raw = fix.get("teams", {}).get("away", {}).get("name", "")

            yes_odds = no_odds = 0.0
            for bm in entry.get("bookmakers", []):
                for bet in bm.get("bets", []):
                    for val in bet.get("values", []):
                        if val.get("value") == "Yes":
                            yes_odds = float(val.get("odd", 0))
                        elif val.get("value") == "No":
                            no_odds = float(val.get("odd", 0))

            if yes_odds > 1.0 or no_odds > 1.0:
                # Store under both raw and canonical keys for flexible matching
                raw_key = f"{home_raw} vs {away_raw}"
                canon_key = f"{_norm(home_raw)} vs {_norm(away_raw)}"
                payload = {"yes": yes_odds, "no": no_odds}
                result[raw_key]   = payload
                result[canon_key] = payload

    if result:
        print(f"  [btts] Bet365 BTTS odds fetched: {len(result) // 2} match(es)")
    else:
        print("  [btts] No Bet365 BTTS odds available (check API_FOOTBALL_KEY or quota)")

    return result


def overlay_btts_odds(match_dict: dict, btts_map: dict[str, dict]) -> dict:
    """
    Adds btts_yes_odds / btts_no_odds to a match dict from TheOddsAPI.
    Matches by canonical team name, case-insensitive.
    """
    if not btts_map:
        return match_dict

    home = match_dict.get("home_team", "")
    away = match_dict.get("away_team", "")
    canon_key = f"{_norm(home)} vs {_norm(away)}"
    raw_key   = f"{home} vs {away}"

    entry = btts_map.get(raw_key) or btts_map.get(canon_key)
    if entry:
        match_dict = dict(match_dict)
        match_dict["btts_yes_odds"] = entry["yes"]
        match_dict["btts_no_odds"]  = entry["no"]

    return match_dict
