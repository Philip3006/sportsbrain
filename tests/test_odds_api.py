"""Tests for src/data/odds_api.py — Region defaults + 422 fallback + O1-1 circuit breaker."""
from __future__ import annotations

import inspect
import pickle
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from src.config import LINE_SHOPPING_REGIONS
from src.data.odds_api import (
    _fetch_events_and_odds_per_event,
    _load_stale_upcoming_cache,
    fetch_event_player_props,
    fetch_upcoming_matches,
)


def test_player_props_default_regions_covers_all_configured():
    """F2: player-props Default darf nicht mehr auf 'eu,uk' regressen."""
    default = inspect.signature(fetch_event_player_props).parameters["regions"].default
    for r in LINE_SHOPPING_REGIONS:
        assert r in default, f"region {r} fehlt im default '{default}'"


def test_fetch_events_and_odds_per_event_aggregates(monkeypatch):
    """F3: bei 422 auf Bulk-Endpoint werden Single-Event-Odds pro Event geholt."""
    events_response = MagicMock()
    events_response.status_code = 200
    events_response.raise_for_status = MagicMock()
    events_response.json = MagicMock(return_value=[
        {"id": "ev1", "home_team": "A", "away_team": "B"},
        {"id": "ev2", "home_team": "C", "away_team": "D"},
    ])

    per_event_response = MagicMock()
    per_event_response.status_code = 200
    per_event_response.json = MagicMock(return_value={
        "id": "ev1", "home_team": "A", "away_team": "B",
        "bookmakers": [{"key": "bm1", "markets": []}],
    })

    call_log = []

    def _fake_get(url, params=None, timeout=None):
        call_log.append(url)
        if url.endswith("/events"):
            return events_response
        return per_event_response

    with patch("src.data.odds_api.requests.get", side_effect=_fake_get):
        out = _fetch_events_and_odds_per_event(
            "soccer_epl", "APIKEY", "eu,us,uk,au", "h2h,totals,spreads",
        )
    assert len(out) == 2  # ein Result pro Event
    assert any("/events" in u and "ev1" not in u for u in call_log)
    assert any("ev1/odds" in u for u in call_log)


def test_fetch_events_returns_empty_when_events_fail():
    """Wenn /events schon crashed, gibt Fallback [] zurück (nicht raise)."""
    with patch("src.data.odds_api.requests.get", side_effect=Exception("network down")):
        out = _fetch_events_and_odds_per_event(
            "soccer_epl", "APIKEY", "eu,us,uk,au", "h2h",
        )
    assert out == []


# ── O1-1: circuit breaker + 401/403/429 handling ──────────────────────────

_STALE_MATCHES = [
    {"match_id": "stale_1", "home_team": "Hamburg", "away_team": "Bremen",
     "home_odds": 2.1, "draw_odds": 3.2, "away_odds": 3.8,
     "spreads": {}, "totals_lines": {}, "bookmakers_h2h": []}
]


def _make_4xx_resp(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock(
        side_effect=requests.HTTPError(f"{status} Client Error")
    )
    resp.headers = {}
    return resp


def _patch_data_cache(monkeypatch, tmp_path):
    """Redirect DATA_CACHE in both odds_api and cache modules to tmp_path."""
    import src.data.odds_api as _oa
    import src.data.cache as _dc
    monkeypatch.setattr(_oa, "DATA_CACHE", tmp_path)
    monkeypatch.setattr(_dc, "DATA_CACHE", tmp_path)
    monkeypatch.setattr(_oa, "get_api_key", lambda *a, **kw: "test-key")


def test_401_loads_stale_cache_not_empty_list(tmp_path, monkeypatch):
    """O1-1: 401 from TheOddsAPI must load stale cache, never return []."""
    _patch_data_cache(monkeypatch, tmp_path)
    (tmp_path / "odds_api_upcoming_wide.pkl").write_bytes(pickle.dumps(_STALE_MATCHES))

    with patch("src.data.odds_api._http_get_with_retry", return_value=_make_4xx_resp(401)):
        with patch("src.signals.provider_budget.is_provider_available", return_value=True):
            result = fetch_upcoming_matches(sport="soccer_test", force=True)

    assert result == _STALE_MATCHES, "401 must load stale cache, not return []"
    import src.data.odds_api as _m
    assert _m.USED_STALE_CACHE is True


def test_403_loads_stale_cache(tmp_path, monkeypatch):
    """O1-1: 403 (auth failure) also loads stale cache."""
    _patch_data_cache(monkeypatch, tmp_path)
    (tmp_path / "odds_api_upcoming_wide.pkl").write_bytes(pickle.dumps(_STALE_MATCHES))

    with patch("src.data.odds_api._http_get_with_retry", return_value=_make_4xx_resp(403)):
        with patch("src.signals.provider_budget.is_provider_available", return_value=True):
            result = fetch_upcoming_matches(sport="soccer_test", force=True)

    assert result == _STALE_MATCHES


def test_circuit_open_skips_api_call(tmp_path, monkeypatch):
    """O1-1: circuit open → stale cache returned WITHOUT any API call."""
    _patch_data_cache(monkeypatch, tmp_path)
    (tmp_path / "odds_api_upcoming_wide.pkl").write_bytes(pickle.dumps(_STALE_MATCHES))

    api_called = []

    def _no_api(*a, **kw):
        api_called.append(1)
        raise AssertionError("API must not be called when circuit is open")

    with patch("src.data.odds_api._http_get_with_retry", side_effect=_no_api):
        with patch("src.signals.provider_budget.is_provider_available", return_value=False):
            result = fetch_upcoming_matches(sport="soccer_test", force=True)

    assert result == _STALE_MATCHES
    assert not api_called, "API was called despite circuit being open"


def test_circuit_open_no_cache_raises(tmp_path, monkeypatch):
    """O1-1: circuit open + no stale cache → RuntimeError (fail closed)."""
    _patch_data_cache(monkeypatch, tmp_path)
    # no cache file written → stale cache unavailable

    with patch("src.signals.provider_budget.is_provider_available", return_value=False):
        try:
            fetch_upcoming_matches(sport="soccer_test", force=True)
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "circuit open" in str(e).lower()


def test_401_records_circuit_error(tmp_path, monkeypatch):
    """O1-1: 401 response opens the circuit breaker."""
    _patch_data_cache(monkeypatch, tmp_path)
    (tmp_path / "odds_api_upcoming_wide.pkl").write_bytes(pickle.dumps(_STALE_MATCHES))

    recorded: list[dict] = []

    def _fake_record(name, code, *, open_circuit=False):
        recorded.append({"name": name, "code": code, "open": open_circuit})

    with patch("src.data.odds_api._http_get_with_retry", return_value=_make_4xx_resp(401)):
        with patch("src.signals.provider_budget.is_provider_available", return_value=True):
            with patch("src.signals.provider_budget.record_error", _fake_record):
                fetch_upcoming_matches(sport="soccer_test", force=True)

    assert any(r["code"] == 401 and r["open"] for r in recorded), \
        "401 must call record_error with open_circuit=True"
