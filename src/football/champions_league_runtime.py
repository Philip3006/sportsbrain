"""Disabled-by-default Champions League runtime operations contract.

This module is the CL-specific boundary around the generic Football contracts.
It validates an injected, offline run bundle and records a deterministic
lifecycle trace.  It deliberately does not import a provider client, a model,
the scheduler, the ledger, or a publisher.  A future operator can connect
those dependencies only behind an explicit production review; the default
configuration cannot execute network work or mutate production state.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from threading import RLock
from typing import Protocol

from src.football.production_contracts import (
    ActivationMode,
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    PredictionArtifact,
    ProductionContractError,
    ShadowSignalArtifact,
    _utc,
)

CHAMPIONS_LEAGUE_CODE = "UCL"
CHAMPIONS_LEAGUE_SPORT_KEY = "soccer_uefa_champs_league"
CHAMPIONS_LEAGUE_PROVIDER = "the_odds_api"
CHAMPIONS_LEAGUE_RESULT_SOURCE = "results_router:ucl"


class ChampionsLeagueRuntimeError(ProductionContractError):
    """Raised when an injected CL runtime bundle violates a safety boundary."""


class ChampionsLeaguePhase(str, Enum):
    FIXTURES_DISCOVERED = "fixtures_discovered"
    PREMATCH_SCANNED = "prematch_scanned"
    ODDS_REFRESHED = "odds_refreshed"
    PREDICTION_DISPATCHED = "prediction_dispatched"
    SIGNAL_EMITTED = "signal_emitted"
    RESULT_INGESTED = "result_ingested"
    SETTLED = "settled"
    ROLLED_BACK = "rolled_back"


_LIFECYCLE_ORDER = (
    ChampionsLeaguePhase.FIXTURES_DISCOVERED,
    ChampionsLeaguePhase.PREMATCH_SCANNED,
    ChampionsLeaguePhase.ODDS_REFRESHED,
    ChampionsLeaguePhase.PREDICTION_DISPATCHED,
    ChampionsLeaguePhase.SIGNAL_EMITTED,
    ChampionsLeaguePhase.RESULT_INGESTED,
    ChampionsLeaguePhase.SETTLED,
)


@dataclass(frozen=True)
class ChampionsLeagueRuntimeConfig:
    """CL metadata and safety flags; this is not a production registration."""

    provider_name: str = CHAMPIONS_LEAGUE_PROVIDER
    sport_key: str = CHAMPIONS_LEAGUE_SPORT_KEY
    result_source: str = CHAMPIONS_LEAGUE_RESULT_SOURCE
    model_adapter_id: str = "unbound"
    activation_mode: ActivationMode = ActivationMode.DISABLED
    network_enabled: bool = False
    scheduler_enabled: bool = False
    publication_enabled: bool = False
    betting_enabled: bool = False
    ledger_enabled: bool = False
    retry_attempts: int = 1
    timeout_seconds: float = 5.0
    max_signal_odds_age_seconds: int = 900

    def validate(self) -> None:
        if self.provider_name != CHAMPIONS_LEAGUE_PROVIDER:
            raise ChampionsLeagueRuntimeError(
                "Champions League production authority must remain the_odds_api"
            )
        if self.sport_key != CHAMPIONS_LEAGUE_SPORT_KEY:
            raise ChampionsLeagueRuntimeError("unexpected Champions League sport key")
        if self.result_source != CHAMPIONS_LEAGUE_RESULT_SOURCE:
            raise ChampionsLeagueRuntimeError(
                "unexpected Champions League result source"
            )
        if self.model_adapter_id != "unbound":
            raise ChampionsLeagueRuntimeError(
                "production model binding is not configured"
            )
        if ActivationMode(self.activation_mode) is not ActivationMode.DISABLED:
            raise ChampionsLeagueRuntimeError(
                "Champions League runtime must remain disabled"
            )
        if any(
            (
                self.network_enabled,
                self.scheduler_enabled,
                self.publication_enabled,
                self.betting_enabled,
                self.ledger_enabled,
            )
        ):
            raise ChampionsLeagueRuntimeError(
                "Champions League production side effects are disabled"
            )
        if self.retry_attempts != 1:
            raise ChampionsLeagueRuntimeError(
                "CL runtime does not permit automatic retries"
            )
        if self.timeout_seconds <= 0:
            raise ChampionsLeagueRuntimeError("CL runtime timeout must be positive")
        if self.max_signal_odds_age_seconds <= 0:
            raise ChampionsLeagueRuntimeError(
                "CL odds freshness window must be positive"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider": self.provider_name,
            "sport_key": self.sport_key,
            "result_source": self.result_source,
            "model_adapter_id": self.model_adapter_id,
            "activation_mode": self.activation_mode.value,
            "network_enabled": self.network_enabled,
            "scheduler_enabled": self.scheduler_enabled,
            "publication_enabled": self.publication_enabled,
            "betting_enabled": self.betting_enabled,
            "ledger_enabled": self.ledger_enabled,
            "retry_attempts": self.retry_attempts,
            "timeout_seconds": self.timeout_seconds,
            "max_signal_odds_age_seconds": self.max_signal_odds_age_seconds,
        }


@dataclass(frozen=True)
class ChampionsLeagueSafety:
    """Immutable per-run safety declaration."""

    no_bet: bool = True
    publication: bool = False
    activation: bool = False
    monetary_spend: bool = False

    def validate(self) -> None:
        if not self.no_bet:
            raise ChampionsLeagueRuntimeError("CL run must remain no-bet")
        if self.publication or self.activation or self.monetary_spend:
            raise ChampionsLeagueRuntimeError(
                "CL run contains a forbidden production side effect"
            )


@dataclass(frozen=True)
class ChampionsLeagueResult:
    """Non-financial result observation used by the settlement boundary."""

    result_id: str
    fixture_key: str
    home_score: int
    away_score: int
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "observed_at", _utc(self.observed_at, "result observed_at")
        )

    def validate(self) -> None:
        if any(
            not value.strip()
            for value in (self.result_id, self.fixture_key, self.source)
        ):
            raise ChampionsLeagueRuntimeError("result provenance is incomplete")
        if (
            isinstance(self.home_score, bool)
            or isinstance(self.away_score, bool)
            or self.home_score < 0
            or self.away_score < 0
        ):
            raise ChampionsLeagueRuntimeError(
                "result scores must be non-negative integers"
            )
        _utc(self.observed_at, "result observed_at")


@dataclass(frozen=True)
class ChampionsLeagueSettlement:
    """Non-financial settlement record; no ledger write is available here."""

    fixture_key: str
    result_id: str
    status: str
    settled_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "settled_at", _utc(self.settled_at, "settled_at"))

    def validate(self) -> None:
        if any(not value.strip() for value in (self.fixture_key, self.result_id)):
            raise ChampionsLeagueRuntimeError("settlement provenance is incomplete")
        if self.status not in {"settled", "void"}:
            raise ChampionsLeagueRuntimeError("unknown non-financial settlement status")
        _utc(self.settled_at, "settled_at")


@dataclass(frozen=True)
class ChampionsLeagueRunBundle:
    """Complete injected CL run package for offline validation and replay."""

    run_id: str
    fixtures: tuple[Fixture, ...]
    snapshots: tuple[MarketSnapshot, ...]
    predictions: tuple[PredictionArtifact, ...]
    signals: tuple[ShadowSignalArtifact, ...]
    results: tuple[ChampionsLeagueResult, ...]
    settlements: tuple[ChampionsLeagueSettlement, ...]
    safety: ChampionsLeagueSafety = ChampionsLeagueSafety()

    def validate(self, config: ChampionsLeagueRuntimeConfig, *, now: datetime) -> None:
        config.validate()
        self.safety.validate()
        current = _utc(now, "run validation now")
        if not self.run_id.strip():
            raise ChampionsLeagueRuntimeError("run_id is required")
        if not self.fixtures:
            raise ChampionsLeagueRuntimeError("CL run requires at least one fixture")

        fixtures = {fixture.fixture_key: fixture for fixture in self.fixtures}
        if len(fixtures) != len(self.fixtures):
            raise ChampionsLeagueRuntimeError("duplicate CL fixture identity")
        for fixture in self.fixtures:
            fixture.validate()
            if fixture.league_code != CHAMPIONS_LEAGUE_CODE:
                raise ChampionsLeagueRuntimeError("foreign league entered CL runtime")

        snapshots = self._validate_snapshots(config, fixtures, current)
        self._validate_predictions(config, fixtures, snapshots)
        self._validate_signals(config, fixtures)
        self._validate_results(fixtures)
        self._validate_settlements(fixtures)

    def digest(self, config: ChampionsLeagueRuntimeConfig) -> str:
        """Return a stable digest for idempotency and restart/replay checks."""

        config.validate()
        return _digest(
            {
                "config": config.as_payload(),
                "run_id": self.run_id,
                "fixtures": _sorted_payload(
                    self.fixtures, key=lambda item: item.fixture_key
                ),
                "snapshots": _sorted_payload(
                    self.snapshots, key=lambda item: item.fixture_key
                ),
                "predictions": _sorted_payload(
                    self.predictions, key=lambda item: item.fixture_key
                ),
                "signals": _sorted_payload(
                    self.signals, key=lambda item: item.fixture_key
                ),
                "results": _sorted_payload(
                    self.results, key=lambda item: item.fixture_key
                ),
                "settlements": _sorted_payload(
                    self.settlements, key=lambda item: item.fixture_key
                ),
                "safety": _as_payload(self.safety),
            }
        )

    def _validate_snapshots(
        self,
        config: ChampionsLeagueRuntimeConfig,
        fixtures: Mapping[str, Fixture],
        now: datetime,
    ) -> dict[str, MarketSnapshot]:
        selected: dict[str, MarketSnapshot] = {}
        for snapshot in self.snapshots:
            snapshot.validate()
            if snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
                raise ChampionsLeagueRuntimeError(
                    "closing odds cannot enter CL prediction input"
                )
            if snapshot.source != config.provider_name:
                raise ChampionsLeagueRuntimeError(
                    "CL odds source differs from production authority"
                )
            fixture = fixtures.get(snapshot.fixture_key)
            if fixture is None:
                raise ChampionsLeagueRuntimeError(
                    "odds snapshot references an unknown CL fixture"
                )
            if not snapshot.snapshot_id.strip():
                raise ChampionsLeagueRuntimeError(
                    "CL odds snapshot requires durable identity"
                )
            age = (now - snapshot.captured_at).total_seconds()
            if age < 0 or age > config.max_signal_odds_age_seconds:
                raise ChampionsLeagueRuntimeError(
                    "CL odds snapshot is stale or future-dated"
                )
            if snapshot.fixture_key in selected:
                raise ChampionsLeagueRuntimeError("duplicate CL signal-time snapshot")
            selected[snapshot.fixture_key] = snapshot
        if set(selected) != set(fixtures):
            raise ChampionsLeagueRuntimeError(
                "CL run must have one signal-time snapshot per fixture"
            )
        return selected

    def _validate_predictions(
        self,
        config: ChampionsLeagueRuntimeConfig,
        fixtures: Mapping[str, Fixture],
        snapshots: Mapping[str, MarketSnapshot],
    ) -> None:
        seen: set[str] = set()
        for prediction in self.predictions:
            prediction.validate()
            if prediction.league_code != CHAMPIONS_LEAGUE_CODE:
                raise ChampionsLeagueRuntimeError("prediction has a foreign league")
            fixture = fixtures.get(prediction.fixture_key)
            snapshot = snapshots.get(prediction.fixture_key)
            if fixture is None or snapshot is None:
                raise ChampionsLeagueRuntimeError("prediction has unknown CL identity")
            if prediction.model_adapter_id != config.model_adapter_id:
                raise ChampionsLeagueRuntimeError(
                    "prediction model binding differs from CL config"
                )
            if prediction.snapshot_id != snapshot.snapshot_id:
                raise ChampionsLeagueRuntimeError(
                    "prediction snapshot binding differs from odds"
                )
            if prediction.fixture_key in seen:
                raise ChampionsLeagueRuntimeError("duplicate CL prediction")
            seen.add(prediction.fixture_key)
        if seen != set(fixtures):
            raise ChampionsLeagueRuntimeError(
                "CL run must have one prediction per fixture"
            )

    def _validate_signals(
        self,
        config: ChampionsLeagueRuntimeConfig,
        fixtures: Mapping[str, Fixture],
    ) -> None:
        seen: set[str] = set()
        for signal in self.signals:
            signal.validate()
            if signal.league_code != CHAMPIONS_LEAGUE_CODE:
                raise ChampionsLeagueRuntimeError("signal has a foreign league")
            if signal.fixture_key not in fixtures:
                raise ChampionsLeagueRuntimeError("signal has unknown CL fixture")
            if signal.source != config.provider_name:
                raise ChampionsLeagueRuntimeError(
                    "signal source differs from CL authority"
                )
            if signal.model_adapter_id != config.model_adapter_id:
                raise ChampionsLeagueRuntimeError(
                    "signal model binding differs from CL config"
                )
            if signal.fixture_key in seen:
                raise ChampionsLeagueRuntimeError("duplicate CL signal")
            seen.add(signal.fixture_key)
        if seen != set(fixtures):
            raise ChampionsLeagueRuntimeError(
                "CL run must have one shadow signal per fixture"
            )

    def _validate_results(self, fixtures: Mapping[str, Fixture]) -> None:
        seen: set[str] = set()
        for result in self.results:
            result.validate()
            if result.fixture_key not in fixtures:
                raise ChampionsLeagueRuntimeError("result has unknown CL fixture")
            if result.fixture_key in seen:
                raise ChampionsLeagueRuntimeError("duplicate CL result")
            seen.add(result.fixture_key)
        if seen != set(fixtures):
            raise ChampionsLeagueRuntimeError("CL run must have one result per fixture")

    def _validate_settlements(self, fixtures: Mapping[str, Fixture]) -> None:
        results = {result.result_id: result for result in self.results}
        seen: set[str] = set()
        for settlement in self.settlements:
            settlement.validate()
            result = results.get(settlement.result_id)
            if result is None or result.fixture_key != settlement.fixture_key:
                raise ChampionsLeagueRuntimeError("settlement/result identity mismatch")
            if settlement.fixture_key in seen:
                raise ChampionsLeagueRuntimeError("duplicate CL settlement")
            seen.add(settlement.fixture_key)
        if seen != set(fixtures):
            raise ChampionsLeagueRuntimeError(
                "CL run must have one settlement per fixture"
            )


@dataclass(frozen=True)
class ChampionsLeagueLifecycleEvent:
    run_id: str
    phase: ChampionsLeaguePhase
    digest: str
    recorded_at: datetime
    safety: ChampionsLeagueSafety = ChampionsLeagueSafety()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "recorded_at", _utc(self.recorded_at, "lifecycle recorded_at")
        )

    @property
    def key(self) -> str:
        return f"{self.run_id}:{self.phase.value}"


class ChampionsLeagueLifecycleStore(Protocol):
    def get(self, key: str) -> ChampionsLeagueLifecycleEvent | None: ...

    def put_if_absent(
        self, event: ChampionsLeagueLifecycleEvent
    ) -> ChampionsLeagueLifecycleEvent: ...


class InMemoryChampionsLeagueLifecycleStore:
    """Deterministic test/replay store; production state remains external."""

    def __init__(self) -> None:
        self._events: dict[str, ChampionsLeagueLifecycleEvent] = {}
        self._lock = RLock()

    def get(self, key: str) -> ChampionsLeagueLifecycleEvent | None:
        with self._lock:
            return self._events.get(key)

    def put_if_absent(
        self, event: ChampionsLeagueLifecycleEvent
    ) -> ChampionsLeagueLifecycleEvent:
        with self._lock:
            existing = self._events.get(event.key)
            if existing is not None:
                if existing.digest != event.digest or existing.phase is not event.phase:
                    raise ChampionsLeagueRuntimeError("conflicting CL lifecycle replay")
                return existing
            self._events[event.key] = event
            return event

    def events_for_run(self, run_id: str) -> tuple[ChampionsLeagueLifecycleEvent, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        event
                        for event in self._events.values()
                        if event.run_id == run_id
                    ),
                    key=lambda event: event.recorded_at,
                )
            )


@dataclass(frozen=True)
class ChampionsLeagueHealth:
    fixture_discovery: bool = False
    prematch_scan: bool = False
    odds_refresh: bool = False
    prediction_dispatch: bool = False
    signal_lifecycle: bool = False
    result_ingestion: bool = False
    settlement: bool = False
    idempotency: bool = False
    scheduler_contract: bool = False
    rollback: bool = False

    def missing(self) -> tuple[str, ...]:
        return tuple(name for name, value in self.__dict__.items() if not value)


@dataclass(frozen=True)
class ChampionsLeagueReadiness:
    offline_ready: bool
    activation_allowed: bool
    scheduler_enabled: bool
    provider_authority: str
    missing_components: tuple[str, ...]


@dataclass(frozen=True)
class ChampionsLeagueRunReport:
    run_id: str
    bundle_digest: str
    phases: tuple[ChampionsLeaguePhase, ...]
    fixture_count: int
    result_count: int
    settlement_count: int
    activation_allowed: bool = False
    publication_allowed: bool = False
    betting_allowed: bool = False


class ChampionsLeagueRuntime:
    """Offline lifecycle coordinator for the disabled CL runtime boundary."""

    def __init__(
        self,
        config: ChampionsLeagueRuntimeConfig | None = None,
        store: ChampionsLeagueLifecycleStore | None = None,
    ) -> None:
        self.config = config or ChampionsLeagueRuntimeConfig()
        self.store = store or InMemoryChampionsLeagueLifecycleStore()
        self.config.validate()

    def readiness(self, health: ChampionsLeagueHealth) -> ChampionsLeagueReadiness:
        self.config.validate()
        missing = health.missing()
        return ChampionsLeagueReadiness(
            offline_ready=not missing,
            activation_allowed=False,
            scheduler_enabled=False,
            provider_authority=self.config.provider_name,
            missing_components=missing,
        )

    def run_offline(
        self,
        bundle: ChampionsLeagueRunBundle,
        *,
        now: datetime,
    ) -> ChampionsLeagueRunReport:
        """Validate and record the full lifecycle without network or side effects."""

        self.config.validate()
        bundle.validate(self.config, now=now)
        digest = bundle.digest(self.config)
        phases = _LIFECYCLE_ORDER
        for phase in phases:
            self.store.put_if_absent(
                ChampionsLeagueLifecycleEvent(
                    run_id=bundle.run_id,
                    phase=phase,
                    digest=digest,
                    recorded_at=now,
                    safety=bundle.safety,
                )
            )
        return ChampionsLeagueRunReport(
            run_id=bundle.run_id,
            bundle_digest=digest,
            phases=phases,
            fixture_count=len(bundle.fixtures),
            result_count=len(bundle.results),
            settlement_count=len(bundle.settlements),
        )

    def rollback(
        self, run_id: str, *, now: datetime, reason: str
    ) -> ChampionsLeagueLifecycleEvent:
        """Record a non-financial rollback marker; no external mutation occurs."""

        self.config.validate()
        if not run_id.strip() or not reason.strip():
            raise ChampionsLeagueRuntimeError(
                "rollback requires run identity and reason"
            )
        return self.store.put_if_absent(
            ChampionsLeagueLifecycleEvent(
                run_id=run_id,
                phase=ChampionsLeaguePhase.ROLLED_BACK,
                digest=_digest({"run_id": run_id, "reason": reason}),
                recorded_at=now,
            )
        )

    def recover(self, run_id: str) -> tuple[ChampionsLeagueLifecycleEvent, ...]:
        """Read the durable lifecycle trace needed after a process restart."""

        self.config.validate()
        if not run_id.strip():
            raise ChampionsLeagueRuntimeError("recovery requires a run identity")
        events_for_run = getattr(self.store, "events_for_run", None)
        if events_for_run is None:
            raise ChampionsLeagueRuntimeError(
                "configured lifecycle store does not support recovery"
            )
        return tuple(events_for_run(run_id))

    def execute_network(self, *_args: object, **_kwargs: object) -> None:
        """Keep network execution unavailable until a separate release decision."""

        raise ChampionsLeagueRuntimeError(
            "Champions League network execution is disabled; use the reviewed offline contract"
        )


def _as_payload(value: object) -> object:
    if isinstance(value, datetime):
        return _utc(value, "payload timestamp").isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _as_payload(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_as_payload(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _as_payload(getattr(value, name))
            for name in value.__dataclass_fields__
        }
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        _as_payload(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _sorted_payload(
    values: Sequence[object], *, key: Callable[[object], str]
) -> list[object]:
    """Serialize repeated identities in stable order for replay/idempotency."""

    return [_as_payload(item) for item in sorted(values, key=key)]


__all__ = [
    "CHAMPIONS_LEAGUE_CODE",
    "CHAMPIONS_LEAGUE_PROVIDER",
    "CHAMPIONS_LEAGUE_RESULT_SOURCE",
    "CHAMPIONS_LEAGUE_SPORT_KEY",
    "ChampionsLeagueHealth",
    "ChampionsLeagueLifecycleEvent",
    "ChampionsLeagueLifecycleStore",
    "ChampionsLeaguePhase",
    "ChampionsLeagueReadiness",
    "ChampionsLeagueResult",
    "ChampionsLeagueRunBundle",
    "ChampionsLeagueRunReport",
    "ChampionsLeagueRuntime",
    "ChampionsLeagueRuntimeConfig",
    "ChampionsLeagueRuntimeError",
    "ChampionsLeagueSafety",
    "ChampionsLeagueSettlement",
    "InMemoryChampionsLeagueLifecycleStore",
]
