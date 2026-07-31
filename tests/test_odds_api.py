"""Tests for src/data/odds_api.py — Region defaults + 422 fallback."""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from src.config import LINE_SHOPPING_REGIONS
from src.data.odds_api import (
    _fetch_events_and_odds_per_event,
    fetch_event_player_props,
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
