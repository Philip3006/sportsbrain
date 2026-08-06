"""Tier 3 — WebSearch-Ensemble als Football-Odds-Fallback.

Analog zu src/tennis/odds/websearch.py. Aktiviert wenn:
  - Tier-1/2 liefern <3 Bookies für 1X2 (Coverage-Gate), ODER
  - Spezial-Märkte (Torschützen, Double Chance) nicht in TheOddsAPI.

Multi-Query über DuckDuckGo, medianisiert Preise aus mehreren Treffern.
Ab ≥ 2 unabhängigen Quotes: kein no_bet_flag; sonst Display-only.

Kein LLM-Parse aktiviert (Kosten-Kontrolle — Platzhalter dokumentiert).
"""
from __future__ import annotations

from statistics import median
from typing import Optional

from src.football.odds.base import FootballOddsQuote, sanity_1x2

name = "websearch"
tier = 3


def _search_odds(home: str, away: str, tournament_hint: str) -> list[tuple[float, float, float]]:
    """Sammelt (home,draw,away)-Tuples aus mehreren Query-Varianten."""
    try:
        from scripts.daily_scan import _websearch_football_fallback  # type: ignore[import]
    except ImportError:
        try:
            from scripts.bundesliga2_scan import _websearch_football_fallback  # type: ignore[import]
        except ImportError:
            return []

    queries = [
        f"{home} vs {away} odds {tournament_hint}",
        f"{home} {away} bundesliga 2 wettquoten",
        f"2 bundesliga {home} {away} betting odds",
    ]
    seen: set[tuple[float, float, float]] = set()
    results: list[tuple[float, float, float]] = []

    for q in queries:
        try:
            r = _websearch_football_fallback(home, away, query_override=q)
        except Exception:
            r = None
        if not r:
            continue
        h = float(r.get("home", 0.0))
        d = float(r.get("draw", 0.0))
        a = float(r.get("away", 0.0))
        if not sanity_1x2(h, d, a):
            continue
        key = (round(h, 2), round(d, 2), round(a, 2))
        if key in seen:
            continue
        seen.add(key)
        results.append((h, d, a))

    return results


def fetch(match_hint: dict) -> Optional[FootballOddsQuote]:
    home_raw = match_hint.get("home_team", "")
    away_raw = match_hint.get("away_team", "")
    if not home_raw or not away_raw:
        return None

    tournament_hint = match_hint.get("tournament", "2. bundesliga")
    quotes = _search_odds(home_raw, away_raw, tournament_hint)
    if not quotes:
        return None

    med_h = round(median([q[0] for q in quotes]), 2)
    med_d = round(median([q[1] for q in quotes]), 2)
    med_a = round(median([q[2] for q in quotes]), 2)

    if not sanity_1x2(med_h, med_d, med_a):
        return None

    single_source = len(quotes) < 2
    return FootballOddsQuote(
        home_team=home_raw,
        away_team=away_raw,
        source="websearch",
        source_tier=tier,
        h2h_home=med_h,
        h2h_draw=med_d,
        h2h_away=med_a,
        bookmaker="consensus",
        bookies_count_1x2=len(quotes),
        confidence=min(1.0, 0.3 + 0.15 * len(quotes)),
        no_bet_flag=single_source,
    )
