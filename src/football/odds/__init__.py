"""Football-Odds Multi-Source Provider.

Analog zu src/tennis/odds/ — Parallel-Merger über mehrere Quellen,
priorisiert nach source_tier. Kern-Kontrakt: FootballOddsQuote.

Aktivierte Quellen (Tier 1→2→3→5):
    Tier 1: Betfair Exchange, Pinnacle (Sharp-Referenz / CLV-Ground-Truth)
    Tier 2: TheOddsAPI (eu+uk+au Multi-Region Konsens)
    Tier 3: WebSearch-Ensemble (Fallback für <3 Bookies oder Spezial-Märkte)
    Tier 5: DC-Modell-Implied (Display-only, no_bet_flag=True)
"""
from src.football.odds.base import FootballOddsQuote, sanity_1x2, sanity_2way
from src.football.odds.merger import fetch_all_sources, fetch_best_football_odds, merge_by_tier

__all__ = [
    "FootballOddsQuote", "sanity_1x2", "sanity_2way",
    "fetch_all_sources", "fetch_best_football_odds", "merge_by_tier",
]
