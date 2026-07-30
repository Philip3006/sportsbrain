"""Tests fuer scripts/update_tennis_closing_odds.py (Roadmap TENNIS P1.2)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.update_tennis_closing_odds import (
    _closing_for_bet,
    _extract_h2h,
    _looks_tennis,
    _match_key,
    _parse_kickoff,
)


# --------------------------------------------------------------------------- #
# Utility functions                                                            #
# --------------------------------------------------------------------------- #

def test_match_key_normalizes():
    assert _match_key("Sabalenka", "Rybakina") == "sabalenka vs rybakina"
    assert _match_key("  Alcaraz ", " Zverev ") == "alcaraz vs zverev"


def test_parse_kickoff_iso_utc():
    ts = "2026-08-01T14:00:00Z"
    dt = _parse_kickoff(ts)
    assert dt is not None
    assert dt.year == 2026 and dt.hour == 14 and dt.tzinfo is not None


def test_parse_kickoff_bad_input():
    assert _parse_kickoff("") is None
    assert _parse_kickoff("not-a-date") is None


# --------------------------------------------------------------------------- #
# Tennis-Bet-Detection                                                         #
# --------------------------------------------------------------------------- #

def test_looks_tennis_by_market():
    assert _looks_tennis({"market": "first_set_a"})
    assert _looks_tennis({"market": "ah-1.5_a"})
    assert _looks_tennis({"market": "o/u_sets_2.5_over"})
    assert _looks_tennis({"market": "score_2-1"})


def test_looks_tennis_by_source():
    assert _looks_tennis({"market": "home", "source": "tennis_scanner"})
    assert _looks_tennis({"market": "home", "stake_reason": "tennis_atp_wimbledon"})


def test_looks_tennis_ambiguous_home_not_flagged():
    # 'home' alleine ohne Tennis-Hint -> nicht als Tennis erkennbar
    assert not _looks_tennis({"market": "home", "source": "football_scan"})


# --------------------------------------------------------------------------- #
# H2H Odds Extraction                                                          #
# --------------------------------------------------------------------------- #

def test_extract_h2h_picks_best_price():
    event = {
        "home_team": "Sabalenka",
        "away_team": "Rybakina",
        "bookmakers": [
            {"markets": [{"key": "h2h", "outcomes": [
                {"name": "Sabalenka", "price": 1.72}, {"name": "Rybakina", "price": 2.10}
            ]}]},
            {"markets": [{"key": "h2h", "outcomes": [
                {"name": "Sabalenka", "price": 1.78}, {"name": "Rybakina", "price": 2.05}
            ]}]},
        ],
    }
    a, b = _extract_h2h(event)
    assert a == 1.78  # best (highest) price for A
    assert b == 2.10


def test_extract_h2h_missing_returns_none():
    assert _extract_h2h({"home_team": "A"}) is None  # no away
    assert _extract_h2h({"home_team": "A", "away_team": "B", "bookmakers": []}) is None


def test_extract_h2h_ignores_non_h2h_markets():
    event = {
        "home_team": "X", "away_team": "Y",
        "bookmakers": [{"markets": [
            {"key": "spreads", "outcomes": [{"name": "X", "price": 5.0}]},
            {"key": "h2h", "outcomes": [
                {"name": "X", "price": 1.5}, {"name": "Y", "price": 2.5},
            ]},
        ]}],
    }
    a, b = _extract_h2h(event)
    assert a == 1.5 and b == 2.5


# --------------------------------------------------------------------------- #
# Closing-Odds Resolver                                                        #
# --------------------------------------------------------------------------- #

def _oddsmap(home, away, oa, ob, kickoff=None):
    return {_match_key(home, away): (oa, ob, kickoff)}


def test_closing_for_home_bet():
    om = _oddsmap("Alcaraz", "Zverev", 1.62, 2.35)
    bet = {"market": "home", "home": "Alcaraz", "away": "Zverev"}
    assert _closing_for_bet(bet, om) == 1.62


def test_closing_for_away_bet():
    om = _oddsmap("Alcaraz", "Zverev", 1.62, 2.35)
    bet = {"market": "away", "home": "Alcaraz", "away": "Zverev"}
    assert _closing_for_bet(bet, om) == 2.35


def test_closing_returns_none_for_unsupported_markets():
    """AH/O/U/score/first_set brauchen echte Marktdaten, nicht h2h-Approximation."""
    om = _oddsmap("A", "B", 1.5, 2.5)
    for market in ("ah-1.5_a", "ah+1.5_b", "o/u_sets_2.5_over",
                   "o/u_games_22.5_under", "score_2-1", "first_set_a"):
        bet = {"market": market, "home": "A", "away": "B"}
        assert _closing_for_bet(bet, om) is None, f"Market {market} sollte None sein"


def test_closing_returns_none_for_unknown_match():
    om = _oddsmap("A", "B", 1.5, 2.5)
    bet = {"market": "home", "home": "X", "away": "Y"}
    assert _closing_for_bet(bet, om) is None
