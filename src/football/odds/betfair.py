"""Tier 1 — Betfair Exchange (Sharp H2H-Referenz für Football).

Football event type ID = 1. Holt MATCH_ODDS-Märkte (3-way: Home/Draw/Away).
Identische Session-Login-Strategie wie src/tennis/odds/betfair.py.

Konfiguration via env:
    BETFAIR_APP_KEY, BETFAIR_USERNAME, BETFAIR_PASSWORD
Ohne diese env-vars: fetch() → None (Merger geht zu Tier 2).
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests

from src.football.odds.base import (
    FootballOddsQuote, ThreadSafeCache, ThreadSafeDictCache,
    canonical_team, sanity_1x2,
)

name = "betfair"
tier = 1

_LOGIN_URL = "https://identitysso.betfair.com/api/login"
_API_URL = "https://api.betfair.com/exchange/betting/rest/v1.0"
_FOOTBALL_EVENT_TYPE_ID = "1"

_SESSION_TTL_S = 4 * 60 * 60
_BULK_TTL_S = 5 * 60

_session: ThreadSafeCache[str] = ThreadSafeCache(ttl=_SESSION_TTL_S)
_bulk: ThreadSafeCache[dict[str, dict]] = ThreadSafeCache(ttl=_BULK_TTL_S)


def _login() -> Optional[str]:
    cached = _session.get()
    if cached:
        return cached
    app_key = os.getenv("BETFAIR_APP_KEY", "")
    user = os.getenv("BETFAIR_USERNAME", "")
    pwd = os.getenv("BETFAIR_PASSWORD", "")
    if not (app_key and user and pwd):
        return None
    try:
        r = requests.post(
            _LOGIN_URL,
            data={"username": user, "password": pwd},
            headers={
                "X-Application": app_key,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return None
        js = r.json()
        if js.get("status") != "SUCCESS":
            return None
        token = js.get("token", "")
        _session.set(token)
        return token
    except Exception:
        return None


def _api_call(endpoint: str, payload: dict) -> Optional[list]:
    token = _login()
    if not token:
        return None
    app_key = os.getenv("BETFAIR_APP_KEY", "")
    try:
        r = requests.post(
            f"{_API_URL}/{endpoint}/",
            json=payload,
            headers={
                "X-Application": app_key,
                "X-Authentication": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _refresh_bulk() -> dict[str, dict]:
    """Holt aktuelle Football-MATCH_ODDS-Märkte aus Betfair Exchange (Bulk)."""
    cached = _bulk.get()
    if cached is not None:
        return cached

    catalogue = _api_call("listMarketCatalogue", {
        "filter": {
            "eventTypeIds": [_FOOTBALL_EVENT_TYPE_ID],
            "marketTypeCodes": ["MATCH_ODDS"],
            "inPlayOnly": False,
        },
        "maxResults": "200",
        "marketProjection": ["EVENT", "RUNNER_DESCRIPTION"],
    })
    if not catalogue:
        _bulk.set({})
        return {}

    market_ids = [m["marketId"] for m in catalogue if m.get("marketId")]
    if not market_ids:
        _bulk.set({})
        return {}

    prices = _api_call("listMarketBook", {
        "marketIds": market_ids,
        "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
    }) or []
    price_by_id = {p["marketId"]: p for p in prices if p.get("marketId")}

    out: dict[str, dict] = {}
    for market in catalogue:
        mid = market.get("marketId", "")
        runners = market.get("runners", [])
        if len(runners) != 3:
            continue
        names = [r.get("runnerName", "") for r in runners]
        sels = [r.get("selectionId") for r in runners]
        pb = price_by_id.get(mid, {})
        pb_runners = {r["selectionId"]: r for r in pb.get("runners", [])}

        def _best_back(sel_id) -> float:
            r = pb_runners.get(sel_id, {})
            backs = r.get("ex", {}).get("availableToBack", [])
            return float(backs[0].get("price", 0.0)) if backs else 0.0

        prices_by_name = {canonical_team(names[i]): _best_back(sels[i]) for i in range(3)}
        out[mid] = {"event_name": market.get("event", {}).get("name", ""), "prices": prices_by_name}

    _bulk.set(out)
    return out


def fetch(match_hint: dict) -> Optional[FootballOddsQuote]:
    home_raw = match_hint.get("home_team", "")
    away_raw = match_hint.get("away_team", "")
    if not home_raw or not away_raw:
        return None

    if not (os.getenv("BETFAIR_APP_KEY") and os.getenv("BETFAIR_USERNAME")
            and os.getenv("BETFAIR_PASSWORD")):
        return None

    bulk = _refresh_bulk()
    if not bulk:
        return None

    home_key = canonical_team(home_raw)
    away_key = canonical_team(away_raw)

    for m in bulk.values():
        prices = m.get("prices", {})
        keys = set(prices.keys())
        if home_key not in keys or away_key not in keys:
            continue
        draw_keys = keys - {home_key, away_key}
        if not draw_keys:
            continue
        draw_key = next(iter(draw_keys))
        h = prices.get(home_key, 0.0)
        d = prices.get(draw_key, 0.0)
        a = prices.get(away_key, 0.0)
        if not sanity_1x2(h, d, a):
            continue
        return FootballOddsQuote(
            home_team=home_raw,
            away_team=away_raw,
            source="betfair",
            source_tier=tier,
            h2h_home=h,
            h2h_draw=d,
            h2h_away=a,
            bookmaker="exchange",
            bookies_count_1x2=1,
            confidence=0.95,
        )
    return None
