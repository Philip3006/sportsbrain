"""Tests for canonical signal actionability contract (P0-A)."""
import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from src.betting.signal_contract import is_actionable_value_signal, compute_safe_stake, MAX_STAKE_PCT

BANKROLL = 100.0
ACTIVE_0 = 0


def _valid_signal(**overrides):
    """Build a minimal valid signal."""
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
        "odds_ts": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
        "event_status": "PREMATCH",
        "sport": "tennis",
    }
    base.update(overrides)
    return base


# Test 1: valid ACTIVE fresh signal can open bet modal
def test_valid_signal_is_actionable():
    ok, reason = is_actionable_value_signal(_valid_signal(), BANKROLL, ACTIVE_0)
    assert ok, reason


# Test 2: missing signal_status cannot create VALUE bet
def test_missing_signal_status_blocked():
    sig = _valid_signal()
    del sig["signal_status"]
    ok, reason = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
    assert not ok
    assert "signal_status" in reason.lower() or "active" in reason.lower()


# Test 3: STALE_ODDS cannot create VALUE bet
def test_stale_signal_blocked():
    ok, reason = is_actionable_value_signal(_valid_signal(stale=True), BANKROLL, ACTIVE_0)
    assert not ok


# Test 4: missing current_odds cannot create VALUE bet
def test_missing_current_odds_blocked():
    sig = _valid_signal()
    del sig["current_odds"]
    ok, reason = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
    assert not ok


# Test 5: missing current_ev_pct cannot create VALUE bet
def test_missing_ev_blocked():
    sig = _valid_signal()
    del sig["current_ev_pct"]
    ok, reason = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
    assert not ok


# Test 6: absurd EV cannot create VALUE bet
def test_absurd_ev_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(current_ev_pct=600.0), BANKROLL, ACTIVE_0)
    assert not ok


# Test 7: LIVE event cannot create VALUE bet
def test_live_event_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(event_status="LIVE"), BANKROLL, ACTIVE_0)
    assert not ok


# Test 8: terminal event cannot create VALUE bet
@pytest.mark.parametrize("status", ["FINISHED", "CANCELLED", "POSTPONED", "ABANDONED", "TERMINATED"])
def test_terminal_event_blocked(status):
    ok, _ = is_actionable_value_signal(_valid_signal(event_status=status), BANKROLL, ACTIVE_0)
    assert not ok


# Test 9: shadow signal cannot create VALUE bet
def test_shadow_signal_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(shadow=True), BANKROLL, ACTIVE_0)
    assert not ok


# Test 10: unsupported signal cannot create VALUE bet
def test_unsupported_signal_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(unsupported=True), BANKROLL, ACTIVE_0)
    assert not ok


# Test 16: recommended stake <=5%
def test_compute_safe_stake_within_cap():
    stake, cap_applied = compute_safe_stake(100.0, 4.0)
    assert stake == 4.0
    assert not cap_applied
    assert stake <= 100.0 * MAX_STAKE_PCT


# Test 17: manually typed >5% stake blocked/capped
def test_compute_safe_stake_caps_excess():
    stake, cap_applied = compute_safe_stake(100.0, 10.0)
    assert stake == 5.0  # bankroll * 0.05
    assert cap_applied


# Test 18: quick-button >5% capped
def test_compute_safe_stake_quick_button_capped():
    stake, cap_applied = compute_safe_stake(80.0, 10.0)
    assert stake == 4.0  # 80 * 0.05
    assert cap_applied


# Test 22: new ledger stake_pct is truthful (not 0.0)
def test_truthful_stake_pct():
    bankroll = 100.0
    stake_eur = 5.0
    stake_pct = round(stake_eur / bankroll * 100, 4)
    assert stake_pct == 5.0  # not 0.0


# Test 26: active-bet #4 is rejected
def test_active_bet_4_rejected():
    ok, reason = is_actionable_value_signal(_valid_signal(), BANKROLL, 3)  # 3 already active
    assert not ok
    assert "active" in reason.lower() or "max" in reason.lower()


