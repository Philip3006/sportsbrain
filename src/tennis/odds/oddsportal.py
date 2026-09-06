"""Agent 5 (Fallback) — OddsPortal.com Scraper für Tennis-H2H.

Wird nur aufgerufen, wenn Tennis-Explorer für ein Match nichts liefert.
OddsPortal aggregiert 30+ Bookies, ist aber Cloudflare-geschützt → wir
setzen einen realistischen User-Agent und akzeptieren, dass ~30% der Calls
mit 403 zurückkommen (dann None → nächster Tier greift).

Konservativ: keine per-match-Detail-Requests, sondern eine Tages-Übersicht
`/matches/tennis/YYYY-MM-DD/` (JSON-embedded, ein Bulk-Request pro Tag).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from src.tennis.name_norm import to_elo_name_from_odds_api
from src.tennis.odds.base import OddsQuote, ProviderOutcome, sanity_ok

_log = logging.getLogger("sportsbrain.tennis.odds.oddsportal")
name = "oddsportal"
tier = 2

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
_BULK: dict[str, list[dict]] = {}   # date_iso -> parsed matches
_TS: dict[str, float] = {}
_TTL_S = 15 * 60

# OddsPortal embedded JSON pattern: window.opgd = {...}
_RE_INLINE_JSON = re.compile(r'window\.opgd\s*=\s*(\{.*?\});', re.DOTALL)


def _fetch_day(date_iso: str) -> list[dict]:
    # Keep the established 500-match truncation contract in this public helper.
    return _fetch_day_with_diagnostics(date_iso)[0]


def _fetch_day_with_diagnostics(date_iso: str) -> tuple[list[dict], ProviderOutcome]:
    if date_iso in _BULK and time.time() - _TS.get(date_iso, 0) < _TTL_S:
        return _BULK[date_iso], ProviderOutcome(name, True, True, "success")

    url = f"https://www.oddsportal.com/matches/tennis/{date_iso}/"
    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=8)
        if resp.status_code != 200:
            _BULK[date_iso] = []
            _TS[date_iso] = time.time()
            return [], ProviderOutcome(name, True, True, "http_error", http_status=resp.status_code)
    except requests.Timeout:
        return [], ProviderOutcome(name, True, True, "timeout", error_class="Timeout")
    except requests.RequestException as exc:
        return [], ProviderOutcome(name, True, True, "exception", error_class=type(exc).__name__)

    # Fallback-Parser (Regex auf HTML): Zeilen mit Player-Vs-Player und 2 Odds.
    # OddsPortal versteckt Detail-Odds hinter JS, daher extrahieren wir nur
    # das minimale H2H aus der SSR-Tabelle.
    matches: list[dict] = []
    pattern = re.compile(
        r'<a[^>]*href="/tennis/[^"]*"[^>]*>([A-Z][^<]{2,40})\s*-\s*([A-Z][^<]{2,40})</a>'
        r'.*?(\d\.\d{2}).*?(\d\.\d{2})',
        re.DOTALL,
    )
    # J8-B5: Truncation-Limit von 200 → 500, plus Warn-Log wenn Grenze erreicht
    all_hits = list(pattern.finditer(resp.text))
    _OP_TRUNCATION_LIMIT = 500
    if len(all_hits) >= _OP_TRUNCATION_LIMIT:
        _log.warning("[oddsportal] %d matches auf %s — Truncation-Limit %d greift", len(all_hits), date_iso, _OP_TRUNCATION_LIMIT)
    for m in all_hits[:_OP_TRUNCATION_LIMIT]:
        try:
            pa = m.group(1).strip()
            pb = m.group(2).strip()
            a = float(m.group(3))
            b = float(m.group(4))
            if sanity_ok(a, b):
                matches.append({"player_a": pa, "player_b": pb, "h2h_a": a, "h2h_b": b})
        except Exception:
            continue

    _BULK[date_iso] = matches
    _TS[date_iso] = time.time()
    return matches, ProviderOutcome(name, True, True, "success")


def _match_key(n: str) -> str:
    return n.lower().replace(".", "").strip()


def fetch(match_hint: dict) -> Optional[OddsQuote]:
    pa = match_hint.get("player_a", "")
    pb = match_hint.get("player_b", "")
    if not pa or not pb:
        return None

    date_iso = ""
    ct = match_hint.get("commence_time", "")
    if ct:
        try:
            date_iso = datetime.fromisoformat(ct.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            pass
    if not date_iso:
        date_iso = datetime.utcnow().strftime("%Y-%m-%d")

    pa_key = _match_key(to_elo_name_from_odds_api(pa))
    pb_key = _match_key(to_elo_name_from_odds_api(pb))

    for m in _fetch_day(date_iso):
        mpa = _match_key(to_elo_name_from_odds_api(m["player_a"]))
        mpb = _match_key(to_elo_name_from_odds_api(m["player_b"]))
        forward = (mpa == pa_key and mpb == pb_key)
        reverse = (mpa == pb_key and mpb == pa_key)
        if not (forward or reverse):
            continue
        a, b = m["h2h_a"], m["h2h_b"]
        if reverse:
            a, b = b, a
        return OddsQuote(
            player_a=pa,
            player_b=pb,
            h2h_a=a,
            h2h_b=b,
            source="oddsportal",
            source_tier=2,
            bookmaker="consensus",
            confidence=0.7,
            bookies_count=5,
        )
    return None


def fetch_with_diagnostics(match_hint: dict) -> tuple[OddsQuote | None, ProviderOutcome]:
    pa = match_hint.get("player_a", "")
    pb = match_hint.get("player_b", "")
    if not pa or not pb:
        return None, ProviderOutcome(name, False, False, "ineligible")
    date_iso = ""
    ct = match_hint.get("commence_time", "")
    if ct:
        try:
            date_iso = datetime.fromisoformat(ct.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            pass
    date_iso = date_iso or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    matches, outcome = _fetch_day_with_diagnostics(date_iso)
    if outcome.status_class != "success":
        return None, outcome
    pa_key, pb_key = _match_key(to_elo_name_from_odds_api(pa)), _match_key(to_elo_name_from_odds_api(pb))
    for match in matches:
        forward = (_match_key(to_elo_name_from_odds_api(match["player_a"])) == pa_key and _match_key(to_elo_name_from_odds_api(match["player_b"])) == pb_key)
        reverse = (_match_key(to_elo_name_from_odds_api(match["player_a"])) == pb_key and _match_key(to_elo_name_from_odds_api(match["player_b"])) == pa_key)
        if forward or reverse:
            a, b = match["h2h_a"], match["h2h_b"]
            if reverse:
                a, b = b, a
            quote = OddsQuote(pa, pb, a, b, name, tier, bookmaker="consensus", confidence=0.7, bookies_count=5)
            if not quote.sane():
                return quote, ProviderOutcome(name, True, True, "invalid_quote")
            return quote, ProviderOutcome(name, True, True, "success", result="usable_quote")
    return None, ProviderOutcome(name, True, True, "no_match")
