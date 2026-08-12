"""P1.5-F regression: stale `in_progress` live-score entries must expire.

Prevents the class of production bug where the PWA renders a match as LIVE
based on a `status=in_progress` record that was written days ago and never
cleared (e.g. bet closed, ESPN stopped reporting, or match dropped from the
open-bets loop).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import scripts.tennis_live_push as tlp


def _iso_hours_ago(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_prune_drops_stale_in_progress_entry():
    cache = {
        "stale_match|foo": {
            "home": "A", "away": "B",
            "sets": [[6, 4], [4, 6]],
            "status": "in_progress",
            "updated": _iso_hours_ago(48),
        },
        "fresh_match|bar": {
            "home": "C", "away": "D",
            "sets": [[3, 2]],
            "status": "in_progress",
            "updated": _iso_hours_ago(1),
        },
        "_meta": {"heartbeat_at": _iso_hours_ago(0), "reason": "ok"},
    }
    result = tlp._prune_stale(cache)
    assert "stale_match|foo" not in result
    assert "fresh_match|bar" in result
    assert "_meta" in result


def test_prune_preserves_entries_without_timestamp():
    cache = {"legacy_key": {"home": "X", "away": "Y", "status": "in_progress"}}
    result = tlp._prune_stale(cache)
    assert "legacy_key" in result


def test_prune_handles_unparseable_timestamp():
    cache = {"weird": {"status": "in_progress", "updated": "not-a-date"}}
    result = tlp._prune_stale(cache)
    assert "weird" in result  # kept, not dropped silently


def test_prune_boundary_at_stale_threshold():
    """Entry just past the threshold should be dropped."""
    hours_over = tlp._STALE_LIVE_HOURS + 0.5
    cache = {
        "over": {"status": "in_progress", "updated": _iso_hours_ago(hours_over)},
        "under": {"status": "in_progress", "updated": _iso_hours_ago(tlp._STALE_LIVE_HOURS - 0.5)},
    }
    result = tlp._prune_stale(cache)
    assert "over" not in result
    assert "under" in result
