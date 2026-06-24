"""Tests für scripts/tennis_scan.py (Roadmap J2-D)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts.tennis_scan import (
    _parse_event_markets,
    category_min_edge,
    category_mode,
    detect_all_markets,
    format_scan_report,
)
from src.tennis.tournaments import get_tournament


def test_category_min_edge_known():
    assert category_min_edge("grand_slam") == 0.05
    assert category_min_edge("m1000") == 0.08


def test_category_min_edge_unknown_uses_global_default():
    from src.config import MIN_EDGE
    assert category_min_edge("nonexistent") == MIN_EDGE


def test_category_mode_grand_slam_is_live():
    assert category_mode("grand_slam") == "live"


def test_category_mode_atp250_default_shadow():
    assert category_mode("atp250") == "shadow"


def test_category_mode_all_live_override():
    assert category_mode("atp250", all_live=True) == "live"


# ---- _parse_event_markets --------------------------------------------

def _build_event(home, away, h2h=True, spreads=False, totals=False, set_bet=False, n_bookies=3):
    bookmakers = []
    for i in range(n_bookies):
        markets = []
        if h2h:
            markets.append({"key": "h2h", "outcomes": [
                {"name": home, "price": 1.80 + i*0.05},
                {"name": away, "price": 2.10 - i*0.05},
            ]})
        if spreads:
            markets.append({"key": "spreads", "outcomes": [
                {"name": home, "price": 2.00, "point": -1.5},
                {"name": away, "price": 1.85, "point": 1.5},
            ]})
        if totals:
            markets.append({"key": "totals", "outcomes": [
                {"name": "Over", "price": 1.95, "point": 21.5},
                {"name": "Under", "price": 1.85, "point": 21.5},
            ]})
        if set_bet:
            markets.append({"key": "set_betting", "outcomes": [
                {"name": "2-0", "price": 2.20},
                {"name": "2-1", "price": 3.40},
                {"name": "0-2", "price": 4.80},
                {"name": "1-2", "price": 3.80},
            ]})
        bookmakers.append({"key": f"bm{i}", "markets": markets})
    return {
        "id": f"{home}_vs_{away}",
        "home_team": home, "away_team": away,
        "commence_time": "2026-06-25T13:00:00Z",
        "bookmakers": bookmakers,
    }


def test_parse_event_h2h_only():
    event = _build_event("A", "B")
    parsed = _parse_event_markets(event, "tennis_atp_wimbledon")
    assert parsed is not None
    assert parsed["player_a"] == "A"
    assert parsed["player_b"] == "B"
    assert parsed["odds_a"] > 0
    assert parsed["odds_b"] > 0
    assert parsed["totals_over"] == {}


def test_parse_event_with_totals():
    event = _build_event("A", "B", totals=True)
    parsed = _parse_event_markets(event, "tennis_atp_wimbledon")
    assert 21.5 in parsed["totals_over"]
    assert parsed["totals_over"][21.5] > 0


def test_parse_event_with_set_betting():
    event = _build_event("A", "B", set_bet=True)
    parsed = _parse_event_markets(event, "tennis_atp_wimbledon")
    assert "2-0" in parsed["scorelines"]
    assert "2-1" in parsed["scorelines"]


def test_parse_event_skipped_below_min_bookmakers():
    event = _build_event("A", "B", n_bookies=1)
    parsed = _parse_event_markets(event, "tennis_atp_wimbledon")
    # WebSearch stub returns None → match skipped
    assert parsed is None


def test_parse_event_missing_h2h_returns_none():
    event = _build_event("A", "B", h2h=False)
    parsed = _parse_event_markets(event, "tennis_atp_wimbledon")
    assert parsed is None


def test_parse_takes_best_price_across_bookies():
    """3 Bookies mit unterschiedlichen h2h-Preisen → max ausgewählt."""
    event = _build_event("A", "B")
    parsed = _parse_event_markets(event, "tennis_atp_wimbledon")
    # i=0:1.80, i=1:1.85, i=2:1.90 → max ist 1.90
    assert parsed["odds_a"] == pytest.approx(1.90)
    # i=0:2.10, i=1:2.05, i=2:2.00 → max ist 2.10
    assert parsed["odds_b"] == pytest.approx(2.10)


# ---- detect_all_markets aggregator ----------------------------------

def test_detect_all_markets_smoke():
    t = get_tournament("wimbledon_atp")
    assert t is not None
    m = {
        "match_id": "test1",
        "player_a": "A", "player_b": "B",
        "odds_a": 1.80, "odds_b": 2.10,
        "ah_odds_a": 0.0, "ah_odds_b": 0.0,
        "first_set_odds_a": 0.0, "first_set_odds_b": 0.0,
        "totals_over": {3.5: 1.50},
        "totals_under": {3.5: 2.50},
        "scorelines": {},
    }
    probs = {"p_a": 0.55, "p_b": 0.45}
    # Sollte nicht crashen
    signals = detect_all_markets(m, probs, bankroll=100.0, min_edge=0.0, tournament=t)
    assert isinstance(signals, list)


# ---- format_scan_report ----------------------------------------------

def test_format_scan_report_empty():
    text = format_scan_report({}, "2026-06-24")
    assert "Tennis Scan 2026-06-24" in text
    assert "Aktive Turniere:** 0" in text


def test_format_scan_report_with_data():
    t = get_tournament("wimbledon_atp")
    per_t = {
        t.slug: {
            "tournament": t, "signals": [], "n_matches": 3, "mode": "live",
        }
    }
    text = format_scan_report(per_t, "2026-06-24")
    assert "Wimbledon" in text
    assert "🔴 LIVE" in text
    assert "Matches gescannt: 3" in text
