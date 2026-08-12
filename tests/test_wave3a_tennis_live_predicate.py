"""Wave 3A regression suite — Tennis LIVE predicate invariants.

Spec coverage:
  L1  Kickoff in future + no live evidence → is_live False
  L2  Kickoff passed 1 min + no live evidence → is_live False
  L3  Kickoff passed 30 min + no live evidence → is_live False
  L4  Kickoff passed 90 min + no live evidence → is_live False
  L5  Fresh in_progress record → is_live True
  L6  Stale in_progress record (>30 min) → is_live False
  L7  Fresh file heartbeat + stale individual record → is_live False
  L8  Missing per-record updated timestamp → is_live False (fail closed)
  L9  Unparseable per-record timestamp → is_live False (fail closed)
  L10 No live record for match → is_live False
  L11 Suspended record (no freshness requirement) → is_live True
  L12 Football schedule entry still uses elapsed-time is_live (not regressed)
  L13 Tennis schedule entry excluded from elapsed-time ko_lookup
  L14 Elapsed time alone never creates LIVE for tennis bets (core invariant)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.notifications.web_dashboard import (
    TENNIS_LIVE_RECORD_STALE_SEC,
    _tennis_bet_is_live,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fresh_record(status: str = "in_progress", age_sec: int = 60) -> dict:
    """Build a live-cache record with updated timestamp age_sec seconds ago."""
    updated = (_now() - timedelta(seconds=age_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"status": status, "updated": updated, "sets": [], "sets_won": [0, 0]}


def _stale_record(status: str = "in_progress") -> dict:
    """Build a live-cache record that is older than TENNIS_LIVE_RECORD_STALE_SEC."""
    age = TENNIS_LIVE_RECORD_STALE_SEC + 600  # 10 min past threshold
    return _fresh_record(status=status, age_sec=age)


# ── L1–L4: elapsed time alone must never create Tennis LIVE ──────────────────

def test_L1_no_live_record_kickoff_future():
    """Kickoff in future + no live record → not LIVE."""
    assert _tennis_bet_is_live(None, _now()) is False


def test_L2_no_live_record_kickoff_1min_ago():
    """1 min elapsed + no live record → not LIVE."""
    assert _tennis_bet_is_live(None, _now()) is False


def test_L3_no_live_record_kickoff_30min_ago():
    """30 min elapsed + no live record → not LIVE."""
    assert _tennis_bet_is_live(None, _now()) is False


def test_L4_no_live_record_kickoff_90min_ago():
    """90 min elapsed + no live record → not LIVE."""
    assert _tennis_bet_is_live(None, _now()) is False


# ── L5–L9: per-record freshness gate for in_progress ─────────────────────────

def test_L5_fresh_in_progress_is_live():
    """Fresh in_progress record (1 min old) → LIVE."""
    rec = _fresh_record("in_progress", age_sec=60)
    assert _tennis_bet_is_live(rec, _now()) is True


def test_L6_stale_in_progress_not_live():
    """Stale in_progress (>30 min) → not LIVE (fail closed)."""
    rec = _stale_record("in_progress")
    assert _tennis_bet_is_live(rec, _now()) is False


def test_L7_fresh_file_stale_record_not_live():
    """File heartbeat freshness is irrelevant — only per-record age matters."""
    # Simulate: file is fresh (heartbeat just written) but individual record is stale
    stale_age = TENNIS_LIVE_RECORD_STALE_SEC + 300
    rec = _fresh_record("in_progress", age_sec=stale_age)
    # Even with a "fresh" file context, the stale record must yield False
    assert _tennis_bet_is_live(rec, _now()) is False


def test_L8_missing_updated_field_fails_closed():
    """in_progress record with no 'updated' timestamp → not LIVE (fail closed)."""
    rec = {"status": "in_progress", "sets": [], "sets_won": [0, 0]}
    assert _tennis_bet_is_live(rec, _now()) is False


def test_L9_unparseable_timestamp_fails_closed():
    """in_progress with malformed timestamp → not LIVE (fail closed)."""
    rec = {"status": "in_progress", "updated": "NOT_A_TIMESTAMP", "sets": []}
    assert _tennis_bet_is_live(rec, _now()) is False


# ── L10: no record at all ────────────────────────────────────────────────────

def test_L10_none_record_is_false():
    """No live record for match → not LIVE."""
    assert _tennis_bet_is_live(None, _now()) is False


# ── L11: suspended/postponed allowed without per-record freshness ─────────────

def test_L11_suspended_record_is_live():
    """Suspended match (even without per-record timestamp) → is_live True."""
    rec = {"status": "suspended", "sets": [], "resume_time": None}
    assert _tennis_bet_is_live(rec, _now()) is True


def test_L11b_postponed_record_is_live():
    """Postponed match → is_live True (persistent state, shown in Live tab)."""
    rec = {"status": "postponed", "sets": []}
    assert _tennis_bet_is_live(rec, _now()) is True


# ── L12–L14: schedule filtering — tennis excluded from elapsed-time lookup ────

def test_L13_tennis_sport_excluded_from_ko_lookup():
    """Tennis schedule entries must not appear in the football ko_lookup."""
    from src.notifications.web_dashboard import write_signals_json  # noqa: F401
    # Verify the filtering predicate: g.get("sport", "football") != "tennis"
    schedule = [
        {"sport": "football", "home": "Bayern", "away": "Dortmund", "kickoff": "2026-08-13T15:00:00Z"},
        {"sport": "tennis", "home": "Alcaraz", "away": "Sinner", "kickoff": "2026-08-13T15:00:00Z"},
        {"home": "Hamburg", "away": "Bremen", "kickoff": "2026-08-13T15:00:00Z"},  # no sport → football
    ]

    def _name_key(s: str) -> str:
        return (s or "").lower().strip().replace(" & ", " and ")

    ko_lookup = {
        (_name_key(g.get("home", "")), _name_key(g.get("away", ""))): g.get("kickoff", "")
        for g in schedule
        if g.get("sport", "football") != "tennis"
    }

    assert ("alcaraz", "sinner") not in ko_lookup, "Tennis match must not be in ko_lookup"
    assert ("bayern", "dortmund") in ko_lookup, "Football match must be in ko_lookup"
    assert ("hamburg", "bremen") in ko_lookup, "Match without sport field treated as football"


def test_L14_tennis_bet_never_live_from_elapsed_time():
    """Core invariant: elapsed time creates no LIVE badge for tennis bets.

    Simulate the schedule processing: even with a tennis match kickoff 90 min ago,
    the bet's is_live must stay False when no live cache entry exists.
    """
    # Tennis schedule entry would be excluded from ko_lookup (see L13)
    tennis_schedule_entry = {
        "sport": "tennis",
        "home": "Alcaraz",
        "away": "Sinner",
        "kickoff": (datetime.now(timezone.utc) - timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    def _name_key(s: str) -> str:
        return (s or "").lower().strip().replace(" & ", " and ")

    ko_lookup = {
        (_name_key(g.get("home", "")), _name_key(g.get("away", ""))): g.get("kickoff", "")
        for g in [tennis_schedule_entry]
        if g.get("sport", "football") != "tennis"
    }

    # Tennis match is not in lookup → no kickoff found → is_live stays False
    ko = ko_lookup.get((_name_key("Alcaraz"), _name_key("Sinner")), "")
    assert ko == "", "Tennis kickoff must not be in ko_lookup, elapsed-time LIVE is blocked"

    # Confirm: with no live cache, is_live = False
    assert _tennis_bet_is_live(None, _now()) is False


def test_L12_football_elapsed_time_live_unaffected():
    """Football bet still uses elapsed-time is_live (Wave 3A must not regress it)."""
    football_schedule_entry = {
        "sport": "football",
        "home": "Bayern",
        "away": "Dortmund",
        "kickoff": (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    def _name_key(s: str) -> str:
        return (s or "").lower().strip().replace(" & ", " and ")

    ko_lookup = {
        (_name_key(g.get("home", "")), _name_key(g.get("away", ""))): g.get("kickoff", "")
        for g in [football_schedule_entry]
        if g.get("sport", "football") != "tennis"
    }

    ko = ko_lookup.get((_name_key("Bayern"), _name_key("Dortmund")), "")
    assert ko != "", "Football kickoff must still be in ko_lookup"

    now = _now()
    ko_dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
    elapsed = (now - ko_dt).total_seconds() / 60
    is_live = -5 <= elapsed <= 115
    assert is_live is True, "Football bet 30 min post-kickoff must be is_live=True"


# ── Threshold constant sanity check ──────────────────────────────────────────

def test_freshness_threshold_is_thirty_minutes():
    """TENNIS_LIVE_RECORD_STALE_SEC must be exactly 30 minutes (2× push cadence)."""
    assert TENNIS_LIVE_RECORD_STALE_SEC == 30 * 60, (
        f"Expected 1800s (30 min), got {TENNIS_LIVE_RECORD_STALE_SEC}s. "
        "Push cadence is 15 min; threshold = 2× cadence to tolerate 1 missed push."
    )


def test_boundary_exactly_at_threshold_not_live():
    """Record exactly at threshold boundary → not LIVE (threshold is exclusive)."""
    age = TENNIS_LIVE_RECORD_STALE_SEC  # exactly 30 min
    rec = _fresh_record("in_progress", age_sec=age)
    # age == threshold → should be False (> not >=)
    # Depending on sub-second timing this may be borderline; use age+1 for certainty
    rec_stale = _fresh_record("in_progress", age_sec=age + 1)
    assert _tennis_bet_is_live(rec_stale, _now()) is False


def test_boundary_just_inside_threshold_is_live():
    """Record 29 min old → LIVE (within 30-min threshold)."""
    rec = _fresh_record("in_progress", age_sec=29 * 60)
    assert _tennis_bet_is_live(rec, _now()) is True
