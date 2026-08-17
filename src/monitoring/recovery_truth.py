"""P0-B3 Recovery Truth Layer.

Canonical semantic chain:
    REQUESTED → DISPATCHED → OBSERVED → VERIFIED → RECOVERED

RECOVERED is legal only when all four prior states are satisfied:
  1. the intended recovery actor was actually dispatched,
  2. a NEW execution correlated to this recovery attempt was observed,
  3. that execution is successful under existing P0-B1 execution truth,
  4. fresh post-execution verification confirms the original failure resolved.

Unsupported, inactive, or ambiguous recovery bindings → RECOVERY_UNAVAILABLE.

Design constraints:
- Reuses P0-B1 _coerce_exit_code for exit evidence truth.
- Reuses P0-B2 JOB_EXPECTATIONS job names (no duplication).
- Reuses Symptom.id / suggested_action from outcome_checks where applicable.
- State transitions are pure functions (return new RecoveryAttempt, never mutate).
- RecoveryStore is idempotent: terminal states are immutable on re-save.
- Replay-safe: same attempt_id processed twice yields identical terminal state.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
RECOVERY_STORE_PATH = ROOT / "results" / "health" / "recovery_attempts.json"
_HEALTH_DIR = ROOT / "results" / "health"


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class RecoveryState(str, Enum):
    REQUESTED           = "REQUESTED"
    DISPATCHED          = "DISPATCHED"
    OBSERVED            = "OBSERVED"
    VERIFIED            = "VERIFIED"
    RECOVERED           = "RECOVERED"
    FAILED              = "FAILED"
    RECOVERY_UNAVAILABLE = "RECOVERY_UNAVAILABLE"


_TERMINAL_STATES = {
    RecoveryState.RECOVERED,
    RecoveryState.FAILED,
    RecoveryState.RECOVERY_UNAVAILABLE,
}


# ---------------------------------------------------------------------------
# Evidence types
# ---------------------------------------------------------------------------

@dataclass
class ExecutionEvidence:
    """Proof that a new execution correlated to a recovery attempt occurred."""
    run_id: str | None       # health snapshot run_id if available
    observed_at: str         # ISO-8601 UTC — must post-date attempt.requested_at
    exit_code: int | None    # P0-B1 canonical int (None = unknown, not fabricated)
    source: str              # "health_snapshot" | "process_exit" | "health_snapshot_absent"


@dataclass
class VerificationEvidence:
    """Proof that the original failure is actually resolved."""
    verified_at: str         # ISO-8601 UTC
    symptom_absent: bool     # re-check: original symptom no longer present
    detail: str              # what was checked and how


# ---------------------------------------------------------------------------
# Recovery Attempt record
# ---------------------------------------------------------------------------

@dataclass
class RecoveryAttempt:
    """Immutable record of a single recovery attempt and its evidence chain."""
    attempt_id: str
    target: str                             # job name or symptom id
    symptom_id: str | None                  # stable Symptom.id from outcome_checks
    binding_action: str                     # action key in RECOVERY_REGISTRY
    state: RecoveryState
    requested_at: str                       # ISO-8601 UTC (set once at creation)
    dispatched_at: str | None = None
    execution_evidence: ExecutionEvidence | None = None
    verification_evidence: VerificationEvidence | None = None
    terminal_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RecoveryAttempt":
        exe = d.get("execution_evidence")
        ver = d.get("verification_evidence")
        return cls(
            attempt_id=d["attempt_id"],
            target=d["target"],
            symptom_id=d.get("symptom_id"),
            binding_action=d["binding_action"],
            state=RecoveryState(d["state"]),
            requested_at=d["requested_at"],
            dispatched_at=d.get("dispatched_at"),
            execution_evidence=ExecutionEvidence(**exe) if exe else None,
            verification_evidence=VerificationEvidence(**ver) if ver else None,
            terminal_reason=d.get("terminal_reason"),
        )


# ---------------------------------------------------------------------------
# Recovery Binding registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecoveryBinding:
    """Allowlisted mapping: action → recovery actor → execution evidence source."""
    action: str              # action key (matches outcome_checks.suggested_action or layer-1 verb)
    actor_layer: str         # "layer1" | "layer2_outcome" | "unavailable"
    health_job: str | None   # job whose health snapshot provides execution evidence; None = process_exit only
    active: bool = True      # inactive → RECOVERY_UNAVAILABLE (requires human action)


# Canonical registry — every recoverable action must appear here.
# Actions not listed → RECOVERY_UNAVAILABLE (fail-closed).
RECOVERY_REGISTRY: dict[str, RecoveryBinding] = {
    # ── Layer-2 outcome actions (auto_heal_ai._handle_outcome_symptoms) ──────
    # Settle re-run: settle_bets.py writes its own health snapshot.
    "re-run-settle": RecoveryBinding(
        action="re-run-settle",
        actor_layer="layer2_outcome",
        health_job="settle",
    ),
    # Consume pending bets: consume_pending_bets.py writes its health snapshot.
    "re-consume": RecoveryBinding(
        action="re-consume",
        actor_layer="layer2_outcome",
        health_job="consume_pending_bets",
    ),
    # Force-refresh signals: daily_scan.py writes health snapshot.
    "force-refresh-signals": RecoveryBinding(
        action="force-refresh-signals",
        actor_layer="layer2_outcome",
        health_job="daily_scan",
    ),
    # VAPID test: no health snapshot — execution evidence is process exit only.
    # This is weaker than snapshot-backed evidence; noted as remaining risk.
    "re-test-vapid": RecoveryBinding(
        action="re-test-vapid",
        actor_layer="layer2_outcome",
        health_job=None,
    ),
    # Human-required actions — no auto recovery possible.
    "prompt-resubscribe": RecoveryBinding(
        action="prompt-resubscribe",
        actor_layer="unavailable",
        health_job=None,
        active=False,
    ),
    "none": RecoveryBinding(
        action="none",
        actor_layer="unavailable",
        health_job=None,
        active=False,
    ),
    # ── Layer-1 job retries (auto_heal_cron.sh _retry_job) ──────────────────
    # Process exit alone is NOT sufficient — health snapshot must update.
    "settle_retry": RecoveryBinding(
        action="settle_retry",
        actor_layer="layer1",
        health_job="settle",
    ),
    "daily_scan_retry": RecoveryBinding(
        action="daily_scan_retry",
        actor_layer="layer1",
        health_job="daily_scan",
    ),
    "auto_retrain_retry": RecoveryBinding(
        action="auto_retrain_retry",
        actor_layer="layer1",
        health_job="auto_retrain",
    ),
    "closing_odds_retry": RecoveryBinding(
        action="closing_odds_retry",
        actor_layer="layer1",
        health_job="closing_odds",
    ),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _fail(
    attempt: RecoveryAttempt,
    reason: str,
    *,
    verification_evidence: VerificationEvidence | None = None,
) -> RecoveryAttempt:
    return RecoveryAttempt(
        attempt_id=attempt.attempt_id,
        target=attempt.target,
        symptom_id=attempt.symptom_id,
        binding_action=attempt.binding_action,
        state=RecoveryState.FAILED,
        requested_at=attempt.requested_at,
        dispatched_at=attempt.dispatched_at,
        execution_evidence=attempt.execution_evidence,
        verification_evidence=verification_evidence,
        terminal_reason=reason,
    )


# ---------------------------------------------------------------------------
# State machine — pure transition functions
# ---------------------------------------------------------------------------

def request_recovery(
    target: str,
    binding_action: str,
    symptom_id: str | None = None,
    *,
    attempt_id: str | None = None,
    requested_at: str | None = None,
) -> RecoveryAttempt:
    """Create a new RecoveryAttempt.

    Fails closed immediately if the binding is unsupported or inactive
    (RECOVERY_UNAVAILABLE), so callers cannot proceed as if recovery is possible.
    """
    aid = attempt_id or f"{target}_{_now_iso()}_{uuid.uuid4().hex[:8]}"
    rat = requested_at or _now_iso()

    binding = RECOVERY_REGISTRY.get(binding_action)

    if binding is None:
        return RecoveryAttempt(
            attempt_id=aid,
            target=target,
            symptom_id=symptom_id,
            binding_action=binding_action,
            state=RecoveryState.RECOVERY_UNAVAILABLE,
            requested_at=rat,
            terminal_reason=(
                f"No recovery binding registered for action {binding_action!r}. "
                "Unsupported actions fail closed."
            ),
        )

    if not binding.active:
        return RecoveryAttempt(
            attempt_id=aid,
            target=target,
            symptom_id=symptom_id,
            binding_action=binding_action,
            state=RecoveryState.RECOVERY_UNAVAILABLE,
            requested_at=rat,
            terminal_reason=(
                f"Recovery binding for {binding_action!r} is inactive "
                f"(actor_layer={binding.actor_layer!r}). Requires human action."
            ),
        )

    return RecoveryAttempt(
        attempt_id=aid,
        target=target,
        symptom_id=symptom_id,
        binding_action=binding_action,
        state=RecoveryState.REQUESTED,
        requested_at=rat,
    )


def mark_dispatched(
    attempt: RecoveryAttempt,
    *,
    dispatched_at: str | None = None,
) -> RecoveryAttempt:
    """Advance REQUESTED → DISPATCHED.

    Terminal states are preserved unchanged (idempotent).
    Any other non-REQUESTED state fails closed.
    """
    if attempt.state in _TERMINAL_STATES:
        return attempt
    if attempt.state != RecoveryState.REQUESTED:
        return _fail(
            attempt,
            f"Cannot dispatch from state {attempt.state.value}. "
            "Dispatch is only valid from REQUESTED.",
        )
    return RecoveryAttempt(
        attempt_id=attempt.attempt_id,
        target=attempt.target,
        symptom_id=attempt.symptom_id,
        binding_action=attempt.binding_action,
        state=RecoveryState.DISPATCHED,
        requested_at=attempt.requested_at,
        dispatched_at=dispatched_at or _now_iso(),
    )


def observe_execution(
    attempt: RecoveryAttempt,
    evidence: ExecutionEvidence,
) -> RecoveryAttempt:
    """Advance DISPATCHED → OBSERVED.

    Validates:
    - Temporal ordering: evidence.observed_at must be strictly after attempt.requested_at.
      Pre-attempt executions cannot satisfy a new attempt.
    - P0-B1 exit truth: exit_code must be canonical int 0.
      Non-zero exit → execution failure, not recovery.
      None exit → unknown evidence, cannot claim successful execution.
    """
    if attempt.state in _TERMINAL_STATES:
        return attempt
    if attempt.state != RecoveryState.DISPATCHED:
        return _fail(
            attempt,
            f"Cannot observe execution from state {attempt.state.value}. "
            "Observation requires prior dispatch.",
        )

    # Temporal correlation: execution must post-date the recovery request.
    requested_dt = _parse_iso(attempt.requested_at)
    observed_dt = _parse_iso(evidence.observed_at)
    if requested_dt is None or observed_dt is None:
        return _fail(
            attempt,
            "Cannot parse timestamps for temporal correlation check. "
            f"requested_at={attempt.requested_at!r}, observed_at={evidence.observed_at!r}.",
        )
    if observed_dt <= requested_dt:
        return _fail(
            attempt,
            f"Execution evidence is pre-attempt: observed_at={evidence.observed_at!r} "
            f"<= requested_at={attempt.requested_at!r}. "
            "Pre-attempt execution cannot satisfy a new recovery attempt.",
        )

    # P0-B1 exit truth: only int 0 is canonical success.
    if evidence.exit_code is None:
        return _fail(
            attempt,
            f"Execution evidence has unknown/invalid exit_code=None "
            f"(source={evidence.source!r}). "
            "Cannot confirm successful execution — P0-B1 requires canonical int exit.",
        )
    if evidence.exit_code != 0:
        return _fail(
            attempt,
            f"Correlated execution has exit_code={evidence.exit_code} (non-zero). "
            "Execution failure does not constitute recovery.",
        )

    return RecoveryAttempt(
        attempt_id=attempt.attempt_id,
        target=attempt.target,
        symptom_id=attempt.symptom_id,
        binding_action=attempt.binding_action,
        state=RecoveryState.OBSERVED,
        requested_at=attempt.requested_at,
        dispatched_at=attempt.dispatched_at,
        execution_evidence=evidence,
    )


def verify_resolution(
    attempt: RecoveryAttempt,
    evidence: VerificationEvidence,
) -> RecoveryAttempt:
    """Advance OBSERVED → VERIFIED → RECOVERED, or FAILED.

    Validates that the original failure symptom is actually absent.
    Successful execution with stale/unchanged output → FAILED, not RECOVERED.
    """
    if attempt.state in _TERMINAL_STATES:
        return attempt
    if attempt.state != RecoveryState.OBSERVED:
        return _fail(
            attempt,
            f"Cannot verify from state {attempt.state.value}. "
            "Verification requires observed execution.",
            verification_evidence=evidence,
        )

    if not evidence.symptom_absent:
        return _fail(
            attempt,
            f"Verification failed: original symptom still present after recovery action. "
            f"Successful execution with unchanged/stale output is not RECOVERED. "
            f"Detail: {evidence.detail}",
            verification_evidence=evidence,
        )

    # All five chain steps satisfied → RECOVERED.
    return RecoveryAttempt(
        attempt_id=attempt.attempt_id,
        target=attempt.target,
        symptom_id=attempt.symptom_id,
        binding_action=attempt.binding_action,
        state=RecoveryState.RECOVERED,
        requested_at=attempt.requested_at,
        dispatched_at=attempt.dispatched_at,
        execution_evidence=attempt.execution_evidence,
        verification_evidence=evidence,
        terminal_reason=None,
    )


# ---------------------------------------------------------------------------
# Health snapshot evidence collector (reuses P0-B1 exit truth)
# ---------------------------------------------------------------------------

def collect_health_snapshot_evidence(
    job: str,
    requested_at: str,
    *,
    health_dir: Path | None = None,
) -> ExecutionEvidence | None:
    """Read a job's health snapshot and build ExecutionEvidence.

    Returns None if the snapshot doesn't exist.
    The exit_code is validated using P0-B1 _coerce_exit_code so unknown
    evidence is stored as None, never fabricated.

    The caller must still pass this evidence through observe_execution() which
    enforces the temporal ordering invariant.
    """
    from src.monitoring.health_writer import _coerce_exit_code  # P0-B1 reuse

    hdir = health_dir or _HEALTH_DIR
    path = hdir / f"{job}.json"
    if not path.exists():
        return None
    try:
        snap = json.loads(path.read_text())
    except Exception:
        return None

    last_run = snap.get("last_run_at")
    if not last_run:
        return None

    coerced_exit = _coerce_exit_code(snap.get("exit_code"))
    return ExecutionEvidence(
        run_id=snap.get("run_id"),
        observed_at=last_run,
        exit_code=coerced_exit,
        source="health_snapshot",
    )


# ---------------------------------------------------------------------------
# Persistent store — idempotent and replay-safe
# ---------------------------------------------------------------------------

class RecoveryStore:
    """JSON-backed store for recovery attempts.

    Terminal states (RECOVERED / FAILED / RECOVERY_UNAVAILABLE) are immutable:
    attempting to overwrite them is a no-op (idempotent).
    """

    def __init__(self, path: Path = RECOVERY_STORE_PATH) -> None:
        self._path = path

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except Exception:
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self._path)

    def get(self, attempt_id: str) -> RecoveryAttempt | None:
        raw = self._load().get(attempt_id)
        if raw is None:
            return None
        try:
            return RecoveryAttempt.from_dict(raw)
        except Exception:
            return None

    def save(self, attempt: RecoveryAttempt) -> None:
        """Save attempt. Terminal states are immutable — re-saving does not overwrite."""
        data = self._load()
        existing_raw = data.get(attempt.attempt_id)
        if existing_raw is not None:
            try:
                existing = RecoveryAttempt.from_dict(existing_raw)
                if existing.state in _TERMINAL_STATES:
                    return  # terminal → immutable, idempotent
            except Exception:
                pass  # corrupt entry — overwrite safely
        data[attempt.attempt_id] = attempt.to_dict()
        self._write(data)

    def recent(self, limit: int = 50) -> list[RecoveryAttempt]:
        """Return recent attempts, newest first by requested_at."""
        data = self._load()
        attempts: list[RecoveryAttempt] = []
        for raw in data.values():
            try:
                attempts.append(RecoveryAttempt.from_dict(raw))
            except Exception:
                pass
        attempts.sort(key=lambda a: a.requested_at, reverse=True)
        return attempts[:limit]
