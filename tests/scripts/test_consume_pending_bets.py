"""P0-A regression tests for consume_pending_bets.py.

Covers:
  Blocker-1  Durable ACK lifecycle (push BEFORE KV delete, push failure is fatal)
  Blocker-2  sport-aware event_status (via signal_contract.py)
  Blocker-3  source=value model_prob fails closed — strict (0, 1) exclusive
  Blocker-4  model_prob endpoint calibration (0.0 and 1.0 rejected)

  FND-20260814-001  Remote durability — clean staging area is not proof of origin/main durability
  FND-20260814-002  ACCEPT/REJECT/RETRY semantics — bankroll outage is RETRY, never REJECT
  FND-20260814-003  Cancellation durability — durable push before cancel_requests ACK

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


# ── FND-20260814-001: Remote durability ──────────────────────────────────────
# Tests for _durable_push verifying remote containment, not just clean staging area.

_PATCH_SUBPROCESS = "scripts.consume_pending_bets.subprocess.run"
_PATCH_TIME_SLEEP = "scripts.consume_pending_bets._time.sleep"


def _git_mock(
    staging_has_changes: bool = False,
    is_ancestor: bool = True,
    push_succeeds: bool = True,
    *,
    add_rc: int = 0,
    fetch_rc: int = 0,
    merge_base_rc: int = -1,  # -1 = auto (0 if is_ancestor else 1)
    pull_rc: int = 0,
):
    """Build a subprocess.run mock for git commands in _durable_push.

    merge_base_rc=-1 means auto: 0 if is_ancestor else 1.
    Other values override directly (e.g. 2 = git command error).
    """
    git_calls: list[str] = []

    def run_side(args, **kwargs):
        r = MagicMock()
        r.returncode = 0
        r.stderr = ""
        r.stdout = ""
        if len(args) < 2:
            return r
        cmd = list(args[1:])  # skip "git"
        git_calls.append(cmd[0])
        if cmd[0] == "add":
            r.returncode = add_rc
        elif cmd[0] == "diff":
            r.returncode = 1 if staging_has_changes else 0
        elif cmd[0] == "fetch":
            r.returncode = fetch_rc
        elif cmd[0] == "merge-base":
            r.returncode = (0 if is_ancestor else 1) if merge_base_rc == -1 else merge_base_rc
        elif cmd[0] == "commit":
            r.returncode = 0
        elif cmd[0] == "pull":
            r.returncode = pull_rc
        elif cmd[0] == "push":
            r.returncode = 0 if push_succeeds else 1
        return r

    return run_side, git_calls


@patch(_PATCH_TIME_SLEEP)
@patch(_PATCH_SUBPROCESS)
def test_fnd001_remote_contained_noop(mock_subrun, mock_sleep):
    """FND-001: Nothing staged + HEAD already in origin/main → no push, return True."""
    from scripts.consume_pending_bets import _durable_push

    run_side, git_calls = _git_mock(staging_has_changes=False, is_ancestor=True, push_succeeds=True)
    mock_subrun.side_effect = run_side

    result = _durable_push(0)

    assert result is True, "should return True when HEAD is already in origin/main"
    assert "push" not in git_calls, "push must NOT be called when HEAD is already in origin/main"
    assert "commit" not in git_calls, "commit must NOT be called when nothing is staged"
    assert "merge-base" in git_calls, "remote containment check (merge-base) must be performed"


@patch(_PATCH_TIME_SLEEP)
@patch(_PATCH_SUBPROCESS)
def test_fnd001_local_ahead_clean_tree_pushes_before_ack(mock_subrun, mock_sleep):
    """FND-001: Local commit exists, nothing staged, HEAD not in origin/main → push runs."""
    from scripts.consume_pending_bets import _durable_push

    run_side, git_calls = _git_mock(staging_has_changes=False, is_ancestor=False, push_succeeds=True)
    mock_subrun.side_effect = run_side

    result = _durable_push(0)

    assert result is True, "push of existing local commit must succeed"
    assert "push" in git_calls, "push must be called when local commit not yet in origin/main"
    assert "commit" not in git_calls, "no new commit when staging is clean"


@patch(_PATCH_TIME_SLEEP)
@patch(_PATCH_SUBPROCESS)
def test_fnd001_local_ahead_push_fails_returns_false(mock_subrun, mock_sleep):
    """FND-001: Local commit exists, push fails → return False (accepted bets stay in KV)."""
    from scripts.consume_pending_bets import _durable_push

    run_side, git_calls = _git_mock(staging_has_changes=False, is_ancestor=False, push_succeeds=False)
    mock_subrun.side_effect = run_side

    result = _durable_push(0)

    assert result is False, "must return False when push of local-ahead commit fails"
    assert "push" in git_calls, "push must be attempted"


@patch(_PATCH_TIME_SLEEP)
@patch(_PATCH_SUBPROCESS)
def test_fnd001_staged_changes_commit_then_push(mock_subrun, mock_sleep):
    """FND-001: Staging has changes → commit → push → return True (normal happy path)."""
    from scripts.consume_pending_bets import _durable_push

    run_side, git_calls = _git_mock(staging_has_changes=True, is_ancestor=True, push_succeeds=True)
    mock_subrun.side_effect = run_side

    result = _durable_push(1)

    assert result is True
    assert "commit" in git_calls
    assert "push" in git_calls
    assert "merge-base" not in git_calls, "remote containment check skipped when staging has changes"


# ── FND-20260814-002: ACCEPT / REJECT / RETRY semantics ──────────────────────

def test_fnd002_bankroll_none_is_retry_not_reject():
    """FND-002: bankroll=None must produce RETRY, not REJECT (transient infrastructure failure)."""
    import pandas as pd

    from scripts.consume_pending_bets import _RETRY_PREFIX, _row_from_bet

    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    bet = _valid_bet()
    row, reason = _row_from_bet(bet, today, bankroll=None)

    assert row is None, "bankroll=None must not produce a valid row"
    assert isinstance(reason, str) and reason.startswith(_RETRY_PREFIX), (
        f"bankroll=None must return RETRY reason, got: {reason!r}"
    )


def test_fnd002_bankroll_zero_is_reject():
    """FND-002: bankroll=0 (finite, zero) is a permanent risk REJECT — infrastructure available
    but state is invalid. This must NOT be RETRY (which is reserved for lookup failures).
    """
    import pandas as pd

    from scripts.consume_pending_bets import _RETRY_PREFIX, _row_from_bet

    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    row, reason = _row_from_bet(_valid_bet(), today, bankroll=0)

    assert row is None
    assert isinstance(reason, str), f"reason must be a string, got: {reason!r}"
    assert not reason.startswith(_RETRY_PREFIX), (
        f"bankroll=0 must be permanent REJECT (not RETRY), got: {reason!r}"
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
def test_fnd002_retry_bets_not_acked(
    mock_bankroll, mock_http, mock_cancel, mock_append, mock_push, mock_users, mock_write, _mock_count
):
    """FND-002: RETRY bets (bankroll unavailable) must NOT be ACKed — stay in KV for next run."""
    from scripts.consume_pending_bets import main

    mock_users.return_value = ["philip"]
    mock_bankroll.return_value = None  # bankroll unavailable → RETRY
    mock_cancel.return_value = 0
    mock_write.return_value = None

    delete_calls: list[str] = []

    def http_side_effect(method, url, **kwargs):
        if method == "GET":
            return _make_pending_response([_valid_bet({"id": "retry_bet"})])
        if method == "DELETE":
            delete_calls.append(url)
            return _make_delete_response()
        return MagicMock(status_code=200)

    mock_http.side_effect = http_side_effect

    result = main()

    assert result == 0, "RETRY does not cause main() to fail"
    mock_push.assert_not_called(), "push must NOT be called for RETRY bets"
    mock_append.assert_not_called(), "append must NOT be called for RETRY bets"
    assert not delete_calls, (
        f"RETRY bets must NOT be ACKed (DELETE) — got: {delete_calls}"
    )


@_ENV_PATCH
@patch(f"{_PATCH_BASE}.count_open_bets", side_effect=Exception("db unavailable"))
@patch(f"{_WD_BASE}.write_signals_json_all_users")
@patch(f"{_WD_BASE}.list_known_users")
@patch(f"{_PATCH_BASE}._durable_push")
@patch(f"{_PATCH_BASE}._append_rows")
@patch(f"{_PATCH_BASE}._process_cancel_requests")
@patch(f"{_PATCH_BASE}.retry_request")
@patch(f"{_PATCH_BASE}._get_live_bankroll")
def test_fnd002_open_bet_count_unavailable_is_retry(
    mock_bankroll, mock_http, mock_cancel, mock_append, mock_push, mock_users, mock_write, _mock_count
):
    """FND-002: count_open_bets() failure is RETRY — bet must not be ACKed."""
    from scripts.consume_pending_bets import main

    mock_users.return_value = ["philip"]
    mock_bankroll.return_value = 100.0
    mock_cancel.return_value = 0
    mock_write.return_value = None

    delete_calls: list[str] = []

    def http_side_effect(method, url, **kwargs):
        if method == "GET":
            return _make_pending_response([_valid_bet({"id": "count_retry_bet"})])
        if method == "DELETE":
            delete_calls.append(url)
            return _make_delete_response()
        return MagicMock(status_code=200)

    mock_http.side_effect = http_side_effect

    result = main()

    assert result == 0
    mock_push.assert_not_called()
    mock_append.assert_not_called()
    assert not delete_calls, (
        f"bet with unavailable open-bet count must NOT be ACKed: {delete_calls}"
    )


# ── FND-20260814-003: Cancellation durability ─────────────────────────────────

@_ENV_PATCH
@patch(f"{_PATCH_BASE}.count_open_bets", return_value=0)
@patch(f"{_WD_BASE}.write_signals_json_all_users")
@patch(f"{_WD_BASE}.list_known_users")
@patch(f"{_PATCH_BASE}._durable_push")
@patch(f"{_PATCH_BASE}._append_rows")
@patch("src.betting.ledger.cancel_bet")
@patch(f"{_PATCH_BASE}.retry_request")
@patch(f"{_PATCH_BASE}._get_live_bankroll")
def test_fnd003_cancel_durable_push_before_ack(
    mock_bankroll, mock_http, mock_cancel_bet, mock_append, mock_push, mock_users, mock_write, _mock_count
):
    """FND-003: cancel_bet() persists locally; durable push must happen BEFORE cancel ACK (DELETE)."""
    from scripts.consume_pending_bets import main

    mock_users.return_value = ["philip"]
    mock_bankroll.return_value = 100.0
    mock_write.return_value = None
    mock_cancel_bet.return_value = "ok"
    mock_push.return_value = True  # push succeeds

    call_order: list[str] = []

    def push_side(*a, **kw):
        call_order.append("PUSH")
        return True

    mock_push.side_effect = push_side

    def http_side_effect(method, url, **kwargs):
        if method == "GET":
            if "cancel_requests" in url:
                r = MagicMock()
                r.status_code = 200
                r.json.return_value = {"requests": [{"home": "A", "away": "B", "market": "home"}]}
                return r
            # No pending bets
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"bets": []}
            return r
        if method == "DELETE":
            call_order.append(f"DELETE:{url.split('/')[-1]}")
            return _make_delete_response()
        return MagicMock(status_code=200)

    mock_http.side_effect = http_side_effect

    result = main()

    assert result == 0
    deletes = [x for x in call_order if x.startswith("DELETE:")]
    assert deletes, "cancel_requests DELETE (ACK) must be called after successful push"
    assert "PUSH" in call_order, "durable push must be called for cancellations"
    push_idx = call_order.index("PUSH")
    for d in deletes:
        assert call_order.index(d) > push_idx, "cancel ACK must come AFTER durable push"


@_ENV_PATCH
@patch(f"{_PATCH_BASE}.count_open_bets", return_value=0)
@patch(f"{_WD_BASE}.write_signals_json_all_users")
@patch(f"{_WD_BASE}.list_known_users")
@patch(f"{_PATCH_BASE}._durable_push")
@patch(f"{_PATCH_BASE}._append_rows")
@patch("src.betting.ledger.cancel_bet")
@patch(f"{_PATCH_BASE}.retry_request")
@patch(f"{_PATCH_BASE}._get_live_bankroll")
def test_fnd003_cancel_push_fail_no_ack(
    mock_bankroll, mock_http, mock_cancel_bet, mock_append, mock_push, mock_users, mock_write, _mock_count
):
    """FND-003: If durable push fails for cancellation, cancel_requests must NOT be ACKed."""
    from scripts.consume_pending_bets import main

    mock_users.return_value = ["philip"]
    mock_bankroll.return_value = 100.0
    mock_write.return_value = None
    mock_cancel_bet.return_value = "ok"
    mock_push.return_value = False  # push FAILS

    delete_calls: list[str] = []

    def http_side_effect(method, url, **kwargs):
        if method == "GET":
            if "cancel_requests" in url:
                r = MagicMock()
                r.status_code = 200
                r.json.return_value = {"requests": [{"home": "A", "away": "B", "market": "home"}]}
                return r
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"bets": []}
            return r
        if method == "DELETE":
            delete_calls.append(url)
            return _make_delete_response()
        return MagicMock(status_code=200)

    mock_http.side_effect = http_side_effect

    result = main()

    assert result == 0  # push failure for cancellations does not crash main()
    assert not delete_calls, (
        f"cancel_requests must NOT be ACKed when durable push fails: {delete_calls}"
    )


# ── FND-20260814-001: additional fail-closed git-command tests ─────────────────


@patch(_PATCH_TIME_SLEEP)
@patch(_PATCH_SUBPROCESS)
def test_fnd001_git_add_failure_returns_false(mock_subrun, mock_sleep):
    """FND-001: git add nonzero → _durable_push returns False immediately."""
    from scripts.consume_pending_bets import _durable_push

    run_side, git_calls = _git_mock(add_rc=1)
    mock_subrun.side_effect = run_side

    result = _durable_push(1)

    assert result is False, "git add failure must return False"
    assert "add" in git_calls, "git add must be attempted"
    assert "commit" not in git_calls, "commit must NOT be called after add failure"
    assert "push" not in git_calls, "push must NOT be called after add failure"


@patch(_PATCH_TIME_SLEEP)
@patch(_PATCH_SUBPROCESS)
def test_fnd001_fetch_failure_returns_false(mock_subrun, mock_sleep):
    """FND-001: git fetch failure (nothing staged) → return False before containment claim."""
    from scripts.consume_pending_bets import _durable_push

    run_side, git_calls = _git_mock(staging_has_changes=False, fetch_rc=1)
    mock_subrun.side_effect = run_side

    result = _durable_push(0)

    assert result is False, "fetch failure must return False"
    assert "fetch" in git_calls, "fetch must be attempted"
    assert "merge-base" not in git_calls, "merge-base must NOT be called after fetch failure"
    assert "push" not in git_calls, "push must NOT be called after fetch failure"


@patch(_PATCH_TIME_SLEEP)
@patch(_PATCH_SUBPROCESS)
def test_fnd001_merge_base_error_returns_false(mock_subrun, mock_sleep):
    """FND-001: merge-base rc>1 is a git command error (not a containment result) → False."""
    from scripts.consume_pending_bets import _durable_push

    run_side, git_calls = _git_mock(staging_has_changes=False, merge_base_rc=2)
    mock_subrun.side_effect = run_side

    result = _durable_push(0)

    assert result is False, "merge-base rc>1 (command error) must return False"
    assert "merge-base" in git_calls, "merge-base must be attempted"
    assert "push" not in git_calls, "push must NOT be called after merge-base error"


@patch(_PATCH_TIME_SLEEP)
@patch(_PATCH_SUBPROCESS)
def test_fnd001_pull_rebase_failure_skips_push_attempt(mock_subrun, mock_sleep):
    """FND-001: failed pull/rebase must not be ignored — push is skipped on that attempt."""
    from scripts.consume_pending_bets import _durable_push

    run_side, git_calls = _git_mock(staging_has_changes=False, is_ancestor=False, pull_rc=1)
    mock_subrun.side_effect = run_side

    result = _durable_push(0)

    assert result is False, "all pull failures → no push possible → return False"
    assert "pull" in git_calls, "pull must be attempted"
    assert "push" not in git_calls, "push must NOT be called when pull --rebase fails"


# ── FND-20260814-002: additional REJECT/RETRY boundary tests ──────────────────


def test_fnd002_bankroll_negative_is_reject():
    """FND-002: bankroll<0 (finite, negative) is a permanent risk REJECT — not RETRY."""
    import pandas as pd

    from scripts.consume_pending_bets import _RETRY_PREFIX, _row_from_bet

    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    row, reason = _row_from_bet(_valid_bet(), today, bankroll=-10.0)

    assert row is None
    assert isinstance(reason, str), f"reason must be a string, got: {reason!r}"
    assert not reason.startswith(_RETRY_PREFIX), (
        f"bankroll<0 must be permanent REJECT (not RETRY), got: {reason!r}"
    )


def test_fnd002_bankroll_positive_accepted():
    """FND-002: positive authoritative bankroll with a valid bet must produce a row."""
    import pandas as pd

    from scripts.consume_pending_bets import _row_from_bet

    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    row, _ = _row_from_bet(_valid_bet(), today, bankroll=100.0)

    assert row is not None, "valid bet with positive bankroll must be accepted"
    assert row["bankroll_at_placement"] == "100.00"


# ── FND-20260814-003: mixed placement + cancellation orchestration ────────────


@_ENV_PATCH
@patch(f"{_PATCH_BASE}.count_open_bets", return_value=0)
@patch(f"{_WD_BASE}.write_signals_json_all_users")
@patch(f"{_WD_BASE}.list_known_users")
@patch(f"{_PATCH_BASE}._durable_push")
@patch(f"{_PATCH_BASE}._append_rows")
@patch("src.betting.ledger.cancel_bet")
@patch(f"{_PATCH_BASE}.retry_request")
@patch(f"{_PATCH_BASE}._get_live_bankroll")
def test_fnd003_mixed_placement_and_cancel_both_persisted(
    mock_bankroll, mock_http, mock_cancel_bet, mock_append, mock_push, mock_users, mock_write, _mock_count
):
    """FND-003: one run with both accepted placement AND cancellation.

    Order: cancel runs first (with its own durable push), placement runs after.
    Both mutation types require push BEFORE ACK; ACK failure is idempotent.
    """
    from scripts.consume_pending_bets import main

    mock_users.return_value = ["philip"]
    mock_bankroll.return_value = 100.0
    mock_write.return_value = None
    mock_cancel_bet.return_value = "ok"

    push_calls: list[str] = []

    def push_side(added_count, *a, **kw):
        push_calls.append(f"push:{added_count}")
        return True

    mock_push.side_effect = push_side
    mock_append.return_value = 1

    call_order: list[str] = []

    def http_side_effect(method, url, **kwargs):
        if method == "GET":
            if "cancel_requests" in url:
                r = MagicMock()
                r.status_code = 200
                r.json.return_value = {"requests": [{"home": "X", "away": "Y", "market": "home"}]}
                return r
            # One valid pending bet
            return _make_pending_response([_valid_bet({"id": "mixed_bet_01"})])
        if method == "DELETE":
            call_order.append(f"DELETE:{url.split('/')[-1].split('?')[0]}")
            return _make_delete_response()
        return MagicMock(status_code=200)

    mock_http.side_effect = http_side_effect

    result = main()

    assert result == 0, f"mixed run must succeed, got {result}"
    # Both push paths executed: cancel push (added=0) and placement push (added=1)
    assert len(push_calls) >= 2, f"both cancel and placement must trigger durable push: {push_calls}"
    # Both ACKs executed
    acks = [x for x in call_order if x.startswith("DELETE:")]
    assert len(acks) >= 2, f"both cancel ACK and placement ACK must fire: {acks}"


# ── FND-20260814-030: exact consumer source ────────────────────────────────────


def test_fnd030_missing_source_permanent_reject():
    """FND-030: missing source (key absent) is a permanent REJECT — no default to 'value'."""
    bet = _valid_bet()
    del bet["source"]
    from scripts.consume_pending_bets import _RETRY_PREFIX

    row, reason = _run_row_from_bet(bet)

    assert row is None, "missing source must be rejected"
    assert isinstance(reason, str) and not reason.startswith(_RETRY_PREFIX), (
        f"missing source must be permanent REJECT (not RETRY), got: {reason!r}"
    )
    assert "source" in reason.lower() or "missing" in reason.lower(), reason


def test_fnd030_empty_source_permanent_reject():
    """FND-030: empty string source is a permanent REJECT."""
    from scripts.consume_pending_bets import _RETRY_PREFIX

    row, reason = _run_row_from_bet(_valid_bet({"source": ""}))

    assert row is None, "empty source must be rejected"
    assert not reason.startswith(_RETRY_PREFIX), (
        f"empty source must be permanent REJECT, got: {reason!r}"
    )


def test_fnd030_unknown_source_permanent_reject():
    """FND-030: source='auto' (unknown) is a permanent REJECT."""
    from scripts.consume_pending_bets import _RETRY_PREFIX

    row, reason = _run_row_from_bet(_valid_bet({"source": "auto"}))

    assert row is None, "unknown source must be rejected"
    assert not reason.startswith(_RETRY_PREFIX), (
        f"unknown source must be permanent REJECT, got: {reason!r}"
    )


def test_fnd030_value_uppercase_rejected():
    """FND-030: source='Value' (wrong case) is REJECT — no case normalization."""
    from scripts.consume_pending_bets import _RETRY_PREFIX

    row, reason = _run_row_from_bet(_valid_bet({"source": "Value"}))

    assert row is None, "source='Value' must be rejected (exact match required)"
    assert not reason.startswith(_RETRY_PREFIX), (
        f"wrong-case source must be permanent REJECT, got: {reason!r}"
    )


def test_fnd030_manual_uppercase_rejected():
    """FND-030: source='MANUAL' (wrong case) is REJECT — no case normalization."""
    from scripts.consume_pending_bets import _RETRY_PREFIX

    row, reason = _run_row_from_bet(_valid_bet({"source": "MANUAL", "signal_id": ""}))

    assert row is None, "source='MANUAL' must be rejected (exact match required)"
    assert not reason.startswith(_RETRY_PREFIX), (
        f"wrong-case source must be permanent REJECT, got: {reason!r}"
    )


def test_fnd030_whitespace_source_rejected():
    """FND-030: source=' value ' (with spaces) is REJECT — no trim normalization."""
    from scripts.consume_pending_bets import _RETRY_PREFIX

    row, reason = _run_row_from_bet(_valid_bet({"source": " value "}))

    assert row is None, "source with whitespace must be rejected (no trim)"
    assert not reason.startswith(_RETRY_PREFIX), (
        f"whitespace-padded source must be permanent REJECT, got: {reason!r}"
    )
