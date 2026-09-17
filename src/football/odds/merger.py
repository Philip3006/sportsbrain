"""The Odds API-only Football odds merger.

The existing helper is retained for API compatibility, but the active
Football registry contains only The Odds API.  Provider failure is fail closed.

Coverage-Gate:
    bookies_count_1x2 < MIN_BOOKIES_1X2 (3) → no_bet_flag=True auf allen Signalen.
    Gilt auch wenn Quellen insgesamt vorhanden, aber nur wenige Bookies.

There is no alternate-provider, WebSearch, cache, or implied-odds fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.football.odds.base import FootballOddsQuote
from src.football.provider_cascade.contracts import FOOTBALL_PROVIDER_REPERTOIRE

MIN_BOOKIES_1X2 = 3
_log = logging.getLogger("sportsbrain.football.odds.merger")


def _load_sources() -> list[tuple[str, int, Callable]]:
    sources: list[tuple[str, int, Callable]] = []
    try:
        from src.football.odds import the_odds_api as _toa

        sources.append((_toa.name, _toa.tier, _toa.fetch))
    except (ImportError, AttributeError):
        return sources
    return sources


ENABLED_SOURCES: list[tuple[str, int, Callable]] = _load_sources()


def fetch_all_sources(
    match_hint: dict,
    timeout_s: float = 5.0,
) -> list[FootballOddsQuote]:
    """Fetch the one canonical football source and fail closed.

    match_hint muss mindestens 'home_team' und 'away_team' enthalten.
    Optionales 'bookmakers' spart TheOddsAPI-Quota (aus Bulk-Fetch).
    Model probabilities never substitute for an observed provider quote.

    Return: Liste aller Quotes die geliefert wurden (inkl. no_bet_flag-Quotes).
    """
    quotes: list[FootballOddsQuote] = []
    source = next(
        (
            source
            for source in ENABLED_SOURCES
            if source[0] in FOOTBALL_PROVIDER_REPERTOIRE
        ),
        None,
    )
    if source is None:
        return quotes

    # The filter and first-match selection above are invariants, not just a
    # default. They prevent a mutable compatibility test seam from introducing
    # a second football provider or a duplicate call into this runtime path.
    src_name, src_tier, fn = source
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {pool.submit(fn, match_hint): (src_name, src_tier)}
        try:
            for future in as_completed(futures, timeout=timeout_s + 2):
                try:
                    quote: FootballOddsQuote | None = future.result(timeout=timeout_s)
                except Exception as exc:  # noqa: BLE001 - provider boundary fails closed
                    _log.debug("The Odds API source failed: %s", exc)
                    quote = None
                if quote is not None and quote.h2h_home > 0:
                    quotes.append(quote)
        except TimeoutError:
            for future in futures:
                if not future.done():
                    continue
                try:
                    quote = future.result(timeout=0)
                except Exception as exc:  # noqa: BLE001 - provider boundary fails closed
                    _log.debug("The Odds API source timed out: %s", exc)
                    quote = None
                if quote is not None and quote.h2h_home > 0:
                    quotes.append(quote)

    return quotes


def merge_by_tier(quotes: list[FootballOddsQuote]) -> FootballOddsQuote | None:
    """Wählt beste Quote: Tier 1 vor Tier 2 vor Tier 3; innerhalb Tier → bester Bookie-Count.

    Gibt None zurück wenn keine Quote vorhanden.
    """
    if not quotes:
        return None
    real = [q for q in quotes if not q.no_bet_flag]
    pool = real if real else quotes
    top_tier = min(q.source_tier for q in pool)
    tier_quotes = [q for q in pool if q.source_tier == top_tier]
    # Innerhalb Tier: meiste Bookies = beste Coverage
    best = max(tier_quotes, key=lambda q: q.bookies_count_1x2)
    return best


def _apply_coverage_gate(quote: FootballOddsQuote) -> FootballOddsQuote:
    """Setzt no_bet_flag wenn Bookie-Count für 1X2 < MIN_BOOKIES_1X2."""
    if quote.bookies_count_1x2 < MIN_BOOKIES_1X2 and not quote.no_bet_flag:
        quote.no_bet_flag = True
        quote.bookmaker = f"{quote.bookmaker}_low_coverage"
    return quote


def fetch_best_football_odds(
    match_hint: dict,
    timeout_s: float = 5.0,
    allow_implied: bool = True,
) -> FootballOddsQuote | None:
    """Fetch The Odds API, apply the coverage gate, and fail closed.

    Return:
      - FootballOddsQuote mit no_bet_flag=False → normales Signal
      - FootballOddsQuote mit no_bet_flag=True  → Display-only
      - None → no authoritative provider quote
    """
    quotes = fetch_all_sources(match_hint, timeout_s=timeout_s)
    best = merge_by_tier(quotes)

    if best is not None:
        return _apply_coverage_gate(best)

    return None
