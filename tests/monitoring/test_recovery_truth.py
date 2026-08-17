"""TASK-P0B-003 — Recovery Truth fail-closed tests.

Proves all 12 required invariants:

  1.  request only != recovered
  2.  dispatch acknowledgement only != recovered
  3.  retry process exit=0 without new execution evidence != recovered
  4.  correlated execution with exit failure != recovered
  5.  successful execution but unchanged/stale output != recovered
  6.  wrong job/attempt correlation is rejected
  7.  pre-attempt execution cannot satisfy a new attempt
  8.  new correlated successful execution + fresh verified output => RECOVERED
  9.  unsupported/inactive/ambiguous binding => RECOVERY_UNAVAILABLE
  10. repeated processing is idempotent
  11. existing MON-001 execution invariants remain green (regression guard)
  12. existing MON-002/MON-012 schedule tests remain green (regression guard)

No live network calls. All state transitions are deterministic.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.monitoring.recovery_truth import (
    RecoveryAttempt,
    RecoveryState,
    RecoveryStore,
    ExecutionEvidence,
    VerificationEvidence,
    RECOVERY_REGISTRY,
    collect_health_snapshot_evidence,
    mark_dispatched,
    observe_execution,
    request_recovery,
    verify_resolution,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0,
         second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _exe(observed_at: str, exit_code: int | None = 0,
         source: str = "health_snapshot") -> ExecutionEvidence:
    return ExecutionEvidence(run_id=None, observed_at=observed_at,
                             exit_code=exit_code, source=source)


def _ver(symptom_absent: bool, detail: str = "re-check") -> VerificationEvidence:
    return VerificationEvidence(
        verified_at=_iso(_utc(2026, 8, 17, 12, 30)),
        symptom_absent=symptom_absent,
        detail=detail,
    )


_REQUESTED_AT = _iso(_utc(2026, 8, 17, 12, 0))
_AFTER_AT = _iso(_utc(2026, 8, 17, 12, 5))   # 5 min after request
_BEFORE_AT = _iso(_utc(2026, 8, 17, 11, 59))  # 1 min before request


def _base_attempt(
    action: str = "re-run-settle",
    state: RecoveryState = RecoveryState.REQUESTED,
) -> RecoveryAttempt:
    return RecoveryAttempt(
        attempt_id="test-attempt-001",
        target="stuck_open_bets",
        symptom_id="stuck_open_bets",
        binding_action=action,
        state=state,
        requested_at=_REQUESTED_AT,
        dispatched_at=_AFTER_AT if state != RecoveryState.REQUESTED else None,
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
    def test_exit0_with_no_health_snapshot_update_not_recovered(self):
        """[INV-3] Process exit=0 with health_snapshot_absent → observe fails → not RECOVERED."""
        dispatched = _base_attempt(state=RecoveryState.DISPATCHED)
        dispatched = RecoveryAttempt(
            **{**dispatched.__dict__,
               "dispatched_at": _AFTER_AT,
               "state": RecoveryState.DISPATCHED}
        )
        # Simulate: process exited 0 but health snapshot was not updated (absent).
        exe = ExecutionEvidence(
            run_id=None,
            observed_at=_AFTER_AT,
            exit_code=None,           # absent snapshot → None, not fabricated
            source="health_snapshot_absent",
        )
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED
        assert result.state != RecoveryState.RECOVERED

    def test_exit0_process_exit_source_with_no_snapshot_cannot_recover(self):
        """[INV-3] process_exit source with exit=0 alone is observed, but verification decides recovery.

        INV-3 focuses on missing health snapshot evidence. When source=process_exit
        and exit_code=0, observe_execution passes (temporal + exit satisfied), but
        recovery still requires symptom-check verification. Stale output → FAILED.
        """
        dispatched = RecoveryAttempt(
            attempt_id="inv3-002",
            target="stuck_open_bets",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.DISPATCHED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
        )
        exe = _exe(_AFTER_AT, exit_code=0, source="process_exit")
        observed = observe_execution(dispatched, exe)
        assert observed.state == RecoveryState.OBSERVED

        # Now verify with symptom still present → FAILED, not RECOVERED.
        ver = _ver(symptom_absent=False, detail="symptom persists")
        final = verify_resolution(observed, ver)
        assert final.state == RecoveryState.FAILED
        assert final.state != RecoveryState.RECOVERED


# ===========================================================================
# 4. Correlated execution with exit failure != recovered
# ===========================================================================

class TestInvariant4ExitFailure:
    def test_exit1_not_recovered(self):
        """[INV-4] Correlated execution with exit_code=1 → FAILED, not RECOVERED."""
        dispatched = RecoveryAttempt(
            attempt_id="inv4-001",
            target="stuck_open_bets",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.DISPATCHED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
        )
        exe = _exe(_AFTER_AT, exit_code=1)
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED
        assert "non-zero" in (result.terminal_reason or "").lower() or \
               "exit_code=1" in (result.terminal_reason or "")

    def test_exit127_not_recovered(self):
        """[INV-4] Non-zero exit (127) → FAILED."""
        dispatched = RecoveryAttempt(
            attempt_id="inv4-002",
            target="stuck_open_bets",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.DISPATCHED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
        )
        result = observe_execution(dispatched, _exe(_AFTER_AT, exit_code=127))
        assert result.state == RecoveryState.FAILED

    def test_unknown_exit_not_recovered(self):
        """[INV-4] Unknown exit_code (None) → FAILED (P0-B1 cannot claim success)."""
        dispatched = RecoveryAttempt(
            attempt_id="inv4-003",
            target="stuck_open_bets",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.DISPATCHED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
        )
        result = observe_execution(dispatched, _exe(_AFTER_AT, exit_code=None))
        assert result.state == RecoveryState.FAILED
        assert "None" in (result.terminal_reason or "") or \
               "unknown" in (result.terminal_reason or "").lower()


# ===========================================================================
# 5. Successful execution but unchanged/stale output != recovered
# ===========================================================================

class TestInvariant5StaleOutputNotRecovered:
    def test_exit0_but_symptom_still_present_not_recovered(self):
        """[INV-5] exit=0 + symptom still present → FAILED, not RECOVERED."""
        attempt = RecoveryAttempt(
            attempt_id="inv5-001",
            target="stuck_open_bets",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.OBSERVED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
            execution_evidence=_exe(_AFTER_AT, exit_code=0),
        )
        ver = _ver(symptom_absent=False, detail="settle ran but bets still open")
        result = verify_resolution(attempt, ver)
        assert result.state == RecoveryState.FAILED
        assert result.state != RecoveryState.RECOVERED
        assert result.verification_evidence is not None

    def test_stale_verification_error_recorded(self):
        """[INV-5] Failure reason must mention stale/unchanged output."""
        attempt = RecoveryAttempt(
            attempt_id="inv5-002",
            target="stuck_open_bets",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.OBSERVED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
            execution_evidence=_exe(_AFTER_AT, exit_code=0),
        )
        ver = _ver(symptom_absent=False, detail="unchanged output")
        result = verify_resolution(attempt, ver)
        assert result.state == RecoveryState.FAILED
        reason = result.terminal_reason or ""
        # Must mention that output didn't change / symptom persists
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
        attempt = RecoveryAttempt(
            attempt_id="correlation-check-id",
            target="stuck_open_bets",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.DISPATCHED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
        )
        observed = observe_execution(attempt, _exe(_AFTER_AT, exit_code=0))
        assert observed.attempt_id == "correlation-check-id"

        ver = _ver(symptom_absent=True)
        recovered = verify_resolution(observed, ver)
        assert recovered.attempt_id == "correlation-check-id"


# ===========================================================================
# 7. Pre-attempt execution cannot satisfy a new attempt
# ===========================================================================

class TestInvariant7PreAttemptExecution:
    def test_execution_before_request_rejected(self):
        """[INV-7] observed_at <= requested_at → FAILED (pre-attempt execution)."""
        dispatched = RecoveryAttempt(
            attempt_id="inv7-001",
            target="stuck_open_bets",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.DISPATCHED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
        )
        # Evidence timestamped 1 minute before the recovery request.
        exe = _exe(_BEFORE_AT, exit_code=0)
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED
        assert "pre-attempt" in (result.terminal_reason or "").lower() or \
               "<=  requested_at" in (result.terminal_reason or "") or \
               "<=" in (result.terminal_reason or "")

    def test_execution_exactly_at_request_time_rejected(self):
        """[INV-7] observed_at == requested_at is not strictly after → rejected."""
        dispatched = RecoveryAttempt(
            attempt_id="inv7-002",
            target="stuck_open_bets",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.DISPATCHED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
        )
        exe = _exe(_REQUESTED_AT, exit_code=0)  # same timestamp
        result = observe_execution(dispatched, exe)
        assert result.state == RecoveryState.FAILED

    def test_execution_one_second_after_request_accepted(self):
        """[INV-7] observed_at > requested_at by even 1 second → temporal check passes."""
        dispatched = RecoveryAttempt(
            attempt_id="inv7-003",
            target="stuck_open_bets",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.DISPATCHED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
        )
        one_second_later = _iso(_utc(2026, 8, 17, 12, 0, 1))
        exe = _exe(one_second_later, exit_code=0)
        result = observe_execution(dispatched, exe)
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

        exe = _exe(_AFTER_AT, exit_code=0, source="health_snapshot")
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
        exe = _exe(_AFTER_AT, exit_code=0, source="health_snapshot")
        attempt = observe_execution(attempt, exe)
        ver = VerificationEvidence(
            verified_at=_AFTER_AT,
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
        attempt = observe_execution(attempt, _exe(_AFTER_AT, exit_code=0))
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

        dispatched = RecoveryAttempt(
            attempt_id="idem-002",
            target="settle",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.DISPATCHED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
        )
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
            "last_run_at": _AFTER_AT, "run_id": "settle-run-abc",
        })
        ev = collect_health_snapshot_evidence("settle", _REQUESTED_AT, health_dir=tmp_path)
        assert ev is not None
        assert ev.observed_at == _AFTER_AT
        assert ev.exit_code == 0
        assert ev.source == "health_snapshot"
        assert ev.run_id == "settle-run-abc"

    def test_snapshot_missing_returns_none(self, tmp_path):
        """No health snapshot → None (cannot fabricate evidence)."""
        ev = collect_health_snapshot_evidence("settle", _REQUESTED_AT, health_dir=tmp_path)
        assert ev is None

    def test_snapshot_with_nonzero_exit_carries_failure(self, tmp_path):
        """Health snapshot with exit_code=1 → ExecutionEvidence with exit_code=1."""
        self._write_snap(tmp_path, "settle", {
            "job": "settle", "status": "error", "exit_code": 1,
            "last_run_at": _AFTER_AT, "run_id": None,
        })
        ev = collect_health_snapshot_evidence("settle", _REQUESTED_AT, health_dir=tmp_path)
        assert ev is not None
        assert ev.exit_code == 1

    def test_snapshot_with_string_exit_returns_none_exit(self, tmp_path):
        """Health snapshot with exit_code='0' (string) → None per P0-B1 truth."""
        self._write_snap(tmp_path, "settle", {
            "job": "settle", "status": "ok", "exit_code": "0",
            "last_run_at": _AFTER_AT, "run_id": None,
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

    def test_snapshot_evidence_then_fails_observe_if_pre_attempt(self, tmp_path):
        """[INV-7 integration] Even valid snapshot evidence fails if pre-dated."""
        self._write_snap(tmp_path, "settle", {
            "job": "settle", "status": "ok", "exit_code": 0,
            "last_run_at": _BEFORE_AT,  # before the recovery request
        })
        ev = collect_health_snapshot_evidence("settle", _REQUESTED_AT, health_dir=tmp_path)
        assert ev is not None
        assert ev.observed_at == _BEFORE_AT

        dispatched = RecoveryAttempt(
            attempt_id="snap-pre-001",
            target="settle",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.DISPATCHED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
        )
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
        result = observe_execution(attempt, _exe(_AFTER_AT, exit_code=0))
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
        attempt = RecoveryAttempt(
            attempt_id="order-004",
            target="settle",
            symptom_id="stuck_open_bets",
            binding_action="re-run-settle",
            state=RecoveryState.OBSERVED,
            requested_at=_REQUESTED_AT,
            dispatched_at=_AFTER_AT,
            execution_evidence=_exe(_AFTER_AT, exit_code=0),
        )
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
