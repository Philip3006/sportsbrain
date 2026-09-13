"""Deterministic signal-time schedule evaluation for shadow readiness."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    SignalTimeContract,
    _utc,
)
from src.football.top5_dispatch import OfflineDispatchLedger, make_dispatch_key


@dataclass(frozen=True)
class SignalTimeCandidate:
    """One candidate contract plus bounded retry and inference assumptions."""

    contract: SignalTimeContract
    retry_interval_seconds: int = 300
    max_retries: int = 2
    inference_duration_seconds: int = 30

    def validate(self) -> None:
        self.contract.validate()
        if self.retry_interval_seconds <= 0:
            raise ProductionContractError("signal-time retry interval must be positive")
        if self.max_retries < 0:
            raise ProductionContractError("signal-time max retries must be non-negative")
        if self.inference_duration_seconds < 0:
            raise ProductionContractError("inference duration must be non-negative")


@dataclass(frozen=True)
class ScheduledAttempt:
    fixture_key: str
    attempted_at: datetime
    attempt_number: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempted_at", _utc(self.attempted_at, "attempted_at"))


@dataclass(frozen=True)
class RequestBatch:
    league_code: str
    requested_at: datetime
    fixture_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_at", _utc(self.requested_at, "requested_at"))


@dataclass(frozen=True)
class SignalTimeFixtureDiagnostic:
    fixture_key: str
    eligible: bool
    first_eligible_execution: datetime | None
    retry_count: int
    expected_inference_time: datetime | None
    stale_odds_rejections: int
    duplicate_dispatches_prevented: int
    attempts_evaluated: int
    failure_reasons: tuple[str, ...]


@dataclass(frozen=True)
class SignalTimeSimulationResult:
    fixture_diagnostics: tuple[SignalTimeFixtureDiagnostic, ...]
    request_batches: tuple[RequestBatch, ...]
    contract: SignalTimeContract

    @property
    def fixture_count(self) -> int:
        return len(self.fixture_diagnostics)

    @property
    def eligible_count(self) -> int:
        return sum(diagnostic.eligible for diagnostic in self.fixture_diagnostics)

    @property
    def missed_count(self) -> int:
        return self.fixture_count - self.eligible_count

    @property
    def coverage(self) -> float:
        return self.eligible_count / self.fixture_count if self.fixture_count else 1.0

    @property
    def stale_odds_rejections(self) -> int:
        return sum(diagnostic.stale_odds_rejections for diagnostic in self.fixture_diagnostics)

    @property
    def duplicate_dispatches_prevented(self) -> int:
        return sum(diagnostic.duplicate_dispatches_prevented for diagnostic in self.fixture_diagnostics)

    @property
    def retry_count(self) -> int:
        return sum(diagnostic.retry_count for diagnostic in self.fixture_diagnostics)

    def as_payload(self) -> dict[str, object]:
        return {
            "contract_id": _contract_id(self.contract),
            "fixture_count": self.fixture_count,
            "eligible": self.eligible_count,
            "missed": self.missed_count,
            "coverage": self.coverage,
            "stale_odds_rejections": self.stale_odds_rejections,
            "duplicate_dispatches_prevented": self.duplicate_dispatches_prevented,
            "retry_count": self.retry_count,
            "request_batches": len(self.request_batches),
            "fixtures": [
                {
                    "fixture_key": diagnostic.fixture_key,
                    "eligible": diagnostic.eligible,
                    "first_eligible_execution": _iso_or_none(diagnostic.first_eligible_execution),
                    "retry_count": diagnostic.retry_count,
                    "expected_inference_time": _iso_or_none(diagnostic.expected_inference_time),
                    "stale_odds_rejections": diagnostic.stale_odds_rejections,
                    "duplicate_dispatches_prevented": diagnostic.duplicate_dispatches_prevented,
                    "attempts_evaluated": diagnostic.attempts_evaluated,
                    "failure_reasons": list(diagnostic.failure_reasons),
                }
                for diagnostic in self.fixture_diagnostics
            ],
        }


def build_schedule(
    fixture: Fixture,
    candidate: SignalTimeCandidate,
    *,
    start_at: datetime,
) -> tuple[ScheduledAttempt, ...]:
    """Build bounded attempts inside the fixture's event-relative window."""

    fixture.validate()
    candidate.validate()
    start_utc = _utc(start_at, "start_at")
    window_open = fixture.kickoff - timedelta(
        minutes=candidate.contract.maximum_minutes_before_kickoff
    )
    window_close = fixture.kickoff - timedelta(
        minutes=candidate.contract.minimum_minutes_before_kickoff
    )
    first = max(start_utc, window_open)
    if first > window_close:
        return ()
    attempts: list[ScheduledAttempt] = []
    for attempt_number in range(candidate.max_retries + 1):
        attempted_at = first + timedelta(
            seconds=attempt_number * candidate.retry_interval_seconds
        )
        if attempted_at > window_close:
            break
        attempts.append(ScheduledAttempt(fixture.fixture_key, attempted_at, attempt_number))
    return tuple(attempts)


