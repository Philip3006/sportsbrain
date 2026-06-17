import pytest
from src.betting.odds_utils import (
    extract_market_odds,
    market_to_all_odds_key,
    overround,
    remove_margin_shin,
)


class TestShinMarginRemoval:
    def test_output_sums_to_one(self):
        odds = (2.0, 3.5, 4.5)
        ph, pd_, pa = remove_margin_shin(odds)
        assert abs(ph + pd_ + pa - 1.0) < 1e-6

    def test_reduces_overround(self):
        odds = (1.9, 3.4, 4.0)
        raw_implied = sum(1 / o for o in odds)
        ph, pd_, pa = remove_margin_shin(odds)
        fair_implied = ph + pd_ + pa
        assert raw_implied > 1.0
        assert abs(fair_implied - 1.0) < 1e-6

    def test_symmetric_50_50(self):
        # Even two-outcome market — no draw
        # Use near-even 3-outcome: heavy favourite
        odds = (1.01, 50.0, 50.0)
        ph, pd_, pa = remove_margin_shin(odds)
        # Home must be very close to 1.0
        assert ph > 0.95

    def test_all_positive(self):
        odds = (2.1, 3.2, 3.8)
        ph, pd_, pa = remove_margin_shin(odds)
        assert ph > 0 and pd_ > 0 and pa > 0

    def test_no_margin_passthrough(self):
        # Fair odds summing to exactly 1 — should pass through unchanged
        fair_odds = (2.0, 4.0, 4.0)  # 0.5 + 0.25 + 0.25 = 1.0
        ph, pd_, pa = remove_margin_shin(fair_odds)
        assert abs(ph - 0.5) < 1e-5
        assert abs(pd_ - 0.25) < 1e-5
        assert abs(pa - 0.25) < 1e-5


class TestOverround:
    def test_positive_vig(self):
        assert overround((1.9, 3.5, 4.0)) > 0

    def test_zero_for_fair_book(self):
        assert abs(overround((2.0, 4.0, 4.0))) < 1e-9


class TestMarketToAllOddsKey:
    def test_maps_standard_totals_line(self):
        assert market_to_all_odds_key("o/u2.5_over") == "over25"

    def test_maps_quarter_totals_line(self):
        assert market_to_all_odds_key("o/u2.25_under") == "under225"

    def test_maps_ah_home_negative_line(self):
        assert market_to_all_odds_key("ah-1.5_home") == "ah-1.5_home"

    def test_maps_ah_away_positive_label_to_home_line_key(self):
        assert market_to_all_odds_key("ah+1.5_away") == "ah-1.5_away"


class TestExtractMarketOdds:
    def test_extracts_double_chance_direct_key(self):
        match = {"dc_1x_odds": 1.42}
        assert extract_market_odds(match, "dc_1x") == 1.42

    def test_extracts_quarter_total_from_dynamic_totals(self):
        match = {"totals_lines": {2.25: {"over": 2.11, "under": 1.76}}}
        assert extract_market_odds(match, "o/u2.25_over") == 2.11

    def test_extracts_away_ah_from_normalized_home_line(self):
        match = {"spreads": {-1.5: {"home": 2.05, "away": 1.81}}}
        assert extract_market_odds(match, "ah+1.5_away") == 1.81
