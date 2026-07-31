"""Agent 10 — WebSearch-Ensemble als last-resort Odds-Quelle.

Multi-Query gegen DuckDuckGo (Bing/Google-Bibliotheken als optional). Sammelt
Quoten aus mehreren Suchtreffern, medianisiert Preise über die Treffer für
mehr Robustheit gegen einzelne Bookie-Ausreißer.

Tier 4: nur wenn Tier 1-2 nichts liefern. Bereits vorhandener DDG-Scraper
in scripts/tennis_scan._websearch_tennis_fallback wird wiederverwendet und
über mehrere Query-Varianten ausgeführt.

Optionaler LLM-Parse (deferred): wenn ANTHROPIC_API_KEY gesetzt UND alle
anderen Tiers leer, könnte Claude die Top-Suchergebnisse parsen. Momentan
NICHT aktiviert (Kosten-Kontrolle) — Platzhalter dokumentiert.
"""
from __future__ import annotations

from statistics import median
from typing import Optional

from src.tennis.odds.base import OddsQuote, sanity_ok

name = "websearch"
tier = 4


def _collect_quotes(pa: str, pb: str, tournament_hint: str) -> list[tuple[float, float]]:
    """Sammelt (a,b)-Tuples aus mehreren Query-Varianten."""
    try:
        from scripts.tennis_scan import _websearch_tennis_fallback
    except Exception:
        return []

    variants = [
        tournament_hint or "",
        "atp" if tournament_hint and "atp" in tournament_hint.lower() else "wta",
        "match odds",
    ]
    seen: set[tuple[float, float]] = set()
    results: list[tuple[float, float]] = []
    for v in variants:
        try:
            r = _websearch_tennis_fallback(pa, pb, tournament=v)
        except Exception:
            r = None
        if not r:
            continue
        a = float(r.get("a", 0.0))
        b = float(r.get("b", 0.0))
        if not sanity_ok(a, b):
            continue
        # 2-Dezimalen-Bucketing damit "1.85" und "1.851" gleich zählen
        key = (round(a, 2), round(b, 2))
        if key in seen:
            continue
        seen.add(key)
        results.append((a, b))
    return results


def fetch(match_hint: dict) -> Optional[OddsQuote]:
    pa = match_hint.get("player_a", "")
    pb = match_hint.get("player_b", "")
    if not pa or not pb:
        return None

    tournament_hint = match_hint.get("sport_key", "") or match_hint.get("tournament", "")
    quotes = _collect_quotes(pa, pb, tournament_hint)
    if not quotes:
        return None

    # Median über alle Treffer (robuster als Best-Price bei Web-Scrape)
    med_a = round(median([q[0] for q in quotes]), 2)
    med_b = round(median([q[1] for q in quotes]), 2)
    if not sanity_ok(med_a, med_b):
        return None

    return OddsQuote(
        player_a=pa,
        player_b=pb,
        h2h_a=med_a,
        h2h_b=med_b,
        source="websearch",
        source_tier=4,
        bookmaker="consensus",
        confidence=min(1.0, 0.3 + 0.15 * len(quotes)),
        bookies_count=len(quotes),
    )
