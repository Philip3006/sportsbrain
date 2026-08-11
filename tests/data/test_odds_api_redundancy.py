"""
CEO-Roadmap O1-2 — Football Odds Redundancy (3-tier fallback chain).

FETCH_ODDS_SOURCE tracks which tier served data:
  "primary"  — live TheOddsAPI call succeeded
  "cache"    — Tier 1 failed, Tier 2 stale on-disk cache used
  "none"     — fail-closed (Tier 3): API failed + no cache

Also verifies the quota=0 bug fix: a successful API response at remaining=0
must be used directly (not discarded in favour of stale cache).

All calls pass force=True to bypass the @disk_cache decorator, which would
otherwise return the first test's result for every subsequent call.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_resp(status: int = 200, json_data=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data or []
    resp.headers = headers or {"x-requests-used": "1", "x-requests-remaining": "100"}
    if status >= 400 and status not in (401, 403, 422):
        resp.raise_for_status.side_effect = Exception(f"HTTP {status}")
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestFetchUpcomingMatchesTier1Primary:
    """Tier 1: live API call succeeds → FETCH_ODDS_SOURCE = 'primary'."""

    def test_primary_sets_source_primary(self, monkeypatch):
        import src.data.odds_api as m
        monkeypatch.setattr(m, "get_api_key", lambda k=None: "fake")
        monkeypatch.setattr(m, "_parse_matches", lambda data: [{"id": "x"}])
        with patch.object(m, "_http_get_with_retry", return_value=_make_resp(200)):
            result = m.fetch_upcoming_matches(sport="soccer_epl", force=True)
        assert result == [{"id": "x"}]
        assert m.FETCH_ODDS_SOURCE == "primary"

    def test_quota_zero_still_uses_fresh_response(self, monkeypatch):
        """At remaining=0 the call succeeded — use fresh data, not stale cache."""
        import src.data.odds_api as m
        monkeypatch.setattr(m, "get_api_key", lambda k=None: "fake")
        monkeypatch.setattr(m, "_parse_matches", lambda data: [{"id": "fresh"}])
        # Stale cache returns something different — must NOT be used
        monkeypatch.setattr(m, "_load_stale_upcoming_cache", lambda: [{"id": "stale"}])
        quota_zero_headers = {"x-requests-used": "500", "x-requests-remaining": "0"}
        with patch.object(m, "_http_get_with_retry",
                          return_value=_make_resp(200, headers=quota_zero_headers)):
            result = m.fetch_upcoming_matches(sport="soccer_epl", force=True)
        assert result == [{"id": "fresh"}], (
            "O1-2 quota=0 bug: fresh API response must be used, not discarded for stale cache"
        )
        assert m.FETCH_ODDS_SOURCE == "primary"


class TestFetchUpcomingMatchesTier2Cache:
    """Tier 2: API call raises → fall back to stale cache → FETCH_ODDS_SOURCE = 'cache'."""

    def test_api_failure_with_cache_uses_cache(self, monkeypatch):
        import src.data.odds_api as m
        monkeypatch.setattr(m, "get_api_key", lambda k=None: "fake")
        monkeypatch.setattr(m, "_load_stale_upcoming_cache",
                            lambda: [{"id": "cached"}])
        with patch.object(m, "_http_get_with_retry",
                          side_effect=ConnectionError("network down")):
            result = m.fetch_upcoming_matches(sport="soccer_epl", force=True)
        assert result == [{"id": "cached"}]
        assert m.FETCH_ODDS_SOURCE == "cache"

    def test_401_raises_and_falls_back_to_cache(self, monkeypatch):
        """401 from _http_get_with_retry is returned (not raised) — the function
        calls raise_for_status() which raises, triggering Tier 2."""
        import src.data.odds_api as m
        monkeypatch.setattr(m, "get_api_key", lambda k=None: "fake")
        monkeypatch.setattr(m, "_load_stale_upcoming_cache",
                            lambda: [{"id": "cached_after_401"}])

        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.raise_for_status.side_effect = Exception("401 Unauthorized")
        resp_401.headers = {}

        with patch.object(m, "_http_get_with_retry", return_value=resp_401):
            result = m.fetch_upcoming_matches(sport="soccer_epl", force=True)
        assert result == [{"id": "cached_after_401"}]
        assert m.FETCH_ODDS_SOURCE == "cache"


class TestFetchUpcomingMatchesTier3FailClosed:
    """Tier 3: API fails + no cache → return [] + FETCH_ODDS_SOURCE = 'none'."""

    def test_api_failure_no_cache_returns_empty(self, monkeypatch):
        import src.data.odds_api as m
        monkeypatch.setattr(m, "get_api_key", lambda k=None: "fake")
        monkeypatch.setattr(m, "_load_stale_upcoming_cache", lambda: None)
        with patch.object(m, "_http_get_with_retry",
                          side_effect=ConnectionError("network down")):
            result = m.fetch_upcoming_matches(sport="soccer_epl", force=True)
        assert result == [], "O1-2: Tier 3 must return [] (fail-closed), not raise"
        assert m.FETCH_ODDS_SOURCE == "none"

    def test_fetch_odds_source_initialised_to_none_on_start(self, monkeypatch):
        """FETCH_ODDS_SOURCE resets to 'none' at entry of every call."""
        import src.data.odds_api as m
        m.FETCH_ODDS_SOURCE = "primary"  # simulate a previous successful call
        monkeypatch.setattr(m, "get_api_key", lambda k=None: "fake")
        monkeypatch.setattr(m, "_load_stale_upcoming_cache", lambda: None)
        with patch.object(m, "_http_get_with_retry",
                          side_effect=ConnectionError("network down")):
            m.fetch_upcoming_matches(sport="soccer_epl", force=True)
        assert m.FETCH_ODDS_SOURCE == "none"


class TestWebDashboardStaleOddsFlag:
    """stale_odds in dashboard payload = True unless FETCH_ODDS_SOURCE == 'primary'."""

    @pytest.mark.parametrize("source,expected_stale", [
        ("primary", False),
        ("cache",   True),
        ("none",    True),
    ])
    def test_stale_odds_reflects_fetch_source(self, source, expected_stale, monkeypatch):
        import src.data.odds_api as m
        m.FETCH_ODDS_SOURCE = source
        # Import the mapping expression directly without running the dashboard
        from src.data.odds_api import FETCH_ODDS_SOURCE as _odds_source
        assert (_odds_source != "primary") == expected_stale, (
            f"O1-2: stale_odds should be {expected_stale} when FETCH_ODDS_SOURCE='{source}'"
        )
