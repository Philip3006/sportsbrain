"""Tests für _websearch_tennis_fallback + _fetch_events_only (Roadmap J2-I)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts.tennis_scan import (
    _fetch_events_only,
    _parse_event_markets,
    _websearch_tennis_fallback,
)


# ---- WebSearch ------------------------------------------------------

def test_websearch_returns_none_if_ddgs_fails():
    with patch("ddgs.DDGS") as mock_ddg:
        mock_ddg.return_value.text.side_effect = RuntimeError("net down")
        assert _websearch_tennis_fallback("A", "B") is None


def test_websearch_returns_none_if_no_results():
    with patch("ddgs.DDGS") as mock_ddg:
        mock_ddg.return_value.text.return_value = []
        assert _websearch_tennis_fallback("A", "B") is None


def test_websearch_parses_jsonld():
    html = """<html><script type="application/ld+json">
    {"@type": "SportsEvent", "offers": [{"price": 1.85}, {"price": 2.05}]}
    </script></html>"""
    with patch("ddgs.DDGS") as mock_ddg, \
         patch("requests.get") as mock_get:
        mock_ddg.return_value.text.return_value = [{"href": "https://example.com/match"}]
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = html
        out = _websearch_tennis_fallback("Alcaraz", "Sinner")
    assert out == {"a": 1.85, "b": 2.05}


def test_websearch_rejects_implausible_overround():
    html = """<html><script type="application/ld+json">
    {"@type": "SportsEvent", "offers": [{"price": 1.20}, {"price": 1.20}]}
    </script></html>"""
    with patch("ddgs.DDGS") as mock_ddg, \
         patch("requests.get") as mock_get:
        mock_ddg.return_value.text.return_value = [{"href": "https://example.com/match"}]
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = html
        # overround = 1/1.2+1/1.2 = 1.67 → reject
        assert _websearch_tennis_fallback("A", "B") is None


# ---- /events endpoint ----------------------------------------------

def test_fetch_events_returns_empty_on_failure():
    with patch("scripts.tennis_scan.retry_request") as mock_req:
        mock_req.side_effect = RuntimeError("boom")
        assert _fetch_events_only("dummy", "tennis_atp_wimbledon") == []


def test_fetch_events_returns_list():
    resp = MagicMock()
    resp.json.return_value = [{"id": "1", "home_team": "A", "away_team": "B",
                               "commence_time": "2026-06-26T10:00:00Z"}]
    resp.raise_for_status = MagicMock()
    with patch("scripts.tennis_scan.retry_request", return_value=resp):
        out = _fetch_events_only("dummy", "tennis_atp_wimbledon")
    assert len(out) == 1
    assert out[0]["home_team"] == "A"


# ---- _parse_event_markets uses WebSearch when bookmakers sparse ----

def test_parse_event_uses_websearch_when_no_bookmakers():
    event = {
        "id": "x", "home_team": "Alcaraz", "away_team": "Sinner",
        "commence_time": "2026-06-26T10:00:00Z", "bookmakers": [],
    }
    with patch("scripts.tennis_scan._websearch_tennis_fallback",
               return_value={"a": 1.85, "b": 2.05}):
        parsed = _parse_event_markets(event, "tennis_atp_wimbledon")
    assert parsed is not None
    assert parsed["odds_a"] == 1.85
    assert parsed["odds_b"] == 2.05


def test_parse_event_skips_when_websearch_also_fails():
    event = {"id": "x", "home_team": "A", "away_team": "B",
             "commence_time": "2026-06-26T10:00:00Z", "bookmakers": []}
    with patch("scripts.tennis_scan._websearch_tennis_fallback", return_value=None):
        assert _parse_event_markets(event, "tennis_atp_wimbledon") is None
