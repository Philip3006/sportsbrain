"""Offline-first Top-5 runtime operations and activation preflight.

This module composes the accepted Top-5 contracts into one deterministic
five-league execution seam.  It deliberately has no provider, scheduler,
publisher, ledger, Cloudflare, or filesystem-writer dependency.  Production
integration must inject those boundaries only after the explicit activation
gate has passed.

The runner is useful for three things that were previously spread across
separate contracts:

* all-or-nothing offline rehearsal for BL1/EPL/LL/SA/L1;
* a serializable checkpoint contract for safe restart and idempotent replay;
* a side-effect-free activation precheck with explicit blocking reasons.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
from typing import Protocol

from src.football.production_contracts import (
    ActivationMode,
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    SignalTimeContract,
    _utc,
)
from src.football.provider_cascade.contracts import (
    CANDIDATE_ONLY_PROVIDER_IDENTITIES,
)
from src.football.top5_adapters import Top5LeagueAdapter
from src.football.top5_controlled_shadow_provider_qualification import TOP5_LEAGUES
from src.football.top5_offline import (
    OfflineCompatibilityResult,
    run_offline_compatibility,
)

TOP5_RUNTIME_CONTRACT_VERSION = "top5-runtime-operations-v1"
TOP5_PRODUCTION_PROVIDER = "the_odds_api"
TOP5_RUNTIME_LEAGUES = TOP5_LEAGUES
_HEX_DIGEST_LENGTHS = (40, 64)


class Top5RuntimeError(ProductionContractError):
    """Raised when the composite Top-5 runtime contract is invalid."""


class Top5RuntimeStatus(str, Enum):
    DISABLED = "DISABLED"
    READY = "READY"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    BLOCKED_BY_EVIDENCE = "BLOCKED_BY_EVIDENCE"
    BLOCKED_BY_AUTHORITY = "BLOCKED_BY_AUTHORITY"
    STALE = "STALE"


class Top5RuntimeStage(str, Enum):
    FIXTURES_DISCOVERED = "FIXTURES_DISCOVERED"
    ODDS_REFRESHED = "ODDS_REFRESHED"
    PREDICTIONS_GENERATED = "PREDICTIONS_GENERATED"
    SIGNALS_READY = "SIGNALS_READY"
    RESULTS_INGESTED = "RESULTS_INGESTED"
    SETTLED = "SETTLED"
    FAILED_CLOSED = "FAILED_CLOSED"


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Top5RuntimeError(f"{name} must be non-empty text")
    return value.strip()


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash(name: str, value: object) -> str:
    result = _text(name, value).lower()
    if len(result) not in _HEX_DIGEST_LENGTHS or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise Top5RuntimeError(f"{name} must be a hexadecimal digest")
    return result


def _bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise Top5RuntimeError(f"{name} must be boolean")
    return value


@dataclass(frozen=True)
class Top5RuntimeConfig:
    """Safe defaults for the future runtime boundary.

    The current implementation is intentionally disabled.  A future runtime
    writer must provide a separate, explicitly authorized configuration rather
    than mutating this default or treating evidence as activation authority.
    """

    provider_identity: str = TOP5_PRODUCTION_PROVIDER
    activation_mode: ActivationMode = ActivationMode.DISABLED
    scheduler_enabled: bool = False
    publication_enabled: bool = False
    betting_enabled: bool = False
    ledger_mutation_enabled: bool = False
    max_retries: int = 0
    timeout_seconds: float = 30.0
    maximum_odds_age_seconds: int = 900

    def validate(self) -> None:
        provider = _text("provider_identity", self.provider_identity)
        if provider in CANDIDATE_ONLY_PROVIDER_IDENTITIES:
            raise Top5RuntimeError(
                "candidate-only provider cannot be runtime authority"
            )
        if provider != TOP5_PRODUCTION_PROVIDER:
            raise Top5RuntimeError("Top-5 production runtime requires the_odds_api")
        if ActivationMode(self.activation_mode) is not ActivationMode.DISABLED:
            raise Top5RuntimeError("Top-5 runtime remains disabled by default")
        for name, value in (
            ("scheduler_enabled", self.scheduler_enabled),
            ("publication_enabled", self.publication_enabled),
            ("betting_enabled", self.betting_enabled),
            ("ledger_mutation_enabled", self.ledger_mutation_enabled),
        ):
            if not isinstance(value, bool):
                raise Top5RuntimeError(f"{name} must be boolean")
            if value:
                raise Top5RuntimeError(f"{name} must remain disabled")
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 0
            or self.max_retries > 1
        ):
            raise Top5RuntimeError("max_retries must be bounded to 0 or 1")
        if not isfinite(float(self.timeout_seconds)) or self.timeout_seconds <= 0:
            raise Top5RuntimeError("timeout_seconds must be finite and positive")
        if (
            isinstance(self.maximum_odds_age_seconds, bool)
            or not isinstance(self.maximum_odds_age_seconds, int)
            or self.maximum_odds_age_seconds <= 0
        ):
            raise Top5RuntimeError("maximum_odds_age_seconds must be positive")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "contract_version": TOP5_RUNTIME_CONTRACT_VERSION,
            "provider_identity": self.provider_identity,
            "activation_mode": ActivationMode(self.activation_mode).value,
            "scheduler_enabled": False,
            "publication_enabled": False,
            "betting_enabled": False,
            "ledger_mutation_enabled": False,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "maximum_odds_age_seconds": self.maximum_odds_age_seconds,
        }


@dataclass(frozen=True)
class Top5ResultBinding:
    """Offline result provenance; it never writes or settles a financial ledger."""

    fixture_key: str
    league_code: str
    result_id: str
    result_source: str
    outcome: str
    source_timestamp: datetime
    captured_at: datetime
    result_digest: str
    synthetic: bool = True

    def validate(self) -> None:
        for name, value in (
            ("fixture_key", self.fixture_key),
            ("league_code", self.league_code),
            ("result_id", self.result_id),
            ("result_source", self.result_source),
            ("outcome", self.outcome),
        ):
            _text(name, value)
        if self.league_code not in TOP5_RUNTIME_LEAGUES:
            raise Top5RuntimeError("result league is outside the Top-5 scope")
        if self.outcome not in {"home", "draw", "away", "void", "postponed"}:
            raise Top5RuntimeError("result outcome is unsupported")
        _utc(self.source_timestamp, "result source_timestamp")
        _utc(self.captured_at, "result captured_at")
        if self.captured_at < self.source_timestamp:
            raise Top5RuntimeError("result capture precedes source timestamp")
        _hash("result_digest", self.result_digest)
        _bool("synthetic", self.synthetic)

    def _payload(self, *, include_settlement: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "fixture_key": self.fixture_key,
            "league_code": self.league_code,
            "result_id": self.result_id,
            "result_source": self.result_source,
            "outcome": self.outcome,
            "source_timestamp": _utc(
                self.source_timestamp, "source_timestamp"
            ).isoformat(),
            "captured_at": _utc(self.captured_at, "captured_at").isoformat(),
            "result_digest": self.result_digest,
            "synthetic": self.synthetic,
        }
        if include_settlement:
            payload["settlement_id"] = f"top5-settlement:{_digest(payload)[:32]}"
        return payload

    @property
    def settlement_id(self) -> str:
        self.validate()
        return str(self._payload(include_settlement=True)["settlement_id"])

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return self._payload(include_settlement=True)


@dataclass(frozen=True)
class Top5LeagueRuntimeInput:
    """One explicitly injected, offline league input."""

    adapter: Top5LeagueAdapter
    fixtures: tuple[Fixture, ...]
    signal_snapshots: tuple[MarketSnapshot, ...]
    signal_time: SignalTimeContract
    results: tuple[Top5ResultBinding, ...] = ()
    closing_benchmark_snapshots: tuple[MarketSnapshot, ...] = ()
    provider_identity: str = TOP5_PRODUCTION_PROVIDER

    def validate(self) -> None:
        self.adapter.validate()
        config = self.adapter.config
        if self.provider_identity != TOP5_PRODUCTION_PROVIDER:
            if self.provider_identity in CANDIDATE_ONLY_PROVIDER_IDENTITIES:
                raise Top5RuntimeError("candidate provider cannot enter runtime input")
            raise Top5RuntimeError("runtime input provider is not the_odds_api")
        if config.provider_mapping is None:
            raise Top5RuntimeError("Top-5 runtime input is missing provider mapping")
        if config.provider_mapping.provider_name != TOP5_PRODUCTION_PROVIDER:
            raise Top5RuntimeError("runtime input mapping is not the_odds_api")
        self.signal_time.validate()
        if not self.fixtures:
            raise Top5RuntimeError(
                f"{config.league_code} requires at least one fixture"
            )
        fixture_keys: set[str] = set()
        for fixture in self.fixtures:
            fixture.validate()
            if fixture.league_code != config.league_code:
                raise Top5RuntimeError("fixture league differs from runtime input")
            if fixture.fixture_key in fixture_keys:
                raise Top5RuntimeError("runtime input contains a duplicate fixture")
            fixture_keys.add(fixture.fixture_key)
        for snapshot in self.signal_snapshots:
            snapshot.validate()
            if snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
                raise Top5RuntimeError(
                    "closing odds cannot enter signal-time runtime input"
                )
            if snapshot.fixture_key not in fixture_keys:
                raise Top5RuntimeError("signal snapshot references an unknown fixture")
        for snapshot in self.closing_benchmark_snapshots:
            snapshot.validate()
            if snapshot.kind is not MarketSnapshotKind.CLOSING:
                raise Top5RuntimeError(
                    "closing benchmark input must be a closing snapshot"
                )
            if snapshot.fixture_key not in fixture_keys:
                raise Top5RuntimeError(
                    "closing benchmark references an unknown fixture"
                )
        result_keys = set()
        for result in self.results:
            result.validate()
            if (
                result.league_code != config.league_code
                or result.fixture_key not in fixture_keys
            ):
                raise Top5RuntimeError("result binding does not match runtime input")
            if result.fixture_key in result_keys:
                raise Top5RuntimeError(
                    "runtime input contains duplicate result bindings"
                )
            result_keys.add(result.fixture_key)
        if result_keys and result_keys != fixture_keys:
            raise Top5RuntimeError("results must cover every fixture or be absent")

    @property
    def league_code(self) -> str:
        return self.adapter.league_code

    def as_payload(self) -> dict[str, object]:
        self.validate()
        config = self.adapter.config
        mapping = config.provider_mapping
        assert mapping is not None
        return {
            "league": self.league_code,
            "provider_identity": self.provider_identity,
            "adapter_config": {
                "league_code": config.league_code,
                "display_name": config.display_name,
                "provider_sport_key": config.provider_sport_key,
                "fixture_source": config.fixture_source,
                "result_source": config.result_source,
                "model_adapter_id": config.model_adapter_id,
                "activation_mode": ActivationMode(config.activation_mode).value,
                "provider_mapping": {
                    "provider_name": mapping.provider_name,
                    "competition_id": mapping.competition_id,
                    "sport_key": mapping.sport_key,
                    "fixture_endpoint": mapping.fixture_endpoint,
                    "result_source": mapping.result_source,
                    "markets": list(mapping.markets),
                    "regions": list(mapping.regions),
                },
            },
            "fixtures": [
                {
                    "fixture_key": fixture.fixture_key,
                    "league_code": fixture.league_code,
                    "home_team": fixture.home_team,
                    "away_team": fixture.away_team,
                    "kickoff": _utc(fixture.kickoff, "kickoff").isoformat(),
                }
                for fixture in self.fixtures
            ],
            "signal_snapshots": [
                {
                    "fixture_key": snapshot.fixture_key,
                    "captured_at": _utc(
                        snapshot.captured_at, "captured_at"
                    ).isoformat(),
                    "kind": MarketSnapshotKind(snapshot.kind).value,
                    "source": snapshot.source,
                    "odds": dict(sorted(snapshot.odds.items())),
                    "snapshot_id": snapshot.snapshot_id,
                }
                for snapshot in self.signal_snapshots
            ],
            "signal_time": {
                "minimum_minutes_before_kickoff": self.signal_time.minimum_minutes_before_kickoff,
                "maximum_minutes_before_kickoff": self.signal_time.maximum_minutes_before_kickoff,
                "maximum_odds_age_seconds": self.signal_time.maximum_odds_age_seconds,
                "approval_ref": self.signal_time.approval_ref,
            },
            "results": [result.as_payload() for result in self.results],
            "closing_benchmark_snapshots": [
                {
                    "fixture_key": snapshot.fixture_key,
                    "captured_at": _utc(
                        snapshot.captured_at, "captured_at"
                    ).isoformat(),
                    "kind": MarketSnapshotKind(snapshot.kind).value,
                    "source": snapshot.source,
                    "odds": dict(sorted(snapshot.odds.items())),
                    "snapshot_id": snapshot.snapshot_id,
                }
                for snapshot in self.closing_benchmark_snapshots
            ],
        }


@dataclass(frozen=True)
class Top5RuntimeRequest:
    """One all-or-nothing offline execution request."""

    run_id: str
    session_id: str
    requested_at: datetime
    inputs: Mapping[str, Top5LeagueRuntimeInput]
    config: Top5RuntimeConfig = field(default_factory=Top5RuntimeConfig)
    offline_only: bool = True
    no_bet: bool = True
    publication_enabled: bool = False
    activation_enabled: bool = False

    def validate(self) -> None:
        _text("run_id", self.run_id)
        _text("session_id", self.session_id)
        _utc(self.requested_at, "requested_at")
        self.config.validate()
        for name, value in (
            ("offline_only", self.offline_only),
            ("no_bet", self.no_bet),
            ("publication_enabled", self.publication_enabled),
            ("activation_enabled", self.activation_enabled),
        ):
            if not isinstance(value, bool):
                raise Top5RuntimeError(f"{name} must be boolean")
        if not self.offline_only:
            raise Top5RuntimeError("Top-5 runtime operations are offline-only")
        if not self.no_bet or self.publication_enabled or self.activation_enabled:
            raise Top5RuntimeError("offline Top-5 runtime violates safety flags")
        if set(self.inputs) != set(TOP5_RUNTIME_LEAGUES) or len(self.inputs) != len(
            TOP5_RUNTIME_LEAGUES
        ):
            raise Top5RuntimeError(
                "runtime request must contain exactly the five Top-5 leagues"
            )
        all_fixture_keys: set[str] = set()
        for league in TOP5_RUNTIME_LEAGUES:
            input_value = self.inputs[league]
            input_value.validate()
            if input_value.league_code != league:
                raise Top5RuntimeError(
                    "runtime input key differs from canonical league identity"
                )
            for fixture in input_value.fixtures:
                if fixture.fixture_key in all_fixture_keys:
                    raise Top5RuntimeError(
                        "fixture identity is duplicated across leagues"
                    )
                all_fixture_keys.add(fixture.fixture_key)

    @property
    def request_digest(self) -> str:
        self.validate()
        return _digest(
            {
                "contract_version": TOP5_RUNTIME_CONTRACT_VERSION,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "requested_at": _utc(self.requested_at, "requested_at").isoformat(),
                "inputs": {
                    league: self.inputs[league].as_payload()
                    for league in TOP5_RUNTIME_LEAGUES
                },
                "config": self.config.as_payload(),
                "offline_only": True,
                "no_bet": True,
                "publication_enabled": False,
                "activation_enabled": False,
            }
        )


@dataclass(frozen=True)
class Top5RuntimeHealthSnapshot:
    status: Top5RuntimeStatus
    run_id: str
    session_id: str
    observed_at: datetime
    league_status: Mapping[str, str]
    latest_stage: Top5RuntimeStage
    latest_failure: str | None
    activation_state: str = "disabled"
    provider_authority: str = TOP5_PRODUCTION_PROVIDER
    evidence_reference: str | None = None
    fixture_count: int = 0
    provider_request_count: int = 0
    network_request_count: int = 0
    retry_count: int = 0
    duplicate_suppression_count: int = 0
    stale: bool = False
    no_bet: bool = True
    publication_enabled: bool = False
    scheduler_enabled: bool = False

    def validate(self) -> None:
        if not isinstance(self.status, Top5RuntimeStatus):
            raise Top5RuntimeError("runtime health status is invalid")
        _text("run_id", self.run_id)
        _text("session_id", self.session_id)
        _utc(self.observed_at, "observed_at")
        if set(self.league_status) != set(TOP5_RUNTIME_LEAGUES):
            raise Top5RuntimeError("runtime health must report all five leagues")
        if self.provider_authority != TOP5_PRODUCTION_PROVIDER:
            raise Top5RuntimeError("runtime health provider authority is invalid")
        if (
            self.activation_state != "disabled"
            or self.publication_enabled
            or self.scheduler_enabled
            or not self.no_bet
        ):
            raise Top5RuntimeError("runtime health violates disabled/no-bet safety")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.fixture_count,
                self.provider_request_count,
                self.network_request_count,
                self.retry_count,
                self.duplicate_suppression_count,
            )
        ):
            raise Top5RuntimeError(
                "runtime health counts must be non-negative integers"
            )
        _bool("stale", self.stale)

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": TOP5_RUNTIME_CONTRACT_VERSION,
            "status": self.status.value,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "observed_at": _utc(self.observed_at, "observed_at").isoformat(),
            "league_status": {
                key: self.league_status[key] for key in TOP5_RUNTIME_LEAGUES
            },
            "latest_stage": self.latest_stage.value,
            "latest_failure": self.latest_failure,
            "activation_state": "disabled",
            "provider_authority": TOP5_PRODUCTION_PROVIDER,
            "evidence_reference": self.evidence_reference,
            "fixture_count": self.fixture_count,
            "provider_request_count": self.provider_request_count,
            "network_request_count": self.network_request_count,
            "retry_count": self.retry_count,
            "duplicate_suppression_count": self.duplicate_suppression_count,
            "stale": self.stale,
            "no_bet": True,
            "publication_enabled": False,
            "scheduler_enabled": False,
        }


@dataclass(frozen=True)
class Top5RuntimeExecutionReport:
    run_id: str
    session_id: str
    request_digest: str
    status: Top5RuntimeStatus
    completed_stages: tuple[Top5RuntimeStage, ...]
    league_status: Mapping[str, str]
    qualification_output_count: int
    prediction_count: int
    signal_count: int
    settlement_count: int
    provider_request_count: int
    network_request_count: int
    retry_count: int
    duplicate_suppression_count: int
    no_bet: bool
    publication_enabled: bool
    activation_enabled: bool
    committed: bool
    health: Top5RuntimeHealthSnapshot
    errors: tuple[str, ...] = ()
    report_digest: str = ""

    def __post_init__(self) -> None:
        if not self.report_digest:
            object.__setattr__(self, "report_digest", self.computed_digest)

    @property
    def computed_digest(self) -> str:
        return _digest(self._payload(include_digest=False))

    def _payload(self, *, include_digest: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": TOP5_RUNTIME_CONTRACT_VERSION,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "request_digest": self.request_digest,
            "status": self.status.value,
            "completed_stages": [stage.value for stage in self.completed_stages],
            "league_status": {
                key: self.league_status[key] for key in TOP5_RUNTIME_LEAGUES
            },
            "qualification_output_count": self.qualification_output_count,
            "prediction_count": self.prediction_count,
            "signal_count": self.signal_count,
            "settlement_count": self.settlement_count,
            "provider_request_count": self.provider_request_count,
            "network_request_count": self.network_request_count,
            "retry_count": self.retry_count,
            "duplicate_suppression_count": self.duplicate_suppression_count,
            "no_bet": self.no_bet,
            "publication_enabled": self.publication_enabled,
            "activation_enabled": self.activation_enabled,
            "committed": self.committed,
            "health": self.health.as_payload(),
            "errors": list(self.errors),
        }
        if include_digest:
            payload["report_digest"] = self.report_digest
        return payload

    def validate(self) -> None:
        _hash("request_digest", self.request_digest)
        _text("run_id", self.run_id)
        _text("session_id", self.session_id)
        if set(self.league_status) != set(TOP5_RUNTIME_LEAGUES):
            raise Top5RuntimeError("execution report must contain all five leagues")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.qualification_output_count,
                self.prediction_count,
                self.signal_count,
                self.settlement_count,
                self.provider_request_count,
                self.network_request_count,
                self.retry_count,
                self.duplicate_suppression_count,
            )
        ):
            raise Top5RuntimeError(
                "execution report counts must be non-negative integers"
            )
        if not self.no_bet or self.publication_enabled or self.activation_enabled:
            raise Top5RuntimeError("execution report violates no-bet/disabled safety")
        self.health.validate()
        if self.report_digest.lower() != self.computed_digest.lower():
            raise Top5RuntimeError("execution report digest mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return self._payload(include_digest=True)


@dataclass(frozen=True)
class Top5RuntimeCheckpoint:
    """Serializable restart marker for an operator-owned external store."""

    run_id: str
    session_id: str
    request_digest: str
    report: Top5RuntimeExecutionReport
    checkpoint_digest: str = ""

    def __post_init__(self) -> None:
        if not self.checkpoint_digest:
            object.__setattr__(self, "checkpoint_digest", self.computed_digest)

    @property
    def computed_digest(self) -> str:
        return _digest(
            {
                "run_id": self.run_id,
                "session_id": self.session_id,
                "request_digest": self.request_digest,
                "report_digest": self.report.report_digest,
            }
        )

    def validate(self) -> None:
        _text("checkpoint run_id", self.run_id)
        _text("checkpoint session_id", self.session_id)
        _hash("checkpoint request_digest", self.request_digest)
        self.report.validate()
        if (
            self.report.run_id != self.run_id
            or self.report.session_id != self.session_id
            or self.report.request_digest != self.request_digest
        ):
            raise Top5RuntimeError("checkpoint/report identity mismatch")
        if self.checkpoint_digest.lower() != self.computed_digest.lower():
            raise Top5RuntimeError("runtime checkpoint digest mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": TOP5_RUNTIME_CONTRACT_VERSION,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "request_digest": self.request_digest,
            "report": self.report.as_payload(),
            "checkpoint_digest": self.checkpoint_digest,
        }


class Top5RuntimeCheckpointStore(Protocol):
    """External persistence boundary; no implementation writes in this module."""

    def load(self, run_id: str) -> Top5RuntimeCheckpoint | None: ...

    def commit(self, checkpoint: Top5RuntimeCheckpoint) -> None: ...


class InMemoryTop5RuntimeCheckpointStore:
    """Test double for restart/idempotency; never a production state writer."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, Top5RuntimeCheckpoint] = {}

    def load(self, run_id: str) -> Top5RuntimeCheckpoint | None:
        return self._checkpoints.get(run_id)

    def commit(self, checkpoint: Top5RuntimeCheckpoint) -> None:
        checkpoint.validate()
        previous = self._checkpoints.get(checkpoint.run_id)
        if (
            previous is not None
            and previous.checkpoint_digest != checkpoint.checkpoint_digest
        ):
            raise Top5RuntimeError("conflicting runtime checkpoint identity")
        self._checkpoints[checkpoint.run_id] = checkpoint


