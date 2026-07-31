"""Parallel-Fetch + Priorisierter Merger für Tennis-Odds.

Statt sequential-fallback (alte tennis_scan.py): Aktivierte Quellen werden
parallel angefragt (ThreadPoolExecutor, konfigurierbares Timeout). Aus den
gelieferten OddsQuotes wird der Best-Price aus dem höchsten Tier (kleinste
Ziffer = beste Quelle) gewählt.

Wenn keine Quelle liefert → Agent 8 (implied_from_elo) als Display-only.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from src.tennis.odds.base import OddsQuote, sanity_ok
from src.tennis.odds.implied import fetch_implied

# Registry aktivierter Quellen: (name, tier, fetch_callable)
# Weitere Sources werden hier registriert (Agent 5, 3, 10, ...).
ENABLED_SOURCES: list[tuple[str, int, Callable]] = [
    # Foundation ships mit implied als Fallback-Only-Quelle;
    # externe Quellen (TE, Betfair, WebSearch) werden via tennis_scan.py
    # separat verdrahtet, bis die einzelnen Provider-Module gelandet sind.
]


def fetch_all_sources(match_hint: dict,
                      timeout_s: float = 3.0,
                      ratings=None) -> list[OddsQuote]:
    """Fragt alle registrierten Quellen parallel ab.

    Return: Liste aller Quotes, die Sanity bestehen. Kann leer sein.
    """
    quotes: list[OddsQuote] = []
    if not ENABLED_SOURCES:
        return quotes

    with ThreadPoolExecutor(max_workers=max(1, len(ENABLED_SOURCES))) as pool:
        futures = {
            pool.submit(fn, match_hint): (name, tier)
            for (name, tier, fn) in ENABLED_SOURCES
        }
        for fut in as_completed(futures, timeout=timeout_s + 2):
            name, tier = futures[fut]
            try:
                q: Optional[OddsQuote] = fut.result(timeout=timeout_s)
            except Exception:
                q = None
            if q and q.sane():
                quotes.append(q)
    return quotes


def merge_by_tier(quotes: list[OddsQuote]) -> Optional[OddsQuote]:
    """Wählt die beste Quote aus dem höchsten Tier (kleinste tier-Ziffer)."""
    real = [q for q in quotes if not q.no_bet_flag]
    if not real:
        return None
    top_tier = min(q.source_tier for q in real)
    tier_quotes = [q for q in real if q.source_tier == top_tier]
    # Best-Price = max h2h_a (aus Wetten-Sicht: höchste erhältliche Quote)
    return max(tier_quotes, key=lambda q: q.h2h_a + q.h2h_b)


def fetch_best_odds(match_hint: dict,
                    timeout_s: float = 3.0,
                    ratings=None,
                    allow_implied: bool = True) -> Optional[OddsQuote]:
    """Convenience: fetch_all_sources → merge_by_tier → implied-Fallback.

    Return:
      - OddsQuote mit no_bet_flag=False → normales Signal
      - OddsQuote mit no_bet_flag=True  → Display-only (implied)
      - None → nichts brauchbar (keine Quelle, kein implied möglich)
    """
    quotes = fetch_all_sources(match_hint, timeout_s=timeout_s, ratings=ratings)
    best = merge_by_tier(quotes)
    if best is not None:
        return best
    if allow_implied:
        return fetch_implied(match_hint, ratings=ratings)
    return None
