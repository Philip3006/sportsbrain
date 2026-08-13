"""Tests for Cloudflare Worker bet validation logic (P0-A)."""
import pytest

# Mirror the Worker's validation logic in Python for unit testing

WORKER_ABSOLUTE_MAX = 25.0


def worker_validate_bet(body: dict) -> tuple[bool, str]:
    """Mirror Worker /pending_bets POST validation."""
    stake = body.get("stake_eur")
    source = body.get("source", "value")
    bankroll_hint = body.get("bankroll_hint")
    open_bets_count = body.get("open_bets_count")
    signal_id = body.get("signal_id")

    try:
        stake = float(stake)
    except (TypeError, ValueError):
        return False, "invalid stake_eur"

    if not (0.5 <= stake <= WORKER_ABSOLUTE_MAX):
        return False, f"stake_eur out of range (0.5–{WORKER_ABSOLUTE_MAX})"

    if source not in ("value", "manual"):
        return False, "invalid source"

    # For value bets, enforce bankroll_hint check
    if source == "value":
        if bankroll_hint is None or not isinstance(bankroll_hint, (int, float)) or bankroll_hint <= 0:
            return False, "bankroll_hint required for value bets"
        cap = float(bankroll_hint) * 0.05
        if stake > cap + 0.001:
            return False, f"stake_eur {stake} exceeds 5% cap ({cap:.2f})"
        # signal_id required for value bets
        if not signal_id:
            return False, "signal_id required for value bets"

    # Active bet count check
    if open_bets_count is not None:
        if open_bets_count >= 3:
            return False, "max active bets (3) reached"

    return True, "ok"


# Test 19: Worker rejects >5%
def test_worker_rejects_over_5pct():
    ok, reason = worker_validate_bet({
        "stake_eur": 10.0,
        "source": "value",
        "bankroll_hint": 100.0,
        "open_bets_count": 0,
        "signal_id": "sig_001",
    })
    assert not ok
    assert "cap" in reason.lower() or "5%" in reason


# Test 20: Worker does not trust inflated client bankroll to allow >25
def test_worker_absolute_max_enforced():
    # Even with a huge "bankroll", absolute max is 25
    ok, reason = worker_validate_bet({
        "stake_eur": 30.0,
        "source": "value",
        "bankroll_hint": 10000.0,
        "open_bets_count": 0,
        "signal_id": "sig_001",
    })
    assert not ok


# Test: Worker fail-closed when no bankroll_hint for value bet
def test_worker_fails_closed_no_bankroll_hint():
    ok, reason = worker_validate_bet({
        "stake_eur": 5.0,
        "source": "value",
        "signal_id": "sig_001",
        "open_bets_count": 0,
        # no bankroll_hint
    })
    assert not ok


# Test: Worker allows manual bet without bankroll_hint (manual has different rules)
def test_worker_allows_manual_without_hint():
    ok, reason = worker_validate_bet({
        "stake_eur": 5.0,
        "source": "manual",
        "open_bets_count": 0,
    })
    assert ok, reason


# Test: Worker blocks 4th active bet
def test_worker_blocks_4th_active_bet():
    ok, reason = worker_validate_bet({
        "stake_eur": 5.0,
        "source": "value",
        "bankroll_hint": 200.0,
        "open_bets_count": 3,
        "signal_id": "sig_001",
    })
    assert not ok
    assert "3" in reason or "max" in reason.lower()


# Test: Worker rejects invalid source
def test_worker_rejects_invalid_source():
    ok, reason = worker_validate_bet({
        "stake_eur": 5.0,
        "source": "robot",
        "bankroll_hint": 100.0,
        "open_bets_count": 0,
        "signal_id": "sig_001",
    })
    assert not ok
    assert "source" in reason.lower() or "invalid" in reason.lower()


# Test: Worker rejects value bet without signal_id
def test_worker_rejects_value_bet_no_signal_id():
    ok, reason = worker_validate_bet({
        "stake_eur": 5.0,
        "source": "value",
        "bankroll_hint": 100.0,
        "open_bets_count": 0,
        # no signal_id
    })
    assert not ok
    assert "signal_id" in reason.lower()


# Test: Worker allows value bet at exactly 5%
def test_worker_allows_value_bet_at_5pct():
    ok, reason = worker_validate_bet({
        "stake_eur": 5.0,
        "source": "value",
        "bankroll_hint": 100.0,
        "open_bets_count": 0,
        "signal_id": "sig_001",
    })
    assert ok, reason


# Test: Worker rejects stake below 0.5
def test_worker_rejects_stake_below_minimum():
    ok, reason = worker_validate_bet({
        "stake_eur": 0.4,
        "source": "manual",
        "open_bets_count": 0,
    })
    assert not ok


# Test: Worker allows 3 open bets (count=2 is valid, 4th at count=3 is blocked)
def test_worker_allows_up_to_3_active():
    for count in range(3):
        ok, reason = worker_validate_bet({
            "stake_eur": 5.0,
            "source": "value",
            "bankroll_hint": 100.0,
            "open_bets_count": count,
            "signal_id": "sig_001",
        })
        assert ok, f"count={count}: {reason}"
