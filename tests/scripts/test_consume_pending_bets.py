"""P0-A regression tests for consume_pending_bets.py.

Covers:
  Blocker-1  Durable ACK lifecycle (push BEFORE KV delete, push failure is fatal)
  Blocker-2  sport-aware event_status (via signal_contract.py)
  Blocker-3  source=value model_prob fails closed — strict (0, 1) exclusive
  Blocker-4  model_prob endpoint calibration (0.0 and 1.0 rejected)

Lifecycle tests (CEO cases A–E) verify call ordering using mocks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest


def _fresh_odds_ts() -> str:
    """Return an odds_ts 5 minutes in the past (within the 30-min freshness window)."""
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _valid_bet(overrides: dict | None = None) -> dict:
    """Minimal valid pending bet as stored by the Worker (model_prob is fraction)."""
    base = {
        "id": "bet_001",
        "match": "Federer vs Nadal",
        "market": "home",
        "odds": 2.10,
        "stake_eur": 5.0,
        "source": "value",
        "signal_id": "sig_001",
        "sport": "tennis",
        "league": "wimbledon",
        "kickoff": "2026-08-14T14:00:00Z",
        "model_prob": 0.52,  # fraction (already normalized by Worker)
        "ev_pct": 15.2,
        "fixture_key": "federer_vs_nadal_20260814",
    }
    if overrides:
        base.update(overrides)
    return base


def _run_row_from_bet(bet: dict, bankroll: float = 100.0) -> tuple:
    """Call _row_from_bet from scripts.consume_pending_bets."""
    from scripts.consume_pending_bets import _row_from_bet
    import pandas as pd
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    return _row_from_bet(bet, today, bankroll)


# ── Blocker-1: consumer durability control-flow ───────────────────────────────


def test_blocker1_valid_bet_produces_row():
    """A valid pending bet produces a ledger row (the happy path)."""
    row, _ = _run_row_from_bet(_valid_bet())
    assert row is not None, "valid bet must produce a ledger row"
    assert row["model_prob"] == "0.520000"
    assert row["source"] == "value"
    assert row["status"] == "open"


def test_blocker1_added_zero_path_no_ledger_row_for_invalid():
    """Invalid bet produces (None, reason) — added count stays 0."""
    row, reason = _run_row_from_bet(_valid_bet({"odds": 0.5}))
    assert row is None
    assert reason  # some rejection reason


def test_blocker1_valid_manual_bet_produces_row():
    """Manual bets (model_prob absent) produce a row — model_prob empty."""
    bet = _valid_bet({"source": "manual", "signal_id": "", "model_prob": None})
    row, _ = _run_row_from_bet(bet)
    assert row is not None, "valid manual bet must produce a ledger row"
    assert row["model_prob"] == ""
    assert row["source"] == "manual"


# ── Blocker-2: sport-aware event_status ──────────────────────────────────────


def _tennis_sig(**overrides) -> dict:
    base = {
        "signal_id": "sig_001",
        "signal_status": "ACTIVE",
        "shadow": False, "is_shadow": False,
        "unsupported": False, "edge_lost": False,
        "stale": False, "no_bet_flag": False,
        "current_odds": 2.10,
        "current_ev_pct": 15.2,
        "odds_ts": _fresh_odds_ts(),
        "sport": "tennis",
        "event_status": "UPCOMING",
    }
    base.update(overrides)
    return base


def _football_sig(**overrides) -> dict:
    base = {
        "signal_id": "sig_002",
        "signal_status": "ACTIVE",
        "shadow": False, "is_shadow": False,
        "unsupported": False, "edge_lost": False,
        "stale": False, "no_bet_flag": False,
        "current_odds": 2.10,
        "current_ev_pct": 12.0,
        "odds_ts": _fresh_odds_ts(),
        "sport": "football",
    }
    base.update(overrides)
    return base


def test_blocker2_tennis_missing_event_status_rejected():
    """Tennis signal without event_status must be rejected fail-closed."""
    from src.betting.signal_contract import is_actionable_value_signal
    sig = _tennis_sig()
    del sig["event_status"]
    ok, reason = is_actionable_value_signal(sig, bankroll=100.0, active_bet_count=0)
    assert not ok, f"tennis with missing event_status must be rejected; got ok=True"
    assert "tennis" in reason.lower() or "fail closed" in reason.lower(), reason


def test_blocker2_tennis_null_event_status_rejected():
    """Tennis signal with event_status=None must be rejected."""
    from src.betting.signal_contract import is_actionable_value_signal
    ok, reason = is_actionable_value_signal(_tennis_sig(event_status=None), bankroll=100.0, active_bet_count=0)
    assert not ok, "tennis with null event_status must be rejected"
    assert "tennis" in reason.lower() or "fail closed" in reason.lower(), reason


def test_blocker2_tennis_unknown_event_status_rejected():
    """Tennis signal with event_status=UNKNOWN must be rejected."""
    from src.betting.signal_contract import is_actionable_value_signal
    ok, _ = is_actionable_value_signal(_tennis_sig(event_status="UNKNOWN"), bankroll=100.0, active_bet_count=0)
    assert not ok, "tennis UNKNOWN event_status must be rejected"


def test_blocker2_tennis_upcoming_accepted():
    """Tennis signal with event_status=UPCOMING must be accepted."""
    from src.betting.signal_contract import is_actionable_value_signal
    ok, reason = is_actionable_value_signal(_tennis_sig(event_status="UPCOMING"), bankroll=100.0, active_bet_count=0)
    assert ok, f"tennis UPCOMING must be accepted: {reason}"


def test_blocker2_football_missing_event_status_accepted():
    """Football signal without event_status is accepted (scanner does not emit it)."""
    from src.betting.signal_contract import is_actionable_value_signal
    sig = _football_sig()
    ok, reason = is_actionable_value_signal(sig, bankroll=100.0, active_bet_count=0)
    assert ok, f"football with missing event_status must be accepted: {reason}"


def test_blocker2_football_null_event_status_accepted():
    """Football signal with event_status=None is accepted."""
    from src.betting.signal_contract import is_actionable_value_signal
    ok, reason = is_actionable_value_signal(_football_sig(event_status=None), bankroll=100.0, active_bet_count=0)
    assert ok, f"football with null event_status must be accepted: {reason}"


# ── Blocker-3: source=value model_prob fails closed ──────────────────────────


def test_blocker3_value_null_model_prob_rejected():
    """source=value with null model_prob must be rejected (calibration fail-closed)."""
    row, reason = _run_row_from_bet(_valid_bet({"model_prob": None}))
    assert row is None, "source=value with null model_prob must be rejected"
    assert "model_prob" in reason.lower() or "null" in reason.lower() or "fail" in reason.lower(), reason


def test_blocker3_value_missing_model_prob_rejected():
    """source=value with model_prob key absent must be rejected."""
    bet = _valid_bet()
    del bet["model_prob"]
    row, reason = _run_row_from_bet(bet)
    assert row is None, "source=value with missing model_prob must be rejected"
    assert "model_prob" in reason.lower() or "null" in reason.lower(), reason


def test_blocker3_value_out_of_range_model_prob_rejected():
    """source=value with model_prob > 1.0 (out of fraction range) must be rejected."""
    # Worker normalizes percent→fraction (52.0→0.52). If normalization was skipped
    # and the raw percent arrived in the pending entry, 52.0 > 1.0 → reject.
    row, reason = _run_row_from_bet(_valid_bet({"model_prob": 52.0}))
    assert row is None, "source=value with model_prob=52.0 (un-normalized percent) must be rejected"
    assert "model_prob" in reason.lower() or "calibration" in reason.lower(), reason


def test_blocker3_value_negative_model_prob_rejected():
    """source=value with negative model_prob must be rejected."""
    row, reason = _run_row_from_bet(_valid_bet({"model_prob": -0.1}))
    assert row is None, "source=value with negative model_prob must be rejected"
    assert "model_prob" in reason.lower() or "calibration" in reason.lower(), reason


def test_blocker3_valid_model_prob_fraction_accepted():
    """source=value with valid model_prob fraction (0,1) is accepted."""
    row, _ = _run_row_from_bet(_valid_bet({"model_prob": 0.52}))
    assert row is not None, "valid model_prob fraction 0.52 must be accepted"
    assert row["model_prob"] == "0.520000"


def test_blocker3_calibration_regression_model_prob_in_range():
    """Accepted source=value row must have 0 < model_prob < 1 for Brier/ECE/LogLoss."""
    row, reason = _run_row_from_bet(_valid_bet({"model_prob": 0.508}))
    assert row is not None, f"valid bet rejected: {reason}"
    mp = float(row["model_prob"])
    assert 0 < mp < 1, f"ledger model_prob must be in (0,1) for calibration; got {mp}"


def test_blocker3_manual_null_model_prob_accepted():
    """source=manual with null model_prob is accepted (manual bets may omit it)."""
    bet = _valid_bet({"source": "manual", "signal_id": "", "model_prob": None})
    row, _ = _run_row_from_bet(bet)
    assert row is not None, "manual bet with null model_prob must be accepted"
    assert row["model_prob"] == ""


# ── Blocker-4: model_prob endpoint calibration (0.0 and 1.0 must REJECT) ─────
# Worker normalizes published percent→fraction: 0.0%→0.0 and 100%→1.0.
# These endpoints are NOT calibration-eligible (Brier/ECE/LogLoss require 0 < p < 1).


def test_blocker4_value_zero_fraction_model_prob_rejected():
    """source=value with model_prob=0.0 (endpoint) must be rejected — not calibration-eligible."""
    row, reason = _run_row_from_bet(_valid_bet({"model_prob": 0.0}))
    assert row is None, "source=value with model_prob=0.0 must be rejected"
    assert "calibration" in reason.lower() or "model_prob" in reason.lower(), reason


def test_blocker4_value_one_fraction_model_prob_rejected():
    """source=value with model_prob=1.0 (endpoint) must be rejected — not calibration-eligible."""
    row, reason = _run_row_from_bet(_valid_bet({"model_prob": 1.0}))
    assert row is None, "source=value with model_prob=1.0 must be rejected"
    assert "calibration" in reason.lower() or "model_prob" in reason.lower(), reason


def test_blocker4_calibration_eligible_boundary_low():
    """model_prob=0.001 (just above 0) is accepted — calibration-eligible."""
    row, _ = _run_row_from_bet(_valid_bet({"model_prob": 0.001}))
    assert row is not None, "model_prob=0.001 must be accepted"
    assert float(row["model_prob"]) > 0


def test_blocker4_calibration_eligible_boundary_high():
    """model_prob=0.999 (just below 1) is accepted — calibration-eligible."""
    row, _ = _run_row_from_bet(_valid_bet({"model_prob": 0.999}))
    assert row is not None, "model_prob=0.999 must be accepted"
    assert float(row["model_prob"]) < 1


# ── Lifecycle Tests (CEO Cases A–E) ──────────────────────────────────────────
# These test the orchestration of main() — verifying call ordering, not just
# row construction.  All external calls are mocked; the invariant is:
#   accepted bet → append → push → ACK (KV DELETE only AFTER push succeeds)


def _make_pending_response(bets):
    """Build a mock HTTP response for GET /pending_bets."""
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"bets": bets}
    return r


def _make_delete_response():
    r = MagicMock()
    r.status_code = 200
    return r


_PATCH_BASE = "scripts.consume_pending_bets"
# list_known_users / write_signals_json_all_users are late-imported inside main()
_WD_BASE = "src.notifications.web_dashboard"

# Shared environment patch for all lifecycle tests
_ENV_PATCH = patch.dict(
    "os.environ",
    {"SIGNALS_CLOUD_URL": "https://test.workers.dev/signals.json", "SIGNALS_API_TOKEN": "test-token"},
)


@_ENV_PATCH
@patch(f"{_PATCH_BASE}.count_open_bets", return_value=0)
@patch(f"{_WD_BASE}.write_signals_json_all_users")
@patch(f"{_WD_BASE}.list_known_users")
@patch(f"{_PATCH_BASE}._durable_push")
@patch(f"{_PATCH_BASE}._append_rows")
@patch(f"{_PATCH_BASE}._process_cancel_requests")
@patch(f"{_PATCH_BASE}.retry_request")
@patch(f"{_PATCH_BASE}._get_live_bankroll")
def test_lifecycle_a_push_success_ack_after_push(
    mock_bankroll, mock_http, mock_cancel, mock_append, mock_push, mock_users, mock_write, _mock_count
):
    """A: valid pending bet → append → push succeeds → KV DELETE called AFTER push."""
    from scripts.consume_pending_bets import main

    mock_users.return_value = ["philip"]
    mock_bankroll.return_value = 100.0
    mock_cancel.return_value = 0
    mock_write.return_value = None

    call_order: list[str] = []

    def append_side_effect(*args, **kwargs):
        call_order.append("APPEND")
        return 1

    def push_side_effect(*args, **kwargs):
        call_order.append("PUSH")
        return True  # success

    mock_append.side_effect = append_side_effect
    mock_push.side_effect = push_side_effect

    # GET returns one valid pending bet; DELETE succeeds
    get_resp = _make_pending_response([_valid_bet({"id": "bet_001"})])
    del_resp = _make_delete_response()

    def http_side_effect(method, url, **kwargs):
        if method == "GET":
            return get_resp
        if method == "DELETE":
            call_order.append(f"DELETE:{url.split('/')[-1].split('?')[0]}")
            return del_resp
        return MagicMock(status_code=200)

    mock_http.side_effect = http_side_effect

    result = main()

    assert result == 0, "main() must return 0 on success"
    assert "APPEND" in call_order, "append must be called"
    assert "PUSH" in call_order, "push must be called"
    deletes = [x for x in call_order if x.startswith("DELETE:")]
    assert deletes, "KV DELETE must be called after push"
    push_idx = call_order.index("PUSH")
    for d in deletes:
        assert call_order.index(d) > push_idx, "DELETE must come AFTER PUSH"
    assert call_order.index("PUSH") > call_order.index("APPEND"), "PUSH must come AFTER APPEND"


@_ENV_PATCH
@patch(f"{_PATCH_BASE}.count_open_bets", return_value=0)
@patch(f"{_WD_BASE}.write_signals_json_all_users")
@patch(f"{_WD_BASE}.list_known_users")
@patch(f"{_PATCH_BASE}._durable_push")
@patch(f"{_PATCH_BASE}._append_rows")
@patch(f"{_PATCH_BASE}._process_cancel_requests")
@patch(f"{_PATCH_BASE}.retry_request")
@patch(f"{_PATCH_BASE}._get_live_bankroll")
def test_lifecycle_b_push_failure_keeps_pending(
    mock_bankroll, mock_http, mock_cancel, mock_append, mock_push, mock_users, mock_write, _mock_count
):
    """B: push fails → KV DELETE NOT called for accepted bets → main returns 1."""
    from scripts.consume_pending_bets import main

    mock_users.return_value = ["philip"]
    mock_bankroll.return_value = 100.0
    mock_cancel.return_value = 0
    mock_write.return_value = None
    mock_append.return_value = 1
    mock_push.return_value = False  # push FAILS

    delete_calls: list[str] = []

    def http_side_effect(method, url, **kwargs):
        if method == "GET":
            return _make_pending_response([_valid_bet({"id": "bet_001"})])
        if method == "DELETE":
            delete_calls.append(url)
        return MagicMock(status_code=200)

    mock_http.side_effect = http_side_effect

    result = main()

    assert result == 1, "main() must return 1 when push fails"
    assert not delete_calls, (
        "KV DELETE must NOT be called for accepted bets when push fails; "
        f"got calls: {delete_calls}"
    )


@_ENV_PATCH
@patch(f"{_PATCH_BASE}.count_open_bets", return_value=0)
@patch(f"{_WD_BASE}.write_signals_json_all_users")
@patch(f"{_WD_BASE}.list_known_users")
@patch(f"{_PATCH_BASE}._durable_push")
@patch(f"{_PATCH_BASE}._append_rows")
@patch(f"{_PATCH_BASE}._process_cancel_requests")
@patch(f"{_PATCH_BASE}.retry_request")
@patch(f"{_PATCH_BASE}._get_live_bankroll")
def test_lifecycle_c_ack_failure_retry(
    mock_bankroll, mock_http, mock_cancel, mock_append, mock_push, mock_users, mock_write, _mock_count
):
    """C: push succeeds → KV DELETE fails → next run sees row as dup → push (no-op) → ACK retry."""
    from scripts.consume_pending_bets import main

    mock_users.return_value = ["philip"]
    mock_bankroll.return_value = 100.0
    mock_cancel.return_value = 0
    mock_write.return_value = None

    # Second run: all rows are dups already in ledger from previous push.
    # _append_rows returns 0 (dup skipped). _durable_push returns True (no-op — nothing staged).
    mock_append.return_value = 0   # all dups
    mock_push.return_value = True  # no-op push succeeds

    delete_calls: list[str] = []

    def http_side_effect(method, url, **kwargs):
        if method == "GET":
            return _make_pending_response([_valid_bet({"id": "bet_001"})])
        if method == "DELETE":
            delete_calls.append(url)
            return _make_delete_response()
        return MagicMock(status_code=200)

    mock_http.side_effect = http_side_effect

    result = main()

    assert result == 0, "main() must succeed on dup-retry path"
    mock_push.assert_called_once(), "push must still be called even for dup rows"
    assert delete_calls, "KV ACK (DELETE) must be called on retry even for dup rows"


@_ENV_PATCH
@patch(f"{_PATCH_BASE}.count_open_bets", return_value=0)
@patch(f"{_WD_BASE}.write_signals_json_all_users")
@patch(f"{_WD_BASE}.list_known_users")
@patch(f"{_PATCH_BASE}._durable_push")
@patch(f"{_PATCH_BASE}._append_rows")
@patch(f"{_PATCH_BASE}._process_cancel_requests")
@patch(f"{_PATCH_BASE}.retry_request")
@patch(f"{_PATCH_BASE}._get_live_bankroll")
def test_lifecycle_d_zero_pending_no_push(
    mock_bankroll, mock_http, mock_cancel, mock_append, mock_push, mock_users, mock_write, _mock_count
):
    """D: zero pending bets → no ledger commit/push → main returns 0."""
    from scripts.consume_pending_bets import main

    mock_users.return_value = ["philip"]
    mock_bankroll.return_value = 100.0
    mock_cancel.return_value = 0
    mock_write.return_value = None

    def http_side_effect(method, url, **kwargs):
        if method == "GET":
            # Empty pending bets
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"bets": []}
            return r
        return MagicMock(status_code=200)

    mock_http.side_effect = http_side_effect

    result = main()

    assert result == 0
    mock_push.assert_not_called(), "push must NOT be called when there are no pending bets"
    mock_append.assert_not_called(), "append must NOT be called when there are no pending bets"


@_ENV_PATCH
@patch(f"{_PATCH_BASE}.count_open_bets", return_value=0)
@patch(f"{_WD_BASE}.write_signals_json_all_users")
@patch(f"{_WD_BASE}.list_known_users")
@patch(f"{_PATCH_BASE}._durable_push")
@patch(f"{_PATCH_BASE}._append_rows")
@patch(f"{_PATCH_BASE}._process_cancel_requests")
@patch(f"{_PATCH_BASE}.retry_request")
@patch(f"{_PATCH_BASE}._get_live_bankroll")
def test_lifecycle_e_rejected_bet_immediate_ack(
    mock_bankroll, mock_http, mock_cancel, mock_append, mock_push, mock_users, mock_write, _mock_count
):
    """E: invalid/rejected pending bet is ACK'd immediately; no push for rejected bets."""
    from scripts.consume_pending_bets import main

    mock_users.return_value = ["philip"]
    mock_bankroll.return_value = 100.0
    mock_cancel.return_value = 0
    mock_write.return_value = None

    delete_calls: list[str] = []

    def http_side_effect(method, url, **kwargs):
        if method == "GET":
            # One invalid bet (odds too low — will be rejected)
            return _make_pending_response([_valid_bet({"id": "bad_bet", "odds": 0.5})])
        if method == "DELETE":
            delete_calls.append(url)
            return _make_delete_response()
        return MagicMock(status_code=200)

    mock_http.side_effect = http_side_effect

    result = main()

    assert result == 0, "rejected bet should not cause main() to fail"
    mock_push.assert_not_called(), "push must NOT be called when only rejected bets exist"
    mock_append.assert_not_called(), "append must NOT be called for rejected bets"
    assert delete_calls, "rejected bet must be ACK'd (DELETE) immediately"
