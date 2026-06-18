"""
Fetches BTTS (Both Teams to Score) odds from Bet365 via Sofascore (RapidAPI).

Setup:
1. Du hast bereits einen RapidAPI-Key (API_FOOTBALL_KEY in .env)
2. Auf RapidAPI nach "Sofascore" (by Api Dojo) suchen → Free subscriben
3. Gleicher Key funktioniert für beide APIs

Sofascore Endpoints:
  - /api/v1/sport/football/scheduled-events/{date}  → Match-IDs für ein Datum
  - /api/v1/event/{id}/odds/1/featured              → Odds incl. BTTS von Bet365
"""
import os
from datetime import datetime, timezone

import requests

from src.data.cache import disk_cache
from src.config import canonical_name

_HOST_SOFASCORE = "sofascore.p.rapidapi.com"
_BET365_ID = 16   # Bet365 bookmaker ID in Sofascore
_BTTS_MARKET = "Both teams to score"


def _api_key() -> str | None:
    return os.getenv("API_FOOTBALL_KEY")


def _norm(name: str) -> str:
    try:
        return canonical_name(name).lower().strip()
    except Exception:
        return name.lower().strip()


def _get_sofascore_events(date_str: str, headers: dict) -> list[dict]:
    """Returns upcoming football events from Sofascore for a given date."""
    url = f"https://{_HOST_SOFASCORE}/api/v1/sport/football/scheduled-events/{date_str}"
    resp = requests.get(url, headers=headers, timeout=12)
    if resp.status_code == 403:
        print("  [btts] Sofascore nicht abonniert — auf RapidAPI 'Sofascore' (Api Dojo) gratis subscriben.")
        return []
    if resp.status_code != 200:
        return []
    return resp.json().get("events", [])


def _get_btts_odds(event_id: int, headers: dict) -> tuple[float, float]:
    """Returns (yes_odds, no_odds) from Bet365 for a Sofascore event."""
    url = f"https://{_HOST_SOFASCORE}/api/v1/event/{event_id}/odds/1/featured"
    resp = requests.get(url, headers=headers, timeout=12)
    if resp.status_code != 200:
        return 0.0, 0.0

    yes_odds = no_odds = 0.0
    for market in resp.json().get("featured", {}).get("markets", []):
        if "both" not in market.get("marketName", "").lower():
            continue
        for choice in market.get("choices", []):
            bm = choice.get("bookmakers", [])
            bet365 = next((b for b in bm if b.get("id") == _BET365_ID), None)
            if not bet365:
                bet365 = bm[0] if bm else None  # fallback to any bookmaker
            if not bet365:
                continue
            name = choice.get("name", "").lower()
            odds = float(bet365.get("fractionalValue", 0) or bet365.get("decimalValue", 0) or 0)
            if odds <= 1.0:
                continue
            if name in ("yes", "ja"):
                yes_odds = odds
            elif name in ("no", "nein"):
                no_odds = odds

    return yes_odds, no_odds


@disk_cache("btts_odds_sofascore", max_age_hours=4.0)
def fetch_btts_odds(matches: list[dict] | None = None, force: bool = False) -> dict[str, dict]:
    """
    Returns {match_key: {"yes": float, "no": float}} for upcoming WM2026 matches.
    Uses Sofascore API (RapidAPI) — needs subscription (free tier, same key).
    Returns {} silently if key not set or not subscribed.
    """
    key = _api_key()
    if not key:
        return {}

    headers = {
        "x-rapidapi-key": key,
        "x-rapidapi-host": _HOST_SOFASCORE,
    }

    # Collect unique dates from match list
    dates: set[str] = set()
    if matches:
        for m in matches:
            ct = m.get("commence_time", "")
            if ct:
                try:
                    d = datetime.fromisoformat(ct.replace("Z", "+00:00")).strftime("%Y-%m-%d")
                    dates.add(d)
                except ValueError:
                    pass
    if not dates:
        dates = {datetime.now(timezone.utc).strftime("%Y-%m-%d")}

    # Build a lookup: canonical_match_key → commence_time (from TheOddsAPI matches)
    match_lookup: dict[str, str] = {}
    if matches:
        for m in matches:
            h = _norm(m.get("home_team", ""))
            a = _norm(m.get("away_team", ""))
            match_lookup[f"{h} vs {a}"] = m.get("commence_time", "")

    result: dict[str, dict] = {}

    for date_str in sorted(dates):
        events = _get_sofascore_events(date_str, headers)
        if not events:
            continue

        for ev in events:
            ht = ev.get("homeTeam", {}).get("name", "")
            at = ev.get("awayTeam", {}).get("name", "")
            canon_key = f"{_norm(ht)} vs {_norm(at)}"

            # Only fetch odds for matches the scanner is interested in
            if match_lookup and canon_key not in match_lookup:
                continue

            event_id = ev.get("id")
            if not event_id:
                continue

            yes_odds, no_odds = _get_btts_odds(event_id, headers)
            if yes_odds > 1.0 or no_odds > 1.0:
                payload = {"yes": yes_odds, "no": no_odds}
                result[f"{ht} vs {at}"] = payload
                result[canon_key] = payload

    if result:
        print(f"  [btts] Bet365 BTTS odds (Sofascore): {len(result) // 2} match(es)")
    else:
        print("  [btts] Keine BTTS-Odds — Sofascore auf RapidAPI gratis subscriben (gleicher Key)")

    return result


def overlay_btts_odds(match_dict: dict, btts_map: dict[str, dict]) -> dict:
    """Adds btts_yes_odds / btts_no_odds to a match dict from TheOddsAPI."""
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
