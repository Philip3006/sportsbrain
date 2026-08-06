"""Parallel-Fetch + Priorisierter Merger für Football-Odds.

Analog zu src/tennis/odds/merger.py. Alle registrierten Quellen werden
parallel angefragt (ThreadPoolExecutor, 5s Timeout). Aus den gelieferten
FootballOddsQuotes wird der Best-Price aus dem höchsten Tier (kleinste
Ziffer = schärfste Quelle) gewählt.

Coverage-Gate:
    bookies_count_1x2 < MIN_BOOKIES_1X2 (3) → no_bet_flag=True auf allen Signalen.
    Gilt auch wenn Quellen insgesamt vorhanden, aber nur wenige Bookies.

Fallback-Kette:
    Tier 1 (Betfair, Pinnacle) → Tier 2 (TheOddsAPI multi-region) →
    Tier 3 (WebSearch) → Tier 5 (Implied-DC, no_bet_flag immer True)
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from src.football.odds.base import FootballOddsQuote, sanity_1x2
from src.football.odds.implied import fetch as _fetch_implied

MIN_BOOKIES_1X2 = 3


def _load_sources() -> list[tuple[str, int, Callable]]:
    sources: list[tuple[str, int, Callable]] = []
    try:
        from src.football.odds import the_odds_api as _toa
        sources.append((_toa.name, _toa.tier, _toa.fetch))
    except Exception:
        pass
    try:
        from src.football.odds import betfair as _bf
        sources.append((_bf.name, _bf.tier, _bf.fetch))
    except Exception:
        pass
    try:
        from src.football.odds import pinnacle as _pin
        sources.append((_pin.name, _pin.tier, _pin.fetch))
    except Exception:
        pass
    try:
        from src.football.odds import websearch as _ws
        sources.append((_ws.name, _ws.tier, _ws.fetch))
    except Exception:
        pass
    return sources


ENABLED_SOURCES: list[tuple[str, int, Callable]] = _load_sources()


def fetch_all_sources(
    match_hint: dict,
    timeout_s: float = 5.0,
) -> list[FootballOddsQuote]:
    """Fragt alle registrierten Quellen parallel ab.

    match_hint muss mindestens 'home_team' und 'away_team' enthalten.
    Optionales 'bookmakers' spart TheOddsAPI-Quota (aus Bulk-Fetch).
    Optionales 'model_probs' {p_home,p_draw,p_away} ermöglicht Tier-5-Fallback.

    Return: Liste aller Quotes die geliefert wurden (inkl. no_bet_flag-Quotes).
    """
    quotes: list[FootballOddsQuote] = []
    if not ENABLED_SOURCES:
        return quotes

    with ThreadPoolExecutor(max_workers=max(1, len(ENABLED_SOURCES))) as pool:
        futures = {
            pool.submit(fn, match_hint): (src_name, src_tier)
            for (src_name, src_tier, fn) in ENABLED_SOURCES
        }
        try:
            for fut in as_completed(futures, timeout=timeout_s + 2):
                try:
                    q: Optional[FootballOddsQuote] = fut.result(timeout=timeout_s)
                except Exception:
                    q = None
                if q is not None and q.h2h_home > 0:
                    quotes.append(q)
        except TimeoutError:
            for fut in futures:
                if fut.done():
                    try:
                        q = fut.result(timeout=0)
                        if q is not None and q.h2h_home > 0:
                            quotes.append(q)
                    except Exception:
                        pass

    return quotes


def merge_by_tier(quotes: list[FootballOddsQuote]) -> Optional[FootballOddsQuote]:
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
) -> Optional[FootballOddsQuote]:
    """Convenience: fetch_all_sources → merge_by_tier → Coverage-Gate → Implied-Fallback.

    Return:
      - FootballOddsQuote mit no_bet_flag=False → normales Signal
      - FootballOddsQuote mit no_bet_flag=True  → Display-only
      - None → nichts brauchbar, kein Implied möglich
    """
    quotes = fetch_all_sources(match_hint, timeout_s=timeout_s)
    best = merge_by_tier(quotes)

    if best is not None:
        return _apply_coverage_gate(best)

    if allow_implied and match_hint.get("model_probs"):
        return _fetch_implied(match_hint)

    return None
