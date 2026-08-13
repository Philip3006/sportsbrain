"""Tests for Cloudflare Worker bet validation logic (P0-A).

The Worker is JavaScript; these tests mirror its validation logic in Python.
The key architectural invariant: Worker reads authoritative state from KV
(published by the Python backend), NOT from client-supplied hints.

authoritative_state represents what the Worker reads from signals_json KV:
  { "bankroll": float|None, "open_bets_count": int|None }
where None means the data was unavailable (Worker would FAIL CLOSED).
"""
WORKER_ABSOLUTE_MAX = 25.0
MAX_ACTIVE_BETS = 3


def worker_validate_bet(
    body: dict,
    authoritative_state: dict | None = None,
) -> tuple[bool, str]:
    """Mirror Worker /pending_bets POST validation.

    authoritative_state: dict with keys 'bankroll' and 'open_bets_count'.
    None means authoritative state could not be read from KV → FAIL CLOSED.
    """
    stake = body.get("stake_eur")
    source = body.get("source", "value")
    signal_id = body.get("signal_id", "")

    try:
        stake = float(stake)
    except (TypeError, ValueError):
        return False, "invalid stake_eur"

    if not (0.5 <= stake <= WORKER_ABSOLUTE_MAX):
        return False, f"stake_eur out of range (0.5–{WORKER_ABSOLUTE_MAX})"

    if source not in ("value", "manual"):
        return False, "invalid source"

    # P0-A: source=value requires signal_id — REJECT, do NOT silently reclassify
    if source == "value" and not signal_id:
        return False, "source=value requires signal_id"

    # P0-A: authoritative state required for ALL bet types
    if authoritative_state is None:
        return False, "authoritative state unavailable — fail closed"

    auth_bankroll = authoritative_state.get("bankroll")
    auth_open_bets = authoritative_state.get("open_bets_count")

    # P0-A: authoritative bankroll required — FAIL CLOSED if unavailable
    if auth_bankroll is None or auth_bankroll <= 0:
        return False, "authoritative bankroll unavailable — fail closed"

    # P0-A: 5% cap based on AUTHORITATIVE bankroll for ALL bet types
    cap = float(auth_bankroll) * 0.05
    if stake > cap + 0.001:
        return False, f"stake_eur {stake} exceeds 5% cap ({cap:.2f}) of authoritative bankroll {auth_bankroll}"

    # P0-A: authoritative open-bet count — FAIL CLOSED if unavailable
    if auth_open_bets is None:
        return False, "authoritative open-bet count unavailable — fail closed"

    if int(auth_open_bets) >= MAX_ACTIVE_BETS:
        return False, f"max active bets ({MAX_ACTIVE_BETS}) reached"

    return True, "ok"


# Helper: minimal valid authoritative state
def _auth(bankroll: float = 100.0, open_bets: int = 0) -> dict:
    return {"bankroll": bankroll, "open_bets_count": open_bets}


# Helper: minimal valid value-bet body
def _value_body(stake: float = 5.0, signal_id: str = "sig_001") -> dict:
    return {"stake_eur": stake, "source": "value", "signal_id": signal_id}


def _manual_body(stake: float = 5.0) -> dict:
    return {"stake_eur": stake, "source": "manual"}


# ── Basic acceptance ──────────────────────────────────────────────────────────

def test_valid_value_bet_accepted():
    ok, reason = worker_validate_bet(_value_body(5.0), _auth(100.0, 0))
    assert ok, reason


def test_valid_manual_bet_accepted():
    ok, reason = worker_validate_bet(_manual_body(5.0), _auth(100.0, 0))
    assert ok, reason


def test_value_bet_at_exactly_5pct_accepted():
    ok, reason = worker_validate_bet(_value_body(5.0), _auth(100.0, 0))
    assert ok, reason


# ── Absolute ceiling ──────────────────────────────────────────────────────────

def test_worker_absolute_max_enforced():
    # Even with a huge authoritative bankroll, absolute €25 max applies
    ok, _r = worker_validate_bet({"stake_eur": 30.0, "source": "value", "signal_id": "s"}, _auth(10000.0, 0))
    assert not ok


# ── 5% cap based on AUTHORITATIVE bankroll (spoof resistance) ────────────────

def test_client_bankroll_spoof_rejected():
    """Authoritative bankroll=€100, stake=€20 → REJECT even if client would send hint=€1000."""
    # Client cannot inflate the authoritative bankroll — only server KV counts
    ok, reason = worker_validate_bet(_value_body(20.0), _auth(100.0, 0))
    assert not ok, "€20 stake on €100 auth bankroll must be rejected (exceeds 5% cap)"
    assert "cap" in reason.lower() or "5%" in reason or "exceed" in reason.lower()


