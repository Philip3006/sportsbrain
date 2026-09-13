"""Deterministic signal-time scheduling, bulk coalescing, and fallback evaluation."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    SignalTimeContract,
    _utc,
)
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS
from src.football.top5_dispatch import OfflineDispatchLedger, make_dispatch_key
from src.football.top5_provider_semantics import (
    BulkProviderRequest,
    BulkRequestOutcome,
    FallbackEventRequest,
    FallbackScenario,
    LogicalFixtureEvaluation,
    ProviderRequestContext,
)

# Kept as a compatibility name for callers of the first readiness pass.
RequestBatch = BulkProviderRequest
FixtureIdentity = tuple[str, str]


@dataclass(frozen=True)
class SignalTimeCandidate:
    """One contract plus bounded retry, inference, and coalescing assumptions."""

    contract: SignalTimeContract
    retry_interval_seconds: int = 300
    max_retries: int = 2
    inference_duration_seconds: int = 30
    request_bucket_seconds: int = 60

    def validate(self) -> None:
        self.contract.validate()
        if self.retry_interval_seconds <= 0:
            raise ProductionContractError("signal-time retry interval must be positive")
        if self.max_retries < 0:
            raise ProductionContractError("signal-time max retries must be non-negative")
        if self.inference_duration_seconds < 0:
            raise ProductionContractError("inference duration must be non-negative")
        if self.request_bucket_seconds <= 0:
            raise ProductionContractError("request bucket size must be positive")


@dataclass(frozen=True)
class ScheduledAttempt:
    fixture_key: str
    attempted_at: datetime
    attempt_number: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempted_at", _utc(self.attempted_at, "attempted_at"))


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
    scheduled_attempts: int = 0
    logical_fixture_evaluations: int = 0
    fallback_event_requests: int = 0
    provider_failures: int = 0
    max_retries_exhausted: bool = False


@dataclass(frozen=True)
class SignalTimeSimulationResult:
    fixture_diagnostics: tuple[SignalTimeFixtureDiagnostic, ...]
    bulk_requests: tuple[BulkProviderRequest, ...]
    fallback_event_requests: tuple[FallbackEventRequest, ...]
    logical_fixture_evaluations: tuple[LogicalFixtureEvaluation, ...]
    scheduled_attempts: int
    contract: SignalTimeContract

    @property
    def request_batches(self) -> tuple[BulkProviderRequest, ...]:
        """Compatibility alias; new callers should use ``bulk_requests``."""

        return self.bulk_requests

    @property
    def fixture_count(self) -> int:
        return len(self.fixture_diagnostics)

    @property
    def fixture_evaluation_count(self) -> int:
        return len(self.logical_fixture_evaluations)

    @property
    def bulk_request_count(self) -> int:
        return len(self.bulk_requests)

    @property
    def fallback_event_request_count(self) -> int:
        return len(self.fallback_event_requests)

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

    @property
    def provider_failure_count(self) -> int:
        return sum(diagnostic.provider_failures for diagnostic in self.fixture_diagnostics)

    @property
    def bulk_provider_failure_count(self) -> int:
        return sum(
            request.outcome is not BulkRequestOutcome.SUCCESS
            for request in self.bulk_requests
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "contract_id": _contract_id(self.contract),
            "fixture_count": self.fixture_count,
            "fixture_evaluations": self.fixture_evaluation_count,
            "scheduled_attempts": self.scheduled_attempts,
            "eligible": self.eligible_count,
            "missed": self.missed_count,
            "coverage": self.coverage,
            "bulk_provider_requests": self.bulk_request_count,
            "fallback_event_requests": self.fallback_event_request_count,
            "stale_odds_rejections": self.stale_odds_rejections,
            "duplicate_dispatches_prevented": self.duplicate_dispatches_prevented,
            "retry_count": self.retry_count,
            "provider_failures": self.provider_failure_count,
            "bulk_provider_failures": self.bulk_provider_failure_count,
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
                    "scheduled_attempts": diagnostic.scheduled_attempts,
                    "logical_fixture_evaluations": diagnostic.logical_fixture_evaluations,
                    "fallback_event_requests": diagnostic.fallback_event_requests,
                    "provider_failures": diagnostic.provider_failures,
                    "max_retries_exhausted": diagnostic.max_retries_exhausted,
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
    snapshots_by_fixture: Mapping[object, Sequence[MarketSnapshot]],
    candidate: SignalTimeCandidate,
    *,
    start_at: datetime,
    dispatch_ledger: OfflineDispatchLedger | None = None,
    provider_contexts: Mapping[str, ProviderRequestContext] | None = None,
    fallback_scenarios: Mapping[str, FallbackScenario] | None = None,
) -> SignalTimeSimulationResult:
    """Evaluate static snapshots with bulk coalescing and bounded fallbacks."""

    candidate.validate()
    start_utc = _utc(start_at, "start_at")
    ledger = dispatch_ledger or OfflineDispatchLedger()
    fixture_tuple = tuple(fixtures)
    schedules: dict[FixtureIdentity, tuple[ScheduledAttempt, ...]] = {}
    contexts: dict[str, ProviderRequestContext] = {}
    snapshot_values: dict[FixtureIdentity, tuple[MarketSnapshot, ...]] = {}
    diagnostics_state: dict[FixtureIdentity, dict[str, object]] = {}
    seen_fixture_keys: set[FixtureIdentity] = set()

    for fixture in fixture_tuple:
        fixture.validate()
        identity = (fixture.league_code, fixture.fixture_key)
        if identity in seen_fixture_keys:
            raise ProductionContractError("signal-time fixtures must have unique league/fixture identity")
        seen_fixture_keys.add(identity)
        context = _resolve_context(fixture.league_code, provider_contexts)
        if context.league_code != fixture.league_code:
            raise ProductionContractError("provider context belongs to another league")
        contexts[fixture.league_code] = context
        attempts = build_schedule(fixture, candidate, start_at=start_utc)
        schedules[identity] = attempts
        snapshot_values[identity] = _resolve_snapshots(fixture, snapshots_by_fixture)
        diagnostics_state[identity] = {
            "stale": 0,
            "duplicates": 0,
            "failures": [],
            "first_eligible": None,
            "expected_inference": None,
            "accepted_attempt": None,
            "evaluated": 0,
            "fallback": 0,
            "provider_failures": 0,
            "finished": not attempts,
        }
        if not attempts:
            diagnostics_state[identity]["failures"].append("schedule missed event-relative window")

    scenarios = fallback_scenarios or {}
    for league_code, scenario in scenarios.items():
        if league_code not in contexts:
            raise ProductionContractError("fallback scenario contains an unknown league")
        scenario.validate(
            identity[1]
            for identity in schedules
            if identity[0] == league_code
        )

    next_attempt_index = {identity: 0 for identity in schedules}
    bulk_requests: list[BulkProviderRequest] = []
    fallback_requests: list[FallbackEventRequest] = []
    logical_evaluations: list[LogicalFixtureEvaluation] = []

    while True:
        pending = [
            identity
            for identity, attempts in schedules.items()
            if not diagnostics_state[identity]["finished"]
            and next_attempt_index[identity] < len(attempts)
        ]
        if not pending:
            break
        earliest_bucket = min(
            _request_bucket(
                schedules[identity][next_attempt_index[identity]].attempted_at,
                candidate.request_bucket_seconds,
            )[0]
            for identity in pending
        )
        batch_identities = [
            identity
            for identity in pending
            if _request_bucket(
                schedules[identity][next_attempt_index[identity]].attempted_at,
                candidate.request_bucket_seconds,
            )[0]
            == earliest_bucket
        ]
        grouped: dict[tuple[str, str, str, str, tuple[str, ...], tuple[str, ...]], list[FixtureIdentity]] = {}
        for identity in batch_identities:
            context = contexts[identity[0]]
            attempt = schedules[identity][next_attempt_index[identity]]
            _bucket_start, bucket_id = _request_bucket(
                attempt.attempted_at,
                candidate.request_bucket_seconds,
            )
            batch_identity = (
                context.league_code,
                context.provider_name,
                context.sport_key,
                bucket_id,
                context.markets,
                context.regions,
            )
            grouped.setdefault(batch_identity, []).append(identity)

        for batch_identity, identities in sorted(grouped.items()):
            league_code, provider_name, sport_key, bucket_id, markets, regions = batch_identity
            identities.sort()
            attempts = [
                schedules[identity][next_attempt_index[identity]]
                for identity in identities
            ]
            scenario = scenarios.get(league_code, FallbackScenario("bulk_success"))
            fixture_keys = tuple(identity[1] for identity in identities)
            scenario.validate(fixture_keys)
            request = BulkProviderRequest(
                league_code=league_code,
                provider_name=provider_name,
                sport_key=sport_key,
                requested_at=min(attempt.attempted_at for attempt in attempts).astimezone(timezone.utc),
                request_bucket=bucket_id,
                markets=markets,
                regions=regions,
                fixture_keys=fixture_keys,
                outcome=scenario.outcome,
            )
            request.validate()
            bulk_requests.append(request)
            fallback_keys = set(scenario.fallback_keys(fixture_keys))
            if scenario.outcome is not BulkRequestOutcome.SUCCESS:
                for identity in identities:
                    diagnostics_state[identity]["provider_failures"] += 1
                for identity in identities:
                    if identity[1] in fallback_keys:
                        attempt = schedules[identity][next_attempt_index[identity]]
                        fallback_request = FallbackEventRequest(
                            league_code=league_code,
                            provider_name=provider_name,
                            sport_key=sport_key,
                            fixture_key=identity[1],
                            requested_at=attempt.attempted_at,
                            reason=scenario.outcome,
                        )
                        fallback_request.validate()
                        fallback_requests.append(fallback_request)
                        diagnostics_state[identity]["fallback"] += 1

            for identity in identities:
                state = diagnostics_state[identity]
                attempt = schedules[identity][next_attempt_index[identity]]
                logical = LogicalFixtureEvaluation(
                    fixture_key=identity[1],
                    league_code=identity[0],
                    attempted_at=attempt.attempted_at,
                    attempt_number=attempt.attempt_number,
                    request_bucket=bucket_id,
                    provider=contexts[identity[0]],
                )
                logical.validate()
                logical_evaluations.append(logical)
                state["evaluated"] += 1
                if scenario.outcome is not BulkRequestOutcome.SUCCESS and identity[1] not in fallback_keys:
                    state["failures"].append(f"bulk {scenario.outcome.value} without event fallback")
                    _advance_or_finish(state, identity, next_attempt_index, schedules)
                    continue
                snapshot = _latest_snapshot_before(snapshot_values[identity], attempt.attempted_at)
                if snapshot is None:
                    state["failures"].append("no snapshot available")
                    _advance_or_finish(state, identity, next_attempt_index, schedules)
                    continue
                if snapshot.kind is MarketSnapshotKind.CLOSING:
                    state["failures"].append("closing snapshot rejected")
                    _advance_or_finish(state, identity, next_attempt_index, schedules)
                    continue
                fixture = next(
                    fixture
                    for fixture in fixture_tuple
                    if (fixture.league_code, fixture.fixture_key) == identity
                )
                if not candidate.contract.accepts(
                    fixture.kickoff,
                    snapshot.captured_at,
                    attempt.attempted_at,
                ):
                    state["stale"] += 1
                    state["failures"].append("stale or out-of-window signal snapshot")
                    _advance_or_finish(state, identity, next_attempt_index, schedules)
                    continue
                key = make_dispatch_key(identity[0], identity[1], candidate.contract, snapshot)
                retry_reason = "scheduled_signal_retry" if attempt.attempt_number else None
                claim = ledger.claim(key, retry_reason=retry_reason)
                if not claim.accepted:
                    state["duplicates"] += 1
                    state["failures"].append("duplicate dispatch suppressed")
                    state["finished"] = True
                    continue
                state["first_eligible"] = attempt.attempted_at
                state["expected_inference"] = attempt.attempted_at + timedelta(
                    seconds=candidate.inference_duration_seconds
                )
                state["accepted_attempt"] = attempt.attempt_number
                state["finished"] = True

    diagnostics: list[SignalTimeFixtureDiagnostic] = []
    for fixture in fixture_tuple:
        identity = (fixture.league_code, fixture.fixture_key)
        state = diagnostics_state[identity]
        evaluated = int(state["evaluated"])
        accepted_attempt = state["accepted_attempt"]
        retry_count = int(accepted_attempt) if accepted_attempt is not None else max(0, evaluated - 1)
        diagnostics.append(
            SignalTimeFixtureDiagnostic(
                fixture_key=fixture.fixture_key,
                eligible=state["first_eligible"] is not None,
                first_eligible_execution=state["first_eligible"],
                retry_count=retry_count,
                expected_inference_time=state["expected_inference"],
                stale_odds_rejections=int(state["stale"]),
                duplicate_dispatches_prevented=int(state["duplicates"]),
                attempts_evaluated=evaluated,
                failure_reasons=tuple(state["failures"]),
                scheduled_attempts=len(schedules[identity]),
                logical_fixture_evaluations=evaluated,
                fallback_event_requests=int(state["fallback"]),
                provider_failures=int(state["provider_failures"]),
                max_retries_exhausted=(
                    state["first_eligible"] is None
                    and bool(schedules[identity])
                    and evaluated >= len(schedules[identity])
                ),
            )
        )
    return SignalTimeSimulationResult(
        fixture_diagnostics=tuple(diagnostics),
        bulk_requests=tuple(bulk_requests),
        fallback_event_requests=tuple(fallback_requests),
        logical_fixture_evaluations=tuple(logical_evaluations),
        scheduled_attempts=sum(len(attempts) for attempts in schedules.values()),
        contract=candidate.contract,
    )


def _resolve_context(
    league_code: str,
    provider_contexts: Mapping[str, ProviderRequestContext] | None,
) -> ProviderRequestContext:
    if provider_contexts is not None and league_code in provider_contexts:
        context = provider_contexts[league_code]
        context.validate()
        return context
    adapter = TOP5_LEAGUE_ADAPTERS.get(league_code)
    if adapter is None or adapter.config.provider_mapping is None:
        raise ProductionContractError(f"no provider context for league {league_code}")
    return ProviderRequestContext.from_mapping(league_code, adapter.config.provider_mapping)


def _resolve_snapshots(
    fixture: Fixture,
    snapshots_by_fixture: Mapping[object, Sequence[MarketSnapshot]],
) -> tuple[MarketSnapshot, ...]:
    values = snapshots_by_fixture.get(
        (fixture.league_code, fixture.fixture_key),
        snapshots_by_fixture.get(fixture.fixture_key, ()),
    )
    snapshots = tuple(values)
    for snapshot in snapshots:
        if snapshot.fixture_key != fixture.fixture_key:
            raise ProductionContractError("signal-time snapshot belongs to another fixture")
        snapshot.validate()
    return snapshots


def _request_bucket(attempted_at: datetime, bucket_seconds: int) -> tuple[datetime, str]:
    timestamp = _utc(attempted_at, "attempted_at").timestamp()
    bucket_epoch = int(timestamp // bucket_seconds) * bucket_seconds
    bucket_start = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)
    return bucket_start, bucket_start.isoformat()


def _advance_or_finish(
    state: dict[str, object],
    identity: FixtureIdentity,
    next_attempt_index: dict[FixtureIdentity, int],
    schedules: Mapping[FixtureIdentity, tuple[ScheduledAttempt, ...]],
) -> None:
    next_attempt_index[identity] += 1
    if next_attempt_index[identity] >= len(schedules[identity]):
        state["finished"] = True


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
