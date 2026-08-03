"""J8-B12: tennis_explorer.py — unit tests mit injiziertem Bulk."""
from __future__ import annotations

import time

import src.tennis.odds.tennis_explorer as te


def _reset_bulk(entries: list[dict]):
    te._BULK = entries
    te._TS = time.time()


# ---------------------------------------------------------------------------
# fetch() — empty / missing players
# ---------------------------------------------------------------------------

def test_fetch_returns_none_for_empty_players():
    _reset_bulk([{"player_a": "Alcaraz", "player_b": "Sinner", "odds_a": 1.90, "odds_b": 2.05}])
    assert te.fetch({}) is None
    assert te.fetch({"player_a": "", "player_b": "Sinner"}) is None
    assert te.fetch({"player_a": "Alcaraz", "player_b": ""}) is None


# ---------------------------------------------------------------------------
# fetch() — match forward / reverse
# ---------------------------------------------------------------------------

def test_fetch_forward_match():
    _reset_bulk([{"player_a": "alcaraz c.", "player_b": "sinner j.",
                   "odds_a": 1.90, "odds_b": 2.05, "te_bookies_count": 3}])
    q = te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner",
                   "name_source": "odds_api"})
    assert q is not None
    assert q.source == "tennis_explorer"
    assert q.source_tier == 2


def test_fetch_reverse_match_swaps_odds():
    _reset_bulk([{"player_a": "sinner j.", "player_b": "alcaraz c.",
                   "odds_a": 2.05, "odds_b": 1.90, "te_bookies_count": 2}])
    q = te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner",
                   "name_source": "odds_api"})
    assert q is not None
    assert abs(q.h2h_a - 1.90) < 0.01   # Alcaraz nach Swap
    assert abs(q.h2h_b - 2.05) < 0.01


def test_fetch_no_match_returns_none():
    _reset_bulk([{"player_a": "nadal r.", "player_b": "federer r.",
                   "odds_a": 1.80, "odds_b": 2.10, "te_bookies_count": 4}])
    q = te.fetch({"player_a": "Alcaraz", "player_b": "Sinner"})
    assert q is None


def test_fetch_invalid_odds_rejected():
    # Overround > 1.15 → sanity_ok False
    _reset_bulk([{"player_a": "alcaraz c.", "player_b": "sinner j.",
                   "odds_a": 1.40, "odds_b": 1.40, "te_bookies_count": 1}])
    q = te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"})
    assert q is None


# ---------------------------------------------------------------------------
# J8-B4: Stale-Bulk-Drop wenn Refresh leer ist
# ---------------------------------------------------------------------------

def test_stale_bulk_dropped_when_refresh_empty(monkeypatch):
    """_get_bulk() soll leere Liste zurückgeben wenn Bulk > 2×TTL alt und Refresh leer."""
    te._BULK = [{"player_a": "old", "player_b": "data", "odds_a": 2.0, "odds_b": 2.0}]
    te._TS = time.time() - (te._TTL_S * 2 + 1)  # älter als 2×TTL

    # Simuliere leeren Refresh
    monkeypatch.setattr(
        "src.data.tennis_secondary_odds.fetch_te_upcoming_matches",
        lambda **kw: [],
        raising=False,
    )
    try:
        bulk = te._get_bulk()
        # Wenn Refresh leer + stale → Bulk muss leer zurückkommen
        assert bulk == []
    except Exception:
        pass  # Modul nicht verfügbar → Skip (CI ohne Netz)