def test_5pct_cap_enforced_for_value():
    ok, reason = worker_validate_bet(_value_body(10.0), _auth(100.0, 0))
    assert not ok
    assert "cap" in reason.lower() or "exceed" in reason.lower()


def test_5pct_cap_enforced_for_manual():
    """Manual bets must also obey the 5% cap — no bypass for any source."""
    ok, _ = worker_validate_bet(_manual_body(10.0), _auth(100.0, 0))
    assert not ok, "Manual bets must also respect 5% cap"


def test_5pct_cap_on_different_bankroll():
    # €200 bankroll → cap = €10
    ok, reason = worker_validate_bet(_value_body(10.0), _auth(200.0, 0))
    assert ok, f"€10 at €200 bankroll (5%) should be accepted: {reason}"
    # €11 over cap
    ok2, _ = worker_validate_bet(_value_body(11.0), _auth(200.0, 0))
    assert not ok2


# ── Authoritative open-bet count (spoof resistance) ──────────────────────────

def test_authoritative_3_bets_rejects_new():
    """Authoritative open bets = 3 → reject new bet regardless of client count."""
    ok, reason = worker_validate_bet(_value_body(5.0), _auth(100.0, 3))
    assert not ok, "3 authoritative open bets must block a new bet"
    assert "3" in reason or "max" in reason.lower()


def test_client_count_spoof_rejected():
    """Authoritative open bets=3, client sends open_bets_count=0 in body → REJECT."""
    body = {**_value_body(5.0), "open_bets_count": 0}  # client lies: 0 open bets
    # Worker uses authoritative_state.open_bets_count = 3, ignores client body
    ok, _ = worker_validate_bet(body, _auth(100.0, 3))
    assert not ok, "Authoritative count=3 must reject even if client sends 0"


def test_2_open_bets_allows_third():
    ok, reason = worker_validate_bet(_value_body(5.0), _auth(100.0, 2))
    assert ok, f"2 open bets should allow a 3rd: {reason}"


# ── Fail closed when authoritative state unavailable ────────────────────────

def test_fail_closed_no_authoritative_state():
    ok, reason = worker_validate_bet(_value_body(5.0), None)
    assert not ok
    assert "fail closed" in reason.lower() or "unavailable" in reason.lower()


def test_fail_closed_no_auth_bankroll():
    ok, reason = worker_validate_bet(_value_body(5.0), {"bankroll": None, "open_bets_count": 0})
    assert not ok
    assert "bankroll" in reason.lower()


def test_fail_closed_no_auth_open_bets():
    ok, reason = worker_validate_bet(_value_body(5.0), {"bankroll": 100.0, "open_bets_count": None})
    assert not ok
    assert "open" in reason.lower() or "count" in reason.lower()


def test_fail_closed_manual_no_auth_state():
    """Manual bets without authoritative state → FAIL CLOSED (not allowed without bankroll)."""
    ok, _ = worker_validate_bet(_manual_body(5.0), None)
    assert not ok, "Manual bets without authoritative state must also fail closed"


def test_fail_closed_manual_no_auth_bankroll():
    """Manual bets with unknown bankroll → FAIL CLOSED."""
    ok, _ = worker_validate_bet(_manual_body(5.0), {"bankroll": None, "open_bets_count": 0})
    assert not ok, "Manual bets without authoritative bankroll must fail closed"


# ── source=value without signal_id → REJECT ──────────────────────────────────

def test_value_bet_no_signal_id_rejected():
    ok, reason = worker_validate_bet({"stake_eur": 5.0, "source": "value"}, _auth(100.0, 0))
    assert not ok
    assert "signal_id" in reason.lower()


def test_value_bet_empty_signal_id_rejected():
    ok, _ = worker_validate_bet(
        {"stake_eur": 5.0, "source": "value", "signal_id": ""},
        _auth(100.0, 0),
    )
    assert not ok


# ── Misc validation ───────────────────────────────────────────────────────────

def test_invalid_source_rejected():
    ok, reason = worker_validate_bet({"stake_eur": 5.0, "source": "robot"}, _auth(100.0, 0))
    assert not ok
    assert "source" in reason.lower() or "invalid" in reason.lower()


def test_stake_below_minimum_rejected():
    ok, _ = worker_validate_bet(_manual_body(0.4), _auth(100.0, 0))
    assert not ok


def test_worker_allows_up_to_2_existing_open():
    for count in range(3):  # 0, 1, 2 open bets all allow a new one
        ok, reason = worker_validate_bet(_value_body(5.0), _auth(100.0, count))
        assert ok, f"count={count}: {reason}"
