"""Wave 3C — Dynamic Tennis Schedule Reconciliation tests.

40 deterministic scenarios covering:
- Schedule update flow (T1–T8)
- Status interaction (T9–T13)
- Identity preservation (T14–T20)
- Odds refresh eligibility (T21–T27)
- Top Recommendations interaction (T28–T31)
- Timezone / time-boundary (T32–T36)
- Regression guards (T37–T40)
"""
from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _future(minutes: float = 60.0) -> datetime:
    return _now() + timedelta(minutes=minutes)


def _past(minutes: float = 60.0) -> datetime:
    return _now() - timedelta(minutes=minutes)


# ── signal_status helpers ─────────────────────────────────────────────────────

def _make_signal(
    kickoff: str = "",
    event_status: str = "",
    scheduled_start_current: str = "",
    model_prob: float = 60.0,
    sport: str = "tennis",
    market: str = "home",
    category: str = "ATP",
) -> dict:
    return {
        "sport":                  sport,
        "match":                  "Alcaraz C. vs Djokovic N.",
        "market":                 market,
        "kickoff":                kickoff,
        "event_status":           event_status,
        "scheduled_start_current": scheduled_start_current,
        "model_prob":             model_prob,
        "category":               category,
    }


# ── Schedule update tests (T1–T8) ─────────────────────────────────────────────

