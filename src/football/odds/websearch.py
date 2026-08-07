"""Tier 3 — WebSearch-Ensemble als Football-Odds-Fallback.

Aktiviert wenn Tier-1/2 <3 Bookies liefern. Multi-Query über DuckDuckGo,
medianisiert Preise aus mehreren Treffern. Ab ≥ 2 unabhängigen Quotes
kein no_bet_flag; sonst Display-only.

`_websearch_football_fallback()` ist hier direkt implementiert (nicht aus
einem Script importiert — das wäre ein zirkulärer Pfad und würde beim
CI-Import scheitern).
"""
from __future__ import annotations

import re
from statistics import median
from typing import Optional

from src.football.odds.base import FootballOddsQuote, sanity_1x2

name = "websearch"
tier = 3

# Regex für Dezimalquoten im Text (z.B. "2.10", "3.20", "4.50")
_ODDS_RE = re.compile(r"\b([1-9]\d*\.\d{1,2})\b")


def _websearch_football_fallback(
    home: str,
    away: str,
    query_override: str = "",
) -> Optional[dict[str, float]]:
    """DDG-WebSearch für Football-Quoten. Gibt {home, draw, away} oder None zurück.

    Strategie: parse alle Dezimalzahlen im Snippet, nimm erste drei die
    sanity_1x2 bestehen (longest-valid-triple). Single-source → no_bet_flag=True.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return None

    query = query_override or f"{home} vs {away} odds 1x2 bundesliga 2"
    try:
        results = list(DDGS().text(query, max_results=5))
    except Exception:
        return None

    for r in results or []:
        text = " ".join([r.get("title", ""), r.get("body", "")])
        nums = [float(x) for x in _ODDS_RE.findall(text) if 1.01 < float(x) < 25.0]
        # Suche gültige (h, d, a)-Tripel sliding window
        for i in range(len(nums) - 2):
            h, d, a = nums[i], nums[i + 1], nums[i + 2]
            if sanity_1x2(h, d, a):
                return {"home": h, "draw": d, "away": a}
    return None


def _collect_quotes(home: str, away: str, tournament_hint: str) -> list[tuple[float, float, float]]:
    queries = [
        f"{home} vs {away} wettquoten {tournament_hint}",
        f"{home} {away} 2 bundesliga odds",
        f"2. bundesliga {home} {away} betting 1x2",
    ]
    seen: set[tuple[float, float, float]] = set()
    results: list[tuple[float, float, float]] = []
    for q in queries:
        r = _websearch_football_fallback(home, away, query_override=q)
        if not r:
            continue
        h, d, a = float(r["home"]), float(r["draw"]), float(r["away"])
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
    quotes = _collect_quotes(home_raw, away_raw, tournament_hint)
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
