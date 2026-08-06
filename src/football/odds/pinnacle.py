"""Tier 1 — Pinnacle Guest-API für Football (CLV-Ground-Truth).

Analog zu src/tennis/odds/pinnacle.py — nutzt denselben öffentlichen
Arcadia-Endpoint ohne Auth.

Football sportId = 29. Bundesliga 2 wird unter den deutschen Ligen gelistet.
Liefert 1X2 (moneyline) + AH (spread) pro Match.

Bulk-Strategie:
  1. /leagues?sportId=29 → aktive Football-Ligen filtern auf "Bundesliga 2"
  2. /leagues/{id}/matchups → Matches je Liga
  3. /matchups/{id}/markets/related/straight → 1X2 + AH
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import sys
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_ROOT))

from scripts._http_retry import retry_request

from src.football.odds.base import (
    FootballOddsQuote, ThreadSafeCache, ThreadSafeDictCache,
    canonical_team, sanity_1x2,
)

name = "pinnacle"
tier = 1

_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
_SPORT_ID_FOOTBALL = 29
_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
    "Accept": "application/json",
    "X-API-Key": "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R",
    "Referer": "https://www.pinnacle.com/",
}

_LEAGUES_TTL_S = 10 * 60
_MATCHUPS_TTL_S = 5 * 60
_ODDS_TTL_S = 5 * 60

# Filter: nur diese Schlüsselwörter als Bundesliga-2-Ligen akzeptieren
_BL2_KEYWORDS = ("bundesliga 2", "2. bundesliga", "2. fussball")

_leagues: ThreadSafeCache[list[dict]] = ThreadSafeCache(ttl=_LEAGUES_TTL_S)
_matchups: ThreadSafeDictCache[list[dict]] = ThreadSafeDictCache(ttl=_MATCHUPS_TTL_S)
_odds: ThreadSafeDictCache[dict] = ThreadSafeDictCache(ttl=_ODDS_TTL_S)


def _get_json(url: str) -> Any:
    try:
        r = retry_request(
            "GET", url, headers=_UA, timeout=8,
            retries=3, backoff=(2, 5, 15),
            retry_on_status={429, 502, 503, 504},
            log_prefix="[pinnacle_football]",
        )
        if r is None or r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _refresh_leagues(sport_key: str = "") -> list[dict]:
    cached = _leagues.get()
    if cached is not None:
        return cached
    data = _get_json(f"{_BASE}/leagues?sportId={_SPORT_ID_FOOTBALL}")
    if isinstance(data, list) and data:
        _leagues.set(data)
        return data
    return _leagues.get() or []


def _bl2_league_ids(sport_key: str) -> list[int]:
    """Gibt Pinnacle-Liga-IDs zurück die Bundesliga-2 entsprechen."""
    all_leagues = _refresh_leagues(sport_key)
    ids: list[int] = []
    for lg in all_leagues:
        n = lg.get("name", "").lower()
        if any(kw in n for kw in _BL2_KEYWORDS):
            lid = lg.get("id")
            if lid:
                ids.append(int(lid))
    return ids


def _refresh_matchups(league_id: int) -> list[dict]:
    cached = _matchups.get(league_id)
    if cached is not None:
        return cached
    data = _get_json(f"{_BASE}/leagues/{league_id}/matchups")
    if isinstance(data, list):
        _matchups.set(league_id, data)
        return data
    return []


def _fetch_odds_for_matchup(matchup_id: int) -> Optional[dict]:
    cached = _odds.get(matchup_id)
    if cached is not None:
        return cached
    data = _get_json(f"{_BASE}/matchups/{matchup_id}/markets/related/straight")
    if not isinstance(data, list):
        return None

    h2h_home = h2h_draw = h2h_away = 0.0
    ah_home = ah_away = 0.0
    ah_line = 0.0

    for m in data:
        mtype = m.get("type", "")
        prices = m.get("prices") or []
        if mtype == "moneyline":
            for p in prices:
                des = p.get("designation")
                price = _american_to_decimal(p.get("price"))
                if des == "home":
                    h2h_home = price
                elif des == "away":
                    h2h_away = price
                elif des == "draw":
                    h2h_draw = price
        elif mtype == "spread":
            points = m.get("points")
            if points is None:
                continue
            for p in prices:
                des = p.get("designation")
                price = _american_to_decimal(p.get("price"))
                if des == "home" and float(points) < 0:
                    ah_home = price
                    ah_line = float(points)
                elif des == "away" and float(points) > 0:
                    ah_away = price

    if h2h_home <= 0 or h2h_away <= 0:
        return None

    out = {
        "h2h_home": h2h_home, "h2h_draw": h2h_draw, "h2h_away": h2h_away,
        "ah_line": ah_line, "ah_home": ah_home, "ah_away": ah_away,
    }
    _odds.set(matchup_id, out)
    return out


def _american_to_decimal(price) -> float:
    try:
        v = float(price)
    except (TypeError, ValueError):
        return 0.0
    if v >= 100:
        return round(1.0 + v / 100.0, 3)
    if v <= -100:
        return round(1.0 + 100.0 / abs(v), 3)
    return 0.0


def fetch(match_hint: dict) -> Optional[FootballOddsQuote]:
    home_raw = match_hint.get("home_team", "")
    away_raw = match_hint.get("away_team", "")
    sport_key = match_hint.get("sport_key", "soccer_germany_bundesliga2")
    if not home_raw or not away_raw:
        return None

    home_key = canonical_team(home_raw)
    away_key = canonical_team(away_raw)

    league_ids = _bl2_league_ids(sport_key)
    if not league_ids:
        return None

    for lid in league_ids:
        for mu in _refresh_matchups(lid):
            parts = mu.get("participants") or []
            if len(parts) != 2:
                continue
            mh = canonical_team(parts[0].get("name", ""))
            ma = canonical_team(parts[1].get("name", ""))
            forward = (mh == home_key and ma == away_key)
            reverse = (mh == away_key and ma == home_key)
            if not (forward or reverse):
                continue
            mid = mu.get("id")
            if not mid:
                continue
            odds = _fetch_odds_for_matchup(int(mid))
            if not odds:
                return None
            if reverse:
                odds = {
                    "h2h_home": odds["h2h_away"], "h2h_draw": odds["h2h_draw"],
                    "h2h_away": odds["h2h_home"],
                    "ah_line": -odds["ah_line"], "ah_home": odds["ah_away"],
                    "ah_away": odds["ah_home"],
                }
            if not sanity_1x2(odds["h2h_home"], odds["h2h_draw"], odds["h2h_away"]):
                return None
            q = FootballOddsQuote(
                home_team=home_raw,
                away_team=away_raw,
                source="pinnacle",
                source_tier=tier,
                h2h_home=odds["h2h_home"],
                h2h_draw=odds["h2h_draw"],
                h2h_away=odds["h2h_away"],
                ah_line=odds["ah_line"],
                ah_home=odds["ah_home"],
                ah_away=odds["ah_away"],
                bookmaker="pinnacle",
                bookies_count_1x2=1,
                confidence=0.98,
            )
            time.sleep(0.1)
            return q
    return None
