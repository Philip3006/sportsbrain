"""Tests für src/tennis/discovery.py — TheOddsAPI /sports Discovery + Fallback."""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

import pytest

from src.tennis import discovery as disc


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Cache-Pfad in tmp redirecten."""
    cache_file = tmp_path / "tennis_active_sports.json"
    monkeypatch.setattr(disc, "_CACHE_PATH", cache_file)
    return cache_file


def _fake_response(payload):
    class R:
        def __init__(self, j):
            self._j = j
        def json(self):
            return self._j
        def raise_for_status(self):
            pass
    return R(payload)


def test_fetch_active_no_api_key_returns_empty(monkeypatch, tmp_cache):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    assert disc.fetch_active_tennis_sports(api_key=None) == []


def test_fetch_active_filters_tennis_active(monkeypatch, tmp_cache):
    payload = [
        {"key": "tennis_atp_wimbledon", "active": True, "title": "Wimbledon ATP"},
        {"key": "tennis_wta_wimbledon", "active": True, "title": "Wimbledon WTA"},
        {"key": "tennis_atp_us_open", "active": False, "title": "US Open"},  # nicht aktiv → raus
        {"key": "soccer_epl", "active": True, "title": "Premier League"},     # nicht tennis → raus
    ]
    with patch.object(disc, "retry_request", return_value=_fake_response(payload)):
        sports = disc.fetch_active_tennis_sports(api_key="fake", use_cache=False)
    keys = {s["key"] for s in sports}
    assert keys == {"tennis_atp_wimbledon", "tennis_wta_wimbledon"}


def test_fetch_active_uses_cache(monkeypatch, tmp_cache):
    """Cache-Hit innerhalb TTL umgeht API-Call."""
    cached = [{"key": "tennis_atp_french_open", "active": True}]
    tmp_cache.write_text(json.dumps(cached))

    # API darf nicht gerufen werden
    with patch.object(disc, "retry_request", side_effect=AssertionError("must not call API")):
        sports = disc.fetch_active_tennis_sports(api_key="fake", use_cache=True)
    assert sports == cached


def test_fetch_active_api_error_returns_stale_cache(monkeypatch, tmp_cache):
    """API-Fehler → stale Cache als Notfall."""
    stale = [{"key": "tennis_atp_wimbledon", "active": True}]
    tmp_cache.write_text(json.dumps(stale))
    # TTL ablaufen lassen
    import os, time
    old = time.time() - 7200
    os.utime(tmp_cache, (old, old))

    with patch.object(disc, "retry_request", side_effect=RuntimeError("DNS down")):
        sports = disc.fetch_active_tennis_sports(api_key="fake", use_cache=True)
    assert sports == stale


def test_discover_active_tournaments_with_api(monkeypatch, tmp_cache):
    payload = [
        {"key": "tennis_atp_wimbledon", "active": True, "title": "Wimbledon ATP"},
        {"key": "tennis_wta_wimbledon", "active": True, "title": "Wimbledon WTA"},
    ]
    with patch.object(disc, "retry_request", return_value=_fake_response(payload)):
        tournaments = disc.discover_active_tournaments(
            today=date(2026, 7, 5), api_key="fake", use_cache=False
        )
    slugs = {t.slug for t in tournaments}
    assert slugs == {"wimbledon_atp", "wimbledon_wta"}


def test_discover_unknown_key_wrapped(monkeypatch, tmp_cache):
    payload = [
        {"key": "tennis_atp_brand_new_250", "active": True, "title": "Brand New Open"},
    ]
    with patch.object(disc, "retry_request", return_value=_fake_response(payload)):
        tournaments = disc.discover_active_tournaments(
            api_key="fake", use_cache=False
        )
    assert len(tournaments) == 1
    t = tournaments[0]
    assert t.category == "atp250"   # konservativer Default
    assert t.surface == "unknown"
    assert t.tour == "atp"


def test_discover_fallback_to_month_heuristic(monkeypatch, tmp_cache):
    """Kein API-Key + kein Cache → typical_months-Fallback."""
    monkeypatch.delenv("ODDS_API_KEY", raising=False)

    tournaments = disc.discover_active_tournaments(
        today=date(2026, 7, 5), api_key=None, use_cache=False
    )
    slugs = {t.slug for t in tournaments}
    # Juli enthält Wimbledon + ein paar 250er
    assert "wimbledon_atp" in slugs
    assert "wimbledon_wta" in slugs


def test_discover_no_duplicates(monkeypatch, tmp_cache):
    """Falls TheOddsAPI denselben Key zweimal listet → nur 1× im Output."""
    payload = [
        {"key": "tennis_atp_wimbledon", "active": True},
        {"key": "tennis_atp_wimbledon", "active": True},  # Duplikat
    ]
    with patch.object(disc, "retry_request", return_value=_fake_response(payload)):
        tournaments = disc.discover_active_tournaments(
            api_key="fake", use_cache=False
        )
    slugs = [t.slug for t in tournaments]
    assert len(slugs) == len(set(slugs))