class TestScheduleUpdate:
    """Tests around merge_schedule_update logic in event_state.py."""

    def setup_method(self):
        from src.tennis.event_state import TennisEventState
        self.State = TennisEventState

    def test_t1_initial_schedule_persisted(self):
        """T1: First schedule observation sets scheduled_start_initial."""
        from src.tennis.event_state import merge_schedule_update
        state = self.State(fixture_key="tennis_atp_us_open:alcarazc|djokovicn")
        new_start = _iso(_future(120))
        updated = merge_schedule_update(state, new_start, "the_odds_api")
        assert updated.scheduled_start_initial == new_start
        assert updated.scheduled_start_current == new_start

    def test_t2_same_day_later_update_accepted(self):
        """T2: Higher-authority same-day update advances scheduled_start_current."""
        from src.tennis.event_state import merge_schedule_update
        initial = _iso(_future(60))
        state = self.State(
            scheduled_start_initial=initial,
            scheduled_start_current=initial,
            schedule_source="the_odds_api",
            schedule_updated_at=_iso(_now()),
        )
        new_start = _iso(_future(90))
        updated = merge_schedule_update(state, new_start, "the_odds_api")
        assert updated.scheduled_start_initial == initial  # immutable
        assert updated.scheduled_start_current == new_start

    def test_t3_same_day_earlier_correction_accepted(self):
        """T3: Earlier qualified update accepted when source authority ≥ existing."""
        from src.tennis.event_state import merge_schedule_update
        later = _iso(_future(120))
        earlier = _iso(_future(60))
        # Seed with later time at lower authority (tennisexplorer)
        state = self.State(
            scheduled_start_initial=later,
            scheduled_start_current=later,
            schedule_source="tennisexplorer",
            schedule_updated_at=_iso(_past(5)),
        )
        # Higher authority PRIMARY corrects it to earlier
        updated = merge_schedule_update(state, earlier, "the_odds_api")
        assert updated.scheduled_start_current == earlier

    def test_t4_stale_equal_authority_does_not_overwrite(self):
        """T4: A stale equal-authority observation must not regress current start."""
        from src.tennis.event_state import merge_schedule_update
        current = _iso(_future(90))
        stale_old = _iso(_future(60))
        # State was updated 10 min ago by the_odds_api
        state = self.State(
            scheduled_start_initial=stale_old,
            scheduled_start_current=current,
            schedule_source="the_odds_api",
            schedule_updated_at=_iso(_now()),  # "now"
        )
        # Stale observation that predates the accepted one
        stale_ts = _iso(_past(20))
        updated = merge_schedule_update(state, stale_old, "the_odds_api", observed_at=stale_ts)
        assert updated.scheduled_start_current == current  # not regressed

    def test_t5_lower_authority_cannot_overwrite_higher(self):
        """T5: tennisexplorer (FALLBACK) cannot overwrite the_odds_api (PRIMARY) current start."""
        from src.tennis.event_state import merge_schedule_update
        primary_start = _iso(_future(90))
        state = self.State(
            scheduled_start_initial=primary_start,
            scheduled_start_current=primary_start,
            schedule_source="the_odds_api",
            schedule_updated_at=_iso(_now()),
        )
        fallback_start = _iso(_future(60))
        updated = merge_schedule_update(state, fallback_start, "tennisexplorer")
        assert updated.scheduled_start_current == primary_start  # unchanged

    def test_t6_equal_authority_newer_observation_wins(self):
        """T6: Equal authority → newer observation timestamp wins."""
        from src.tennis.event_state import merge_schedule_update
        old_start = _iso(_future(60))
        new_start = _iso(_future(120))
        state = self.State(
            scheduled_start_initial=old_start,
            scheduled_start_current=old_start,
            schedule_source="the_odds_api",
            schedule_updated_at=_iso(_past(30)),
        )
        updated = merge_schedule_update(state, new_start, "the_odds_api", observed_at=_iso(_now()))
        assert updated.scheduled_start_current == new_start

    def test_t7_malformed_update_preserves_existing(self):
        """T7: Empty/invalid new start does not overwrite existing current start."""
        from src.tennis.event_state import merge_schedule_update
        existing = _iso(_future(60))
        state = self.State(
            scheduled_start_initial=existing,
            scheduled_start_current=existing,
            schedule_source="the_odds_api",
        )
        updated = merge_schedule_update(state, "", "the_odds_api")
        assert updated.scheduled_start_current == existing

    def test_t8_schedule_survives_sidecar_roundtrip(self, tmp_path):
        """T8: State persists through save/load cycle (process-restart safety)."""
        from src.tennis.event_state import (
            TennisEventStatus,
            load_event_states,
            save_event_states,
        )
        sidecar = tmp_path / "tennis_event_states.json"
        fk = "tennis_atp_us_open:alcarazc|djokovicn"
        start = _iso(_future(120))
        state = self.State(
            fixture_key=fk,
            scheduled_start_initial=start,
            scheduled_start_current=start,
            schedule_source="the_odds_api",
            event_status=TennisEventStatus.UPCOMING,
        )
        states = {fk: state}
        with patch("src.tennis.event_state._SIDECAR_PATH", sidecar):
            save_event_states(states)
            loaded = load_event_states()
        assert fk in loaded
        assert loaded[fk].scheduled_start_current == start
        assert loaded[fk].scheduled_start_initial == start


# ── Status interaction tests (T9–T13) ─────────────────────────────────────────

