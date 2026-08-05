"""J8-B12: betfair.py — unit tests ohne Netz/Credentials."""
from __future__ import annotations

import importlib
import sys

import pytest

import src.tennis.odds.betfair as bf


# ---------------------------------------------------------------------------
# _match_key
# ---------------------------------------------------------------------------

def test_match_key_strips_dots_and_lowercases():
    assert bf._match_key("Alcaraz C.") == "alcaraz c"
    assert bf._match_key("SINNER") == "sinner"


# ---------------------------------------------------------------------------
# fetch() — without credentials
# ---------------------------------------------------------------------------

def test_fetch_returns_none_without_env_vars(monkeypatch):
    monkeypatch.delenv("BETFAIR_APP_KEY", raising=False)
    monkeypatch.delenv("BETFAIR_USERNAME", raising=False)
    monkeypatch.delenv("BETFAIR_PASSWORD", raising=False)
    result = bf.fetch({"player_a": "Alcaraz", "player_b": "Sinner"})
    assert result is None


def test_fetch_returns_none_for_empty_players(monkeypatch):
    monkeypatch.setenv("BETFAIR_APP_KEY", "key")
    monkeypatch.setenv("BETFAIR_USERNAME", "user")
    monkeypatch.setenv("BETFAIR_PASSWORD", "pass")
    assert bf.fetch({"player_a": "", "player_b": "Sinner"}) is None
    assert bf.fetch({"player_a": "Alcaraz", "player_b": ""}) is None
    assert bf.fetch({}) is None


# ---------------------------------------------------------------------------
# _refresh_bulk() — with injected bulk data
# ---------------------------------------------------------------------------

def test_bulk_match_forward(monkeypatch):
    """Liefert Quote wenn Spieler in injiziertem Bulk vorwärts matchen."""
    monkeypatch.setenv("BETFAIR_APP_KEY", "key")
    monkeypatch.setenv("BETFAIR_USERNAME", "user")
    monkeypatch.setenv("BETFAIR_PASSWORD", "pass")
    # Inject fake bulk directly via thread-safe cache
    bf._bulk.set({"mkt1": {"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner",
                          "h2h_a": 1.90, "h2h_b": 2.05}})

    q = bf.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"})
    assert q is not None
    assert q.source == "betfair"
    assert q.source_tier == 1
    assert abs(q.h2h_a - 1.90) < 0.01
    assert abs(q.h2h_b - 2.05) < 0.01


def test_bulk_match_reverse(monkeypatch):
    """Odds werden getauscht wenn Spieler-Reihenfolge umgekehrt."""
    monkeypatch.setenv("BETFAIR_APP_KEY", "key")
    monkeypatch.setenv("BETFAIR_USERNAME", "user")
    monkeypatch.setenv("BETFAIR_PASSWORD", "pass")
    bf._bulk.set({"mkt1": {"player_a": "Jannik Sinner", "player_b": "Carlos Alcaraz",
                          "h2h_a": 2.05, "h2h_b": 1.90}})

    q = bf.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"})
    assert q is not None
    assert abs(q.h2h_a - 1.90) < 0.01  # Alcaraz-Preis nach swap
    assert abs(q.h2h_b - 2.05) < 0.01


def test_bulk_no_match_returns_none(monkeypatch):
    monkeypatch.setenv("BETFAIR_APP_KEY", "key")
    monkeypatch.setenv("BETFAIR_USERNAME", "user")
    monkeypatch.setenv("BETFAIR_PASSWORD", "pass")
    bf._bulk.set({"mkt1": {"player_a": "Nadal", "player_b": "Federer",
                          "h2h_a": 1.80, "h2h_b": 2.10}})

    q = bf.fetch({"player_a": "Alcaraz", "player_b": "Sinner"})
    assert q is None


def test_bulk_insane_odds_rejected(monkeypatch):
    """sanity_ok muss fehlschlagen wenn Overround > 1.15."""
    monkeypatch.setenv("BETFAIR_APP_KEY", "key")
    monkeypatch.setenv("BETFAIR_USERNAME", "user")
    monkeypatch.setenv("BETFAIR_PASSWORD", "pass")
    bf._bulk.set({"mkt1": {"player_a": "Alcaraz", "player_b": "Sinner",
                          "h2h_a": 1.40, "h2h_b": 1.40}})  # implied sum 1.43 > 1.15

    q = bf.fetch({"player_a": "Alcaraz", "player_b": "Sinner"})
    assert q is None
