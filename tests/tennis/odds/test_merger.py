"""J8-B12: Merger-Tier-Priority + no_bet_flag-Gate.

Zentraler Test: Wenn mehrere Provider parallel liefern, wählt der Merger
die höchst-priorisierte Tier-Klasse. no_bet_flag-Quotes werden nur als
Display-only durchgereicht.
"""
from __future__ import annotations

from src.tennis.odds.base import OddsQuote
from src.tennis.odds.merger import merge_by_tier


def _q(source: str, tier: int, a: float, b: float, no_bet: bool = False):
    return OddsQuote(
        player_a="Alcaraz", player_b="Sinner",
        h2h_a=a, h2h_b=b,
        source=source, source_tier=tier,
        no_bet_flag=no_bet, bookies_count=1, confidence=0.8,
    )


def test_top_tier_wins_over_lower_tier():
    quotes = [
        _q("tennis_explorer", 2, 1.90, 2.00),
        _q("betfair", 1, 1.95, 2.10),  # Tier 1 sollte gewinnen
        _q("oddsportal", 2, 1.85, 2.05),
    ]
    best = merge_by_tier(quotes)
    assert best is not None
    assert best.source == "betfair"


def test_best_price_within_same_tier():
    quotes = [
        _q("tennis_explorer", 2, 1.90, 2.00),
        _q("oddsportal", 2, 1.95, 2.05),  # bessere Summe → gewinnt
    ]
    best = merge_by_tier(quotes)
    assert best.source == "oddsportal"


def test_no_bet_flag_quotes_excluded():
    quotes = [
        _q("implied_elo", 5, 1.85, 2.10, no_bet=True),
        _q("tennis_explorer", 2, 1.90, 2.00),
    ]
    best = merge_by_tier(quotes)
    assert best.source == "tennis_explorer"


def test_only_no_bet_quotes_returns_none():
    quotes = [_q("implied_elo", 5, 1.85, 2.10, no_bet=True)]
    assert merge_by_tier(quotes) is None


def test_empty_returns_none():
    assert merge_by_tier([]) is None
