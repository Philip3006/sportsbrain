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

from src.tennis.odds.base import OddsQuote, ProviderOutcome, sanity_ok
from src.tennis.odds.implied import fetch_implied


def _outcome(provider: str, *, attempted: bool = True, eligible: bool = True,
             status_class: str, result: str = "no_quote", http_status: int | None = None,
             error_class: str | None = None) -> dict:
    return ProviderOutcome(provider, attempted, eligible, status_class, result, http_status, error_class).as_dict()

# Registry aktivierter Quellen: (name, tier, fetch_callable)
# Reihenfolge irrelevant (Merger sortiert nach tier).
def _load_sources() -> list[tuple[str, int, Callable]]:
    sources: list[tuple[str, int, Callable]] = []
    try:
        from src.tennis.odds import tennis_explorer as _te
        sources.append((_te.name, _te.tier, getattr(_te, "fetch_with_diagnostics", _te.fetch)))
    except Exception:
        pass
    try:
        from src.tennis.odds import oddsportal as _op
        sources.append((_op.name, _op.tier, getattr(_op, "fetch_with_diagnostics", _op.fetch)))
    except Exception:
        pass
    try:
        from src.tennis.odds import betfair as _bf
        sources.append((_bf.name, _bf.tier, getattr(_bf, "fetch_with_diagnostics", _bf.fetch)))
    except Exception:
        pass
    try:
        # J8-I6: Pinnacle als 6. Provider — Tier 1 (Sharp-Ground-Truth für CLV)
        from src.tennis.odds import pinnacle as _pin
        sources.append((_pin.name, _pin.tier, getattr(_pin, "fetch_with_diagnostics", _pin.fetch)))
    except Exception:
        pass
    try:
        from src.tennis.odds import websearch as _ws
        sources.append((_ws.name, _ws.tier, _ws.fetch))
    except Exception:
        pass
    return sources


ENABLED_SOURCES: list[tuple[str, int, Callable]] = _load_sources()


def fetch_all_sources(match_hint: dict,
                      timeout_s: float = 3.0,
                      ratings=None,
                      include_websearch: bool = True) -> list[OddsQuote]:
    """Fragt alle registrierten Quellen parallel ab.

    Return: Liste aller Quotes, die Sanity bestehen. Kann leer sein.
    """
    quotes, _ = fetch_all_sources_with_diagnostics(
        match_hint, timeout_s=timeout_s, ratings=ratings, include_websearch=include_websearch,
    )
    return quotes


def fetch_all_sources_with_diagnostics(match_hint: dict,
                                       timeout_s: float = 3.0,
                                       ratings=None,
                                       include_websearch: bool = True) -> tuple[list[OddsQuote], list[dict]]:
    """Run the unchanged provider set and retain sanitized completion evidence."""
    quotes: list[OddsQuote] = []
    outcomes: list[dict] = []
    if not ENABLED_SOURCES:
        return quotes, outcomes

    sources = [source for source in ENABLED_SOURCES if include_websearch or source[0] != "websearch"]
    if not sources:
        return quotes, outcomes
    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool:
        futures = {
            pool.submit(fn, match_hint): (name, tier)
            for (name, tier, fn) in sources
        }
        try:
            for fut in as_completed(futures, timeout=timeout_s + 2):
                try:
                    raw = fut.result(timeout=timeout_s)
                    q, adapter_outcome = raw if isinstance(raw, tuple) else (raw, None)
                except TimeoutError:
                    q = None
                    outcomes.append(_outcome(futures[fut][0], status_class="timeout", error_class="timeout"))
                except Exception as exc:
                    q = None
                    outcomes.append(_outcome(futures[fut][0], status_class="exception", error_class=type(exc).__name__))
                else:
                    if adapter_outcome is not None:
                        outcomes.append(adapter_outcome.as_dict())
                        if q and q.sane():
                            quotes.append(q)
                        continue
                    if futures[fut][0] == "betfair" and q is None:
                        outcomes.append(_outcome("betfair", attempted=False, eligible=False, status_class="ineligible"))
                        continue
                    outcomes.append(_outcome(
                        futures[fut][0],
                        status_class="success" if q and q.sane() else "no_match",
                        result="usable_quote" if q and q.sane() else "no_quote",
                    ))
                if q and q.sane():
                    quotes.append(q)
        except TimeoutError:
            # Hänger einzelner Quellen werden verworfen — Restliche Quotes reichen.
            for fut in futures:
                if fut.done():
                    try:
                        q = fut.result(timeout=0)
                        if q and q.sane():
                            quotes.append(q)
                    except Exception:
                        pass
    return quotes, outcomes


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
                    allow_implied: bool = True,
                    include_websearch: bool = True) -> Optional[OddsQuote]:
    """Convenience: fetch_all_sources → merge_by_tier → implied-Fallback.

    Return:
      - OddsQuote mit no_bet_flag=False → normales Signal
      - OddsQuote mit no_bet_flag=True  → Display-only (implied)
      - None → nichts brauchbar (keine Quelle, kein implied möglich)
    """
    quotes = fetch_all_sources(
        match_hint,
        timeout_s=timeout_s,
        ratings=ratings,
        include_websearch=include_websearch,
    )
    best = merge_by_tier(quotes)
    if best is not None:
        return best
    if allow_implied:
        return fetch_implied(match_hint, ratings=ratings)
    return None


def fetch_best_odds_with_diagnostics(match_hint: dict,
                                     timeout_s: float = 3.0,
                                     ratings=None,
                                     allow_implied: bool = True,
                                     include_websearch: bool = True) -> tuple[Optional[OddsQuote], list[dict]]:
    quotes, outcomes = fetch_all_sources_with_diagnostics(
        match_hint, timeout_s=timeout_s, ratings=ratings, include_websearch=include_websearch,
    )
    return merge_by_tier(quotes), outcomes