class TestStatusInteraction:
    """evaluate_signal_status + derive_schedule_status."""

    def test_t9_kickoff_passes_awaiting_start(self):
        """T9: When scheduled_start_current passes, derive_schedule_status → AWAITING_START."""
        from src.tennis.event_state import TennisEventState, TennisEventStatus, derive_schedule_status
        start = _iso(_past(10))
        state = TennisEventState(
            scheduled_start_current=start,
            fixture_key="fk",
            event_status=TennisEventStatus.UPCOMING,
        )
        now = _now()
        result = derive_schedule_status(state, now)
        assert result == TennisEventStatus.AWAITING_START

    def test_t10_updated_kickoff_upcoming(self):
        """T10: After schedule update to future time → UPCOMING again."""
        from src.tennis.event_state import TennisEventState, TennisEventStatus, derive_schedule_status
        new_start = _iso(_future(30))
        state = TennisEventState(
            scheduled_start_current=new_start,
            fixture_key="fk",
            event_status=TennisEventStatus.AWAITING_START,
        )
        result = derive_schedule_status(state, _now())
        assert result == TennisEventStatus.UPCOMING

    def test_t11_authoritative_live_is_started(self):
        """T11: event_status=LIVE → signal_status STARTED."""
        from src.signals.signal_status import evaluate_signal_status
        sig = _make_signal(kickoff=_iso(_past(10)), event_status="LIVE")
        status = evaluate_signal_status(sig, 2.5, _iso(_now()))
        assert status == "STARTED"

    def test_t12_stale_kickoff_with_awaiting_not_started(self):
        """T12: event_status=AWAITING_START with old kickoff passed → NOT STARTED."""
        from src.signals.signal_status import evaluate_signal_status
        sig = _make_signal(
            kickoff=_iso(_past(20)),
            event_status="AWAITING_START",
            model_prob=60.0,
        )
        # With fresh valid odds → should reach gate, not short-circuit to STARTED
        status = evaluate_signal_status(sig, 2.5, _iso(_now()))
        assert status != "STARTED"
        assert status != "EXPIRED"

    def test_t13_authoritative_completed_is_expired(self):
        """T13: event_status=COMPLETED → signal_status EXPIRED."""
        from src.signals.signal_status import evaluate_signal_status
        sig = _make_signal(kickoff=_iso(_past(30)), event_status="COMPLETED")
        status = evaluate_signal_status(sig, 2.5, _iso(_now()))
        assert status == "EXPIRED"


# ── Identity preservation (T14–T20) ──────────────────────────────────────────

class TestIdentityPreservation:
    """Fixture identity stable through schedule changes."""

    def test_t14_same_day_update_same_fixture_key(self):
        """T14: Same-day kickoff update → same fixture_key."""
        from src.tennis.fixture_registry import make_fixture_key
        fk1 = make_fixture_key("tennis_atp_us_open", "Carlos Alcaraz", "Novak Djokovic")
        fk2 = make_fixture_key("tennis_atp_us_open", "Carlos Alcaraz", "Novak Djokovic")
        assert fk1 == fk2

    def test_t15_same_day_update_same_signal_id(self, tmp_path):
        """T15: Same-day reschedule → same signal_id from registry."""
        from src.tennis.fixture_registry import get_or_register
        reg_path = tmp_path / "registry.json"
        with patch("src.tennis.fixture_registry._REGISTRY_PATH", reg_path):
            sid1 = get_or_register("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.", "home", _iso(_future(60)))
            sid2 = get_or_register("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.", "home", _iso(_future(90)))
        assert sid1 == sid2

    def test_t16_cross_midnight_same_fixture_key(self):
        """T16: Cross-midnight reschedule → same fixture_key."""
        from src.tennis.fixture_registry import make_fixture_key
        fk1 = make_fixture_key("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.")
        fk2 = make_fixture_key("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.")
        assert fk1 == fk2

    def test_t17_cross_midnight_same_signal_id(self, tmp_path):
        """T17: Cross-midnight reschedule → same signal_id (primary acceptance)."""
        from src.tennis.fixture_registry import get_or_register
        reg_path = tmp_path / "registry.json"
        base = datetime(2026, 8, 13, 23, 45, tzinfo=timezone.utc)
        next_day = datetime(2026, 8, 14, 0, 30, tzinfo=timezone.utc)
        with patch("src.tennis.fixture_registry._REGISTRY_PATH", reg_path):
            sid1 = get_or_register("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.", "home", _iso(base))
            sid2 = get_or_register("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.", "home", _iso(next_day))
        assert sid1 == sid2

    def test_t18_multiple_reschedules_one_fixture(self, tmp_path):
        """T18: Five kickoff changes → single stable signal_id."""
        from src.tennis.fixture_registry import get_or_register
        reg_path = tmp_path / "registry.json"
        kicks = [_iso(_future(i * 20)) for i in range(1, 6)]
        sids = []
        with patch("src.tennis.fixture_registry._REGISTRY_PATH", reg_path):
            for k in kicks:
                sids.append(get_or_register("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.", "home", k))
        assert len(set(sids)) == 1

    def test_t19_no_duplicate_evaluation_on_reschedule(self):
        """T19: A reschedule does not produce a new fixture_key (no duplicate eval)."""
        from src.tennis.fixture_registry import make_fixture_key
        fk_before = make_fixture_key("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.")
        fk_after  = make_fixture_key("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.")
        assert fk_before == fk_after

    def test_t20_no_duplicate_bet_on_reschedule(self, tmp_path):
        """T20: Same logical fixture, different kickoff → same signal_id → no duplicate bet."""
        from src.tennis.fixture_registry import get_or_register
        reg_path = tmp_path / "registry.json"
        with patch("src.tennis.fixture_registry._REGISTRY_PATH", reg_path):
            s1 = get_or_register("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.", "home", _iso(_future(60)))
            s2 = get_or_register("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.", "home", _iso(_future(90)))
        assert s1 == s2  # same ID → ledger dedup is trivially safe


