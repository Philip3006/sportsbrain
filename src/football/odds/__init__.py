"""The Odds API-only Football odds surface.

The football merger is fail closed when The Odds API cannot provide a valid
quote. Tennis has a separate provider architecture.
"""
from src.football.odds.base import FootballOddsQuote, sanity_1x2, sanity_2way
from src.football.odds.merger import (
    fetch_all_sources,
    fetch_best_football_odds,
    merge_by_tier,
)

__all__ = [
    "FootballOddsQuote",
    "fetch_all_sources",
    "fetch_best_football_odds",
    "merge_by_tier",
    "sanity_1x2",
    "sanity_2way",
]
