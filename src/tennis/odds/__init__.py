"""Tennis-Odds Multi-Source Provider (Roadmap J2-L).

Ersetzt die alte Sequential-Fallback-Chain aus scripts/tennis_scan.py durch
einen Parallel-Merger, der mehrere Odds-Quellen gleichzeitig anfragt und nach
source_tier priorisiert.

Kern-Kontrakt: siehe base.OddsQuote.
Aktivierte Quellen: siehe merger.ENABLED_SOURCES.
"""
from src.tennis.odds.base import OddsQuote, OddsSource, sanity_ok
from src.tennis.odds.merger import fetch_best_odds, fetch_all_sources

__all__ = ["OddsQuote", "OddsSource", "sanity_ok",
           "fetch_best_odds", "fetch_all_sources"]