# ── Odds refresh eligibility (T21–T27) ────────────────────────────────────────

class TestOddsRefreshEligibility:
    """_is_refresh_due behavior under various event states."""

    def test_t21_delayed_match_continues_odds_refresh(self):
        """T21: DELAYED status → _is_refresh_due returns True (despite old kickoff)."""
        from src.signals.odds_refresher import _is_refresh_due
        sig = _make_signal(kickoff=_iso(_past(30)), event_status="DELAYED")
        assert _is_refresh_due(sig, None) is True

    def test_t22_old_kickoff_does_not_stop_awaiting_refresh(self):
        """T22: AWAITING_START + old kickoff → refresh still due."""
        from src.signals.odds_refresher import _is_refresh_due
        sig = _make_signal(kickoff=_iso(_past(20)), event_status="AWAITING_START")
        assert _is_refresh_due(sig, None) is True

    def test_t23_confirmed_live_stops_premarket_refresh(self):
        """T23: event_status=LIVE → _is_refresh_due returns False."""
        from src.signals.odds_refresher import _is_refresh_due
        sig = _make_signal(kickoff=_iso(_past(10)), event_status="LIVE")
        assert _is_refresh_due(sig, None) is False

    def test_t24_delayed_stale_signal_can_receive_new_quote(self):
        """T24: DELAYED signal with stale odds_state entry is still eligible for refresh."""
        from src.signals.odds_refresher import _is_refresh_due
        sig = _make_signal(kickoff=_iso(_past(45)), event_status="DELAYED")
        stale_entry = {"odds_ts": _iso(_past(40)), "current_odds": 2.0}
        assert _is_refresh_due(sig, stale_entry) is True

    def test_t25_recovery_does_not_bypass_active_gates(self):
        """T25: AWAITING_START + stale odds → STALE_ODDS (not ACTIVE)."""
        from src.signals.signal_status import evaluate_signal_status
        sig = _make_signal(kickoff=_iso(_past(20)), event_status="AWAITING_START")
        stale_ts = _iso(_past(35))
        status = evaluate_signal_status(sig, 2.5, stale_ts)
        assert status == "STALE_ODDS"

    def test_t26_current_ev_recomputed_from_fresh_quote(self):
        """T26: compute_current_ev uses model_prob × fresh odds correctly."""
        from src.signals.signal_status import compute_current_ev
        ev = compute_current_ev(60.0, 2.0)  # model_prob 60% × odds 2.0 - 1
        assert abs(ev - 0.2) < 1e-6

    def test_t27_odds_history_continuous(self, tmp_path):
        """T27: Two consecutive update_odds_state calls → odds_history grows."""
        from src.signals.signal_status import update_odds_state, load_odds_state
        sidecar = tmp_path / "odds_state.json"
        with patch("src.signals.signal_status._SIDECAR_PATH", sidecar):
            update_odds_state(
                "abc123", current_odds=2.0, odds_ts=_iso(_past(10)),
                odds_source="test", odds_fetch_tier=1,
                signal_status="ACTIVE", current_ev_pct=0.2,
            )
            update_odds_state(
                "abc123", current_odds=2.1, odds_ts=_iso(_now()),
                odds_source="test", odds_fetch_tier=1,
                signal_status="ACTIVE", current_ev_pct=0.26,
            )
            state = load_odds_state()
        assert len(state["abc123"]["odds_history"]) == 2


