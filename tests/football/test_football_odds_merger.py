"""Tests für src/football/odds/ — Multi-Source-Merger."""
from __future__ import annotations

from src.football.odds.base import FootballOddsQuote, sanity_1x2, sanity_2way
from src.football.odds.merger import (
    fetch_all_sources, fetch_best_football_odds, merge_by_tier,
    MIN_BOOKIES_1X2,
)
from src.football.odds.implied import implied_from_probs


def _make_quote(tier: int, bookies: int = 5, no_bet: bool = False) -> FootballOddsQuote:
    return FootballOddsQuote(
        home_team="Hamburg", away_team="Kaiserslautern",
        source="test", source_tier=tier,
        h2h_home=2.10, h2h_draw=3.20, h2h_away=3.80,
        bookies_count_1x2=bookies, no_bet_flag=no_bet,
    )


def _hint_with_bookmakers(n_bookies: int = 3) -> dict:
    bms = [
        {"key": f"bookie{i}", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Hamburg", "price": 2.10 + i * 0.02},
            {"name": "Draw", "price": 3.20},
            {"name": "Kaiserslautern", "price": 3.80 - i * 0.02},
        ]}]}
        for i in range(n_bookies)
    ]
    return {
        "home_team": "Hamburg",
        "away_team": "Kaiserslautern",
        "sport_key": "soccer_germany_bundesliga2",
        "bookmakers": bms,
        "model_probs": {"p_home": 0.42, "p_draw": 0.28, "p_away": 0.30},
    }


class TestSanity:
    def test_valid_1x2(self):
        assert sanity_1x2(2.0, 3.0, 4.0)

    def test_low_implied_fails(self):
        # implied < 1.0 (too high odds)
        assert not sanity_1x2(20.0, 30.0, 40.0)

    def test_high_margin_fails(self):
        # implied > 1.20
        assert not sanity_1x2(1.1, 1.2, 1.3)

    def test_sub_minimum_price_fails(self):
        assert not sanity_1x2(0.9, 3.0, 4.0)

    def test_2way_sanity(self):
        assert sanity_2way(1.50, 2.40)
        assert not sanity_2way(0.5, 2.40)


class TestFootballOddsQuote:
    def test_has_1x2(self):
        q = _make_quote(tier=2)
        assert q.has_1x2()

    def test_has_1x2_incomplete(self):
        q = FootballOddsQuote(home_team="A", away_team="B", source="t", source_tier=2,
                               h2h_home=2.0, h2h_draw=0.0, h2h_away=3.0)
        assert not q.has_1x2()

    def test_sane_1x2(self):
        q = _make_quote(tier=2)
        assert q.sane_1x2()


class TestMerger:
    def test_merge_prefers_lower_tier(self):
        quotes = [_make_quote(tier=2, bookies=4), _make_quote(tier=1, bookies=1)]
        best = merge_by_tier(quotes)
        assert best is not None
        assert best.source_tier == 1

    def test_merge_same_tier_prefers_more_bookies(self):
        q_few = _make_quote(tier=2, bookies=3)
        q_many = _make_quote(tier=2, bookies=8)
        best = merge_by_tier([q_few, q_many])
        assert best.bookies_count_1x2 == 8

    def test_merge_skips_no_bet_flag_when_real_available(self):
        real = _make_quote(tier=2, bookies=5, no_bet=False)
        implied = _make_quote(tier=5, bookies=0, no_bet=True)
        best = merge_by_tier([implied, real])
        assert not best.no_bet_flag

    def test_merge_returns_none_on_empty(self):
        assert merge_by_tier([]) is None


class TestCoverageGate:
    def test_enough_bookies_no_flag(self):
        hint = _hint_with_bookmakers(n_bookies=MIN_BOOKIES_1X2)
        q = fetch_best_football_odds(hint, timeout_s=3.0)
        assert q is not None
        assert not q.no_bet_flag

    def test_too_few_bookies_sets_flag(self):
        hint = _hint_with_bookmakers(n_bookies=MIN_BOOKIES_1X2 - 1)
        q = fetch_best_football_odds(hint, timeout_s=3.0)
        assert q is not None
        assert q.no_bet_flag

    def test_empty_bookmakers_implies_fallback(self):
        hint = {
            "home_team": "Hamburg", "away_team": "Kaiserslautern",
            "bookmakers": [],
            "model_probs": {"p_home": 0.42, "p_draw": 0.28, "p_away": 0.30},
        }
        q = fetch_best_football_odds(hint, timeout_s=3.0, allow_implied=True)
        assert q is not None
        assert q.source == "implied_dc"
        assert q.no_bet_flag  # Tier-5 ist immer no_bet


class TestImplied:
    def test_implied_always_no_bet(self):
        q = implied_from_probs("Hamburg", "Kaiserslautern", 0.42, 0.28, 0.30)
        assert q.no_bet_flag
        assert q.source_tier == 5
        assert q.h2h_home > 1.0
        assert q.h2h_draw > 1.0
        assert q.h2h_away > 1.0

    def test_implied_overround(self):
        q = implied_from_probs("Hamburg", "Kaiserslautern", 0.45, 0.25, 0.30, margin=0.06)
        implied = 1 / q.h2h_home + 1 / q.h2h_draw + 1 / q.h2h_away
        # Model has overround baked in → implied > 1.0
        assert implied > 1.0


class TestTheOddsAPIProvider:
    def test_parses_bookmakers_from_hint(self):
        from src.football.odds.the_odds_api import fetch, _parse_bookmakers
        bookmakers = [
            {"key": "b1", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Hamburg", "price": 2.10},
                {"name": "Draw", "price": 3.20},
                {"name": "Kaiserslautern", "price": 3.80},
            ]}, {"key": "totals", "outcomes": [
                {"name": "Over", "price": 1.85, "point": 2.5},
                {"name": "Under", "price": 1.95, "point": 2.5},
            ]}]},
            {"key": "b2", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Hamburg", "price": 2.05},
                {"name": "Draw", "price": 3.30},
                {"name": "Kaiserslautern", "price": 3.90},
            ]}]},
            {"key": "b3", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Hamburg", "price": 2.15},
                {"name": "Draw", "price": 3.15},
                {"name": "Kaiserslautern", "price": 3.75},
            ]}]},
        ]
        q = _parse_bookmakers(bookmakers, "Hamburg", "Kaiserslautern")
        assert q.bookies_count_1x2 == 3
        assert 2.0 < q.h2h_home < 2.2
        assert q.ou_over > 0
        assert q.ou_under > 0

    def test_no_1x2_returns_quote_with_zero(self):
        from src.football.odds.the_odds_api import _parse_bookmakers
        q = _parse_bookmakers([], "Hamburg", "Kaiserslautern")
        assert q.h2h_home == 0.0
        assert q.bookies_count_1x2 == 0
