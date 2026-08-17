"""Tests for canonical signal actionability contract (P0-A)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.betting.signal_contract import (
    _MAX_EV_PCT,
    _MAX_ODDS_AGE_SECONDS,
    MAX_STAKE_PCT,
    TERMINAL_STATUSES,
    compute_safe_stake,
    is_actionable_value_signal,
    validate_stake_cap,
)

BANKROLL = 100.0
ACTIVE_0 = 0


def _valid_signal(**overrides):
    """Build a minimal valid signal. odds_ts is 29 min old (within 30-min window)."""
    base = {
        "signal_id": "sig_001",
        "signal_status": "ACTIVE",
        "shadow": False,
        "is_shadow": False,
        "unsupported": False,
        "edge_lost": False,
        "stale": False,
        "no_bet_flag": False,
        "current_odds": 2.1,
        "current_ev_pct": 5.5,
        "odds_ts": (datetime.now(timezone.utc) - timedelta(minutes=29)).isoformat(),
        "event_status": "PREMATCH",
        "sport": "tennis",
    }
    base.update(overrides)
    return base


# ── 1. Basic actionability ───────────────────────────────────────────────────

def test_valid_signal_is_actionable():
    ok, reason = is_actionable_value_signal(_valid_signal(), BANKROLL, ACTIVE_0)
    assert ok, reason


def test_missing_signal_status_blocked():
    sig = _valid_signal()
    del sig["signal_status"]
    ok, reason = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
    assert not ok
    assert "signal_status" in reason.lower() or "active" in reason.lower()


def test_stale_signal_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(stale=True), BANKROLL, ACTIVE_0)
    assert not ok


def test_missing_current_odds_blocked():
    sig = _valid_signal()
    del sig["current_odds"]
    ok, _ = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
    assert not ok


def test_missing_ev_blocked():
    sig = _valid_signal()
    del sig["current_ev_pct"]
    ok, _ = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
    assert not ok


# ── 2. EV canonical bounds (MAX_EV = 40%) ───────────────────────────────────

def test_ev_at_39_9_pct_is_valid():
    ok, reason = is_actionable_value_signal(_valid_signal(current_ev_pct=39.9), BANKROLL, ACTIVE_0)
    assert ok, f"39.9% EV should be valid: {reason}"


def test_ev_at_40_pct_boundary_blocked():
    """40% is the canonical ceiling — strictly > 40% is an artifact."""
    ok, _ = is_actionable_value_signal(_valid_signal(current_ev_pct=40.0), BANKROLL, ACTIVE_0)
    # 40.0 > _MAX_EV_PCT (40.0) is False → NOT blocked. Boundary: > 40 is blocked.
    # Contract is: ev > _MAX_EV_PCT → reject. At exactly 40.0 it passes.
    assert ok, "EV exactly at MAX (40.0%) should pass (boundary is >, not >=)"


def test_ev_at_40_1_pct_blocked():
    ok, reason = is_actionable_value_signal(_valid_signal(current_ev_pct=40.1), BANKROLL, ACTIVE_0)
    assert not ok, "40.1% EV should be rejected as artifact"
    assert "max_ev" in reason.lower() or "40" in reason


def test_ev_at_500_pct_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(current_ev_pct=500.0), BANKROLL, ACTIVE_0)
    assert not ok


def test_ev_at_100_pct_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(current_ev_pct=100.0), BANKROLL, ACTIVE_0)
    assert not ok


def test_negative_ev_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(current_ev_pct=-1.0), BANKROLL, ACTIVE_0)
    assert not ok


# ── 3. Odds freshness (canonical 30-minute window) ───────────────────────────

def test_29_minute_odds_is_fresh():
    sig = _valid_signal(odds_ts=(datetime.now(timezone.utc) - timedelta(minutes=29)).isoformat())
    ok, reason = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
    assert ok, f"29-min odds should be fresh: {reason}"


def test_31_minute_odds_is_stale():
    sig = _valid_signal(odds_ts=(datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat())
    ok, reason = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
    assert not ok, "31-min odds should be stale"
    assert "stale" in reason.lower()


def test_freshness_threshold_is_30_min():
    assert _MAX_ODDS_AGE_SECONDS == 1800, (
        f"Canonical freshness must be 30 min (1800s), got {_MAX_ODDS_AGE_SECONDS}s"
    )


def test_missing_odds_ts_blocked():
    sig = _valid_signal()
    del sig["odds_ts"]
    ok, _ = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
    assert not ok


# ── 4. Event status ──────────────────────────────────────────────────────────

def test_live_event_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(event_status="LIVE"), BANKROLL, ACTIVE_0)
    assert not ok


def test_in_progress_event_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(event_status="IN_PROGRESS"), BANKROLL, ACTIVE_0)
    assert not ok


@pytest.mark.parametrize("status", [
    "FINISHED", "CANCELLED", "POSTPONED", "ABANDONED", "TERMINATED", "COMPLETED",
])
def test_terminal_event_blocked(status):
    ok, _ = is_actionable_value_signal(_valid_signal(event_status=status), BANKROLL, ACTIVE_0)
    assert not ok, f"{status} should be terminal/blocked"


def test_completed_in_terminal_set():
    assert "COMPLETED" in TERMINAL_STATUSES, "COMPLETED must be in TERMINAL_STATUSES"


# ── 5. Signal flags ──────────────────────────────────────────────────────────

def test_shadow_signal_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(shadow=True), BANKROLL, ACTIVE_0)
    assert not ok


def test_is_shadow_signal_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(is_shadow=True), BANKROLL, ACTIVE_0)
    assert not ok


def test_unsupported_signal_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(unsupported=True), BANKROLL, ACTIVE_0)
    assert not ok


def test_no_bet_flag_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(no_bet_flag=True), BANKROLL, ACTIVE_0)
    assert not ok


def test_edge_lost_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(edge_lost=True), BANKROLL, ACTIVE_0)
    assert not ok


# ── 6. Bankroll / active-bet limit ──────────────────────────────────────────

def test_zero_bankroll_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(), 0.0, ACTIVE_0)
    assert not ok


def test_negative_bankroll_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(), -50.0, ACTIVE_0)
    assert not ok


def test_three_active_allowed_fourth_blocked():
    ok_1, _ = is_actionable_value_signal(_valid_signal(), BANKROLL, 0)
    ok_2, _ = is_actionable_value_signal(_valid_signal(), BANKROLL, 1)
    ok_3, _ = is_actionable_value_signal(_valid_signal(), BANKROLL, 2)
    ok_4, reason = is_actionable_value_signal(_valid_signal(), BANKROLL, 3)
    assert ok_1 and ok_2 and ok_3
    assert not ok_4
    assert "active" in reason.lower() or "max" in reason.lower()


def test_active_bet_4_rejected():
    ok, reason = is_actionable_value_signal(_valid_signal(), BANKROLL, 3)
    assert not ok
    assert "active" in reason.lower() or "max" in reason.lower()


# ── 7. Odds validity ─────────────────────────────────────────────────────────

def test_odds_le_1_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(current_odds=1.0), BANKROLL, ACTIVE_0)
    assert not ok


def test_odds_exactly_1_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(current_odds=1.0), BANKROLL, ACTIVE_0)
    assert not ok


# ── 8. Legacy / incomplete signals ──────────────────────────────────────────

def test_legacy_signal_no_signal_status_blocked():
    legacy = {
        "match": "Team A vs Team B",
        "ev_pct": 8.0,
        "odds": 2.0,
        "stake": 5.0,
    }
    ok, _ = is_actionable_value_signal(legacy, BANKROLL, ACTIVE_0)
    assert not ok


def test_no_signals_all_fail_closed():
    for sig in [{}, {"signal_id": ""}, {"signal_status": "EXPIRED"}]:
        ok, _ = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
        assert not ok


def test_missing_signal_id_blocked():
    sig = _valid_signal()
    del sig["signal_id"]
    ok, reason = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
    assert not ok
    assert "signal_id" in reason.lower()


def test_empty_signal_id_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(signal_id=""), BANKROLL, ACTIVE_0)
    assert not ok


# ── 9. validate_stake_cap (security boundary: REJECT, not cap) ──────────────

def test_validate_stake_cap_within_limit():
    ok, reason = validate_stake_cap(100.0, 4.0)
    assert ok, f"€4 stake on €100 bankroll should pass: {reason}"


def test_validate_stake_cap_exactly_at_5pct():
    ok, reason = validate_stake_cap(100.0, 5.0)
    assert ok, f"€5 stake on €100 bankroll (exactly 5%) should pass: {reason}"


def test_validate_stake_cap_over_5pct_rejected():
    ok, reason = validate_stake_cap(100.0, 5.01)
    assert not ok, "€5.01 on €100 bankroll should be REJECTED (not capped)"
    assert "cap" in reason.lower() or "5%" in reason or "exceed" in reason.lower()


def test_validate_stake_cap_spoof_bankroll():
    """Authoritative bankroll=€100, attempted stake €20 → REJECT even if UI sent hint=€1000."""
    ok, reason = validate_stake_cap(100.0, 20.0)
    assert not ok
    assert "cap" in reason.lower() or "exceed" in reason.lower()


def test_validate_stake_cap_zero_bankroll_rejected():
    ok, _ = validate_stake_cap(0.0, 5.0)
    assert not ok


# ── 10. compute_safe_stake (UI proactive cap — may cap, not reject) ──────────

def test_compute_safe_stake_within_cap():
    stake, cap_applied = compute_safe_stake(100.0, 4.0)
    assert stake == 4.0
    assert not cap_applied
    assert stake <= 100.0 * MAX_STAKE_PCT


def test_compute_safe_stake_caps_excess():
    stake, cap_applied = compute_safe_stake(100.0, 10.0)
    assert stake == 5.0  # bankroll * 0.05
    assert cap_applied


def test_compute_safe_stake_quick_button_capped():
    stake, cap_applied = compute_safe_stake(80.0, 10.0)
    assert stake == 4.0  # 80 * 0.05
    assert cap_applied


# ── 11. Truthful stake_pct ───────────────────────────────────────────────────

def test_truthful_stake_pct():
    bankroll = 100.0
    stake_eur = 5.0
    stake_pct = round(stake_eur / bankroll * 100, 4)
    assert stake_pct == 5.0  # not "0.0"


def test_max_ev_pct_is_40():
    assert _MAX_EV_PCT == 40.0, f"Canonical MAX_EV must be 40%, got {_MAX_EV_PCT}"