def simulate_signal_time(
    fixtures: Sequence[Fixture],
    snapshots_by_fixture: Mapping[str, Sequence[MarketSnapshot]],
    candidate: SignalTimeCandidate,
    *,
    start_at: datetime,
    dispatch_ledger: OfflineDispatchLedger | None = None,
) -> SignalTimeSimulationResult:
    """Evaluate static snapshots and dispatch claims without a provider call."""

    candidate.validate()
    start_utc = _utc(start_at, "start_at")
    ledger = dispatch_ledger or OfflineDispatchLedger()
    request_batches: list[RequestBatch] = []
    diagnostics: list[SignalTimeFixtureDiagnostic] = []
    for fixture in fixtures:
        fixture.validate()
        attempts = build_schedule(fixture, candidate, start_at=start_utc)
        stale_rejections = 0
        duplicate_suppression = 0
        failure_reasons: list[str] = []
        first_eligible: datetime | None = None
        expected_inference: datetime | None = None
        accepted_attempt_number: int | None = None
        attempts_evaluated = 0
        snapshots = tuple(snapshots_by_fixture.get(fixture.fixture_key, ()))
        for snapshot in snapshots:
            if snapshot.fixture_key != fixture.fixture_key:
                raise ProductionContractError("signal-time snapshot belongs to another fixture")
            snapshot.validate()
        for attempt in attempts:
            attempts_evaluated += 1
            request_batches.append(
                RequestBatch(fixture.league_code, attempt.attempted_at, (fixture.fixture_key,))
            )
            snapshot = _latest_snapshot_before(snapshots, attempt.attempted_at)
            if snapshot is None:
                failure_reasons.append("no snapshot available")
                continue
            if snapshot.kind is MarketSnapshotKind.CLOSING:
                failure_reasons.append("closing snapshot rejected")
                continue
            if not candidate.contract.accepts(
                fixture.kickoff,
                snapshot.captured_at,
                attempt.attempted_at,
            ):
                stale_rejections += 1
                failure_reasons.append("stale or out-of-window signal snapshot")
                continue
            key = make_dispatch_key(
                fixture.league_code,
                fixture.fixture_key,
                candidate.contract,
                snapshot,
            )
            retry_reason = "scheduled_signal_retry" if attempt.attempt_number else None
            claim = ledger.claim(key, retry_reason=retry_reason)
            if not claim.accepted:
                duplicate_suppression += 1
                failure_reasons.append("duplicate dispatch suppressed")
                continue
            first_eligible = attempt.attempted_at
            expected_inference = attempt.attempted_at + timedelta(
                seconds=candidate.inference_duration_seconds
            )
            accepted_attempt_number = attempt.attempt_number
            break
        if first_eligible is None and not attempts:
            failure_reasons.append("schedule missed event-relative window")
        retry_count = (
            accepted_attempt_number
            if accepted_attempt_number is not None
            else max(0, len(attempts) - 1)
        )
        diagnostics.append(
            SignalTimeFixtureDiagnostic(
                fixture_key=fixture.fixture_key,
                eligible=first_eligible is not None,
                first_eligible_execution=first_eligible,
                retry_count=retry_count,
                expected_inference_time=expected_inference,
                stale_odds_rejections=stale_rejections,
                duplicate_dispatches_prevented=duplicate_suppression,
                attempts_evaluated=attempts_evaluated,
                failure_reasons=tuple(failure_reasons),
            )
        )
    return SignalTimeSimulationResult(tuple(diagnostics), tuple(request_batches), candidate.contract)


def _latest_snapshot_before(
    snapshots: Sequence[MarketSnapshot],
    attempted_at: datetime,
) -> MarketSnapshot | None:
    eligible = [
        snapshot
        for snapshot in snapshots
        if _utc(snapshot.captured_at, "captured_at") <= _utc(attempted_at, "attempted_at")
    ]
    return max(eligible, key=lambda snapshot: _utc(snapshot.captured_at, "captured_at"), default=None)


def _contract_id(contract: SignalTimeContract) -> str:
    from src.football.top5_dispatch import signal_time_contract_id

    return signal_time_contract_id(contract)


def _iso_or_none(value: datetime | None) -> str | None:
    return _utc(value, "timestamp").isoformat() if value is not None else None
