import pytest
from src.betting.odds_utils import overround, remove_margin_shin


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
