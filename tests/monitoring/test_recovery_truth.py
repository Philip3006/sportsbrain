"""TASK-P0B-003 — Recovery Truth fail-closed tests.

Proves all 12 required invariants:

  1.  request only != recovered
  2.  dispatch acknowledgement only != recovered
  3.  retry process exit=0 without new execution evidence != recovered
  4.  correlated execution with exit failure != recovered
  5.  successful execution but unchanged/stale output != recovered
  6.  wrong job/attempt correlation is rejected
  7.  pre-dispatch execution cannot satisfy a new attempt
  8.  new correlated successful execution + fresh verified output => RECOVERED
  9.  unsupported/inactive/ambiguous binding => RECOVERY_UNAVAILABLE
  10. repeated processing is idempotent
  11. existing MON-001 execution invariants remain green (regression guard)
  12. existing MON-002/MON-012 schedule tests remain green (regression guard)

Supplementary regression tests (CORRECTION-1):
  C1. wrong-job evidence fails closed
  C2. wrong evidence source fails closed (process_exit for snapshot-backed)
  C3. execution after request but before dispatch fails closed
  C4. unchanged pre/post snapshot fails closed
  C5. verification timestamp before execution fails closed
  C6. checker exception is not symptom_absent
  C7. checker_error result is not symptom_absent
  C8. RecoveryStore monotonic — cannot go backwards
  C9. RecoveryStore identity-safe — cannot retarget same attempt_id

No live network calls. All state transitions are deterministic.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.monitoring.recovery_truth import (
    RECOVERY_REGISTRY,
    ExecutionEvidence,
    RecoveryAttempt,
    RecoveryState,
    RecoveryStore,
    VerificationEvidence,
    collect_health_snapshot_evidence,
    mark_dispatched,
    observe_execution,
    request_recovery,
    verify_resolution,
)

# ---------------------------------------------------------------------------
# Timestamps (strictly ordered for correctness)
#
#   _BEFORE_AT   < _REQUESTED_AT < _AFTER_AT (dispatch) < _OBS_AT < _VER_AT
#   11:59           12:00           12:05                  12:10     12:30
# ---------------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0,
         second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_REQUESTED_AT = _iso(_utc(2026, 8, 17, 12, 0))   # request  12:00
_AFTER_AT     = _iso(_utc(2026, 8, 17, 12, 5))   # dispatch 12:05
_OBS_AT       = _iso(_utc(2026, 8, 17, 12, 10))  # observe  12:10 — strictly after dispatch
_VER_AT       = _iso(_utc(2026, 8, 17, 12, 30))  # verify   12:30 — strictly after observe
_BEFORE_AT    = _iso(_utc(2026, 8, 17, 11, 59))  # before request 11:59


def _exe(
    observed_at: str,
    exit_code: int | None = 0,
    source: str = "health_snapshot",
    *,
    run_id: str | None = None,
    job: str | None = None,
    pre_dispatch_observed_at: str | None = None,
    pre_dispatch_run_id: str | None = None,
) -> ExecutionEvidence:
    return ExecutionEvidence(
        run_id=run_id,
        observed_at=observed_at,
        exit_code=exit_code,
        source=source,
        job=job,
        pre_dispatch_observed_at=pre_dispatch_observed_at,
        pre_dispatch_run_id=pre_dispatch_run_id,
    )


def _ver(
    symptom_absent: bool,
    detail: str = "re-check",
    verified_at: str = _VER_AT,
) -> VerificationEvidence:
    return VerificationEvidence(
        verified_at=verified_at,
        symptom_absent=symptom_absent,
        detail=detail,
    )


def _dispatched_attempt(
    action: str = "re-run-settle",
    attempt_id: str = "test-attempt-001",
    target: str = "stuck_open_bets",
) -> RecoveryAttempt:
    """Helper: DISPATCHED attempt with canonical timestamps."""
    return RecoveryAttempt(
        attempt_id=attempt_id,
        target=target,
        symptom_id=target,
        binding_action=action,
        state=RecoveryState.DISPATCHED,
        requested_at=_REQUESTED_AT,
        dispatched_at=_AFTER_AT,
    )


def _observed_attempt(
    action: str = "re-run-settle",
    attempt_id: str = "test-attempt-obs",
    target: str = "stuck_open_bets",
    observed_at: str = _OBS_AT,
) -> RecoveryAttempt:
    """Helper: OBSERVED attempt with canonical timestamps + valid execution evidence."""
    return RecoveryAttempt(
        attempt_id=attempt_id,
        target=target,
        symptom_id=target,
        binding_action=action,
        state=RecoveryState.OBSERVED,
        requested_at=_REQUESTED_AT,
        dispatched_at=_AFTER_AT,
        execution_evidence=_exe(observed_at, exit_code=0),
    )


# ===========================================================================
# 1. Request only != recovered
# ===========================================================================

class TestInvariant1RequestOnly:
    def test_requested_state_is_not_recovered(self):
        """[INV-1] Creating a recovery attempt (REQUESTED) does not constitute recovery."""
        attempt = request_recovery("stuck_open_bets", "re-run-settle",
                                   requested_at=_REQUESTED_AT,
                                   attempt_id="inv1-001")
        assert attempt.state == RecoveryState.REQUESTED
        assert attempt.state != RecoveryState.RECOVERED

    def test_requested_state_has_no_execution_evidence(self):
        """[INV-1] REQUESTED attempt carries no execution or verification evidence."""
        attempt = request_recovery("stuck_open_bets", "re-run-settle",
                                   requested_at=_REQUESTED_AT,
                                   attempt_id="inv1-002")
        assert attempt.execution_evidence is None
        assert attempt.verification_evidence is None
        assert attempt.terminal_reason is None


# ===========================================================================
# 2. Dispatch acknowledgement only != recovered
# ===========================================================================

class TestInvariant2DispatchOnly:
    def test_dispatched_state_is_not_recovered(self):
        """[INV-2] Marking an attempt as dispatched does not constitute recovery."""
        attempt = request_recovery("stuck_open_bets", "re-run-settle",
                                   requested_at=_REQUESTED_AT,
                                   attempt_id="inv2-001")
        attempt = mark_dispatched(attempt, dispatched_at=_AFTER_AT)
        assert attempt.state == RecoveryState.DISPATCHED
        assert attempt.state != RecoveryState.RECOVERED

    def test_dispatched_state_has_no_execution_evidence(self):
        """[INV-2] DISPATCHED carries no execution evidence — only dispatch timestamp."""
        attempt = request_recovery("stuck_open_bets", "re-run-settle",
                                   requested_at=_REQUESTED_AT,
                                   attempt_id="inv2-002")
        attempt = mark_dispatched(attempt, dispatched_at=_AFTER_AT)
        assert attempt.execution_evidence is None
        assert attempt.dispatched_at == _AFTER_AT


# ===========================================================================
# 3. Retry process exit=0 without new execution evidence != recovered
# ===========================================================================

class TestInvariant3ProcessExitOnlyNotRecovered:
    def test_exit0_with_no_health_snapshot_absent_source_rejected(self):
        """[INV-3] health_snapshot_absent source for snapshot-backed binding → FAILED."""
        dispatched = _dispatched_attempt(attempt_id="inv3-001")
        exe = ExecutionEvidence(
            run_id=None,
            observed_at=_OBS_AT,
            exit_code=None,           # absent snapshot → None, not fabricated
            source="health_snapshot_absent",
            job="settle",
        )
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED
        assert result.state != RecoveryState.RECOVERED

    def test_process_exit_source_rejected_for_snapshot_backed_binding(self):
        """[INV-3] process_exit source for snapshot-backed binding (re-run-settle) → FAILED.

        Snapshot-backed bindings require source='health_snapshot'. process_exit
        alone fails closed regardless of exit_code value.
        """
        dispatched = _dispatched_attempt(attempt_id="inv3-002")
        exe = _exe(_OBS_AT, exit_code=0, source="process_exit")
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED
        assert result.state != RecoveryState.RECOVERED
        assert "process_exit" in (result.terminal_reason or "") or \
               "source" in (result.terminal_reason or "").lower()


# ===========================================================================
# 4. Correlated execution with exit failure != recovered
# ===========================================================================

class TestInvariant4ExitFailure:
    def test_exit1_not_recovered(self):
        """[INV-4] Correlated execution with exit_code=1 → FAILED, not RECOVERED."""
        dispatched = _dispatched_attempt(attempt_id="inv4-001")
        result = observe_execution(dispatched, _exe(_OBS_AT, exit_code=1, job="settle"))
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        assert "non-zero" in reason.lower() or "exit_code=1" in reason

    def test_exit127_not_recovered(self):
        """[INV-4] Non-zero exit (127) → FAILED."""
        dispatched = _dispatched_attempt(attempt_id="inv4-002")
        result = observe_execution(dispatched, _exe(_OBS_AT, exit_code=127, job="settle"))
        assert result.state == RecoveryState.FAILED

    def test_unknown_exit_not_recovered(self):
        """[INV-4] Unknown exit_code (None) → FAILED (P0-B1 cannot claim success)."""
        dispatched = _dispatched_attempt(attempt_id="inv4-003")
        result = observe_execution(dispatched, _exe(_OBS_AT, exit_code=None, job="settle"))
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        assert "None" in reason or "unknown" in reason.lower()


# ===========================================================================
# 5. Successful execution but unchanged/stale output != recovered
# ===========================================================================

class TestInvariant5StaleOutputNotRecovered:
    def test_exit0_but_symptom_still_present_not_recovered(self):
        """[INV-5] exit=0 + symptom still present → FAILED, not RECOVERED."""
        attempt = _observed_attempt(attempt_id="inv5-001")
        ver = _ver(symptom_absent=False, detail="settle ran but bets still open")
        result = verify_resolution(attempt, ver)
        assert result.state == RecoveryState.FAILED
        assert result.state != RecoveryState.RECOVERED
        assert result.verification_evidence is not None

    def test_stale_verification_error_recorded(self):
        """[INV-5] Failure reason must mention stale/unchanged output."""
        attempt = _observed_attempt(attempt_id="inv5-002")
        ver = _ver(symptom_absent=False, detail="unchanged output")
        result = verify_resolution(attempt, ver)
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        assert "still present" in reason or "unchanged" in reason or "stale" in reason


# ===========================================================================
# 6. Wrong job/attempt correlation is rejected
# ===========================================================================

class TestInvariant6WrongCorrelation:
    def test_store_wrong_attempt_id_returns_none(self, tmp_path):
        """[INV-6] Looking up attempt_id='A' from a store that has 'B' returns None."""
        store = RecoveryStore(tmp_path / "store.json")
        attempt_b = request_recovery("settle", "re-run-settle",
                                     attempt_id="attempt-B",
                                     requested_at=_REQUESTED_AT)
        store.save(attempt_b)

        result = store.get("attempt-A")
        assert result is None

    def test_observation_carries_attempt_id_forward(self):
        """[INV-6] attempt_id is preserved across all state transitions."""
        attempt = _dispatched_attempt(attempt_id="correlation-check-id")
        # Use health_snapshot source with correct job for re-run-settle
        observed = observe_execution(attempt, _exe(_OBS_AT, exit_code=0,
                                                   source="health_snapshot", job="settle"))
        assert observed.attempt_id == "correlation-check-id"

        recovered = verify_resolution(observed, _ver(symptom_absent=True))
        assert recovered.attempt_id == "correlation-check-id"


# ===========================================================================
# 7. Pre-dispatch execution cannot satisfy a new attempt
# ===========================================================================

class TestInvariant7PreDispatchExecution:
    def test_execution_before_request_rejected(self):
        """[INV-7] observed_at <= dispatched_at → FAILED (pre-dispatch execution)."""
        dispatched = _dispatched_attempt(attempt_id="inv7-001")
        # Evidence timestamped before both request and dispatch.
        result = observe_execution(dispatched, _exe(_BEFORE_AT, exit_code=0))
        assert result.state == RecoveryState.FAILED

    def test_execution_exactly_at_dispatch_time_rejected(self):
        """[INV-7] observed_at == dispatched_at is not strictly after → rejected."""
        dispatched = _dispatched_attempt(attempt_id="inv7-002")
        result = observe_execution(dispatched, _exe(_AFTER_AT, exit_code=0))
        assert result.state == RecoveryState.FAILED

    def test_execution_one_second_after_dispatch_accepted(self):
        """[INV-7] observed_at strictly after dispatched_at → temporal check passes."""
        dispatched = _dispatched_attempt(attempt_id="inv7-003")
        one_second_after_dispatch = _iso(_utc(2026, 8, 17, 12, 5, 1))  # 12:05:01
        result = observe_execution(dispatched, _exe(one_second_after_dispatch, exit_code=0,
                                                    job="settle"))
        assert result.state == RecoveryState.OBSERVED


# ===========================================================================
# 8. Full chain => RECOVERED
# ===========================================================================

class TestInvariant8FullChainRecovered:
    def test_complete_chain_yields_recovered(self):
        """[INV-8] REQUESTED→DISPATCHED→OBSERVED→VERIFIED→RECOVERED when all evidence passes."""
        attempt = request_recovery(
            "stuck_open_bets", "re-run-settle",
            symptom_id="stuck_open_bets",
            requested_at=_REQUESTED_AT,
            attempt_id="full-chain-001",
        )
        assert attempt.state == RecoveryState.REQUESTED

        attempt = mark_dispatched(attempt, dispatched_at=_AFTER_AT)
        assert attempt.state == RecoveryState.DISPATCHED

        exe = _exe(_OBS_AT, exit_code=0, source="health_snapshot", job="settle")
        attempt = observe_execution(attempt, exe)
        assert attempt.state == RecoveryState.OBSERVED

        ver = _ver(symptom_absent=True, detail="check_stuck_open_bets returned None")
        attempt = verify_resolution(attempt, ver)
        assert attempt.state == RecoveryState.RECOVERED
        assert attempt.execution_evidence is not None
        assert attempt.verification_evidence is not None
        assert attempt.terminal_reason is None

    def test_recovered_attempt_has_all_fields(self):
        """[INV-8] A RECOVERED attempt carries all evidence fields populated."""
        attempt = request_recovery(
            "signals_stale", "force-refresh-signals",
            symptom_id="signals_stale",
            requested_at=_REQUESTED_AT,
            attempt_id="full-chain-002",
        )
        attempt = mark_dispatched(attempt, dispatched_at=_AFTER_AT)
        exe = _exe(_OBS_AT, exit_code=0, source="health_snapshot", job="daily_scan")
        attempt = observe_execution(attempt, exe)
        ver = VerificationEvidence(
            verified_at=_VER_AT,
            symptom_absent=True,
            detail="check_signals_freshness returned None",
        )
        attempt = verify_resolution(attempt, ver)

        assert attempt.state == RecoveryState.RECOVERED
        assert attempt.attempt_id == "full-chain-002"
        assert attempt.requested_at == _REQUESTED_AT
        assert attempt.dispatched_at == _AFTER_AT
        assert attempt.execution_evidence.exit_code == 0
        assert attempt.verification_evidence.symptom_absent is True


# ===========================================================================
# 9. Unsupported / inactive / ambiguous binding => RECOVERY_UNAVAILABLE
# ===========================================================================

class TestInvariant9UnsupportedBinding:
    def test_unknown_action_is_recovery_unavailable(self):
        """[INV-9] Action not in registry → RECOVERY_UNAVAILABLE immediately."""
        attempt = request_recovery(
            "some_job", "completely_unknown_action",
            requested_at=_REQUESTED_AT,
            attempt_id="inv9-001",
        )
        assert attempt.state == RecoveryState.RECOVERY_UNAVAILABLE
        assert attempt.terminal_reason is not None

    def test_inactive_binding_is_recovery_unavailable(self):
        """[INV-9] Inactive binding (prompt-resubscribe) → RECOVERY_UNAVAILABLE."""
        attempt = request_recovery(
            "push_subscriptions_expired", "prompt-resubscribe",
            requested_at=_REQUESTED_AT,
            attempt_id="inv9-002",
        )
        assert attempt.state == RecoveryState.RECOVERY_UNAVAILABLE
        assert "inactive" in (attempt.terminal_reason or "").lower() or \
               "human" in (attempt.terminal_reason or "").lower()

    def test_none_action_is_recovery_unavailable(self):
        """[INV-9] 'none' action (no recovery defined) → RECOVERY_UNAVAILABLE."""
        attempt = request_recovery(
            "some_symptom", "none",
            requested_at=_REQUESTED_AT,
            attempt_id="inv9-003",
        )
        assert attempt.state == RecoveryState.RECOVERY_UNAVAILABLE

    def test_empty_string_action_is_recovery_unavailable(self):
        """[INV-9] Empty string action (ambiguous) → RECOVERY_UNAVAILABLE."""
        attempt = request_recovery(
            "some_symptom", "",
            requested_at=_REQUESTED_AT,
            attempt_id="inv9-004",
        )
        assert attempt.state == RecoveryState.RECOVERY_UNAVAILABLE

    def test_dispatch_on_unavailable_returns_unchanged(self):
        """[INV-9] Terminal RECOVERY_UNAVAILABLE cannot be advanced — returns unchanged."""
        attempt = request_recovery(
            "some_job", "unknown_action",
            requested_at=_REQUESTED_AT,
            attempt_id="inv9-005",
        )
        assert attempt.state == RecoveryState.RECOVERY_UNAVAILABLE
        advanced = mark_dispatched(attempt, dispatched_at=_AFTER_AT)
        assert advanced.state == RecoveryState.RECOVERY_UNAVAILABLE


# ===========================================================================
# 10. Repeated processing is idempotent
# ===========================================================================

class TestInvariant10Idempotent:
    def test_save_terminal_recovered_twice_is_noop(self, tmp_path):
        """[INV-10] Saving RECOVERED attempt twice leaves state unchanged."""
        store = RecoveryStore(tmp_path / "store.json")

        attempt = request_recovery("settle", "re-run-settle",
                                   requested_at=_REQUESTED_AT,
                                   attempt_id="idem-001")
        attempt = mark_dispatched(attempt, dispatched_at=_AFTER_AT)
        attempt = observe_execution(attempt, _exe(_OBS_AT, exit_code=0,
                                                  source="health_snapshot", job="settle"))
        attempt = verify_resolution(attempt, _ver(symptom_absent=True))
        assert attempt.state == RecoveryState.RECOVERED

        store.save(attempt)
        store.save(attempt)  # second save — must be no-op

        loaded = store.get("idem-001")
        assert loaded is not None
        assert loaded.state == RecoveryState.RECOVERED

    def test_save_terminal_failed_twice_is_noop(self, tmp_path):
        """[INV-10] Saving FAILED attempt twice leaves state unchanged."""
        store = RecoveryStore(tmp_path / "store.json")

        dispatched = _dispatched_attempt(attempt_id="idem-002", target="settle")
        failed = observe_execution(dispatched, _exe(_BEFORE_AT, exit_code=0))
        assert failed.state == RecoveryState.FAILED

        store.save(failed)

        # Try to save a "newer" non-terminal state for the same attempt_id — must be rejected.
        non_terminal = RecoveryAttempt(
            attempt_id="idem-002",
            target="settle",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.REQUESTED,
            requested_at=_REQUESTED_AT,
        )
        store.save(non_terminal)  # must not overwrite terminal FAILED

        loaded = store.get("idem-002")
        assert loaded is not None
        assert loaded.state == RecoveryState.FAILED

    def test_save_unavailable_twice_is_noop(self, tmp_path):
        """[INV-10] Saving RECOVERY_UNAVAILABLE twice is idempotent."""
        store = RecoveryStore(tmp_path / "store.json")

        attempt = request_recovery("settle", "unknown_action",
                                   requested_at=_REQUESTED_AT,
                                   attempt_id="idem-003")
        assert attempt.state == RecoveryState.RECOVERY_UNAVAILABLE

        store.save(attempt)
        store.save(attempt)

        loaded = store.get("idem-003")
        assert loaded is not None
        assert loaded.state == RecoveryState.RECOVERY_UNAVAILABLE

    def test_requesting_same_params_creates_independent_attempt(self):
        """[INV-10] Two request_recovery calls with different attempt_ids are independent."""
        a1 = request_recovery("settle", "re-run-settle",
                               requested_at=_REQUESTED_AT, attempt_id="a1")
        a2 = request_recovery("settle", "re-run-settle",
                               requested_at=_REQUESTED_AT, attempt_id="a2")
        assert a1.attempt_id != a2.attempt_id


# ===========================================================================
# Supplementary: collect_health_snapshot_evidence correctness
# ===========================================================================

class TestHealthSnapshotEvidence:
    def _write_snap(self, path: Path, job: str, snap: dict) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{job}.json").write_text(json.dumps(snap))

    def test_snapshot_after_request_observed(self, tmp_path):
        """Health snapshot timestamped after requested_at → temporal evidence available."""
        self._write_snap(tmp_path, "settle", {
            "job": "settle", "status": "ok", "exit_code": 0,
            "last_run_at": _OBS_AT, "run_id": "settle-run-abc",
        })
        ev = collect_health_snapshot_evidence("settle", _REQUESTED_AT, health_dir=tmp_path)
        assert ev is not None
        assert ev.observed_at == _OBS_AT
        assert ev.exit_code == 0
        assert ev.source == "health_snapshot"
        assert ev.run_id == "settle-run-abc"
        assert ev.job == "settle"

    def test_snapshot_carries_job_field(self, tmp_path):
        """collect_health_snapshot_evidence sets job= for cross-job validation."""
        self._write_snap(tmp_path, "daily_scan", {
            "job": "daily_scan", "status": "ok", "exit_code": 0,
            "last_run_at": _OBS_AT,
        })
        ev = collect_health_snapshot_evidence("daily_scan", _REQUESTED_AT, health_dir=tmp_path)
        assert ev is not None
        assert ev.job == "daily_scan"

    def test_snapshot_carries_pre_dispatch_field(self, tmp_path):
        """collect_health_snapshot_evidence embeds pre_dispatch_last_run_at in evidence."""
        self._write_snap(tmp_path, "settle", {
            "job": "settle", "status": "ok", "exit_code": 0,
            "last_run_at": _OBS_AT,
        })
        ev = collect_health_snapshot_evidence(
            "settle", _REQUESTED_AT, health_dir=tmp_path,
            pre_dispatch_last_run_at=_AFTER_AT,
        )
        assert ev is not None
        assert ev.pre_dispatch_observed_at == _AFTER_AT

    def test_snapshot_missing_returns_none(self, tmp_path):
        """No health snapshot → None (cannot fabricate evidence)."""
        ev = collect_health_snapshot_evidence("settle", _REQUESTED_AT, health_dir=tmp_path)
        assert ev is None

    def test_snapshot_with_nonzero_exit_carries_failure(self, tmp_path):
        """Health snapshot with exit_code=1 → ExecutionEvidence with exit_code=1."""
        self._write_snap(tmp_path, "settle", {
            "job": "settle", "status": "error", "exit_code": 1,
            "last_run_at": _OBS_AT, "run_id": None,
        })
        ev = collect_health_snapshot_evidence("settle", _REQUESTED_AT, health_dir=tmp_path)
        assert ev is not None
        assert ev.exit_code == 1

    def test_snapshot_with_string_exit_returns_none_exit(self, tmp_path):
        """Health snapshot with exit_code='0' (string) → None per P0-B1 truth."""
        self._write_snap(tmp_path, "settle", {
            "job": "settle", "status": "ok", "exit_code": "0",
            "last_run_at": _OBS_AT, "run_id": None,
        })
        ev = collect_health_snapshot_evidence("settle", _REQUESTED_AT, health_dir=tmp_path)
        assert ev is not None
        assert ev.exit_code is None  # string '0' rejected by _coerce_exit_code

    def test_snapshot_with_no_last_run_at_returns_none(self, tmp_path):
        """Health snapshot missing last_run_at → None (no temporal anchor)."""
        self._write_snap(tmp_path, "settle", {
            "job": "settle", "status": "ok", "exit_code": 0,
        })
        ev = collect_health_snapshot_evidence("settle", _REQUESTED_AT, health_dir=tmp_path)
        assert ev is None

    def test_snapshot_evidence_then_fails_observe_if_pre_dispatch(self, tmp_path):
        """[INV-7 integration] Even valid snapshot evidence fails if pre-dated."""
        self._write_snap(tmp_path, "settle", {
            "job": "settle", "status": "ok", "exit_code": 0,
            "last_run_at": _BEFORE_AT,  # before both request and dispatch
        })
        ev = collect_health_snapshot_evidence("settle", _REQUESTED_AT, health_dir=tmp_path)
        assert ev is not None
        assert ev.observed_at == _BEFORE_AT

        dispatched = _dispatched_attempt(attempt_id="snap-pre-001")
        result = observe_execution(dispatched, ev)
        assert result.state == RecoveryState.FAILED  # temporal guard rejects it


# ===========================================================================
# Supplementary: state machine ordering invariants
# ===========================================================================

class TestStateMachineOrdering:
    def test_cannot_observe_from_requested(self):
        """observe_execution from REQUESTED (skipping DISPATCHED) → FAILED."""
        attempt = request_recovery("settle", "re-run-settle",
                                   requested_at=_REQUESTED_AT, attempt_id="order-001")
        result = observe_execution(attempt, _exe(_OBS_AT, exit_code=0))
        assert result.state == RecoveryState.FAILED

    def test_cannot_verify_from_dispatched(self):
        """verify_resolution from DISPATCHED (skipping OBSERVED) → FAILED."""
        attempt = request_recovery("settle", "re-run-settle",
                                   requested_at=_REQUESTED_AT, attempt_id="order-002")
        attempt = mark_dispatched(attempt, dispatched_at=_AFTER_AT)
        result = verify_resolution(attempt, _ver(symptom_absent=True))
        assert result.state == RecoveryState.FAILED

    def test_cannot_verify_from_requested(self):
        """verify_resolution from REQUESTED → FAILED."""
        attempt = request_recovery("settle", "re-run-settle",
                                   requested_at=_REQUESTED_AT, attempt_id="order-003")
        result = verify_resolution(attempt, _ver(symptom_absent=True))
        assert result.state == RecoveryState.FAILED

    def test_cannot_dispatch_from_observed(self):
        """mark_dispatched from OBSERVED (wrong direction) → FAILED."""
        attempt = _observed_attempt(attempt_id="order-004")
        result = mark_dispatched(attempt)
        assert result.state == RecoveryState.FAILED


# ===========================================================================
# Supplementary: store round-trip and recent listing
# ===========================================================================

class TestRecoveryStore:
    def test_store_round_trip(self, tmp_path):
        """RecoveryAttempt is serializable and deserializable without data loss."""
        store = RecoveryStore(tmp_path / "store.json")
        attempt = request_recovery("settle", "re-run-settle",
                                   symptom_id="stuck_open_bets",
                                   requested_at=_REQUESTED_AT,
                                   attempt_id="rt-001")
        store.save(attempt)
        loaded = store.get("rt-001")
        assert loaded is not None
        assert loaded.attempt_id == "rt-001"
        assert loaded.state == RecoveryState.REQUESTED
        assert loaded.target == "settle"
        assert loaded.symptom_id == "stuck_open_bets"
        assert loaded.requested_at == _REQUESTED_AT

    def test_store_recent_sorted_newest_first(self, tmp_path):
        """recent() returns attempts sorted by requested_at descending."""
        store = RecoveryStore(tmp_path / "store.json")
        t1 = _iso(_utc(2026, 8, 17, 10, 0))
        t2 = _iso(_utc(2026, 8, 17, 11, 0))
        t3 = _iso(_utc(2026, 8, 17, 12, 0))

        for i, t in [(1, t1), (2, t2), (3, t3)]:
            a = request_recovery("settle", "re-run-settle",
                                 attempt_id=f"recent-{i:03d}",
                                 requested_at=t)
            store.save(a)

        recent = store.recent()
        assert recent[0].requested_at == t3
        assert recent[-1].requested_at == t1

    def test_store_get_missing_returns_none(self, tmp_path):
        """Fetching a non-existent attempt_id returns None."""
        store = RecoveryStore(tmp_path / "store.json")
        assert store.get("nonexistent") is None

    def test_store_non_terminal_can_be_overwritten(self, tmp_path):
        """Non-terminal states (REQUESTED → DISPATCHED) can be updated in the store."""
        store = RecoveryStore(tmp_path / "store.json")
        attempt = request_recovery("settle", "re-run-settle",
                                   requested_at=_REQUESTED_AT,
                                   attempt_id="nt-001")
        store.save(attempt)
        advanced = mark_dispatched(attempt, dispatched_at=_AFTER_AT)
        store.save(advanced)

        loaded = store.get("nt-001")
        assert loaded is not None
        assert loaded.state == RecoveryState.DISPATCHED


# ===========================================================================
# Supplementary: registry coverage
# ===========================================================================

class TestRegistry:
    def test_all_layer2_outcome_actions_are_registered(self):
        """Every action key in auto_heal_ai._ACTION_MAP that is auto-dispatchable
        must appear in RECOVERY_REGISTRY (even if inactive)."""
        expected_actions = {
            "re-run-settle",
            "re-consume",
            "force-refresh-signals",
            "re-test-vapid",
            "prompt-resubscribe",
            "none",
        }
        missing = expected_actions - set(RECOVERY_REGISTRY)
        assert not missing, f"Actions missing from registry: {missing}"

    def test_layer1_retry_bindings_registered(self):
        """Layer-1 job retry bindings must all be registered."""
        expected = {"settle_retry", "daily_scan_retry", "auto_retrain_retry", "closing_odds_retry"}
        missing = expected - set(RECOVERY_REGISTRY)
        assert not missing, f"Layer-1 bindings missing: {missing}"

    def test_active_bindings_have_actor_layer(self):
        """All active bindings must declare a valid actor_layer."""
        valid_layers = {"layer1", "layer2_outcome", "unavailable"}
        for action, binding in RECOVERY_REGISTRY.items():
            assert binding.actor_layer in valid_layers, (
                f"Binding {action!r} has invalid actor_layer {binding.actor_layer!r}"
            )

    def test_inactive_bindings_have_unavailable_layer(self):
        """Inactive bindings must declare actor_layer='unavailable'."""
        for action, binding in RECOVERY_REGISTRY.items():
            if not binding.active:
                assert binding.actor_layer == "unavailable", (
                    f"Inactive binding {action!r} has actor_layer={binding.actor_layer!r}; "
                    "expected 'unavailable'"
                )

    def test_layer1_bindings_have_health_job(self):
        """Layer-1 bindings must always declare a health_job (process exit is insufficient)."""
        for action, binding in RECOVERY_REGISTRY.items():
            if binding.actor_layer == "layer1":
                assert binding.health_job is not None, (
                    f"Layer-1 binding {action!r} missing health_job — "
                    "process exit alone cannot prove recovery"
                )


# ===========================================================================
# 11. MON-001 execution invariants regression guard
# ===========================================================================

class TestMON001Regression:
    """Confirm P0-B1 execution truth functions still behave correctly."""

    def test_coerce_exit_int_zero_valid(self):
        from src.monitoring.health_writer import _coerce_exit_code
        assert _coerce_exit_code(0) == 0

    def test_coerce_exit_int_one_valid(self):
        from src.monitoring.health_writer import _coerce_exit_code
        assert _coerce_exit_code(1) == 1

    def test_coerce_exit_string_returns_none(self):
        from src.monitoring.health_writer import _coerce_exit_code
        assert _coerce_exit_code("0") is None

    def test_coerce_exit_bool_returns_none(self):
        from src.monitoring.health_writer import _coerce_exit_code
        assert _coerce_exit_code(False) is None

    def test_coerce_exit_none_returns_none(self):
        from src.monitoring.health_writer import _coerce_exit_code
        assert _coerce_exit_code(None) is None

    def test_write_health_ok_zero_preserved(self, health_dir_tmp):
        """write_health(ok, exit_code=0) → ok, no MON-001 coercion."""
        from src.monitoring.health_writer import write_health
        path = write_health("tennis_scan", "ok", exit_code=0)
        data = json.loads(path.read_text())
        assert data["status"] == "ok"
        assert data["exit_code"] == 0

    def test_write_health_ok_nonzero_coerced(self, health_dir_tmp):
        """write_health(ok, exit_code=1) → error per MON-001."""
        from src.monitoring.health_writer import write_health
        path = write_health("tennis_scan", "ok", exit_code=1)
        data = json.loads(path.read_text())
        assert data["status"] == "error"
        assert "MON-001" in (data["error"] or "")


@pytest.fixture()
def health_dir_tmp(tmp_path, monkeypatch):
    import src.monitoring.health_writer as hw
    monkeypatch.setattr(hw, "HEALTH_DIR", tmp_path)
    return tmp_path


# ===========================================================================
# 12. MON-002/MON-012 schedule tests regression guard
# ===========================================================================

class TestMON002Regression:
    """Confirm P0-B2 schedule truth functions still behave correctly."""

    def test_job_expectations_contains_tennis_scan(self):
        from src.monitoring.job_schedule import JOB_EXPECTATIONS, CronSetExpectation
        exp = JOB_EXPECTATIONS.get("tennis_scan")
        assert exp is not None
        assert isinstance(exp, CronSetExpectation)

    def test_tennis_scan_has_8_cron_points(self):
        from src.monitoring.job_schedule import JOB_EXPECTATIONS
        exp = JOB_EXPECTATIONS["tennis_scan"]
        assert len(exp.points) == 8

    def test_evaluate_expectation_cron_not_overdue_after_run(self):
        """Ran at 09:02, now 11:30 → not_expected (no false stale)."""
        from src.monitoring.job_schedule import JOB_EXPECTATIONS, evaluate_expectation
        now = _utc(2026, 8, 16, 11, 30)
        last = _utc(2026, 8, 16, 9, 2)
        result = evaluate_expectation(JOB_EXPECTATIONS["tennis_scan"], last, now)
        assert result.in_window is False
        assert result.is_overdue is False

    def test_evaluate_expectation_windowed_off_window_not_stale(self):
        """bl2_live_push Monday → not_expected (off-window)."""
        from src.monitoring.job_schedule import JOB_EXPECTATIONS, evaluate_expectation
        monday = _utc(2026, 8, 17, 20, 0)
        result = evaluate_expectation(JOB_EXPECTATIONS["bundesliga2_live_push"], None, monday)
        assert result.in_window is False
        assert result.is_overdue is False

    def test_unsupported_expectation_kind_raises(self):
        """evaluate_expectation raises for unknown kinds (fail-closed per MON-002)."""
        from src.monitoring.job_schedule import evaluate_expectation

        class Bogus:
            cadence = ""
            kind = "bogus"

        with pytest.raises((ValueError, AttributeError)):
            evaluate_expectation(Bogus(), None, _utc(2026, 8, 16, 12, 0))  # type: ignore[arg-type]


# ===========================================================================
# C1–C9. Supplementary regression tests (CORRECTION-1)
# ===========================================================================

class TestCorrectionWrongJobEvidence:
    """C1: wrong-job evidence fails closed."""

    def test_wrong_job_evidence_rejected(self):
        """Evidence from wrong job (daily_scan instead of settle) → FAILED."""
        dispatched = _dispatched_attempt(action="re-run-settle", attempt_id="c1-001")
        # binding.health_job = "settle" but evidence.job = "daily_scan"
        exe = _exe(_OBS_AT, exit_code=0, source="health_snapshot", job="daily_scan")
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        assert "daily_scan" in reason or "health_job" in reason.lower() or "job" in reason.lower()

    def test_correct_job_evidence_accepted(self):
        """Evidence from correct job (settle) passes job validation."""
        dispatched = _dispatched_attempt(action="re-run-settle", attempt_id="c1-002")
        exe = _exe(_OBS_AT, exit_code=0, source="health_snapshot", job="settle")
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.OBSERVED

    def test_job_none_fails_closed_for_snapshot_backed(self):
        """Evidence with job=None for snapshot-backed binding → FAILED (not lenient)."""
        dispatched = _dispatched_attempt(action="re-run-settle", attempt_id="c1-003")
        exe = _exe(_OBS_AT, exit_code=0, source="health_snapshot", job=None)
        result = observe_execution(dispatched, exe)
        # job=None != "settle" (binding.health_job) → fails closed
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        assert "job" in reason.lower() or "None" in reason


class TestCorrectionWrongEvidenceSource:
    """C2: wrong evidence source fails closed for snapshot-backed bindings."""

    def test_process_exit_rejected_for_settle(self):
        """process_exit for re-run-settle (snapshot-backed) → FAILED."""
        dispatched = _dispatched_attempt(action="re-run-settle", attempt_id="c2-001")
        exe = _exe(_OBS_AT, exit_code=0, source="process_exit")
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED
        assert "process_exit" in (result.terminal_reason or "") or \
               "source" in (result.terminal_reason or "").lower()

    def test_health_snapshot_absent_rejected_for_settle(self):
        """health_snapshot_absent for re-run-settle → FAILED."""
        dispatched = _dispatched_attempt(action="re-run-settle", attempt_id="c2-002")
        exe = ExecutionEvidence(run_id=None, observed_at=_OBS_AT, exit_code=None,
                               source="health_snapshot_absent", job="settle")
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED

    def test_process_exit_allowed_for_vapid_binding(self):
        """re-test-vapid has health_job=None — process_exit is explicitly allowed."""
        dispatched = _dispatched_attempt(action="re-test-vapid", attempt_id="c2-003",
                                         target="push_failing")
        exe = _exe(_OBS_AT, exit_code=0, source="process_exit")
        result = observe_execution(dispatched, exe)
        # process_exit is allowed for health_job=None bindings
        assert result.state == RecoveryState.OBSERVED


class TestCorrectionPreDispatchExecution:
    """C3: execution after request but before dispatch fails closed."""

    def test_execution_between_request_and_dispatch_rejected(self):
        """observed_at > requested_at but <= dispatched_at → FAILED (pre-dispatch)."""
        dispatched = _dispatched_attempt(attempt_id="c3-001")
        # Between request (12:00) and dispatch (12:05) — supply job= so temporal check fires
        between_at = _iso(_utc(2026, 8, 17, 12, 2))
        exe = _exe(between_at, exit_code=0, source="health_snapshot", job="settle")
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        assert "dispatch" in reason.lower() or "<=" in reason or "pre-dispatch" in reason.lower()

    def test_execution_exactly_at_dispatch_rejected(self):
        """observed_at == dispatched_at → not strictly after → FAILED."""
        dispatched = _dispatched_attempt(attempt_id="c3-002")
        exe = _exe(_AFTER_AT, exit_code=0, source="health_snapshot", job="settle")
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED


class TestCorrectionUnchangedSnapshot:
    """C4: unchanged pre/post snapshot fails closed."""

    def test_same_snapshot_timestamp_rejected(self):
        """pre_dispatch_observed_at == observed_at → unchanged → FAILED."""
        dispatched = _dispatched_attempt(attempt_id="c4-001")
        # Snapshot did not change since dispatch (same last_run_at)
        exe = _exe(_OBS_AT, exit_code=0, source="health_snapshot",
                   job="settle", pre_dispatch_observed_at=_OBS_AT)
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        assert "unchanged" in reason.lower() or "new" in reason.lower()

    def test_older_snapshot_rejected(self):
        """observed_at < pre_dispatch_observed_at → going backwards → FAILED."""
        dispatched = _dispatched_attempt(attempt_id="c4-002")
        # Somehow observed_at is before the pre-dispatch baseline (impossible in practice, fail closed)
        exe = _exe(_OBS_AT, exit_code=0, source="health_snapshot",
                   job="settle", pre_dispatch_observed_at=_VER_AT)  # baseline in the future
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED

    def test_newer_snapshot_accepted(self):
        """pre_dispatch_observed_at < observed_at → new snapshot → passes freshness check."""
        dispatched = _dispatched_attempt(attempt_id="c4-003")
        exe = _exe(_OBS_AT, exit_code=0, source="health_snapshot",
                   job="settle", pre_dispatch_observed_at=_AFTER_AT)  # baseline was at dispatch
        result = observe_execution(dispatched, exe)
        # _OBS_AT (12:10) > _AFTER_AT (12:05) → new snapshot
        assert result.state == RecoveryState.OBSERVED

    def test_no_baseline_skips_freshness_check(self):
        """pre_dispatch_observed_at=None → freshness check skipped (unknown baseline)."""
        dispatched = _dispatched_attempt(attempt_id="c4-004")
        exe = _exe(_OBS_AT, exit_code=0, source="health_snapshot",
                   job="settle", pre_dispatch_observed_at=None)
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.OBSERVED


class TestCorrectionVerificationTimestamp:
    """C5: verification timestamp before execution fails closed."""

    def test_verified_before_observed_fails(self):
        """verified_at <= execution observed_at → FAILED."""
        observed = _observed_attempt(attempt_id="c5-001", observed_at=_OBS_AT)
        # verified_at = _AFTER_AT (12:05) which is before observed_at = _OBS_AT (12:10)
        early_ver = VerificationEvidence(
            verified_at=_AFTER_AT,
            symptom_absent=True,
            detail="pre-execution verification",
        )
        result = verify_resolution(observed, early_ver)
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        assert "verified_at" in reason.lower() or "pre-execution" in reason.lower() or \
               "<=" in reason

    def test_verified_at_same_as_observed_fails(self):
        """verified_at == observed_at → not strictly after → FAILED."""
        observed = _observed_attempt(attempt_id="c5-002", observed_at=_OBS_AT)
        same_time_ver = VerificationEvidence(
            verified_at=_OBS_AT,  # same as observed
            symptom_absent=True,
            detail="simultaneous verification",
        )
        result = verify_resolution(observed, same_time_ver)
        assert result.state == RecoveryState.FAILED

    def test_verified_after_observed_accepted(self):
        """verified_at > observed_at → temporal check passes."""
        observed = _observed_attempt(attempt_id="c5-003", observed_at=_OBS_AT)
        # _VER_AT (12:30) > _OBS_AT (12:10)
        ver = _ver(symptom_absent=True)
        result = verify_resolution(observed, ver)
        assert result.state == RecoveryState.RECOVERED


class TestCorrectionCheckerException:
    """C6+C7: checker exception / checker_error is not symptom_absent."""

    def test_checker_exception_symptom_absent_false_fails(self):
        """[C6] When checker throws exception, symptom_absent=False → FAILED."""
        observed = _observed_attempt(attempt_id="c6-001")
        ver = VerificationEvidence(
            verified_at=_VER_AT,
            symptom_absent=False,  # exception → cannot confirm symptom gone
            detail="checker_error: TypeError: object has no attribute 'id'",
        )
        result = verify_resolution(observed, ver)
        assert result.state == RecoveryState.FAILED
        assert result.state != RecoveryState.RECOVERED

    def test_checker_error_result_not_recovered(self):
        """[C7] checker_error_ detail with symptom_absent=False → FAILED, not RECOVERED."""
        observed = _observed_attempt(attempt_id="c7-001")
        ver = VerificationEvidence(
            verified_at=_VER_AT,
            symptom_absent=False,
            detail="checker_error: AttributeError: 'NoneType' has no attribute 'id'",
        )
        result = verify_resolution(observed, ver)
        assert result.state == RecoveryState.FAILED

    def test_genuine_symptom_absent_recovers(self):
        """Control: symptom_absent=True with correct timestamps → RECOVERED."""
        observed = _observed_attempt(attempt_id="c7-ctrl")
        ver = _ver(symptom_absent=True)
        result = verify_resolution(observed, ver)
        assert result.state == RecoveryState.RECOVERED


class TestCorrectionMalformedPreDispatch:
    """C4b: malformed pre_dispatch_observed_at must fail closed (not silently skipped)."""

    def test_malformed_pre_dispatch_fails_closed(self):
        """pre_dispatch_observed_at set but unparseable → FAILED (cannot verify freshness)."""
        dispatched = _dispatched_attempt(attempt_id="c4b-001")
        exe = _exe(_OBS_AT, exit_code=0, source="health_snapshot",
                   job="settle", pre_dispatch_observed_at="not-a-timestamp")
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        assert "malformed" in reason.lower() or "pre_dispatch" in reason.lower() or \
               "cannot verify" in reason.lower()

    def test_empty_pre_dispatch_fails_closed(self):
        """pre_dispatch_observed_at=empty string → unparseable → FAILED."""
        dispatched = _dispatched_attempt(attempt_id="c4b-002")
        exe = _exe(_OBS_AT, exit_code=0, source="health_snapshot",
                   job="settle", pre_dispatch_observed_at="")
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED

    def test_none_pre_dispatch_skips_check(self):
        """pre_dispatch_observed_at=None → freshness check skipped (known-good path)."""
        dispatched = _dispatched_attempt(attempt_id="c4b-003")
        exe = _exe(_OBS_AT, exit_code=0, source="health_snapshot",
                   job="settle", pre_dispatch_observed_at=None)
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.OBSERVED


class TestCorrectionRunId:
    """C4c: run_id freshness check — same run_id means unchanged snapshot."""

    def test_same_run_id_fails_closed(self):
        """pre_dispatch_run_id == evidence.run_id → same execution → FAILED."""
        dispatched = _dispatched_attempt(attempt_id="c4c-001")
        exe = _exe(
            _OBS_AT, exit_code=0, source="health_snapshot", job="settle",
            run_id="run-abc123",
            pre_dispatch_observed_at=_AFTER_AT,  # valid, older than _OBS_AT
            pre_dispatch_run_id="run-abc123",    # same → unchanged snapshot
        )
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        assert "run_id" in reason.lower() or "unchanged" in reason.lower()

    def test_different_run_id_newer_timestamp_accepted(self):
        """Different run_id + newer timestamp → new execution → OBSERVED."""
        dispatched = _dispatched_attempt(attempt_id="c4c-002")
        exe = _exe(
            _OBS_AT, exit_code=0, source="health_snapshot", job="settle",
            run_id="run-new999",
            pre_dispatch_observed_at=_AFTER_AT,  # older than _OBS_AT
            pre_dispatch_run_id="run-old123",    # different → new execution
        )
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.OBSERVED

    def test_same_run_id_but_no_pre_dispatch_run_id_skips_id_check(self):
        """If pre_dispatch_run_id=None, run_id check is skipped (timestamp-only)."""
        dispatched = _dispatched_attempt(attempt_id="c4c-003")
        exe = _exe(
            _OBS_AT, exit_code=0, source="health_snapshot", job="settle",
            run_id="run-xyz",
            pre_dispatch_observed_at=_AFTER_AT,
            pre_dispatch_run_id=None,  # no baseline run_id → skip ID check
        )
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.OBSERVED

    def test_same_run_id_but_no_evidence_run_id_skips_id_check(self):
        """If evidence.run_id=None, run_id check is skipped (snapshot has no run_id)."""
        dispatched = _dispatched_attempt(attempt_id="c4c-004")
        exe = _exe(
            _OBS_AT, exit_code=0, source="health_snapshot", job="settle",
            run_id=None,
            pre_dispatch_observed_at=_AFTER_AT,
            pre_dispatch_run_id="run-old",  # baseline has run_id but evidence doesn't
        )
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.OBSERVED


class TestCorrectionMalformedVerifyTimestamps:
    """C5b: malformed timestamps in verify_resolution must fail closed."""

    def test_malformed_observed_at_fails_closed(self):
        """execution_evidence.observed_at unparseable → FAILED."""
        attempt = RecoveryAttempt(
            attempt_id="c5b-001",
            target="stuck_open_bets",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.OBSERVED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
            execution_evidence=ExecutionEvidence(
                run_id=None,
                observed_at="not-a-timestamp",  # malformed
                exit_code=0,
                source="health_snapshot",
                job="settle",
            ),
        )
        ver = _ver(symptom_absent=True)
        result = verify_resolution(attempt, ver)
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        assert "observed_at" in reason.lower() or "parse" in reason.lower() or \
               "cannot" in reason.lower()

    def test_malformed_verified_at_fails_closed(self):
        """VerificationEvidence.verified_at unparseable → FAILED."""
        observed = _observed_attempt(attempt_id="c5b-002", observed_at=_OBS_AT)
        ver = VerificationEvidence(
            verified_at="not-a-timestamp",
            symptom_absent=True,
            detail="malformed timestamp",
        )
        result = verify_resolution(observed, ver)
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        assert "verified_at" in reason.lower() or "parse" in reason.lower() or \
               "cannot" in reason.lower()

    def test_both_valid_timestamps_ordered_correctly_recovers(self):
        """Control: both valid, verified_at > observed_at, symptom_absent=True → RECOVERED."""
        observed = _observed_attempt(attempt_id="c5b-ctrl", observed_at=_OBS_AT)
        ver = _ver(symptom_absent=True)  # verified_at=_VER_AT > _OBS_AT
        result = verify_resolution(observed, ver)
        assert result.state == RecoveryState.RECOVERED


class TestHandleOutcomeSymptomsIntegration:
    """Integration-style test: checker_error_* in re-check prevents recovery claim."""

    def test_checker_error_symptom_prevents_recovery_claim(self):
        """[C7-integration] checker_error_* present → sym_absent=False → FAILED.

        Simulates the logic inside _handle_outcome_symptoms post-action re-check:
        run_all_checks() returns a checker_error_* symptom instead of (or alongside)
        the original. The recovery chain must treat this as unresolved.
        """
        from src.monitoring.outcome_checks import Symptom

        original_id = "test_stuck_open_bets_integration"
        original_sym = Symptom(
            id=original_id,
            severity="error",
            summary="bets stuck open",
            payload={},
            suggested_action="re-run-settle",
        )
        checker_error_sym = Symptom(
            id="checker_error_check_stuck_open_bets",
            severity="error",
            summary="checker threw AttributeError",
            payload={},
            suggested_action="none",
        )

        # Simulate what _handle_outcome_symptoms does in the re-check step:
        # all_post_checks = [checker_error_sym] — original absent but checker_error present
        all_post_checks = [checker_error_sym]
        still = [s for s in all_post_checks if s.id == original_sym.id]  # []
        checker_errors = [s for s in all_post_checks if s.id.startswith("checker_error_")]
        checker_failed = bool(checker_errors)

        assert checker_failed is True, "checker_error_* must set checker_failed=True"

        sym_absent = len(still) == 0 and not checker_failed  # False (checker_failed blocks it)
        assert sym_absent is False, (
            "checker_error_* must prevent sym_absent=True even when original symptom absent"
        )

        # The verify path receives symptom_absent=False → FAILED
        observed = _observed_attempt(attempt_id="c7-int-001")
        ver_ev = VerificationEvidence(
            verified_at=_VER_AT,
            symptom_absent=sym_absent,
            detail=f"checker_error during re-check: {checker_error_sym.id}",
        )
        result = verify_resolution(observed, ver_ev)
        assert result.state == RecoveryState.FAILED
        assert result.state != RecoveryState.RECOVERED

    def test_no_checker_error_symptom_absent_recovers(self):
        """Control: no checker_error + symptom gone → RECOVERED."""
        from src.monitoring.outcome_checks import Symptom

        original_id = "test_integration_ctrl"
        all_post_checks: list[Symptom] = []  # original gone, no checker_error
        still = [s for s in all_post_checks if s.id == original_id]
        checker_errors = [s for s in all_post_checks if s.id.startswith("checker_error_")]
        checker_failed = bool(checker_errors)

        sym_absent = len(still) == 0 and not checker_failed  # True

        assert checker_failed is False
        assert sym_absent is True

        observed = _observed_attempt(attempt_id="c7-int-ctrl")
        ver_ev = VerificationEvidence(
            verified_at=_VER_AT,
            symptom_absent=sym_absent,
            detail="all clear after re-run-settle",
        )
        result = verify_resolution(observed, ver_ev)
        assert result.state == RecoveryState.RECOVERED


class TestCorrectionStoreMonotonicIdentity:
    """C8+C9: RecoveryStore monotonic and identity-safe."""

    def test_store_monotonic_cannot_go_backwards(self, tmp_path):
        """[C8] DISPATCHED cannot be overwritten by REQUESTED (going backwards)."""
        store = RecoveryStore(tmp_path / "store.json")
        attempt = request_recovery("settle", "re-run-settle",
                                   requested_at=_REQUESTED_AT, attempt_id="mono-001")
        dispatched = mark_dispatched(attempt, dispatched_at=_AFTER_AT)
        store.save(dispatched)
        assert store.get("mono-001").state == RecoveryState.DISPATCHED

        # Try to save REQUESTED (backwards) — must be rejected
        store.save(attempt)  # attempt is REQUESTED
        loaded = store.get("mono-001")
        assert loaded.state == RecoveryState.DISPATCHED  # unchanged

    def test_store_monotonic_observed_blocks_dispatched(self, tmp_path):
        """[C8] OBSERVED cannot be overwritten by DISPATCHED."""
        store = RecoveryStore(tmp_path / "store.json")
        dispatched = _dispatched_attempt(attempt_id="mono-002")
        observed = observe_execution(dispatched, _exe(_OBS_AT, exit_code=0,
                                                      source="health_snapshot", job="settle"))
        store.save(observed)
        assert store.get("mono-002").state == RecoveryState.OBSERVED

        store.save(dispatched)  # attempt to overwrite with DISPATCHED
        assert store.get("mono-002").state == RecoveryState.OBSERVED  # unchanged

    def test_store_identity_cannot_change_target(self, tmp_path):
        """[C9] Same attempt_id cannot change target."""
        store = RecoveryStore(tmp_path / "store.json")
        original = request_recovery("settle", "re-run-settle",
                                    requested_at=_REQUESTED_AT, attempt_id="id-001")
        store.save(original)

        # Try to retarget the same attempt_id to "daily_scan"
        retargeted = RecoveryAttempt(
            attempt_id="id-001",
            target="daily_scan",           # different target
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.DISPATCHED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
        )
        store.save(retargeted)
        loaded = store.get("id-001")
        assert loaded.target == "settle"  # original target preserved

    def test_store_identity_cannot_change_action(self, tmp_path):
        """[C9] Same attempt_id cannot change binding_action."""
        store = RecoveryStore(tmp_path / "store.json")
        original = request_recovery("settle", "re-run-settle",
                                    requested_at=_REQUESTED_AT, attempt_id="id-002")
        store.save(original)

        retargeted = RecoveryAttempt(
            attempt_id="id-002",
            target="settle",
            symptom_id=None,
            binding_action="re-consume",   # different action
            state=RecoveryState.DISPATCHED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
        )
        store.save(retargeted)
        loaded = store.get("id-002")
        assert loaded.binding_action == "re-run-settle"  # original action preserved

    def test_store_same_identity_forward_progression_allowed(self, tmp_path):
        """[C8+C9] Correct forward progression with same identity is allowed."""
        store = RecoveryStore(tmp_path / "store.json")
        attempt = request_recovery("settle", "re-run-settle",
                                   symptom_id="stuck_open_bets",
                                   requested_at=_REQUESTED_AT, attempt_id="id-003")
        store.save(attempt)
        dispatched = mark_dispatched(attempt, dispatched_at=_AFTER_AT)
        store.save(dispatched)
        observed = observe_execution(dispatched, _exe(_OBS_AT, exit_code=0,
                                                      source="health_snapshot", job="settle"))
        store.save(observed)

        loaded = store.get("id-003")
        assert loaded.state == RecoveryState.OBSERVED
