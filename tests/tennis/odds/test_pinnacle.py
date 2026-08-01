"""J8-I6: Pinnacle-Provider — Parsing + Merger-Integration."""
from __future__ import annotations

from src.tennis.odds import pinnacle
from src.tennis.odds.base import OddsQuote
from src.tennis.odds.merger import ENABLED_SOURCES


def test_pinnacle_registered_in_merger():
    names = {name for name, _tier, _fn in ENABLED_SOURCES}
    assert "pinnacle" in names


def test_american_to_decimal_positive():
    # +150 → 2.50
    assert pinnacle._american_to_decimal(150) == 2.50


def test_american_to_decimal_negative():
    # -180 → 1.556
    assert pinnacle._american_to_decimal(-180) == 1.556


def test_american_to_decimal_invalid():
    assert pinnacle._american_to_decimal(0) == 0.0
    assert pinnacle._american_to_decimal(None) == 0.0
    assert pinnacle._american_to_decimal("abc") == 0.0


def test_fetch_returns_none_without_match(monkeypatch):
    monkeypatch.setattr(pinnacle, "_refresh_leagues", lambda: [])
    q = pinnacle.fetch({"player_a": "Alcaraz", "player_b": "Sinner"})
    assert q is None


def test_fetch_returns_quote_with_valid_match(monkeypatch):
    monkeypatch.setattr(pinnacle, "_refresh_leagues", lambda: [{"id": 100, "name": "ATP"}])
    monkeypatch.setattr(pinnacle, "_refresh_matchups", lambda lid: [{
        "id": 999,
        "participants": [
            {"name": "Carlos Alcaraz"},
            {"name": "Jannik Sinner"},
        ],
    }])
    monkeypatch.setattr(pinnacle, "_fetch_odds_for_matchup", lambda mid: {
        "h2h_a": 1.95, "h2h_b": 1.95, "ah_1_5_a": 2.10, "ah_1_5_b": 1.75,
    })
    q = pinnacle.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"})
    assert isinstance(q, OddsQuote)
    assert q.source == "pinnacle"
    assert q.source_tier == 1
    assert q.h2h_a == 1.95
    # AH-Extension via __dict__
    assert q.__dict__.get("ah_1_5_a") == 2.10


def test_reverse_participant_order_swaps_odds(monkeypatch):
    monkeypatch.setattr(pinnacle, "_refresh_leagues", lambda: [{"id": 100}])
    monkeypatch.setattr(pinnacle, "_refresh_matchups", lambda lid: [{
        "id": 999,
        "participants": [
            {"name": "Jannik Sinner"},   # Pinnacle listet B zuerst
            {"name": "Carlos Alcaraz"},
        ],
    }])
    monkeypatch.setattr(pinnacle, "_fetch_odds_for_matchup", lambda mid: {
        "h2h_a": 1.60, "h2h_b": 2.40, "ah_1_5_a": None, "ah_1_5_b": None,
    })
    # Hint: A=Alcaraz, B=Sinner. Pinnacle-Order ist umgekehrt → Swap
    q = pinnacle.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"})
    assert q is not None
    assert q.h2h_a == 2.40  # war Pinnacle "away" (Alcaraz aus deren Sicht)
    assert q.h2h_b == 1.60