def _failure_report(
    request: Top5RuntimeRequest, error: str
) -> Top5RuntimeExecutionReport:
    health = Top5RuntimeHealthSnapshot(
        status=Top5RuntimeStatus.FAILED,
        run_id=request.run_id,
        session_id=request.session_id,
        observed_at=request.requested_at,
        league_status={league: "FAILED_CLOSED" for league in TOP5_RUNTIME_LEAGUES},
        latest_stage=Top5RuntimeStage.FAILED_CLOSED,
        latest_failure=error,
    )
    report = Top5RuntimeExecutionReport(
        run_id=request.run_id,
        session_id=request.session_id,
        request_digest=request.request_digest,
        status=Top5RuntimeStatus.FAILED,
        completed_stages=(Top5RuntimeStage.FAILED_CLOSED,),
        league_status=health.league_status,
        qualification_output_count=0,
        prediction_count=0,
        signal_count=0,
        settlement_count=0,
        provider_request_count=0,
        network_request_count=0,
        retry_count=0,
        duplicate_suppression_count=0,
        no_bet=True,
        publication_enabled=False,
        activation_enabled=False,
        committed=False,
        health=health,
        errors=(error,),
    )
    report.validate()
    return report


def run_offline_top5_runtime(
    request: Top5RuntimeRequest,
    *,
    checkpoint_store: Top5RuntimeCheckpointStore | None = None,
) -> Top5RuntimeExecutionReport:
    """Run the complete injected five-league graph without side effects.

    Input validation happens before any per-league pipeline runs.  A failure
    therefore returns a failed-closed report with zero committed outputs; no
    partial league result can become a downstream authority input.
    """

    request.validate()
    request_digest = request.request_digest
    if checkpoint_store is not None:
        existing = checkpoint_store.load(request.run_id)
        if existing is not None:
            existing.validate()
            if existing.request_digest != request_digest:
                raise Top5RuntimeError("replay request does not match its checkpoint")
            return existing.report

    results: dict[str, OfflineCompatibilityResult] = {}
    try:
        for league in TOP5_RUNTIME_LEAGUES:
            input_value = request.inputs[league]
            result = run_offline_compatibility(
                input_value.adapter,
                input_value.fixtures,
                input_value.signal_snapshots,
                signal_time=input_value.signal_time,
                now=request.requested_at,
            )
            if (
                result.pipeline.health.status != "ok"
                or not result.pipeline.predictions
                or not result.pipeline.signals
            ):
                raise Top5RuntimeError(
                    f"{league} did not produce a complete signal-time output"
                )
            if not result.no_bet or result.closing_odds_used:
                raise Top5RuntimeError(
                    f"{league} violated no-bet or closing-odds isolation"
                )
            results[league] = result
    except (ProductionContractError, TypeError, ValueError) as exc:
        return _failure_report(request, str(exc))

    completed_stages: list[Top5RuntimeStage] = [
        Top5RuntimeStage.FIXTURES_DISCOVERED,
        Top5RuntimeStage.ODDS_REFRESHED,
        Top5RuntimeStage.PREDICTIONS_GENERATED,
        Top5RuntimeStage.SIGNALS_READY,
    ]
    all_results = all(request.inputs[league].results for league in TOP5_RUNTIME_LEAGUES)
    settlement_count = 0
    if all_results:
        completed_stages.extend(
            (Top5RuntimeStage.RESULTS_INGESTED, Top5RuntimeStage.SETTLED)
        )
        settlement_count = sum(
            len(request.inputs[league].results) for league in TOP5_RUNTIME_LEAGUES
        )
    status = Top5RuntimeStatus.READY if all_results else Top5RuntimeStatus.DEGRADED
    league_status = {
        league: "SETTLED" if all_results else "SIGNALS_READY"
        for league in TOP5_RUNTIME_LEAGUES
    }
    health = Top5RuntimeHealthSnapshot(
        status=status,
        run_id=request.run_id,
        session_id=request.session_id,
        observed_at=request.requested_at,
        league_status=league_status,
        latest_stage=completed_stages[-1],
        latest_failure=None if all_results else "result bindings are not present",
        fixture_count=sum(
            len(request.inputs[league].fixtures) for league in TOP5_RUNTIME_LEAGUES
        ),
        provider_request_count=sum(
            len(results[league].pipeline.snapshots) > 0
            for league in TOP5_RUNTIME_LEAGUES
        ),
    )
    report = Top5RuntimeExecutionReport(
        run_id=request.run_id,
        session_id=request.session_id,
        request_digest=request_digest,
        status=status,
        completed_stages=tuple(completed_stages),
        league_status=league_status,
        qualification_output_count=0,
        prediction_count=sum(
            len(results[league].pipeline.predictions) for league in TOP5_RUNTIME_LEAGUES
        ),
        signal_count=sum(
            len(results[league].pipeline.signals) for league in TOP5_RUNTIME_LEAGUES
        ),
        settlement_count=settlement_count,
        provider_request_count=health.provider_request_count,
        network_request_count=0,
        retry_count=0,
        duplicate_suppression_count=0,
        no_bet=True,
        publication_enabled=False,
        activation_enabled=False,
        committed=True,
        health=health,
    )
    report.validate()
    if checkpoint_store is not None:
        checkpoint_store.commit(
            Top5RuntimeCheckpoint(
                run_id=request.run_id,
                session_id=request.session_id,
                request_digest=request_digest,
                report=report,
            )
        )
    return report


