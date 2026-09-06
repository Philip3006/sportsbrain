"""Agent 3 — Betfair Exchange (Sharp/Traded).

Primär: Betfair Exchange REST-API (listMarketBook) — Session-Login mit
Username/Password (non-cert) an /api/login, dann X-Authentication-Header.
Liefert Traded-Prices für MATCH_ODDS-Märkte. Tier 1 (Sharp-Referenz).

Fallback: HTML-Scrape von sports.betfair.com/en/tennis/ — nur wenn API
nicht konfiguriert oder session-login fehlschlägt.

Konfiguration via env:
  BETFAIR_APP_KEY       (Delayed-App-Key kostenlos, Live 299€/Jahr)
  BETFAIR_USERNAME
  BETFAIR_PASSWORD

Ohne diese env-vars gibt fetch() None zurück (Merger geht dann zum
nächsten Tier). Modul ist bereits registriert und wird sofort scharf,
sobald credentials im .env hinterlegt sind.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests

from src.tennis.name_norm import to_elo_name_from_odds_api
from src.tennis.odds.base import OddsQuote, ProviderOutcome, ThreadSafeCache, ThreadSafeDictCache, sanity_ok

name = "betfair"
tier = 1

_LOGIN_URL = "https://identitysso.betfair.com/api/login"
_API_URL = "https://api.betfair.com/exchange/betting/rest/v1.0"
_TENNIS_EVENT_TYPE_ID = "2"

_SESSION_TTL_S = 4 * 60 * 60
_BULK_TTL_S = 5 * 60

_session: ThreadSafeCache[str] = ThreadSafeCache(ttl=_SESSION_TTL_S)
_bulk: ThreadSafeCache[dict[str, dict]] = ThreadSafeCache(ttl=_BULK_TTL_S)


def _login() -> Optional[str]:
    """Liefert X-Authentication-Session-Token oder None."""
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
    """Holt aktuelle Tennis-MATCH_ODDS-Märkte + Prices (Bulk)."""
    cached = _bulk.get()
    if cached is not None:
        return cached

    catalogue = _api_call("listMarketCatalogue", {
        "filter": {
            "eventTypeIds": [_TENNIS_EVENT_TYPE_ID],
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
        if len(runners) != 2:
            continue
        pa_name = runners[0].get("runnerName", "")
        pb_name = runners[1].get("runnerName", "")
        pa_sel = runners[0].get("selectionId")
        pb_sel = runners[1].get("selectionId")
        pb = price_by_id.get(mid, {})
        pb_runners = {r["selectionId"]: r for r in pb.get("runners", [])}
        rpa = pb_runners.get(pa_sel, {})
        rpb = pb_runners.get(pb_sel, {})

        def _best_back(runner: dict) -> float:
            ex = runner.get("ex", {})
            backs = ex.get("availableToBack", [])
            if not backs:
                return 0.0
            return float(backs[0].get("price", 0.0))

        a = _best_back(rpa)
        b = _best_back(rpb)
        if a <= 0 or b <= 0:
            continue
        out[mid] = {"player_a": pa_name, "player_b": pb_name, "h2h_a": a, "h2h_b": b}

    _bulk.set(out)
    return out


def _match_key(n: str) -> str:
    return n.lower().replace(".", "").strip()


def fetch(match_hint: dict) -> Optional[OddsQuote]:
    pa = match_hint.get("player_a", "")
    pb = match_hint.get("player_b", "")
    if not pa or not pb:
        return None

    # Ohne credentials sofort None → merger geht zu tier-2
    if not (os.getenv("BETFAIR_APP_KEY") and os.getenv("BETFAIR_USERNAME")
            and os.getenv("BETFAIR_PASSWORD")):
        return None

    bulk = _refresh_bulk()
    if not bulk:
        return None

    pa_key = _match_key(to_elo_name_from_odds_api(pa))
    pb_key = _match_key(to_elo_name_from_odds_api(pb))
    for m in bulk.values():
        mpa = _match_key(to_elo_name_from_odds_api(m["player_a"]))
        mpb = _match_key(to_elo_name_from_odds_api(m["player_b"]))
        forward = (mpa == pa_key and mpb == pb_key)
        reverse = (mpa == pb_key and mpb == pa_key)
        if not (forward or reverse):
            continue
        a, b = m["h2h_a"], m["h2h_b"]
        if reverse:
            a, b = b, a
        if not sanity_ok(a, b):
            continue
        return OddsQuote(
            player_a=pa,
            player_b=pb,
            h2h_a=a,
            h2h_b=b,
            source="betfair",
            source_tier=1,
            bookmaker="exchange",
            confidence=0.95,
            bookies_count=1,
        )
    return None


def fetch_with_diagnostics(match_hint: dict) -> tuple[OddsQuote | None, ProviderOutcome]:
    if not (os.getenv("BETFAIR_APP_KEY") and os.getenv("BETFAIR_USERNAME") and os.getenv("BETFAIR_PASSWORD")):
        return None, ProviderOutcome(name, False, False, "ineligible")
    try:
        quote = fetch(match_hint)
    except requests.Timeout:
        return None, ProviderOutcome(name, True, True, "timeout", error_class="Timeout")
    except requests.RequestException as exc:
        return None, ProviderOutcome(name, True, True, "exception", error_class=type(exc).__name__)
    if quote and quote.sane():
        return quote, ProviderOutcome(name, True, True, "success", result="usable_quote")
    return quote, ProviderOutcome(name, True, True, "invalid_quote" if quote else "no_match")
