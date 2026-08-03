"""J8-B12: oddsportal.py — unit tests ohne Netz."""
from __future__ import annotations

import time

import src.tennis.odds.oddsportal as op


def _inject_day(date_iso: str, matches: list[dict]):
    op._BULK[date_iso] = matches
    op._TS[date_iso] = time.time()


# ---------------------------------------------------------------------------
# fetch() — empty / missing players
# ---------------------------------------------------------------------------

def test_fetch_returns_none_for_empty_players():
    assert op.fetch({}) is None
    assert op.fetch({"player_a": "", "player_b": "Sinner"}) is None
    assert op.fetch({"player_a": "Alcaraz", "player_b": ""}) is None


# ---------------------------------------------------------------------------
# fetch() — injected day data
# ---------------------------------------------------------------------------

def test_fetch_forward_match():
    _inject_day("2026-08-03", [
        {"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner", "h2h_a": 1.90, "h2h_b": 2.05}
    ])
    q = op.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner",
                   "commence_time": "2026-08-03T14:00:00Z"})
    assert q is not None
    assert q.source == "oddsportal"
    assert q.source_tier == 2


def test_fetch_reverse_match_swaps_odds():
    _inject_day("2026-08-03", [
        {"player_a": "Jannik Sinner", "player_b": "Carlos Alcaraz", "h2h_a": 2.05, "h2h_b": 1.90}
    ])
    q = op.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner",
                   "commence_time": "2026-08-03T14:00:00Z"})
    assert q is not None
    assert abs(q.h2h_a - 1.90) < 0.01
    assert abs(q.h2h_b - 2.05) < 0.01


def test_fetch_no_match_returns_none():
    _inject_day("2026-08-04", [
        {"player_a": "Nadal", "player_b": "Federer", "h2h_a": 1.80, "h2h_b": 2.10}
    ])
    q = op.fetch({"player_a": "Alcaraz", "player_b": "Sinner",
                   "commence_time": "2026-08-04T10:00:00Z"})
    assert q is None


# ---------------------------------------------------------------------------
# J8-B5: Truncation-Limit 500 + Warn-Log
# ---------------------------------------------------------------------------

def test_truncation_limit_in_source():
    """J8-B5: Truncation-Limit muss 500 betragen (steht in _fetch_day-Quellcode)."""
    import inspect
    src = inspect.getsource(op._fetch_day)
    assert "500" in src, "Truncation-Limit muss 500 sein (J8-B5)"


def test_fetch_without_commence_time_uses_today():
    """fetch() ohne commence_time darf nicht crashen — nutzt utcnow()."""
    _inject_day(time.strftime("%Y-%m-%d"), [
        {"player_a": "Nobody", "player_b": "Anyone", "h2h_a": 2.0, "h2h_b": 1.95}
    ])
    result = op.fetch({"player_a": "Alcaraz", "player_b": "Sinner"})
    assert result is None  # kein Match, aber kein Crash


# ---------------------------------------------------------------------------
# _fetch_day() — cached result reused
# ---------------------------------------------------------------------------

def test_fetch_day_uses_cache():
    date = "2026-08-05"
    _inject_day(date, [{"player_a": "A", "player_b": "B", "h2h_a": 2.0, "h2h_b": 1.95}])
    result1 = op._fetch_day(date)
    result2 = op._fetch_day(date)
    assert result1 is result2  # same list object from cache
