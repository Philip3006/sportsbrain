"""Phase 1.3 — Sofascore xG cache resilience.

Sofascore RapidAPI quota silently zeroes out under load (see
sofascore_rate_limit_backlog.md); previously the scanner lost all live-xG
features the moment that happened. These tests pin the fallback chain:

  1. Fresh cache (<3h)  → return cache, no network.
  2. Stale cache + rate-limit → return stale cache + WARN.
  3. No cache + rate-limit → empty DataFrame (downstream disables xG).
"""
from unittest import mock

import pandas as pd
import pytest

from src.data import sofascore


SAMPLE_DF = pd.DataFrame([
    {"home_team": "Mexico", "away_team": "South Africa",
     "date": pd.Timestamp("2026-06-11"), "home_xg": 1.4, "away_xg": 2.1,
     "tournament": "FIFA World Cup"},
])


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    cache = tmp_path / "sofascore_xg.pkl"
    monkeypatch.setattr(sofascore, "_CACHE_PATH", cache)
    return cache


class TestCacheFreshShortCircuits:
    def test_fresh_cache_skips_network(self, tmp_cache, monkeypatch):
        SAMPLE_DF.to_pickle(tmp_cache)
        # If we touched the network we'd raise — assert that doesn't happen.
        called = {"n": 0}

        def boom(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("network should not be touched on fresh cache")

        monkeypatch.setattr(sofascore, "fetch_wc2026_event_ids", boom)
        # Force API key present so we don't bail on the missing-key branch.
        monkeypatch.setenv("API_FOOTBALL_KEY", "test")

        out = sofascore.fetch_wc2026_xg(force=False)
        assert called["n"] == 0
        assert len(out) == 1
        assert out.iloc[0]["home_team"] == "Mexico"


class TestStaleCacheFallback:
    def test_rate_limit_returns_stale_cache(self, tmp_cache, monkeypatch):
        SAMPLE_DF.to_pickle(tmp_cache)
        # Age the cache: bump mtime backwards by 5h (between fresh-ttl=3h and stale-max=48h)
        import os, time
        old_mtime = time.time() - 5 * 3600
        os.utime(tmp_cache, (old_mtime, old_mtime))

        monkeypatch.setenv("API_FOOTBALL_KEY", "test")
        monkeypatch.setattr(
            sofascore, "fetch_wc2026_event_ids",
            lambda: (_ for _ in ()).throw(RuntimeError("sofascore rate-limited (429)")),
        )

        out = sofascore.fetch_wc2026_xg(force=False)
        # Stale cache returned despite rate-limit
        assert len(out) == 1
        assert out.iloc[0]["home_xg"] == 1.4

    def test_force_with_rate_limit_during_per_match_loop(self, tmp_cache, monkeypatch):
        SAMPLE_DF.to_pickle(tmp_cache)
        # Stale-but-usable cache (5h old) — within _CACHE_STALE_MAX_H
        import os, time
        old = time.time() - 5 * 3600
        os.utime(tmp_cache, (old, old))

        monkeypatch.setenv("API_FOOTBALL_KEY", "test")
        # Fixtures return one finished event …
        monkeypatch.setattr(sofascore, "fetch_wc2026_event_ids", lambda: [
            {"event_id": 1, "home_team": "A", "away_team": "B",
             "date": pd.Timestamp("2026-06-12"), "status": "finished",
             "status_desc": "Ended"},
        ])
        # … but fetch_match_xg raises rate-limit on the first call.
        def boom(eid):
            raise RuntimeError("sofascore rate-limited (429)")
        monkeypatch.setattr(sofascore, "fetch_match_xg", boom)

        out = sofascore.fetch_wc2026_xg(force=True)
        # Fell back to stale cache instead of returning empty
        assert len(out) == 1
        assert out.iloc[0]["away_team"] == "South Africa"


class TestNoCacheGracefulEmpty:
    def test_no_cache_no_key_returns_empty(self, tmp_cache, monkeypatch):
        monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
        out = sofascore.fetch_wc2026_xg(force=False)
        assert out.empty
        # Schema must still match downstream expectations.
        for col in ("home_team", "away_team", "date", "home_xg", "away_xg", "tournament"):
            assert col in out.columns
