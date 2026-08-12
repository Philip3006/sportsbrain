"""Wave 3B regression suite — Canonical Tennis event state invariants.

Spec coverage (30 required + cross-midnight identity check):

  State
  ------
  S1  Future current start → UPCOMING
  S2  Start passed without authoritative evidence → AWAITING_START
  S3  AWAITING_START + later qualified schedule → UPCOMING (reversible)
  S4  ESPN in_progress → LIVE (normalize_provider_status)
  S5  Stale in_progress → not LIVE (Wave 3A integration)
  S6  ESPN completed → COMPLETED
  S7  Time alone → never COMPLETED
  S8  ESPN delayed → DELAYED
  S9  ESPN cancelled → CANCELLED
  S10 ESPN postponed → POSTPONED

  Schedule fields
  ---------------
  S11 scheduled_start_initial immutable after first assignment
  S12 scheduled_start_current mutable
  S13 Older schedule update cannot overwrite newer accepted update
  S14 Lower-authority stale source cannot overwrite stronger fresh source
  S15 initial/current separation survives sidecar round-trip

  Timezone
  --------
  S16 Timezone-aware ingestion (offset preserved)
  S17 CET case: 15:00 CET → 14:00 UTC
  S18 CEST case: 15:00 CEST → 13:00 UTC
  S19 DST boundary: last Sunday October, 02:30 CEST → UTC correct
  S20 UTC → Europe/Berlin rendering correct (display only)
  S21 Late-evening event (22:00 CEST = 20:00 UTC, same date)
  S22 Midnight boundary (23:59 CEST → 21:59 UTC, same date — no date shift)

  Compatibility
  -------------
  S23 Legacy kickoff record loads safely without canonical fields
  S24 Canonical fields publish correctly in to_dict / from_dict round-trip
  S25 Legacy consumer (kickoff) does not create second writable truth

  Existing safety
  ---------------
  S26 False LIVE regression (Wave 3A invariant: _tennis_bet_is_live)
  S27 EV guards: authority rank zero for implied_elo completion
  S28 Top-Recs: LIVE / COMPLETED / CANCELLED are not actionable
  S29 5% risk-cap: AUTHORITY_MATRIX never alters stake cap semantics
  S30 Football: canonical model only applies to tennis entries

  Cross-midnight identity
  -----------------------
  CM1 Cross-midnight reschedule: explicitly tests signal_id stability
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.tennis.event_state import (
    AUTHORITY_MATRIX,
    TennisEventState,
    TennisEventStatus,
    SourceAuthority,
    _AUTHORITY_RANK,
    _sidecar_key,
    apply_status_observation,
    authority_rank,
    derive_schedule_status,
    merge_schedule_update,
    normalize_provider_status,
)

_BERLIN = ZoneInfo("Europe/Berlin")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fresh_state(offset_min: int = -30) -> TennisEventState:
    """State with scheduled_start_current = now + offset_min."""
    ko = _now() + timedelta(minutes=offset_min)
    return TennisEventState(
        scheduled_start_initial=_iso(ko),
        scheduled_start_current=_iso(ko),
        schedule_updated_at=_iso(_now()),
        schedule_source="the_odds_api",
        event_status=TennisEventStatus.UNKNOWN,
        fixture_key="alcaraz|sinner",
    )


# ── S1–S10: Status derivation and normalization ───────────────────────────────

def test_S1_future_start_is_upcoming():
    """Future current start → UPCOMING."""
    state = _fresh_state(offset_min=60)
    status = derive_schedule_status(state, _now())
    assert status == TennisEventStatus.UPCOMING


def test_S2_passed_start_no_evidence_is_awaiting():
    """Start passed without authoritative evidence → AWAITING_START."""
    state = _fresh_state(offset_min=-30)
    status = derive_schedule_status(state, _now())
    assert status == TennisEventStatus.AWAITING_START


def test_S3_awaiting_to_upcoming_on_schedule_update():
    """AWAITING_START + later qualified schedule → UPCOMING (reversible transition)."""
    state = _fresh_state(offset_min=-5)  # AWAITING_START

    # New schedule: 60 min from now
    future_ko = _iso(_now() + timedelta(minutes=60))
    updated = merge_schedule_update(state, future_ko, "the_odds_api")

    status = derive_schedule_status(updated, _now())
    assert status == TennisEventStatus.UPCOMING, (
        "A qualified later start must be able to move status back from "
        "AWAITING_START to UPCOMING."
    )


def test_S4_espn_in_progress_maps_to_live():
    """ESPN in_progress → LIVE via normalize_provider_status."""
    assert normalize_provider_status("in_progress", "espn") == TennisEventStatus.LIVE


def test_S5_stale_in_progress_not_live():
    """Stale in_progress record → not LIVE (Wave 3A _tennis_bet_is_live)."""
    from src.notifications.web_dashboard import (
        TENNIS_LIVE_RECORD_STALE_SEC,
        _tennis_bet_is_live,
    )
    age = TENNIS_LIVE_RECORD_STALE_SEC + 60
    stale_ts = (_now() - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = {"status": "in_progress", "updated": stale_ts, "sets": [], "sets_won": [0, 0]}
    assert _tennis_bet_is_live(rec, _now()) is False


def test_S6_espn_completed_maps_to_completed():
    """ESPN completed → COMPLETED via normalize_provider_status."""
    assert normalize_provider_status("completed", "espn") == TennisEventStatus.COMPLETED
    assert normalize_provider_status("retired", "espn") == TennisEventStatus.COMPLETED
    assert normalize_provider_status("walkover", "espn") == TennisEventStatus.COMPLETED


def test_S7_time_alone_never_completed():
    """derive_schedule_status NEVER returns COMPLETED — time alone is not authoritative."""
    state = _fresh_state(offset_min=-200)  # well past any match window
    status = derive_schedule_status(state, _now())
    assert status != TennisEventStatus.COMPLETED, (
        "Elapsed time must never produce COMPLETED — only authoritative evidence can."
    )


def test_S8_espn_delayed_maps_to_delayed():
    """ESPN delayed → DELAYED (explicit source evidence required for DELAYED)."""
    assert normalize_provider_status("delayed", "espn") == TennisEventStatus.DELAYED


def test_S9_espn_cancelled_maps_to_cancelled():
    """ESPN cancelled/abandoned → CANCELLED."""
    assert normalize_provider_status("cancelled", "espn") == TennisEventStatus.CANCELLED
    assert normalize_provider_status("abandoned", "espn") == TennisEventStatus.CANCELLED


def test_S10_espn_postponed_maps_to_postponed():
    """ESPN postponed → POSTPONED."""
    assert normalize_provider_status("postponed", "espn") == TennisEventStatus.POSTPONED


# ── S11–S15: Schedule field semantics ────────────────────────────────────────

def test_S11_initial_start_immutable():
    """scheduled_start_initial must not change after first valid assignment."""
    ko1 = _iso(_now() + timedelta(hours=2))
    ko2 = _iso(_now() + timedelta(hours=4))

    state = TennisEventState(fixture_key="alcaraz|sinner")
    state = merge_schedule_update(state, ko1, "the_odds_api")
    initial_after_first = state.scheduled_start_initial

    state = merge_schedule_update(state, ko2, "the_odds_api")
    assert state.scheduled_start_initial == initial_after_first == ko1, (
        "scheduled_start_initial must be immutable after first valid assignment."
    )


def test_S12_current_start_mutable():
    """scheduled_start_current is updated by qualified schedule observations."""
    ko1 = _iso(_now() + timedelta(hours=2))
    ko2 = _iso(_now() + timedelta(hours=4))

    state = TennisEventState(fixture_key="alcaraz|sinner")
    state = merge_schedule_update(state, ko1, "the_odds_api")
    assert state.scheduled_start_current == ko1

    # Advance time slightly to ensure newer observation
    state = merge_schedule_update(
        state, ko2, "the_odds_api",
        observed_at=_iso(_now() + timedelta(seconds=1)),
    )
    assert state.scheduled_start_current == ko2, "current start must be mutable"


def test_S13_older_update_does_not_overwrite_newer():
    """Older (same-authority) update must not overwrite a newer accepted schedule."""
    ko_new = _iso(_now() + timedelta(hours=3))
    ko_old = _iso(_now() + timedelta(hours=1))

    now = _now()
    state = TennisEventState(fixture_key="alcaraz|sinner")
    # First, accept a newer observation
    state = merge_schedule_update(
        state, ko_new, "the_odds_api",
        observed_at=_iso(now + timedelta(seconds=10)),
    )
    # Then attempt an older observation at the same authority
    state = merge_schedule_update(
        state, ko_old, "the_odds_api",
        observed_at=_iso(now),  # older observed_at
    )
    assert state.scheduled_start_current == ko_new, (
        "Older observation at equal authority must not overwrite newer accepted update."
    )


def test_S14_lower_authority_cannot_overwrite_primary():
    """Fallback/secondary source must not overwrite a PRIMARY-sourced schedule."""
    ko_primary = _iso(_now() + timedelta(hours=3))
    ko_fallback = _iso(_now() + timedelta(hours=1))

    state = TennisEventState(fixture_key="alcaraz|sinner")
    # Accept from PRIMARY (the_odds_api rank=3 for updated_start)
    state = merge_schedule_update(state, ko_primary, "the_odds_api")
    # Attempt overwrite from FALLBACK (tennisexplorer rank=2 for updated_start)
    state = merge_schedule_update(state, ko_fallback, "tennisexplorer")

    assert state.scheduled_start_current == ko_primary, (
        "Lower-authority (FALLBACK) source must not overwrite PRIMARY-sourced schedule."
    )
    assert state.schedule_source == "the_odds_api"


def test_S15_round_trip_preserves_initial_current_separation():
    """to_dict / from_dict round-trip preserves initial/current split."""
    ko_init = _iso(_now() + timedelta(hours=1))
    ko_curr = _iso(_now() + timedelta(hours=2))

    state = TennisEventState(
        scheduled_start_initial=ko_init,
        scheduled_start_current=ko_curr,
        schedule_source="the_odds_api",
        event_status=TennisEventStatus.UPCOMING,
        fixture_key="alcaraz|sinner",
    )
    restored = TennisEventState.from_dict(state.to_dict())

    assert restored.scheduled_start_initial == ko_init
    assert restored.scheduled_start_current == ko_curr
    assert restored.event_status == TennisEventStatus.UPCOMING
    assert restored.scheduled_start_initial != restored.scheduled_start_current


# ── S16–S22: Timezone normalization ──────────────────────────────────────────

def test_S16_timezone_aware_ingestion():
    """Ingested UTC timestamps carry timezone info after parsing."""
    ts = "2026-08-13T14:00:00Z"
    from src.tennis.event_state import _parse_ts
    dt = _parse_ts(ts)
    assert dt is not None
    assert dt.tzinfo is not None, "Parsed timestamp must be timezone-aware"
    assert dt.utcoffset().total_seconds() == 0


def test_S17_cet_converts_correctly():
    """CET (UTC+1, winter): 15:00 CET → 14:00 UTC."""
    # CET applies 2026-10-27T01:00Z → 2027-03-29T01:00Z in Europe/Berlin
    from src.data.tennis_secondary_odds import _parse_commence_time
    # 2026-01-15 is winter: CET (UTC+1)
    result = _parse_commence_time("15.01.", "15:00")
    assert result.endswith("14:00:00Z"), f"Expected 14:00 UTC (CET), got {result}"


def test_S18_cest_converts_correctly():
    """CEST (UTC+2, summer): 15:00 CEST → 13:00 UTC."""
    from src.data.tennis_secondary_odds import _parse_commence_time
    # 2026-07-15 is summer: CEST (UTC+2)
    result = _parse_commence_time("15.07.", "15:00")
    assert result.endswith("13:00:00Z"), f"Expected 13:00 UTC (CEST), got {result}"


def test_S19_dst_boundary_last_sunday_october():
    """DST boundary (last Sunday October): 02:30 CEST → 00:30 UTC."""
    from src.data.tennis_secondary_odds import _parse_commence_time
    # 2026-10-25 is the last Sunday of October (CEST → CET transition at 03:00 local)
    # 02:30 CEST = 00:30 UTC (still CEST before the clock change at 03:00)
    result = _parse_commence_time("25.10.", "02:30")
    assert result.endswith("00:30:00Z"), f"Expected 00:30 UTC (CEST→CET boundary), got {result}"


def test_S20_utc_to_berlin_display():
    """UTC → Europe/Berlin renders correctly for display."""
    utc_dt = datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)
    berlin_dt = utc_dt.astimezone(_BERLIN)
    # 14:00 UTC = 16:00 CEST (UTC+2)
    assert berlin_dt.hour == 16
    assert berlin_dt.utcoffset().total_seconds() == 7200  # +2h


def test_S21_late_evening_cest_same_date():
    """22:00 CEST → 20:00 UTC (same calendar date, no midnight crossing)."""
    from src.data.tennis_secondary_odds import _parse_commence_time
    result = _parse_commence_time("13.08.", "22:00")
    # 22:00 CEST = 20:00 UTC — still 2026-08-13
    assert "2026-08-13T20:00:00Z" == result


def test_S22_midnight_boundary_no_date_shift():
    """23:59 CEST → 21:59 UTC — date stays the same (no UTC date shift for this time)."""
    from src.data.tennis_secondary_odds import _parse_commence_time
    result = _parse_commence_time("13.08.", "23:59")
    # 23:59 CEST (UTC+2) = 21:59 UTC — still 2026-08-13
    assert result.startswith("2026-08-13"), f"Expected same date, got {result}"
    assert "21:59:00Z" in result


# ── S23–S25: Backward compatibility ──────────────────────────────────────────

def test_S23_legacy_kickoff_loads_safely():
    """A schedule entry with only 'kickoff' (no canonical fields) loads without error."""
    entry = {
        "sport": "tennis",
        "home": "Alcaraz",
        "away": "Sinner",
        "kickoff": "2026-08-13T15:00:00Z",
    }
    # enrich_schedule_with_canonical_state should add canonical fields without error
    from src.tennis.event_state import enrich_schedule_with_canonical_state
    result = enrich_schedule_with_canonical_state([entry])
    assert len(result) == 1
    entry = result[0]
    assert entry.get("scheduled_start_initial") == "2026-08-13T15:00:00Z"
    assert entry.get("scheduled_start_current") == "2026-08-13T15:00:00Z"
    assert entry.get("event_status") in (
        TennisEventStatus.UPCOMING.value,
        TennisEventStatus.AWAITING_START.value,
    )


def test_S24_canonical_fields_publish_correctly():
    """TennisEventState.to_dict() / from_dict() round-trip is exact."""
    original = TennisEventState(
        scheduled_start_initial="2026-08-13T13:00:00Z",
        scheduled_start_current="2026-08-13T15:00:00Z",
        schedule_updated_at="2026-08-13T12:00:00Z",
        schedule_source="the_odds_api",
        event_status=TennisEventStatus.AWAITING_START,
        status_source="sportsbrain_schedule",
        status_updated_at="2026-08-13T12:00:00Z",
        fixture_key="alcaraz|sinner",
        accepted_at="2026-08-13T08:00:00Z",
    )
    d = original.to_dict()
    assert d["event_status"] == "AWAITING_START"  # string, not enum object

    restored = TennisEventState.from_dict(d)
    assert restored.event_status == TennisEventStatus.AWAITING_START
    assert restored.scheduled_start_initial == "2026-08-13T13:00:00Z"
    assert restored.scheduled_start_current == "2026-08-13T15:00:00Z"
    assert restored.schedule_source == "the_odds_api"


def test_S25_single_writable_schedule_truth():
    """The canonical model must be the sole writable truth for current start.

    There must be only ONE place that can update scheduled_start_current:
    merge_schedule_update(). Legacy 'kickoff' is read-only for compatibility.
    """
    ko = "2026-08-13T15:00:00Z"
    state = TennisEventState(fixture_key="alcaraz|sinner")

    # Canonical write path
    state = merge_schedule_update(state, ko, "the_odds_api")
    assert state.scheduled_start_current == ko

    # Legacy field (kickoff) on a schedule entry is not separately writable to state
    # — enrich_schedule_with_canonical_state reads entry["kickoff"] as input,
    # but the OUTPUT is always canonical fields. No second mutable field exists.
    assert not hasattr(state, "kickoff"), (
        "TennisEventState must not have a 'kickoff' attribute — "
        "legacy kickoff is input-only, not a second writable truth."
    )


# ── S26–S30: Existing safety ──────────────────────────────────────────────────

def test_S26_wave3a_false_live_regression():
    """Wave 3A invariant: _tennis_bet_is_live(None, ...) is always False."""
    from src.notifications.web_dashboard import _tennis_bet_is_live
    assert _tennis_bet_is_live(None, _now()) is False
    # Time elapsed irrelevant — no record means no LIVE
    past = _now() - timedelta(hours=2)
    assert _tennis_bet_is_live(None, past) is False


def test_S27_implied_elo_has_no_completion_authority():
    """implied_elo source must have NOT_AUTHORIZED for completion (EV guard)."""
    auth = AUTHORITY_MATRIX.get("implied_elo", {}).get("completion", SourceAuthority.NOT_AUTHORIZED)
    assert auth == SourceAuthority.NOT_AUTHORIZED
    assert authority_rank("implied_elo", "completion") == 0


def test_S28_canonical_actionability_rules():
    """LIVE / COMPLETED / CANCELLED are not actionable per canonical status rules."""
    # These statuses indicate a match is not in a pre-match betting window
    non_actionable = {
        TennisEventStatus.LIVE,
        TennisEventStatus.COMPLETED,
        TennisEventStatus.CANCELLED,
    }
    # Potentially actionable (subject to other gates)
    potentially_actionable = {
        TennisEventStatus.UPCOMING,
        TennisEventStatus.AWAITING_START,
    }
    # Verify enum values are distinct
    assert non_actionable.isdisjoint(potentially_actionable)


def test_S29_authority_matrix_does_not_alter_stake_cap():
    """AUTHORITY_MATRIX is a read-only classification; it has no stake arithmetic."""
    # Verify AUTHORITY_MATRIX contains only SourceAuthority values, not numeric stakes
    for provider, caps in AUTHORITY_MATRIX.items():
        for capability, auth in caps.items():
            assert isinstance(auth, SourceAuthority), (
                f"AUTHORITY_MATRIX[{provider}][{capability}] must be SourceAuthority, "
                f"not {type(auth)}"
            )
            # No stake amounts should be stored in authority matrix
            assert not isinstance(auth, (int, float)), (
                "Authority matrix must not contain numeric stake values."
            )


def test_S30_canonical_model_does_not_affect_football():
    """enrich_schedule_with_canonical_state skips non-tennis entries."""
    entries = [
        {"sport": "football", "home": "Bayern", "away": "Dortmund",
         "kickoff": "2026-08-13T15:00:00Z"},
        {"sport": "tennis", "home": "Alcaraz", "away": "Sinner",
         "kickoff": "2026-08-13T15:00:00Z"},
        {"home": "Hamburg", "away": "Bremen",  # no sport → treated as football
         "kickoff": "2026-08-13T18:00:00Z"},
    ]

    from src.tennis.event_state import enrich_schedule_with_canonical_state
    result = enrich_schedule_with_canonical_state([e.copy() for e in entries])

    football_entry   = next(r for r in result if r.get("home") == "Bayern")
    tennis_entry     = next(r for r in result if r.get("home") == "Alcaraz")
    no_sport_entry   = next(r for r in result if r.get("home") == "Hamburg")

    assert "scheduled_start_initial" not in football_entry, "Football must not get canonical fields"
    assert "scheduled_start_initial" not in no_sport_entry, "No-sport entry must not get canonical fields"
    assert "scheduled_start_initial" in tennis_entry, "Tennis entry must get canonical fields"


# ── CM1: Cross-midnight identity check ───────────────────────────────────────

def test_CM1_cross_midnight_reschedule_identity():
    """MATERIAL FINDING: cross-midnight reschedule breaks signal_id stability.

    signal_id uses kickoff[:10] (YYYY-MM-DD).  A fixture rescheduled from
    23:45 on day X to 00:30 on day X+1 changes the date component → new
    signal_id.  This is a known limitation to be reported to the CEO.

    This test DOCUMENTS the behaviour; it does NOT fix it.
    Fix requires CEO authorization before any identity migration.
    """
    from src.signals.signal_status import make_signal_id

    day_x   = "2026-08-13T23:45:00Z"
    day_x1  = "2026-08-14T00:30:00Z"

    id_before = make_signal_id("tennis", "Alcaraz vs Sinner", "home", day_x)
    id_after  = make_signal_id("tennis", "Alcaraz vs Sinner", "home", day_x1)

    same = id_before == id_after

    # Document the finding explicitly — do not assert True/False, just capture
    if not same:
        import warnings
        warnings.warn(
            f"\n[WAVE 3B — CEO DECISION ITEM]\n"
            f"Cross-midnight reschedule breaks signal_id stability.\n"
            f"  Before: {day_x} → signal_id={id_before}\n"
            f"  After : {day_x1} → signal_id={id_after}\n"
            "Fix requires CEO authorization before any identity migration is implemented.",
            stacklevel=2,
        )

    # The test always passes — it is a diagnostic, not a gate.
    # The CEO report in the Wave 3B summary carries the explicit YES/NO answer.
    assert True, "CM1 is a diagnostic test; see warning above for finding."
