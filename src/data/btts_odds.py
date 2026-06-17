"""
Fetches Bet365 football odds from Sofascore (RapidAPI).

Setup:
1. Du hast bereits einen RapidAPI-Key (API_FOOTBALL_KEY in .env)
2. Auf RapidAPI nach "Sofascore" (by Api Dojo) suchen → Free subscriben
3. Gleicher Key funktioniert für beide APIs

Sofascore Endpoints:
  - /api/v1/sport/football/scheduled-events/{date}  → Match-IDs für ein Datum
  - /api/v1/event/{id}/odds/1/featured              → Odds incl. BTTS/player props
"""
import os
from datetime import datetime, timezone

import requests

from src.data.cache import disk_cache
from src.config import canonical_name

_HOST_SOFASCORE = "sofascore.p.rapidapi.com"
_BET365_ID = 16   # Bet365 bookmaker ID in Sofascore
_BTTS_MARKET = "Both teams to score"
_SCORER_BLOCKLIST = ("first", "last", "team", "brace", "hat trick", "assist")


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


def _decimal_from_value(value) -> float:
    """Accept decimal or fractional strings from Sofascore bookmaker rows."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, str) and "/" in value:
        try:
            num, den = value.split("/", 1)
            return float(num) / float(den) + 1.0
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bookmaker_odds(row: dict) -> float:
    """Return decimal odds from one Sofascore bookmaker odds row."""
    return (
        _decimal_from_value(row.get("decimalValue"))
        or _decimal_from_value(row.get("fractionalValue"))
        or _decimal_from_value(row.get("value"))
    )


def _is_bet365_bookmaker(row: dict) -> bool:
    try:
        return int(row.get("id")) == _BET365_ID
    except (TypeError, ValueError):
        return False


def _choice_bet365_odds(choice: dict, fallback_any: bool = False) -> float:
    bookmakers = choice.get("bookmakers", []) or []
    bet365 = next((b for b in bookmakers if _is_bet365_bookmaker(b)), None)
    if not bet365 and fallback_any:
        bet365 = bookmakers[0] if bookmakers else None
    return _bookmaker_odds(bet365 or {})


def _choice_name(choice: dict) -> str:
    for key in ("name", "sourceName", "label", "participantName"):
        value = choice.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    participant = choice.get("participant") or choice.get("player") or {}
    if isinstance(participant, dict):
        for key in ("name", "shortName", "displayName"):
            value = participant.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _is_anytime_goalscorer_market(name: str) -> bool:
    n = (name or "").lower()
    if any(blocked in n for blocked in _SCORER_BLOCKLIST):
        return False
    return (
        ("anytime" in n and ("goalscorer" in n or "goal scorer" in n or "to score" in n))
        or n in {"anytime goalscorer", "anytime goal scorer"}
    )


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
            odds = _choice_bet365_odds(choice, fallback_any=True)
            name = choice.get("name", "").lower()
            if odds <= 1.0:
                continue
            if name in ("yes", "ja"):
                yes_odds = odds
            elif name in ("no", "nein"):
                no_odds = odds

    return yes_odds, no_odds


def _get_bet365_goalscorer_odds(event_id: int, headers: dict) -> dict[str, float]:
    """Returns Bet365 anytime goalscorer odds for one Sofascore event."""
    url = f"https://{_HOST_SOFASCORE}/api/v1/event/{event_id}/odds/1/featured"
    resp = requests.get(url, headers=headers, timeout=12)
    if resp.status_code != 200:
        return {}

    props: dict[str, float] = {}
    for market in resp.json().get("featured", {}).get("markets", []):
        if not _is_anytime_goalscorer_market(market.get("marketName", "")):
            continue
        for choice in market.get("choices", []) or []:
            name = _choice_name(choice)
            odds = _choice_bet365_odds(choice, fallback_any=False)
            if name and odds > 1.0:
                props[name] = max(odds, props.get(name, 0.0))
    return props


def _match_dates(matches: list[dict] | None) -> set[str]:
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
    return dates or {datetime.now(timezone.utc).strftime("%Y-%m-%d")}


def _match_lookup(matches: list[dict] | None) -> dict[str, str]:
    lookup: dict[str, str] = {}
    if matches:
        for m in matches:
            h = _norm(m.get("home_team", ""))
            a = _norm(m.get("away_team", ""))
            lookup[f"{h} vs {a}"] = m.get("commence_time", "")
    return lookup


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

    dates = _match_dates(matches)
    match_lookup = _match_lookup(matches)

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


@disk_cache("bet365_goalscorer_odds_sofascore", max_age_hours=4.0)
def fetch_bet365_goalscorer_odds(
    matches: list[dict] | None = None,
    force: bool = False,
) -> dict[str, dict[str, float]]:
    """
    Returns {match_key: {player_name: anytime_goalscorer_odds}} from Bet365 only.
    If Bet365 has no player prop for a match, that match is omitted.
    """
    key = _api_key()
    if not key:
        return {}

    headers = {
        "x-rapidapi-key": key,
        "x-rapidapi-host": _HOST_SOFASCORE,
    }
    dates = _match_dates(matches)
    match_lookup = _match_lookup(matches)
    result: dict[str, dict[str, float]] = {}

    for date_str in sorted(dates):
        events = _get_sofascore_events(date_str, headers)
        if not events:
            continue
        for ev in events:
            ht = ev.get("homeTeam", {}).get("name", "")
            at = ev.get("awayTeam", {}).get("name", "")
            canon_key = f"{_norm(ht)} vs {_norm(at)}"
            if match_lookup and canon_key not in match_lookup:
                continue
            event_id = ev.get("id")
            if not event_id:
                continue
            props = _get_bet365_goalscorer_odds(event_id, headers)
            if props:
                result[f"{ht} vs {at}"] = props
                result[canon_key] = props

    if result:
        print(f"  [scorer] Bet365 anytime scorer odds (Sofascore): {len(result) // 2} match(es)")
    else:
        print("  [scorer] Keine Bet365-Torschützenquoten via Sofascore.")
    return result


def match_bet365_goalscorer_odds(
    match_dict: dict,
    scorer_map: dict[str, dict[str, float]],
) -> dict[str, float]:
    """Return Bet365 scorer props for a match dict from fetch_upcoming_matches()."""
    if not scorer_map:
        return {}
    home = match_dict.get("home_team", "")
    away = match_dict.get("away_team", "")
    raw_key = f"{home} vs {away}"
    canon_key = f"{_norm(home)} vs {_norm(away)}"
    return scorer_map.get(raw_key) or scorer_map.get(canon_key) or {}


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