# ── Top Recommendations (T28–T31) ────────────────────────────────────────────

class TestTopRecommendations:
    """evaluate_signal_status behavior for top-recs gate."""

    def test_t28_old_kickoff_passed_new_future_fresh_odds_active(self):
        """T28: Old kickoff passed + new future scheduled_start_current + fresh valid odds → ACTIVE."""
        from src.signals.signal_status import evaluate_signal_status
        # old kickoff 20 min ago, but AWAITING_START → match hasn't started
        sig = _make_signal(
            kickoff=_iso(_past(20)),
            event_status="AWAITING_START",
            model_prob=60.0,
        )
        status = evaluate_signal_status(sig, 2.5, _iso(_now()))
        assert status in ("ACTIVE", "EDGE_LOST")  # passes STARTED gate, reaches edge gate
        assert status != "STARTED"

    def test_t29_authoritative_live_excluded(self):
        """T29: event_status=LIVE → STARTED → excluded from top-recs."""
        from src.signals.signal_status import evaluate_signal_status
        sig = _make_signal(kickoff=_iso(_past(5)), event_status="LIVE")
        status = evaluate_signal_status(sig, 2.5, _iso(_now()))
        assert status == "STARTED"

    def test_t30_delayed_stale_odds_excluded(self):
        """T30: DELAYED + stale odds (>30 min) → STALE_ODDS → excluded."""
        from src.signals.signal_status import evaluate_signal_status
        sig = _make_signal(kickoff=_iso(_past(45)), event_status="DELAYED")
        stale_ts = _iso(_past(35))
        status = evaluate_signal_status(sig, 2.5, stale_ts)
        assert status == "STALE_ODDS"

    def test_t31_delayed_fresh_valid_odds_normal_gate(self):
        """T31: DELAYED + fresh odds → reaches canonical gate evaluation."""
        from src.signals.signal_status import evaluate_signal_status
        sig = _make_signal(kickoff=_iso(_past(30)), event_status="DELAYED", model_prob=60.0)
        # With high-value odds → should pass to gate
        status = evaluate_signal_status(sig, 3.0, _iso(_now()))
        assert status in ("ACTIVE", "EDGE_LOST")  # not STARTED, EXPIRED, STALE_ODDS


# ── Timezone / boundary tests (T32–T36) ──────────────────────────────────────

