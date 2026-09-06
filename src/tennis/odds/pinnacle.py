"""J8-I6 — Pinnacle Guest-API als Tier-1 Odds-Source.

Endpoint-Familie: `guest.api.arcadia.pinnacle.com/0.1/*` (public JSON, keine Auth
nötig — dieselbe API die pinnacle.com-Frontend gegen ihren public-CDN feuert).

Bulk-Strategie:
  1. `/leagues?sportId=33` → alle aktiven Tennis-Ligen (Grand Slams, Masters,
     ATP/WTA-Serien, Challenger).
  2. `/leagues/{leagueId}/matchups` → alle Matches je Liga (name, participants,
     matchup_id, starts).
  3. `/matchups/{matchupId}/markets/related/straight` → moneyline + spread
     (Set-AH ±1.5).

Cache: 10 Min für League-List, 5 Min für Matchup+Odds (dieselben Werte wie
Betfair). Timeouts konservativ (8s), retry via zentralem `_http_retry`.

Rate-Limit: Pinnacle blockiert idR bei > 30 req/min pro IP. Bulk-Fetch mit
`time.sleep(0.2)` zwischen Match-Odds-Calls hält uns bei ~10-15 Matches/Min.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import sys
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_ROOT))
from scripts._http_retry import retry_request

from src.tennis.name_norm import to_elo_name_from_odds_api
from src.tennis.odds.base import OddsQuote, ProviderOutcome, ThreadSafeCache, ThreadSafeDictCache, sanity_ok


name = "pinnacle"
tier = 1

_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
_SPORT_ID_TENNIS = 33
_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
    "Accept": "application/json",
    "X-API-Key": "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R",  # public frontend-key
    "Referer": "https://www.pinnacle.com/",
}

_LEAGUES_TTL_S = 10 * 60
_MATCHUPS_TTL_S = 5 * 60
_ODDS_TTL_S = 5 * 60

_leagues: ThreadSafeCache[list[dict]] = ThreadSafeCache(ttl=_LEAGUES_TTL_S)
_matchups: ThreadSafeDictCache[list[dict]] = ThreadSafeDictCache(ttl=_MATCHUPS_TTL_S)
_odds: ThreadSafeDictCache[dict] = ThreadSafeDictCache(ttl=_ODDS_TTL_S)
_last_outcome: ProviderOutcome | None = None


def _get_json(url: str) -> Any:
    global _last_outcome
    try:
        r = retry_request(
            "GET", url, headers=_UA, timeout=8,
            retries=3, backoff=(2, 5, 15),
            retry_on_status={429, 502, 503, 504},
            log_prefix="[pinnacle]",
        )
        if r is None or r.status_code != 200:
            _last_outcome = ProviderOutcome(name, True, True, "http_error", http_status=getattr(r, "status_code", None))
            return None
        return r.json()
    except TimeoutError:
        _last_outcome = ProviderOutcome(name, True, True, "timeout", error_class="Timeout")
        return None
    except Exception as exc:
        _last_outcome = ProviderOutcome(name, True, True, "exception", error_class=type(exc).__name__)
        return None


def _refresh_leagues() -> list[dict]:
    cached = _leagues.get()
    if cached is not None:
        return cached
    data = _get_json(f"{_BASE}/leagues?sportId={_SPORT_ID_TENNIS}")
    if isinstance(data, list) and data:
        _leagues.set(data)
        return data
    return _leagues.get() or []


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
    """Parses moneyline + spread markets for a single matchup.

    Return schema:
      {
        "player_a": str, "player_b": str,
        "h2h_a": float, "h2h_b": float,
        "ah_1_5_a": float | None, "ah_1_5_b": float | None,
      }
    """
    cached = _odds.get(matchup_id)
    if cached is not None:
        return cached
    data = _get_json(f"{_BASE}/matchups/{matchup_id}/markets/related/straight")
    if not isinstance(data, list):
        return None
    # Pinnacle markets-JSON: liste von {type, key, prices:[{designation, price}, ...]}
    h2h_a = h2h_b = 0.0
    ah_a = ah_b = 0.0
    for m in data:
        mtype = m.get("type", "")
        prices = m.get("prices") or []
        if mtype == "moneyline":
            for p in prices:
                des = p.get("designation")
                price = p.get("price")
                if des == "home" and price:
                    h2h_a = _american_to_decimal(price)
                elif des == "away" and price:
                    h2h_b = _american_to_decimal(price)
        elif mtype == "spread":
            # Set-Handicap: points = ±1.5
            points = m.get("points")
            if points is None:
                continue
            for p in prices:
                des = p.get("designation")
                price = p.get("price")
                if not price:
                    continue
                if des == "home" and points == -1.5:
                    ah_a = _american_to_decimal(price)
                elif des == "away" and points == 1.5:
                    ah_b = _american_to_decimal(price)
    if h2h_a <= 0 or h2h_b <= 0:
        return None
    out = {
        "h2h_a": h2h_a, "h2h_b": h2h_b,
        "ah_1_5_a": ah_a or None, "ah_1_5_b": ah_b or None,
    }
    _odds.set(matchup_id, out)
    return out


def _american_to_decimal(price: int | float) -> float:
    """Pinnacle liefert American-Odds (+150 / -180). Konvertiert zu Decimal."""
    try:
        v = float(price)
    except (TypeError, ValueError):
        return 0.0
    if v >= 100:
        return round(1.0 + v / 100.0, 3)
    if v <= -100:
        return round(1.0 + 100.0 / abs(v), 3)
    return 0.0


def _match_key(n: str) -> str:
    return to_elo_name_from_odds_api(n).lower().replace(".", "").strip()


def _find_matchup(pa: str, pb: str) -> Optional[tuple[dict, dict]]:
    """Return (matchup, odds_dict) if found, else None."""
    pa_key = _match_key(pa)
    pb_key = _match_key(pb)
    for league in _refresh_leagues():
        lid = league.get("id")
        if not lid:
            continue
        for mu in _refresh_matchups(lid):
            parts = mu.get("participants") or []
            if len(parts) != 2:
                continue
            mpa = _match_key(parts[0].get("name", ""))
            mpb = _match_key(parts[1].get("name", ""))
            forward = (mpa == pa_key and mpb == pb_key)
            reverse = (mpa == pb_key and mpb == pa_key)
            if not (forward or reverse):
                continue
            mid = mu.get("id")
            if not mid:
                continue
            odds = _fetch_odds_for_matchup(mid)
            if not odds:
                return None
            if reverse:
                odds = {
                    "h2h_a": odds["h2h_b"], "h2h_b": odds["h2h_a"],
                    "ah_1_5_a": odds.get("ah_1_5_b"), "ah_1_5_b": odds.get("ah_1_5_a"),
                }
            return mu, odds
    return None


def fetch(match_hint: dict) -> Optional[OddsQuote]:
    pa = match_hint.get("player_a", "")
    pb = match_hint.get("player_b", "")
    if not pa or not pb:
        return None
    found = _find_matchup(pa, pb)
    if not found:
        return None
    _mu, odds = found
    if not sanity_ok(odds["h2h_a"], odds["h2h_b"]):
        return None
    q = OddsQuote(
        player_a=pa,
        player_b=pb,
        h2h_a=odds["h2h_a"],
        h2h_b=odds["h2h_b"],
        source="pinnacle",
        source_tier=1,
        bookmaker="pinnacle",
        confidence=0.98,
        bookies_count=1,
    )
    # Pinnacle liefert AH-Slot mit — Merger/Detector können darauf zugreifen
    # via getattr()-Extension (nicht Teil des minimalen OddsQuote-Kontrakts).
    q.__dict__["ah_1_5_a"] = odds.get("ah_1_5_a")
    q.__dict__["ah_1_5_b"] = odds.get("ah_1_5_b")
    return q


def fetch_with_diagnostics(match_hint: dict) -> tuple[OddsQuote | None, ProviderOutcome]:
    global _last_outcome
    _last_outcome = None
    quote = fetch(match_hint)
    if quote and quote.sane():
        return quote, ProviderOutcome(name, True, True, "success", result="usable_quote")
    return quote, _last_outcome or ProviderOutcome(name, True, True, "invalid_quote" if quote else "no_match")
