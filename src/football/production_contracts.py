"""Disabled-by-default contracts for future league-isolated football production.

This module contains data contracts and validation only.  It intentionally
does not import a model, call a provider, write an artifact, publish data, or
write the financial ledger.  A future rollout must provide those dependencies
through the protocols below and pass the explicit rollout gates.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Protocol, runtime_checkable


class ProductionContractError(ValueError):
    """Raised when a future league adapter violates a production boundary."""


class ActivationMode(str, Enum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    CONTROLLED = "controlled"
    LIVE = "live"


class MarketSnapshotKind(str, Enum):
    SIGNAL_TIME = "signal_time"
    CLOSING = "closing"


class ArtifactOwner(str, Enum):
    SHADOW_ARCHIVE = "shadow_archive"
    STAGED_PUBLIC = "staged_public"
    HEALTH = "health"


class RuntimeStateOwner(str, Enum):
    WEEKLY_BANKROLL = "weekly_bankroll"
    PROVIDER_BUDGET = "provider_budget"
    SIGNAL_ODDS = "signal_odds"
    TOP5_SHADOW = "top5_shadow"


class RolloutStage(str, Enum):
    RESEARCH_APPROVED = "research_approved"
    ADAPTER_READY = "adapter_ready"
    OFFLINE_COMPATIBLE = "offline_compatible"
    SHADOW_INFERENCE = "shadow_inference"
    SIGNAL_TIME_VALIDATED = "signal_time_validated"
    PROVIDER_VALIDATED = "provider_validated"
    SHADOW_PERFORMANCE = "shadow_performance"
    CEO_APPROVED = "ceo_approved"
    CONTROLLED_ACTIVATION = "controlled_activation"


@dataclass(frozen=True)
class SignalTimeContract:
    """An explicitly configured pre-kickoff window for one league adapter.

    There is deliberately no timing default and no implicit approval.  The
    exact values remain a CEO decision after schedule/provider/quota evidence.
    ``approval_ref`` is required only for controlled/live activation.
    """

    minimum_minutes_before_kickoff: int
    maximum_minutes_before_kickoff: int
    maximum_odds_age_seconds: int
    approval_ref: str | None = None

    def validate(self) -> None:
        if self.minimum_minutes_before_kickoff < 0:
            raise ProductionContractError("minimum lead time must be non-negative")
        if self.maximum_minutes_before_kickoff < self.minimum_minutes_before_kickoff:
            raise ProductionContractError("maximum lead time must be >= minimum lead time")
        if self.maximum_odds_age_seconds <= 0:
            raise ProductionContractError("maximum odds age must be positive")
        if self.approval_ref is not None and not self.approval_ref.strip():
            raise ProductionContractError("approval_ref must not be blank")

    def accepts(self, kickoff: datetime, odds_captured_at: datetime, now: datetime) -> bool:
        self.validate()
        kickoff = _utc(kickoff)
        odds_captured_at = _utc(odds_captured_at)
        now = _utc(now)
        lead_minutes = (kickoff - now).total_seconds() / 60
        odds_age = (now - odds_captured_at).total_seconds()
        return (
            self.minimum_minutes_before_kickoff <= lead_minutes <= self.maximum_minutes_before_kickoff
            and 0 <= odds_age <= self.maximum_odds_age_seconds
        )


@dataclass(frozen=True)
class ProviderMapping:
    """Provider identifiers without any provider client or network behavior."""

    provider_name: str
    competition_id: str
    sport_key: str
    fixture_endpoint: str = "bulk"
    result_source: str = ""
    markets: tuple[str, ...] = ("h2h",)
    regions: tuple[str, ...] = ("eu",)

    def validate(self, *, expected_sport_key: str | None = None) -> None:
        required = {
            "provider_name": self.provider_name,
            "competition_id": self.competition_id,
            "sport_key": self.sport_key,
            "fixture_endpoint": self.fixture_endpoint,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ProductionContractError(f"missing provider mapping fields: {', '.join(missing)}")
        if expected_sport_key is not None and self.sport_key != expected_sport_key:
            raise ProductionContractError("provider mapping sport_key differs from league config")
        if not self.markets or any(not market.strip() for market in self.markets):
            raise ProductionContractError("provider mapping requires non-empty markets")
        if not self.regions or any(not region.strip() for region in self.regions):
            raise ProductionContractError("provider mapping requires non-empty regions")


@dataclass(frozen=True)
class RuntimeStateBinding:
    """Ownership declaration for mutable state kept outside the checkout."""

    owner: RuntimeStateOwner
    relative_path: str
    external_required: bool = True

    def validate(self) -> None:
        path = self.relative_path.strip()
        if not self.external_required:
            raise ProductionContractError("Top-5 mutable state must require external runtime storage")
        if not path or path in (".", "..") or path.startswith(("/", "../")) or "/../" in path:
            raise ProductionContractError("runtime state path must be a safe relative path")
        forbidden = ("src/", "scripts/", "tests/", "docs/data/", "results/ledger")
        if path.startswith(forbidden):
            raise ProductionContractError("runtime state cannot be source, public, or ledger data")
        expected_prefix = {
            RuntimeStateOwner.WEEKLY_BANKROLL: "financial/",
            RuntimeStateOwner.PROVIDER_BUDGET: "data/cache/provider_budget.json",
            RuntimeStateOwner.SIGNAL_ODDS: "data/cache/odds_state.json",
            RuntimeStateOwner.TOP5_SHADOW: "football/top5/",
        }[RuntimeStateOwner(self.owner)]
        if expected_prefix.endswith(".json"):
            owned = path == expected_prefix
        else:
            owned = path.startswith(expected_prefix)
        if not owned:
            raise ProductionContractError(
                f"runtime state path is outside {self.owner.value} ownership"
            )


@dataclass(frozen=True)
class LeagueProductionConfig:
    """League-owned metadata without registering a live production league."""

    league_code: str
    display_name: str
    provider_sport_key: str
    fixture_source: str
    result_source: str
    model_adapter_id: str
    activation_mode: ActivationMode = ActivationMode.DISABLED
    signal_time: SignalTimeContract | None = None
    provider_mapping: ProviderMapping | None = None

    def validate(self) -> None:
        required = {
            "league_code": self.league_code,
            "display_name": self.display_name,
            "provider_sport_key": self.provider_sport_key,
            "fixture_source": self.fixture_source,
            "result_source": self.result_source,
            "model_adapter_id": self.model_adapter_id,
        }
        missing = [name for name, value in required.items() if not value or not value.strip()]
        if missing:
            raise ProductionContractError(f"missing required league fields: {', '.join(missing)}")
        if not self.provider_sport_key.startswith("soccer_"):
            raise ProductionContractError("provider_sport_key must be a soccer key")
        mode = ActivationMode(self.activation_mode)
        if mode is not ActivationMode.DISABLED and self.signal_time is None:
            raise ProductionContractError("an enabled adapter requires an approved signal-time contract")
        if mode in (ActivationMode.CONTROLLED, ActivationMode.LIVE):
            if self.signal_time is None or not self.signal_time.approval_ref:
                raise ProductionContractError("controlled/live activation requires signal-time approval")
        if self.signal_time is not None:
            self.signal_time.validate()
        if self.provider_mapping is not None:
            self.provider_mapping.validate(expected_sport_key=self.provider_sport_key)

    def assert_disabled(self) -> None:
        self.validate()
        if ActivationMode(self.activation_mode) is not ActivationMode.DISABLED:
            raise ProductionContractError("Top-5 production adapters must remain disabled")

    def assert_shadow_ready(self) -> None:
        self.validate()
        if ActivationMode(self.activation_mode) is not ActivationMode.SHADOW:
            raise ProductionContractError("shadow execution requires activation_mode=shadow")
        if self.signal_time is None or self.provider_mapping is None:
            raise ProductionContractError("shadow execution requires signal-time and provider mapping")


@dataclass(frozen=True)
class Fixture:
    fixture_key: str
    league_code: str
    home_team: str
    away_team: str
    kickoff: datetime

    def validate(self) -> None:
        if any(not value.strip() for value in (self.fixture_key, self.league_code, self.home_team, self.away_team)):
            raise ProductionContractError("fixture identity and teams are required")
        if self.home_team.strip() == self.away_team.strip():
            raise ProductionContractError("fixture teams must be distinct")


@dataclass(frozen=True)
class MarketSnapshot:
    fixture_key: str
    captured_at: datetime
    kind: MarketSnapshotKind
    source: str
    odds: Mapping[str, float]
    snapshot_id: str = ""

    def validate(self) -> None:
        if not self.fixture_key.strip() or not self.source.strip():
            raise ProductionContractError("market snapshot identity and source are required")
        if not self.odds:
            raise ProductionContractError("market snapshot must contain odds")
        for market, value in self.odds.items():
            if not market.strip() or not isfinite(float(value)) or float(value) <= 1.0:
                raise ProductionContractError(f"invalid odds for market {market!r}")


@dataclass(frozen=True)
class OddsRequest:
    """One coalescible bulk request for one league/sport and market set."""

    league_code: str
    provider_name: str
    sport_key: str
    fixture_keys: tuple[str, ...]
    markets: tuple[str, ...]
    regions: tuple[str, ...]
    requested_at: datetime

    @classmethod
    def for_config(
        cls,
        config: LeagueProductionConfig,
        fixtures: Sequence[Fixture],
        requested_at: datetime,
    ) -> OddsRequest:
        config.validate()
        mapping = config.provider_mapping
        if mapping is None:
            raise ProductionContractError("provider mapping is required to build an odds request")
        keys = tuple(sorted({fixture.fixture_key for fixture in fixtures}))
        if not keys:
            raise ProductionContractError("odds request requires at least one fixture")
        for fixture in fixtures:
            fixture.validate()
            if fixture.league_code != config.league_code:
                raise ProductionContractError("fixture league differs from request league")
        return cls(
            league_code=config.league_code,
            provider_name=mapping.provider_name,
            sport_key=mapping.sport_key,
            fixture_keys=keys,
            markets=tuple(sorted(set(mapping.markets))),
            regions=tuple(sorted(set(mapping.regions))),
            requested_at=_utc(requested_at),
        )

    @property
    def request_key(self) -> str:
        """Stable key for request coalescing and cache lookup."""
        return "|".join(
            (
                self.provider_name,
                self.sport_key,
                ",".join(self.fixture_keys),
                ",".join(self.markets),
                ",".join(self.regions),
            )
        )


@dataclass(frozen=True)
class PredictionInput:
    """Model-facing input. Closing snapshots are structurally excluded."""

    fixture: Fixture
    signal_snapshot: MarketSnapshot
    features: Mapping[str, float]

    @classmethod
    def create(
        cls,
        fixture: Fixture,
        signal_snapshot: MarketSnapshot,
        features: Mapping[str, float],
        signal_time: SignalTimeContract,
        now: datetime,
    ) -> PredictionInput:
        fixture.validate()
        signal_snapshot.validate()
        if signal_snapshot.fixture_key != fixture.fixture_key:
            raise ProductionContractError("fixture and market snapshot identity differ")
        if signal_snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise ProductionContractError("closing odds are not valid prediction input")
        if not signal_time.accepts(fixture.kickoff, signal_snapshot.captured_at, now):
            raise ProductionContractError("signal-time contract rejected this snapshot")
        for name, value in features.items():
            if not name.strip() or not isfinite(float(value)):
                raise ProductionContractError(f"invalid feature {name!r}")
        return cls(fixture=fixture, signal_snapshot=signal_snapshot, features=features)


@dataclass(frozen=True)
class PredictionArtifact:
    """Serializable prediction provenance with no betting or publishing side effect."""

    prediction_id: str
    fixture_key: str
    league_code: str
    model_adapter_id: str
    generated_at: datetime
    snapshot_id: str
    snapshot_kind: MarketSnapshotKind
    probabilities: Mapping[str, float]

    def validate(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.prediction_id,
                self.fixture_key,
                self.league_code,
                self.model_adapter_id,
                self.snapshot_id,
            )
        ):
            raise ProductionContractError("prediction artifact lacks provenance")
        if self.snapshot_kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise ProductionContractError("prediction artifact cannot use closing odds")
        if not self.probabilities:
            raise ProductionContractError("prediction artifact requires probabilities")
        for name, value in self.probabilities.items():
            if not name.strip() or not isfinite(float(value)) or not 0 <= float(value) <= 1:
                raise ProductionContractError(f"invalid probability for {name!r}")


@dataclass(frozen=True)
class ShadowSignalArtifact:
    """Shadow output; no ledger or publisher side effect is available here."""

    signal_id: str
    fixture_key: str
    league_code: str
    source: str
    model_adapter_id: str
    activation_mode: ActivationMode
    no_bet_flag: bool
    reason: str
    prediction_id: str | None = None
    snapshot_id: str | None = None
    snapshot_kind: MarketSnapshotKind = MarketSnapshotKind.SIGNAL_TIME

    def validate(self) -> None:
        if ActivationMode(self.activation_mode) is not ActivationMode.SHADOW:
            raise ProductionContractError("new Top-5 artifacts may only be shadow artifacts")
        missing = [
            name
            for name, value in {
                "signal_id": self.signal_id,
                "fixture_key": self.fixture_key,
                "league_code": self.league_code,
                "source": self.source,
                "model_adapter_id": self.model_adapter_id,
                "reason": self.reason,
            }.items()
            if not value or not value.strip()
        ]
        if missing:
            raise ProductionContractError(f"shadow artifact lacks provenance: {', '.join(missing)}")
        if not self.no_bet_flag:
            raise ProductionContractError("shadow artifacts must remain no-bet")
        if self.snapshot_kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise ProductionContractError("shadow signals cannot use closing odds")


@dataclass(frozen=True)
class RolloutEvidence:
    """Evidence ledger for the league-by-league rollout sequence."""

    research_approved: bool = False
    adapter_ready: bool = False
    offline_compatible: bool = False
    shadow_inference: bool = False
    signal_time_validated: bool = False
    provider_validated: bool = False
    shadow_performance: bool = False
    ceo_approved: bool = False

    def require(self, stage: RolloutStage) -> None:
        checks = {
            RolloutStage.RESEARCH_APPROVED: self.research_approved,
            RolloutStage.ADAPTER_READY: self.adapter_ready,
            RolloutStage.OFFLINE_COMPATIBLE: self.offline_compatible,
            RolloutStage.SHADOW_INFERENCE: self.shadow_inference,
            RolloutStage.SIGNAL_TIME_VALIDATED: self.signal_time_validated,
            RolloutStage.PROVIDER_VALIDATED: self.provider_validated,
            RolloutStage.SHADOW_PERFORMANCE: self.shadow_performance,
            RolloutStage.CEO_APPROVED: self.ceo_approved,
        }
        if stage is RolloutStage.CONTROLLED_ACTIVATION:
            required = all(checks.values())
        else:
            required = checks[stage]
        if not required:
            raise ProductionContractError(f"rollout evidence missing for stage {stage.value}")


@runtime_checkable
class FixtureIngestor(Protocol):
    def fetch(self, config: LeagueProductionConfig) -> Sequence[Fixture]: ...


@runtime_checkable
class OddsProvider(Protocol):
    name: str

    def fetch(self, request: OddsRequest) -> Sequence[MarketSnapshot]: ...


class FeatureAdapter(Protocol):
    def build(self, fixture: Fixture, snapshot: MarketSnapshot) -> Mapping[str, float]: ...


class ModelAdapter(Protocol):
    def predict(self, model_input: PredictionInput) -> Mapping[str, float]: ...


class SignalDecider(Protocol):
    def decide(
        self,
        fixture: Fixture,
        prediction: Mapping[str, float],
        snapshot: MarketSnapshot,
    ) -> ShadowSignalArtifact: ...


class ShadowArtifactSink(Protocol):
    def write(self, artifact: ShadowSignalArtifact) -> None: ...


def validate_disabled_top5_configs(configs: Sequence[LeagueProductionConfig]) -> None:
    """Guard used by a future registration point before any activation work."""
    seen_codes: set[str] = set()
    seen_sport_keys: set[str] = set()
    for config in configs:
        config.assert_disabled()
        if config.league_code in seen_codes:
            raise ProductionContractError(f"duplicate league_code: {config.league_code}")
        if config.provider_sport_key in seen_sport_keys:
            raise ProductionContractError(f"duplicate provider_sport_key: {config.provider_sport_key}")
        seen_codes.add(config.league_code)
        seen_sport_keys.add(config.provider_sport_key)


def validate_artifact_ownership(path: str, owner: ArtifactOwner | None = None) -> None:
    """Allow only explicit runtime artifact areas; reject source/ledger paths."""
    normalized = path.strip().lstrip("/")
    blocked_prefixes = (
        "src/",
        "scripts/",
        "tests/",
        ".github/",
        "cloudflare/",
        "results/ledger",
        "ledger/",
    )
    if not normalized or ".." in normalized.split("/") or normalized.startswith(blocked_prefixes):
        raise ProductionContractError("Top-5 artifact path is not runtime-safe")
    allowed = {
        ArtifactOwner.SHADOW_ARCHIVE: "results/shadow/",
        ArtifactOwner.STAGED_PUBLIC: "docs/data/",
        ArtifactOwner.HEALTH: "results/health/",
    }
    if owner is not None:
        prefix = allowed[ArtifactOwner(owner)]
        if not normalized.startswith(prefix):
            raise ProductionContractError(f"artifact path is outside {owner.value} ownership")
    elif not normalized.startswith(tuple(allowed.values())):
        raise ProductionContractError("Top-5 artifact path requires an explicit runtime owner")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _disabled_config(
    league_code: str,
    display_name: str,
    sport_key: str,
    result_code: str,
) -> LeagueProductionConfig:
    return LeagueProductionConfig(
        league_code=league_code,
        display_name=display_name,
        provider_sport_key=sport_key,
        fixture_source="the_odds_api",
        result_source=f"football_data:{result_code}",
        model_adapter_id="unbound",
        provider_mapping=ProviderMapping(
            provider_name="the_odds_api",
            competition_id=sport_key,
            sport_key=sport_key,
            markets=("h2h", "totals", "spreads"),
            regions=("eu",),
            result_source=f"football_data:{result_code}",
        ),
    )


# Metadata is present for future league-by-league work, but these entries are
# not added to src.config.LEAGUE_REGISTRY and cannot be consumed by live jobs.
DISABLED_TOP5_LEAGUE_CONFIGS: tuple[LeagueProductionConfig, ...] = (
    _disabled_config("BL1", "Bundesliga", "soccer_germany_bundesliga", "D1"),
    _disabled_config("EPL", "Premier League", "soccer_epl", "E0"),
    _disabled_config("LL", "La Liga", "soccer_spain_la_liga", "SP1"),
    _disabled_config("SA", "Serie A", "soccer_italy_serie_a", "I1"),
    _disabled_config("L1", "Ligue 1", "soccer_france_ligue_1", "F1"),
)
validate_disabled_top5_configs(DISABLED_TOP5_LEAGUE_CONFIGS)
DISABLED_TOP5_REGISTRY: Mapping[str, LeagueProductionConfig] = MappingProxyType(
    {config.league_code: config for config in DISABLED_TOP5_LEAGUE_CONFIGS}
)
