"""Agent 5 (Primär) — Tennisexplorer.com als OddsSource.

Wrappt den bestehenden Bulk-Scraper src.data.tennis_secondary_odds um das
OddsSource-Interface. Bulk-Cache wird beim ersten fetch() gefüllt und dann
30 Minuten in-memory geshared (zusätzlich zum 30-Min-Disk-Cache im Scraper).

Stärken: sehr breite Coverage inkl. ITF/Challenger/Qualifier.
Schwächen: nur H2H (keine AH/Totals/Set-Betting).
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger("sportsbrain.tennis.odds.tennis_explorer")

from src.tennis.name_norm import (
    to_elo_name_from_odds_api,
    to_elo_name_from_te,
)
from src.tennis.odds.base import OddsQuote, ProviderOutcome, sanity_ok

name = "tennis_explorer"
tier = 2

_BULK: list[dict] = []
_TS: float = 0.0
_TTL_S = 30 * 60


def _get_bulk() -> list[dict]:
    return _get_bulk_with_diagnostics()[0]


def _get_bulk_with_diagnostics() -> tuple[list[dict], ProviderOutcome]:
    global _BULK, _TS
    age = time.time() - _TS
    if _BULK and age < _TTL_S:
        return _BULK, ProviderOutcome(name, True, True, "success")
    try:
        from src.data.tennis_secondary_odds import fetch_te_upcoming_matches
        new_bulk = fetch_te_upcoming_matches(min_bookies=2, max_matches=200)
        if new_bulk:
            _BULK = new_bulk
            _TS = time.time()
        else:
            # J8-B4: Refresh lieferte nichts (Rate-Limit / Netzwerkfehler).
            # Bulk-Wert älter als 2×TTL nicht weiterreichen → return leer, damit
            # Merger auf nächstes Tier ausweicht statt gecachte alte Quoten zu servieren.
            if age >= 2 * _TTL_S:
                _log.warning("stale bulk (%.0fs ≥ 2×TTL) und Refresh leer → drop", age)
                _BULK = []
                _TS = time.time()
    except TimeoutError:
        _BULK = []
        _TS = time.time()
        return _BULK, ProviderOutcome(name, True, True, "timeout", error_class="Timeout")
    except Exception as exc:  # noqa: BLE001 - preserve the existing fail-closed bulk behavior
        _BULK = []
        _TS = time.time()
        return _BULK, ProviderOutcome(name, True, True, "exception", error_class=type(exc).__name__)
    return _BULK, ProviderOutcome(name, True, True, "success")


def _match_key(name_norm: str) -> str:
    return name_norm.lower().strip()


def _quote_from_bulk(match_hint: dict, bulk: list[dict]) -> OddsQuote | None:
    pa_raw = match_hint.get("player_a", "")
    pb_raw = match_hint.get("player_b", "")
    if not pa_raw or not pb_raw:
        return None

    src_hint = match_hint.get("name_source", "odds_api")
    if src_hint == "te":
        pa_key = _match_key(to_elo_name_from_te(pa_raw))
        pb_key = _match_key(to_elo_name_from_te(pb_raw))
    else:
        pa_key = _match_key(to_elo_name_from_odds_api(pa_raw))
        pb_key = _match_key(to_elo_name_from_odds_api(pb_raw))

    for m in bulk:
        mpa = _match_key(to_elo_name_from_te(m.get("player_a", "")))
        mpb = _match_key(to_elo_name_from_te(m.get("player_b", "")))
        if not mpa or not mpb:
            continue

        forward = (mpa == pa_key and mpb == pb_key)
        reverse = (mpa == pb_key and mpb == pa_key)
        if not (forward or reverse):
            continue

        a = m.get("odds_a", 0.0)
        b = m.get("odds_b", 0.0)
        if reverse:
            a, b = b, a
        if not sanity_ok(a, b):
            continue

        bookies = int(m.get("te_bookies_count", 1))
        return OddsQuote(
            player_a=pa_raw,
            player_b=pb_raw,
            h2h_a=a,
            h2h_b=b,
            source="tennis_explorer",
            source_tier=2,
            bookmaker="consensus",
            confidence=min(1.0, 0.4 + 0.1 * bookies),
            bookies_count=bookies,
        )
    return None


def fetch(match_hint: dict) -> OddsQuote | None:
    return _quote_from_bulk(match_hint, _get_bulk())


def fetch_with_diagnostics(match_hint: dict) -> tuple[OddsQuote | None, ProviderOutcome]:
    bulk, outcome = _get_bulk_with_diagnostics()
    if outcome.status_class != "success":
        return None, outcome
    quote = _quote_from_bulk(match_hint, bulk)
    if quote and quote.sane():
        return quote, ProviderOutcome(name, True, True, "success", result="usable_quote")
    return quote, ProviderOutcome(name, True, True, "invalid_quote" if quote else "no_match")