@dataclass(frozen=True)
class Top5ActivationPrecheckInput:
    """Operator-supplied evidence for the side-effect-free activation precheck."""

    evidence_reference: str | None = None
    five_league_evidence_valid: bool = False
    builder1_acceptance_passed: bool = False
    provider_authority: str = TOP5_PRODUCTION_PROVIDER
    provider_authority_granted: bool = False
    model_bound: bool = False
    research_bound: bool = False
    signal_time_approved: bool = False
    one_shot_operator_path_ready: bool = False
    recurring_scheduler_registered: bool = False
    scheduler_ready: bool = False
    health_ready: bool = False
    rollback_ready: bool = False
    activation_authorized: bool = False
    publication_preflight_ready: bool = False
    no_synthetic_evidence: bool = False
    league_scope: tuple[str, ...] = TOP5_RUNTIME_LEAGUES
    runtime_config: Top5RuntimeConfig = field(default_factory=Top5RuntimeConfig)

    def validate(self) -> None:
        self.runtime_config.validate()
        if self.provider_authority in CANDIDATE_ONLY_PROVIDER_IDENTITIES:
            raise Top5RuntimeError("candidate provider cannot pass activation precheck")
        if self.provider_authority != TOP5_PRODUCTION_PROVIDER:
            raise Top5RuntimeError(
                "activation precheck requires the_odds_api authority"
            )
        if self.league_scope != TOP5_RUNTIME_LEAGUES:
            raise Top5RuntimeError(
                "activation precheck requires the canonical five-league order"
            )
        if self.recurring_scheduler_registered is not False:
            raise Top5RuntimeError(
                "controlled activation must not register a recurring scheduler"
            )
        for name, value in self.__dict__.items():
            if name.endswith(
                (
                    "valid",
                    "passed",
                    "granted",
                    "bound",
                    "approved",
                    "ready",
                    "authorized",
                    "registered",
                )
            ):
                _bool(name, value)
        if self.evidence_reference is not None:
            _text("evidence_reference", self.evidence_reference)