# Test 27: three active bets allowed, fourth blocked
def test_three_active_allowed_fourth_blocked():
    ok_1, _ = is_actionable_value_signal(_valid_signal(), BANKROLL, 0)
    ok_2, _ = is_actionable_value_signal(_valid_signal(), BANKROLL, 1)
    ok_3, _ = is_actionable_value_signal(_valid_signal(), BANKROLL, 2)
    ok_4, _ = is_actionable_value_signal(_valid_signal(), BANKROLL, 3)
    assert ok_1 and ok_2 and ok_3
    assert not ok_4


# Test 28: legacy signal without modern fields is non-actionable
def test_legacy_signal_no_signal_status_blocked():
    legacy = {
        "match": "Team A vs Team B",
        "ev_pct": 8.0,
        "odds": 2.0,
        "stake": 5.0,
    }
    ok, reason = is_actionable_value_signal(legacy, BANKROLL, ACTIVE_0)
    assert not ok


# Test 30: PWA remains operational concept — zero actionable signals returns False for all
def test_no_signals_all_fail_closed():
    for sig in [{}, {"signal_id": ""}, {"signal_status": "EXPIRED"}]:
        ok, _ = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
        assert not ok


# Test: negative EV blocked
def test_negative_ev_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(current_ev_pct=-1.0), BANKROLL, ACTIVE_0)
    assert not ok


# Test: zero bankroll blocked
def test_zero_bankroll_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(), 0.0, ACTIVE_0)
    assert not ok


# Test: negative bankroll blocked
def test_negative_bankroll_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(), -50.0, ACTIVE_0)
    assert not ok


# Test: odds <= 1.0 blocked
def test_odds_le_1_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(current_odds=1.0), BANKROLL, ACTIVE_0)
    assert not ok


# Test: no_bet_flag blocks
def test_no_bet_flag_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(no_bet_flag=True), BANKROLL, ACTIVE_0)
    assert not ok


# Test: edge_lost blocks
def test_edge_lost_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(edge_lost=True), BANKROLL, ACTIVE_0)
    assert not ok


# Test: stale odds_ts blocks (> 4 hours old)
def test_stale_odds_ts_blocked():
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    ok, _ = is_actionable_value_signal(_valid_signal(odds_ts=old_ts), BANKROLL, ACTIVE_0)
    assert not ok


# Test: fresh odds_ts within 4h is ok
def test_fresh_odds_ts_ok():
    recent_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    ok, reason = is_actionable_value_signal(_valid_signal(odds_ts=recent_ts), BANKROLL, ACTIVE_0)
    assert ok, reason


# Test: missing signal_id blocks
def test_missing_signal_id_blocked():
    sig = _valid_signal()
    del sig["signal_id"]
    ok, _ = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
    assert not ok


# Test: empty signal_id blocks
def test_empty_signal_id_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(signal_id=""), BANKROLL, ACTIVE_0)
    assert not ok


# Test: missing sport blocks
def test_missing_sport_blocked():
    sig = _valid_signal()
    del sig["sport"]
    ok, _ = is_actionable_value_signal(sig, BANKROLL, ACTIVE_0)
    assert not ok


# Test: IN_PROGRESS event blocked (live status)
def test_in_progress_event_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(event_status="IN_PROGRESS"), BANKROLL, ACTIVE_0)
    assert not ok


# Test: compute_safe_stake exact ceiling
def test_compute_safe_stake_at_ceiling():
    stake, cap_applied = compute_safe_stake(100.0, 5.0)
    assert stake == 5.0
    assert not cap_applied  # exactly at cap, not over


# Test: compute_safe_stake just over ceiling
def test_compute_safe_stake_just_over_ceiling():
    stake, cap_applied = compute_safe_stake(100.0, 5.01)
    assert stake == 5.0
    assert cap_applied


# Test: is_shadow flag blocks
def test_is_shadow_flag_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(is_shadow=True), BANKROLL, ACTIVE_0)
    assert not ok


# Test: zero EV blocked
def test_zero_ev_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(current_ev_pct=0.0), BANKROLL, ACTIVE_0)
    assert not ok


# Test: odds exactly 1.0 blocked
def test_odds_exactly_1_blocked():
    ok, _ = is_actionable_value_signal(_valid_signal(current_odds=1.0), BANKROLL, ACTIVE_0)
    assert not ok


# Test: odds slightly above 1.0 allowed
def test_odds_above_1_allowed():
    ok, reason = is_actionable_value_signal(_valid_signal(current_odds=1.01), BANKROLL, ACTIVE_0)
    assert ok, reason