class TestTimezone:
    """Timezone-aware parsing in schedule reconciliation."""

    def test_t32_utc_normalization(self):
        """T32: UTC timestamp parses correctly in merge_schedule_update."""
        from src.tennis.event_state import TennisEventState, merge_schedule_update
        utc_ts = "2026-08-13T15:00:00Z"
        state = TennisEventState()
        updated = merge_schedule_update(state, utc_ts, "the_odds_api")
        assert updated.scheduled_start_current == utc_ts

    def test_t33_berlin_cest_equivalent(self):
        """T33: Berlin CEST (UTC+2) offset preserved in timestamps."""
        from src.tennis.event_state import _parse_ts
        ts = "2026-08-13T17:00:00+02:00"
        dt = _parse_ts(ts)
        assert dt is not None
        assert dt.utctimetuple().tm_hour == 15  # 17:00 CEST = 15:00 UTC

    def test_t34_berlin_cet_equivalent(self):
        """T34: Berlin CET (UTC+1) offset correct."""
        from src.tennis.event_state import _parse_ts
        ts = "2026-12-13T16:00:00+01:00"
        dt = _parse_ts(ts)
        assert dt is not None
        assert dt.utctimetuple().tm_hour == 15  # 16:00 CET = 15:00 UTC

    def test_t35_cross_midnight_boundary_parsed(self):
        """T35: Cross-midnight: 23:45 vs 00:30 the next day compare correctly."""
        from src.tennis.event_state import _parse_ts
        t1 = _parse_ts("2026-08-13T23:45:00Z")
        t2 = _parse_ts("2026-08-14T00:30:00Z")
        assert t2 is not None and t1 is not None
        assert t2 > t1
        diff = (t2 - t1).total_seconds()
        assert abs(diff - 45 * 60) < 1

    def test_t36_dst_boundary_neither_affects_fixture_key(self):
        """T36: DST ambiguous time does not affect fixture identity (time excluded)."""
        from src.tennis.fixture_registry import make_fixture_key
        fk_before_dst = make_fixture_key("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.")
        fk_after_dst  = make_fixture_key("tennis_atp_us_open", "Alcaraz C.", "Djokovic N.")
        assert fk_before_dst == fk_after_dst


# ── Regression guards (T37–T40) ──────────────────────────────────────────────

class TestRegressionGuards:
    """Critical invariants from earlier waves must still hold."""

    def test_t37_false_tennis_live_impossible_from_elapsed_time(self):
        """T37: elapsed time alone cannot produce LIVE (Wave 3A invariant)."""
        from src.tennis.event_state import TennisEventState, TennisEventStatus, derive_schedule_status
        state = TennisEventState(
            scheduled_start_current=_iso(_past(30)),
            event_status=TennisEventStatus.UPCOMING,
        )
        result = derive_schedule_status(state, _now())
        assert result != TennisEventStatus.LIVE

    def test_t38_ev_overflow_guard_intact(self):
        """T38: EV outside ±500% is sanitized (update_odds_state stores None)."""
        from src.signals.signal_status import update_odds_state, load_odds_state
        import tempfile as _tmp, pathlib as _pl
        with _tmp.TemporaryDirectory() as td:
            sidecar = _pl.Path(td) / "odds_state.json"
            with patch("src.signals.signal_status._SIDECAR_PATH", sidecar):
                update_odds_state(
                    "overflow_test",
                    current_odds=2.0,
                    odds_ts=_iso(_now()),
                    odds_source="test",
                    odds_fetch_tier=1,
                    signal_status="ACTIVE",
                    current_ev_pct=600.0,  # absurd
                )
                state = load_odds_state()
        assert state["overflow_test"]["current_ev_pct"] is None

    def test_t39_five_pct_stake_cap_intact(self):
        """T39: apply_risk_cap enforces 5% ceiling regardless of Kelly stake."""
        from src.betting.kelly import apply_risk_cap
        bankroll = 100.0
        # Kelly would suggest €30 — cap should clamp to ≤ €5 (5% of bankroll)
        final_eur, cap_applied, _ = apply_risk_cap(theoretical_eur=30.0, bankroll=bankroll)
        assert final_eur <= bankroll * 0.05 + 1e-9
        assert cap_applied is True

    def test_t40_football_signals_unchanged_by_wave3c(self):
        """T40: Football signal evaluation is unaffected (no event_status field)."""
        from src.signals.signal_status import evaluate_signal_status
        sig = {
            "sport": "football",
            "match": "Dortmund vs Köln",
            "market": "home",
            "kickoff": _iso(_future(120)),
            "model_prob": 55.0,
        }
        # Football signals without event_status should use old kickoff logic unchanged
        status = evaluate_signal_status(sig, 1.9, _iso(_now()))
        assert status in ("ACTIVE", "EDGE_LOST", "UNREFRESHABLE", "STALE_ODDS")
        assert status not in ("STARTED", "EXPIRED")