@dataclass(frozen=True)
class Top5ActivationPrecheckReport:
    status: str
    runtime_status: Top5RuntimeStatus
    failures: tuple[str, ...]
    warnings: tuple[str, ...]
    activation_mode: str = "disabled"
    provider_authority: str = TOP5_PRODUCTION_PROVIDER
    league_scope: tuple[str, ...] = TOP5_RUNTIME_LEAGUES
    mutation_performed: bool = False
    one_shot_operator_path_ready: bool = False
    recurring_scheduler_registered: bool = False

    @property
    def ready(self) -> bool:
        return self.status == "TOP5_RUNTIME_ACTIVATION_READY"

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": TOP5_RUNTIME_CONTRACT_VERSION,
            "status": self.status,
            "ready": self.ready,
            "runtime_status": self.runtime_status.value,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "activation_mode": "disabled",
            "provider_authority": self.provider_authority,
            "league_scope": list(self.league_scope),
            "mutation_performed": False,
            "one_shot_operator_path_ready": self.one_shot_operator_path_ready,
            "recurring_scheduler_registered": self.recurring_scheduler_registered,
            "recurring_scheduler_readiness_required": False,
        }


def top5_activation_precheck(
    values: Top5ActivationPrecheckInput,
) -> Top5ActivationPrecheckReport:
    """Evaluate readiness without enabling, registering, publishing, or writing."""

    try:
        values.validate()
    except Top5RuntimeError as exc:
        return Top5ActivationPrecheckReport(
            "TOP5_RUNTIME_ACTIVATION_BLOCKED",
            Top5RuntimeStatus.BLOCKED_BY_AUTHORITY,
            (str(exc),),
            (),
            provider_authority=values.provider_authority,
            league_scope=values.league_scope,
            one_shot_operator_path_ready=values.one_shot_operator_path_ready,
            recurring_scheduler_registered=values.recurring_scheduler_registered,
        )

    checks = (
        ("five-league evidence is not valid", values.five_league_evidence_valid),
        ("Builder 1 final acceptance is not passed", values.builder1_acceptance_passed),
        ("evidence reference is missing", bool(values.evidence_reference)),
        (
            "provider authority is not explicitly granted",
            values.provider_authority_granted,
        ),
        ("model binding is missing", values.model_bound),
        ("Research binding is missing", values.research_bound),
        ("Signal-Time approval is missing", values.signal_time_approved),
        (
            "one-shot operator execution path is not ready",
            values.one_shot_operator_path_ready,
        ),
        ("health/readiness infrastructure is not ready", values.health_ready),
        ("rollback readiness is missing", values.rollback_ready),
        ("explicit activation authorization is missing", values.activation_authorized),
        ("evidence is synthetic or test-only", values.no_synthetic_evidence),
    )
    failures = tuple(message for message, passed in checks if not passed)
    warnings = (
        ()
        if values.publication_preflight_ready
        else (
            "separate publication preflight remains required; activation does not authorize publication",
        )
    )
    status = (
        "TOP5_RUNTIME_ACTIVATION_READY"
        if not failures
        else "TOP5_RUNTIME_ACTIVATION_BLOCKED"
    )
    runtime_status = (
        Top5RuntimeStatus.READY
        if not failures
        else Top5RuntimeStatus.BLOCKED_BY_EVIDENCE
    )
    return Top5ActivationPrecheckReport(
        status,
        runtime_status,
        failures,
        warnings,
        one_shot_operator_path_ready=values.one_shot_operator_path_ready,
        recurring_scheduler_registered=values.recurring_scheduler_registered,
    )


__all__ = [
    "TOP5_PRODUCTION_PROVIDER",
    "TOP5_RUNTIME_CONTRACT_VERSION",
    "TOP5_RUNTIME_LEAGUES",
    "InMemoryTop5RuntimeCheckpointStore",
    "Top5ActivationPrecheckInput",
    "Top5ActivationPrecheckReport",
    "Top5LeagueRuntimeInput",
    "Top5ResultBinding",
    "Top5RuntimeCheckpoint",
    "Top5RuntimeCheckpointStore",
    "Top5RuntimeConfig",
    "Top5RuntimeError",
    "Top5RuntimeExecutionReport",
    "Top5RuntimeHealthSnapshot",
    "Top5RuntimeRequest",
    "Top5RuntimeStage",
    "Top5RuntimeStatus",
    "run_offline_top5_runtime",
    "top5_activation_precheck",
]
