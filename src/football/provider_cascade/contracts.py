"""Provider-neutral contracts for the Top-5 shadow odds cascade.

This module is deliberately free of network, model, publisher, scheduler, and
financial side effects.  Provider-specific payloads are converted to these
contracts before Builder 1 can consume them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
from types import MappingProxyType

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    _utc,
)

MARKET_PREMATCH_1X2 = "football:pre_match:1x2"
TOP5_LEAGUE_CODES = frozenset({"BL1", "EPL", "LL", "SA", "L1"})
FOOTBALL_PROVIDER_REPERTOIRE = ("the_odds_api",)
DEFAULT_PROVIDER_ORDER = FOOTBALL_PROVIDER_REPERTOIRE
DECOMMISSIONED_FOOTBALL_PROVIDERS = frozenset(
    {"odds_api_io", "api_football", "betfair", "betfair_delayed", "oddsportal"}
)


class ProviderState(str, Enum):
    AVAILABLE = "AVAILABLE"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILED = "AUTH_FAILED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    UNSUPPORTED_FIXTURE = "UNSUPPORTED_FIXTURE"
    UNSUPPORTED_LEAGUE = "UNSUPPORTED_LEAGUE"
    UNSUPPORTED_MARKET = "UNSUPPORTED_MARKET"
    STALE = "STALE"
    MALFORMED = "MALFORMED"
    PARTIAL = "PARTIAL"
    QUALITY_REJECTED = "QUALITY_REJECTED"
    CONFIG_DISABLED = "CONFIG_DISABLED"
    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    CANDIDATE_ONLY = "CANDIDATE_ONLY"
    HEALTH_UNKNOWN = "HEALTH_UNKNOWN"
    DISCOVERY_REQUIRED = "DISCOVERY_REQUIRED"
    IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
    IDENTITY_AMBIGUOUS = "IDENTITY_AMBIGUOUS"


class TransportCapability(str, Enum):
    """Whether an adapter can cross the real network boundary."""

    TEST_INJECTED = "TEST_INJECTED"
    NETWORK_CAPABLE = "NETWORK_CAPABLE"


class ProviderIdentityResolutionState(str, Enum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    DISCOVERY_REQUIRED = "DISCOVERY_REQUIRED"


class TimingProvenance(str, Enum):
    SOURCE_TIMESTAMP = "SOURCE_TIMESTAMP"
    CAPTURE_TIME_ONLY = "CAPTURE_TIME_ONLY"


class ObservationCompleteness(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"


@dataclass(frozen=True)
class QuotaSnapshot:
    """A redacted provider quota/rate snapshot; values may be unknown."""

    used: int | None = None
    remaining: int | None = None
    reset_at: datetime | None = None
    rate_limit: int | None = None
    rate_remaining: int | None = None
    rate_reset_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.used is not None and self.used < 0:
            raise ProductionContractError("quota used must be non-negative")
        if self.remaining is not None and self.remaining < 0:
            raise ProductionContractError("quota remaining must be non-negative")
        if self.rate_limit is not None and self.rate_limit < 0:
            raise ProductionContractError("rate limit must be non-negative")
        if self.rate_remaining is not None and self.rate_remaining < 0:
            raise ProductionContractError("rate remaining must be non-negative")
        if self.reset_at is not None:
            _utc(self.reset_at, "quota reset_at")
        if self.rate_reset_at is not None:
            _utc(self.rate_reset_at, "rate reset_at")

    def as_payload(self) -> dict[str, object]:
        return {
            "used": self.used,
            "remaining": self.remaining,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "rate_limit": self.rate_limit,
            "rate_remaining": self.rate_remaining,
            "rate_reset_at": self.rate_reset_at.isoformat()
            if self.rate_reset_at
            else None,
        }


@dataclass(frozen=True)
class CascadeTimingPolicy:
    """Caller-supplied experiment timing; no production values are implied."""

    maximum_odds_age_seconds: int
    kickoff_tolerance_seconds: int

    def validate(self) -> None:
        if (
            not isinstance(self.maximum_odds_age_seconds, int)
            or isinstance(self.maximum_odds_age_seconds, bool)
            or self.maximum_odds_age_seconds <= 0
            or not isinstance(self.kickoff_tolerance_seconds, int)
            or isinstance(self.kickoff_tolerance_seconds, bool)
            or self.kickoff_tolerance_seconds < 0
        ):
            raise ProductionContractError(
                "explicit experiment timing policy is invalid"
            )


@dataclass(frozen=True)
class NetworkAuthorizationContract:
    """Explicit authority for one controlled-shadow network path."""

    controlled_shadow_run_ref: str
    authorized_providers: tuple[str, ...]
    betting_enabled: bool = False
    publication_enabled: bool = False

    def validate(self) -> None:
        if (
            not isinstance(self.controlled_shadow_run_ref, str)
            or not self.controlled_shadow_run_ref.strip()
        ):
            raise ProductionContractError("controlled shadow run reference is required")
        if not self.authorized_providers or any(
            not isinstance(provider, str) or not provider.strip()
            for provider in self.authorized_providers
        ):
            raise ProductionContractError(
                "controlled shadow authorization requires providers"
            )
        if set(self.authorized_providers) & DECOMMISSIONED_FOOTBALL_PROVIDERS:
            raise ProductionContractError(
                "controlled shadow authorization contains a decommissioned football provider"
            )
        if len(set(self.authorized_providers)) != len(self.authorized_providers):
            raise ProductionContractError(
                "controlled shadow authorization contains duplicate providers"
            )
        if self.betting_enabled or self.publication_enabled:
            raise ProductionContractError(
                "controlled shadow authorization cannot enable betting or publication"
            )

    def permits(self, provider: str) -> bool:
        self.validate()
        return provider in self.authorized_providers


def _quota_from_mapping(raw: object, default: QuotaSnapshot) -> QuotaSnapshot:
    if isinstance(raw, QuotaSnapshot):
        return raw
    if not isinstance(raw, Mapping):
        raise ProductionContractError("initial_quota must be a mapping")

    def optional_int(name: str) -> int | None:
        value = raw.get(name)
        return None if value is None else int(value)

    def optional_datetime(name: str) -> datetime | None:
        value = raw.get(name)
        if value is None:
            return None
        if isinstance(value, datetime):
            return _utc(value, name)
        try:
            return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")), name)
        except (TypeError, ValueError) as exc:
            raise ProductionContractError(f"{name} must be an ISO timestamp") from exc

    return QuotaSnapshot(
        used=optional_int("used") if "used" in raw else default.used,
        remaining=(
            optional_int("remaining") if "remaining" in raw else default.remaining
        ),
        reset_at=(
            optional_datetime("reset_at") if "reset_at" in raw else default.reset_at
        ),
        rate_limit=(
            optional_int("rate_limit") if "rate_limit" in raw else default.rate_limit
        ),
        rate_remaining=(
            optional_int("rate_remaining")
            if "rate_remaining" in raw
            else default.rate_remaining
        ),
        rate_reset_at=(
            optional_datetime("rate_reset_at")
            if "rate_reset_at" in raw
            else default.rate_reset_at
        ),
    )


@dataclass(frozen=True)
class ProviderConfig:
    """Configuration for one provider, with no secret-bearing values."""

    name: str
    enabled: bool = True
    league_allowlist: frozenset[str] = frozenset()
    market_allowlist: tuple[str, ...] = (MARKET_PREMATCH_1X2,)
    bookmakers: tuple[str, ...] = ()
    request_budget: int = 1
    quota_reserve: int = 0
    timeout_seconds: float = 5.0
    max_attempts: int = 1
    shadow_only: bool = True
    quality_eligible: bool = False
    candidate_only: bool = True
    credentials_required: bool = True
    credential_env: tuple[str, ...] = ()
    credential_available: bool | None = None
    initial_quota: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    request_cost: int = 1
    adapter_version: str = "unversioned"
    source_timestamp_required: bool = True

    def validate(self) -> None:
        if not self.name.strip():
            raise ProductionContractError("provider config requires a name")
        if self.name in DECOMMISSIONED_FOOTBALL_PROVIDERS:
            raise ProductionContractError(
                f"football provider is decommissioned: {self.name}"
            )
        if any(not value.strip() for value in self.league_allowlist):
            raise ProductionContractError(
                "provider league allow-list contains a blank value"
            )
        if not self.market_allowlist or any(
            not value.strip() for value in self.market_allowlist
        ):
            raise ProductionContractError(
                "provider market allow-list must not be empty"
            )
        if any(not value.strip() for value in self.bookmakers):
            raise ProductionContractError(
                "provider bookmaker list contains a blank value"
            )
        if self.request_budget < 0 or self.quota_reserve < 0 or self.request_cost <= 0:
            raise ProductionContractError("provider budget values are invalid")
        if self.timeout_seconds <= 0 or not isfinite(self.timeout_seconds):
            raise ProductionContractError(
                "provider timeout must be finite and positive"
            )
        if self.max_attempts != 1:
            raise ProductionContractError("provider max_attempts must remain one")
        if not self.shadow_only:
            raise ProductionContractError(
                "provider live authority is outside the shadow cascade"
            )
        if (
            self.credentials_required
            and not self.credential_env
            and self.credential_available is None
        ):
            raise ProductionContractError(
                "credential environment is required for this provider"
            )
        if not self.adapter_version.strip():
            raise ProductionContractError("provider adapter_version is required")
        self.initial_quota.__post_init__()


@dataclass(frozen=True)
class ProviderIdentityResolution:
    """Deterministic, cost-visible resolution of a provider-side identity."""

    provider: str
    state: ProviderIdentityResolutionState | str
    canonical_fixture_key: str
    provider_id: str | None
    home_team: str
    away_team: str
    kickoff: datetime | None
    league_competition_evidence: str
    resolution_provenance: str
    resolution_timestamp: datetime | None
    resolver_version: str
    digest: str
    runner_mapping: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ProviderIdentityResolution:
        if not isinstance(payload, Mapping):
            raise ProductionContractError(
                "provider identity resolution must be a mapping"
            )

        def optional_datetime(value: object, field_name: str) -> datetime | None:
            if value is None:
                return None
            if isinstance(value, datetime):
                return _utc(value, field_name)
            try:
                return _utc(
                    datetime.fromisoformat(str(value).replace("Z", "+00:00")),
                    field_name,
                )
            except (TypeError, ValueError) as exc:
                raise ProductionContractError(
                    f"{field_name} must be an ISO timestamp"
                ) from exc

        runner_mapping = payload.get("runner_mapping", {})
        if not isinstance(runner_mapping, Mapping):
            raise ProductionContractError("runner mapping must be a mapping")
        result = cls(
            provider=str(payload.get("provider", "")),
            state=payload.get("state", ""),
            canonical_fixture_key=str(payload.get("canonical_fixture_key", "")),
            provider_id=(
                None
                if payload.get("provider_id") is None
                else str(payload.get("provider_id"))
            ),
            home_team=str(payload.get("home_team", "")),
            away_team=str(payload.get("away_team", "")),
            kickoff=optional_datetime(
                payload.get("kickoff"), "resolved provider kickoff"
            ),
            league_competition_evidence=str(
                payload.get("league_competition_evidence", "")
            ),
            resolution_provenance=str(payload.get("resolution_provenance", "")),
            resolution_timestamp=optional_datetime(
                payload.get("resolution_timestamp"), "identity resolution timestamp"
            ),
            resolver_version=str(payload.get("resolver_version", "")),
            digest=str(payload.get("digest", "")),
            runner_mapping={
                str(key): str(value) for key, value in runner_mapping.items()
            },
        )
        result.validate()
        return result

    def _digest_payload(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "state": ProviderIdentityResolutionState(self.state).value,
            "canonical_fixture_key": self.canonical_fixture_key,
            "provider_id": self.provider_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": self.kickoff.isoformat() if self.kickoff else None,
            "league_competition_evidence": self.league_competition_evidence,
            "resolution_provenance": self.resolution_provenance,
            "resolution_timestamp": (
                self.resolution_timestamp.isoformat()
                if self.resolution_timestamp
                else None
            ),
            "resolver_version": self.resolver_version,
            "runner_mapping": dict(sorted(self.runner_mapping.items())),
        }

    def validate(
        self,
        *,
        fixture: Fixture | None = None,
        kickoff_tolerance_seconds: int = 0,
    ) -> None:
        if (
            not isinstance(kickoff_tolerance_seconds, int)
            or isinstance(kickoff_tolerance_seconds, bool)
            or kickoff_tolerance_seconds < 0
        ):
            raise ProductionContractError(
                "identity kickoff tolerance must be non-negative"
            )
        state = ProviderIdentityResolutionState(self.state)
        if not self.provider.strip() or not self.canonical_fixture_key.strip():
            raise ProductionContractError(
                "provider identity resolution is missing identity"
            )
        if not self.resolution_provenance.strip() or not self.resolver_version.strip():
            raise ProductionContractError(
                "provider identity resolution is missing provenance"
            )
        if state is ProviderIdentityResolutionState.RESOLVED:
            required = (
                self.provider_id,
                self.home_team,
                self.away_team,
                self.league_competition_evidence,
                self.digest,
            )
            if any(value is None or not str(value).strip() for value in required):
                raise ProductionContractError(
                    "resolved provider identity is missing contract evidence"
                )
            if self.kickoff is None or self.resolution_timestamp is None:
                raise ProductionContractError(
                    "resolved provider identity requires timestamps"
                )
            _utc(self.kickoff, "resolved provider kickoff")
            _utc(self.resolution_timestamp, "identity resolution timestamp")
            expected_digest = digest_record(self._digest_payload())
            if self.digest.lower() != expected_digest:
                raise ProductionContractError(
                    "provider identity resolution digest mismatch"
                )
            if fixture is not None:
                fixture.validate()
                if (
                    self.canonical_fixture_key != fixture.fixture_key
                    or self.home_team.strip().casefold()
                    != fixture.home_team.strip().casefold()
                    or self.away_team.strip().casefold()
                    != fixture.away_team.strip().casefold()
                    or abs((self.kickoff - fixture.kickoff).total_seconds())
                    > kickoff_tolerance_seconds
                ):
                    raise ProductionContractError(
                        "provider identity resolution does not match fixture"
                    )
        elif self.digest:
            expected_digest = digest_record(self._digest_payload())
            if self.digest.lower() != expected_digest:
                raise ProductionContractError(
                    "provider identity resolution digest mismatch"
                )

    @classmethod
    def resolved(
        cls,
        *,
        provider: str,
        fixture: Fixture,
        provider_id: str,
        league_competition_evidence: str,
        resolution_provenance: str,
        resolution_timestamp: datetime,
        resolver_version: str,
        runner_mapping: Mapping[str, str] | None = None,
        kickoff: datetime | None = None,
        kickoff_tolerance_seconds: int = 0,
    ) -> ProviderIdentityResolution:
        fixture.validate()
        base = cls(
            provider=provider,
            state=ProviderIdentityResolutionState.RESOLVED,
            canonical_fixture_key=fixture.fixture_key,
            provider_id=str(provider_id),
            home_team=fixture.home_team,
            away_team=fixture.away_team,
            kickoff=_utc(kickoff or fixture.kickoff, "resolved provider kickoff"),
            league_competition_evidence=league_competition_evidence,
            resolution_provenance=resolution_provenance,
            resolution_timestamp=_utc(
                resolution_timestamp, "identity resolution timestamp"
            ),
            resolver_version=resolver_version,
            digest="",
            runner_mapping=runner_mapping or {},
        )
        result = cls(
            **{**base.__dict__, "digest": digest_record(base._digest_payload())}
        )
        result.validate(
            fixture=fixture,
            kickoff_tolerance_seconds=kickoff_tolerance_seconds,
        )
        return result

    @property
    def provider_fixture_id(self) -> str | None:
        return self.provider_id

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            **self._digest_payload(),
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ProviderCascadeConfig:
    """Deterministic provider ordering and local request safety caps."""

    provider_order: tuple[str, ...] = DEFAULT_PROVIDER_ORDER
    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)
    global_request_budget: int = 1
    per_run_cap: int = 1
    allow_candidate_only: bool = True
    fail_closed: bool = True
    live_calls_authorized: bool = False
    controlled_shadow_run_ref: str | None = None
    controlled_shadow_authorized_providers: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.provider_order:
            raise ProductionContractError("provider order must not be empty")
        if len(set(self.provider_order)) != len(self.provider_order):
            raise ProductionContractError("provider order contains a duplicate")
        if any(not name.strip() for name in self.provider_order):
            raise ProductionContractError("provider order contains a blank name")
        if set(self.provider_order) & DECOMMISSIONED_FOOTBALL_PROVIDERS:
            raise ProductionContractError(
                "provider order contains a decommissioned football provider"
            )
        unknown = sorted(set(self.provider_order) - set(self.providers))
        if unknown:
            raise ProductionContractError(
                f"provider order contains unknown providers: {', '.join(unknown)}"
            )
        unused = sorted(set(self.providers) - set(self.provider_order))
        if unused:
            raise ProductionContractError(
                f"provider config is not in provider order: {', '.join(unused)}"
            )
        if self.global_request_budget < 0 or self.per_run_cap < 0:
            raise ProductionContractError(
                "global request budget caps must be non-negative"
            )
        if self.per_run_cap > self.global_request_budget:
            raise ProductionContractError(
                "per-run cap cannot exceed global request budget"
            )
        if self.live_calls_authorized and not (
            self.controlled_shadow_run_ref and self.controlled_shadow_run_ref.strip()
        ):
            raise ProductionContractError(
                "live calls require a controlled shadow run reference"
            )
        authorized = tuple(self.controlled_shadow_authorized_providers)
        if len(set(authorized)) != len(authorized) or any(
            not provider.strip() for provider in authorized
        ):
            raise ProductionContractError(
                "controlled shadow authorization providers are invalid"
            )
        if set(authorized) - set(self.provider_order):
            raise ProductionContractError(
                "controlled shadow authorization contains an unconfigured provider"
            )
        for config in self.providers.values():
            config.validate()

    def network_authorization(self) -> NetworkAuthorizationContract | None:
        """Return the only contract that may authorize a network-capable adapter."""

        if not self.live_calls_authorized or not self.controlled_shadow_run_ref:
            return None
        providers = self.controlled_shadow_authorized_providers or self.provider_order
        authorization = NetworkAuthorizationContract(
            controlled_shadow_run_ref=self.controlled_shadow_run_ref,
            authorized_providers=tuple(providers),
        )
        authorization.validate()
        return authorization

    @classmethod
    def default(cls) -> ProviderCascadeConfig:
        return cls(
            providers={
                "the_odds_api": ProviderConfig(
                    name="the_odds_api",
                    credential_env=("ODDS_API_KEY",),
                    initial_quota=QuotaSnapshot(used=500, remaining=0),
                    adapter_version="the-odds-api-v4:1",
                ),
            }
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> ProviderCascadeConfig:
        if not isinstance(raw, Mapping):
            raise ProductionContractError("provider cascade config must be a mapping")
        order_raw = raw.get("provider_order", DEFAULT_PROVIDER_ORDER)
        if isinstance(order_raw, (str, bytes)):
            raise ProductionContractError("provider_order must be a sequence")
        try:
            order = tuple(str(value) for value in order_raw)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ProductionContractError("provider_order must be a sequence") from exc
        base = cls.default().providers
        provider_values = raw.get("providers", {})
        if not isinstance(provider_values, Mapping):
            raise ProductionContractError("providers must be a mapping")
        unknown_configs = sorted(set(provider_values) - set(base))
        if unknown_configs:
            raise ProductionContractError(
                "provider config contains unknown providers: "
                + ", ".join(str(value) for value in unknown_configs)
            )
        unknown_order = sorted(set(order) - set(base))
        if unknown_order:
            raise ProductionContractError(
                "provider order contains unknown providers: " + ", ".join(unknown_order)
            )
        unused_configs = sorted(set(provider_values) - set(order))
        if unused_configs:
            raise ProductionContractError(
                "provider config is not in provider order: "
                + ", ".join(str(value) for value in unused_configs)
            )
        configs: dict[str, ProviderConfig] = {}
        for name in order:
            default = base[name]
            values = provider_values.get(name, {})
            if not isinstance(values, Mapping):
                raise ProductionContractError(
                    f"provider config for {name} must be a mapping"
                )
            configs[name] = ProviderConfig(
                name=name,
                enabled=bool(values.get("enabled", default.enabled)),
                league_allowlist=frozenset(
                    str(v)
                    for v in values.get("league_allowlist", default.league_allowlist)
                ),
                market_allowlist=tuple(
                    str(v)
                    for v in values.get("market_allowlist", default.market_allowlist)
                ),
                bookmakers=tuple(
                    str(v) for v in values.get("bookmakers", default.bookmakers)
                ),
                request_budget=int(
                    values.get("request_budget", default.request_budget)
                ),
                quota_reserve=int(values.get("quota_reserve", default.quota_reserve)),
                timeout_seconds=float(
                    values.get("timeout_seconds", default.timeout_seconds)
                ),
                max_attempts=int(values.get("max_attempts", default.max_attempts)),
                shadow_only=bool(values.get("shadow_only", default.shadow_only)),
                quality_eligible=bool(
                    values.get("quality_eligible", default.quality_eligible)
                ),
                candidate_only=bool(
                    values.get("candidate_only", default.candidate_only)
                ),
                credentials_required=bool(
                    values.get("credentials_required", default.credentials_required)
                ),
                credential_env=tuple(
                    str(v) for v in values.get("credential_env", default.credential_env)
                ),
                credential_available=(
                    None
                    if "credential_available" not in values
                    else bool(values["credential_available"])
                ),
                initial_quota=_quota_from_mapping(
                    values.get("initial_quota", default.initial_quota),
                    default.initial_quota,
                ),
                request_cost=int(values.get("request_cost", default.request_cost)),
                adapter_version=str(
                    values.get("adapter_version", default.adapter_version)
                ),
                source_timestamp_required=bool(
                    values.get(
                        "source_timestamp_required", default.source_timestamp_required
                    )
                ),
            )
        result = cls(
            provider_order=order,
            providers=MappingProxyType(configs),
            global_request_budget=int(raw.get("global_request_budget", 1)),
            per_run_cap=int(raw.get("per_run_cap", 1)),
            allow_candidate_only=bool(raw.get("allow_candidate_only", True)),
            fail_closed=bool(raw.get("fail_closed", True)),
            live_calls_authorized=bool(raw.get("live_calls_authorized", False)),
            controlled_shadow_run_ref=(
                None
                if raw.get("controlled_shadow_run_ref") is None
                else str(raw["controlled_shadow_run_ref"])
            ),
            controlled_shadow_authorized_providers=tuple(
                str(value)
                for value in raw.get("controlled_shadow_authorized_providers", ())
            ),
        )
        result.validate()
        return result


@dataclass(frozen=True)
class NormalizedOddsObservation:
    """Strict provider-neutral pre-match 1X2 observation."""

    league_code: str
    fixture_key: str
    provider_fixture_id: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    market_type: str
    home_odds: float
    draw_odds: float
    away_odds: float
    provider_identity: str
    bookmaker_identity: str
    source_timestamp: datetime | None
    captured_at: datetime
    request_identity: str
    request_started_at: datetime
    request_completed_at: datetime
    latency_ms: int
    provider_priority: int
    fallback_depth: int
    quota_state_before: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    quota_state_after: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    rate_limit_state: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    source_provenance: str = ""
    raw_record_digest: str = ""
    adapter_version: str = ""
    completeness: ObservationCompleteness = ObservationCompleteness.COMPLETE
    error_classification: ProviderState = ProviderState.AVAILABLE
    candidate_only: bool = True
    delayed: bool = False
    delay_seconds: int | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    source_timing_provenance: TimingProvenance | str = TimingProvenance.SOURCE_TIMESTAMP

    def __post_init__(self) -> None:
        object.__setattr__(self, "kickoff_utc", _utc(self.kickoff_utc, "kickoff_utc"))
        if self.source_timestamp is not None:
            object.__setattr__(
                self,
                "source_timestamp",
                _utc(self.source_timestamp, "source_timestamp"),
            )
        object.__setattr__(self, "captured_at", _utc(self.captured_at, "captured_at"))
        object.__setattr__(
            self,
            "request_started_at",
            _utc(self.request_started_at, "request_started_at"),
        )
        object.__setattr__(
            self,
            "request_completed_at",
            _utc(self.request_completed_at, "request_completed_at"),
        )
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        try:
            timing_provenance = TimingProvenance(self.source_timing_provenance)
        except (TypeError, ValueError) as exc:
            raise ProductionContractError(
                "source timing provenance is unknown"
            ) from exc
        object.__setattr__(self, "source_timing_provenance", timing_provenance)
        if self.latency_ms < 0 or self.fallback_depth < 0 or self.provider_priority < 0:
            raise ProductionContractError(
                "observation latency and routing depth must be non-negative"
            )

    def validate(
        self, *, now: datetime | None = None, require_fresh: bool = True
    ) -> None:
        try:
            timing_provenance = TimingProvenance(self.source_timing_provenance)
        except (TypeError, ValueError) as exc:
            raise ProductionContractError(
                "source timing provenance is unknown"
            ) from exc
        required = (
            self.league_code,
            self.fixture_key,
            self.provider_fixture_id,
            self.home_team,
            self.away_team,
            self.provider_identity,
            self.bookmaker_identity,
            self.request_identity,
            self.source_provenance,
            self.raw_record_digest,
            self.adapter_version,
        )
        if any(not value.strip() for value in required):
            raise ProductionContractError(
                "normalized odds observation is missing provenance or identity"
            )
        if self.home_team.strip().casefold() == self.away_team.strip().casefold():
            raise ProductionContractError(
                "normalized odds observation teams must be distinct"
            )
        if self.market_type != MARKET_PREMATCH_1X2:
            raise ProductionContractError("only football pre-match 1X2 is accepted")
        odds = (self.home_odds, self.draw_odds, self.away_odds)
        if any(not isfinite(float(value)) or float(value) <= 1.0 for value in odds):
            raise ProductionContractError(
                "normalized odds must be finite decimal values greater than 1"
            )
        if self.request_completed_at < self.request_started_at:
            raise ProductionContractError("request completion precedes request start")
        if self.source_timestamp is None:
            if timing_provenance is not TimingProvenance.CAPTURE_TIME_ONLY:
                raise ProductionContractError(
                    "source timestamp is required unless timing is capture-only"
                )
        elif self.source_timestamp > self.captured_at:
            raise ProductionContractError("source timestamp is in the future")
        if self.completeness is not ObservationCompleteness.COMPLETE:
            raise ProductionContractError(
                "partial odds observations cannot enter the quality boundary"
            )
        if self.error_classification is not ProviderState.AVAILABLE:
            raise ProductionContractError(
                "non-available observation cannot be accepted"
            )
        if self.delayed and not self.metadata.get("delay_semantics"):
            raise ProductionContractError(
                "delayed observation must retain delay semantics"
            )
        if self.delay_seconds is not None and self.delay_seconds < 0:
            raise ProductionContractError("delay_seconds must be non-negative")
        if require_fresh and now is not None:
            now_utc = _utc(now, "observation now")
            if self.source_timestamp is not None and self.source_timestamp > now_utc:
                raise ProductionContractError("source timestamp is after capture time")

    @property
    def fixture(self) -> Fixture:
        return Fixture(
            self.fixture_key,
            self.league_code,
            self.home_team,
            self.away_team,
            self.kickoff_utc,
        )

    def as_market_snapshot(self) -> MarketSnapshot:
        self.validate()
        return MarketSnapshot(
            fixture_key=self.fixture_key,
            captured_at=self.source_timestamp or self.captured_at,
            kind=MarketSnapshotKind.SIGNAL_TIME,
            source=self.provider_identity,
            odds={
                "home": self.home_odds,
                "draw": self.draw_odds,
                "away": self.away_odds,
            },
            snapshot_id=self.request_identity,
        )

    def as_payload(self) -> dict[str, object]:
        self.validate(require_fresh=False)
        return {
            "league_code": self.league_code,
            "fixture_key": self.fixture_key,
            "provider_fixture_id": self.provider_fixture_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff_utc": self.kickoff_utc.isoformat(),
            "market_type": self.market_type,
            "odds": {
                "home": self.home_odds,
                "draw": self.draw_odds,
                "away": self.away_odds,
            },
            "provider_identity": self.provider_identity,
            "bookmaker_identity": self.bookmaker_identity,
            "source_timestamp": (
                self.source_timestamp.isoformat() if self.source_timestamp else None
            ),
            "captured_at": self.captured_at.isoformat(),
            "request_identity": self.request_identity,
            "request_started_at": self.request_started_at.isoformat(),
            "request_completed_at": self.request_completed_at.isoformat(),
            "latency_ms": self.latency_ms,
            "provider_priority": self.provider_priority,
            "fallback_depth": self.fallback_depth,
            "quota_state_before": self.quota_state_before.as_payload(),
            "quota_state_after": self.quota_state_after.as_payload(),
            "rate_limit_state": self.rate_limit_state.as_payload(),
            "source_provenance": self.source_provenance,
            "raw_record_digest": self.raw_record_digest,
            "adapter_version": self.adapter_version,
            "completeness": self.completeness.value,
            "error_classification": self.error_classification.value,
            "candidate_only": self.candidate_only,
            "delayed": self.delayed,
            "delay_seconds": self.delay_seconds,
            "metadata": dict(self.metadata),
            "source_timing_provenance": TimingProvenance(
                self.source_timing_provenance
            ).value,
        }


@dataclass(frozen=True)
class ProviderAttemptTrace:
    attempt_index: int
    provider: str
    state: ProviderState
    result: str
    reason: str
    network_called: bool
    request_identity: str
    transport_capability: TransportCapability = TransportCapability.TEST_INJECTED
    status_code: int | None = None
    latency_ms: int = 0
    configured_provider_order: tuple[str, ...] = ()
    fallback_depth: int = 0
    preflight_allowed: bool = False
    preflight_reason: str = ""
    budget_decision: str = "REJECTED"
    quota_before: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    quota_after: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    network_request_count: int = 0
    quota_cost_units: float = 0.0
    request_cost_classification: str = "QUOTA_CONSUMING_REQUEST"
    credentials_present: bool | None = None
    provider_readiness_state: str = "CONTRACT_SUPPORTED"
    provider_record_id: str = ""
    raw_record_digest: str = ""
    fixture_key: str = ""
    league_code: str = ""
    home_team: str = ""
    away_team: str = ""
    kickoff: datetime | None = None
    market_type: str = MARKET_PREMATCH_1X2
    home_odds: float | None = None
    draw_odds: float | None = None
    away_odds: float | None = None
    bookmaker_identity: str | None = None
    source_identity: str | None = None
    source_timestamp: datetime | None = None
    source_timing_provenance: str = TimingProvenance.SOURCE_TIMESTAMP.value
    identity_resolution_state: str = ""
    identity_resolution_digest: str = ""
    adapter_version: str = "cascade-trace-v1"
    request_started_at: datetime | None = None
    request_completed_at: datetime | None = None

    def validate(self) -> None:
        if self.attempt_index < 0 or self.latency_ms < 0:
            raise ProductionContractError("provider attempt trace counters are invalid")
        if self.fallback_depth < 0 or self.network_request_count < 0:
            raise ProductionContractError("provider attempt trace depth is invalid")
        if self.network_request_count > 1:
            raise ProductionContractError(
                "one provider attempt cannot contain multiple network requests"
            )
        if self.network_request_count != int(self.network_called):
            raise ProductionContractError(
                "network request count must equal observed network calls"
            )
        try:
            TransportCapability(self.transport_capability)
        except (TypeError, ValueError) as exc:
            raise ProductionContractError(
                "provider attempt transport capability is unknown"
            ) from exc
        if self.quota_cost_units < 0 or not isfinite(self.quota_cost_units):
            raise ProductionContractError("provider attempt quota cost is invalid")
        if self.request_started_at is not None:
            _utc(self.request_started_at, "provider request_started_at")
        if self.request_completed_at is not None:
            _utc(self.request_completed_at, "provider request_completed_at")
        if (
            self.request_started_at is not None
            and self.request_completed_at is not None
            and self.request_completed_at < self.request_started_at
        ):
            raise ProductionContractError("provider request timestamps are invalid")
        if any(
            not value.strip()
            for value in (
                self.provider,
                self.result,
                self.reason,
                self.request_identity,
            )
        ):
            raise ProductionContractError(
                "provider attempt trace is missing safe provenance"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "attempt_index": self.attempt_index,
            "provider": self.provider,
            "state": self.state.value,
            "result": self.result,
            "reason": self.reason,
            "network_called": self.network_called,
            "request_identity": self.request_identity,
            "transport_capability": TransportCapability(
                self.transport_capability
            ).value,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "configured_provider_order": list(self.configured_provider_order),
            "fallback_depth": self.fallback_depth,
            "preflight_allowed": self.preflight_allowed,
            "preflight_reason": self.preflight_reason,
            "budget_decision": self.budget_decision,
            "quota_before": self.quota_before.as_payload(),
            "quota_after": self.quota_after.as_payload(),
            "network_request_count": self.network_request_count,
            "quota_cost_units": self.quota_cost_units,
            "request_cost_classification": self.request_cost_classification,
            "credentials_present": self.credentials_present,
            "provider_readiness_state": self.provider_readiness_state,
            "provider_record_id": self.provider_record_id,
            "raw_record_digest": self.raw_record_digest,
            "fixture_key": self.fixture_key,
            "league_code": self.league_code,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": self.kickoff.isoformat() if self.kickoff else None,
            "market_type": self.market_type,
            "home_odds": self.home_odds,
            "draw_odds": self.draw_odds,
            "away_odds": self.away_odds,
            "bookmaker_identity": self.bookmaker_identity,
            "source_identity": self.source_identity,
            "source_timestamp": (
                self.source_timestamp.isoformat() if self.source_timestamp else None
            ),
            "source_timing_provenance": self.source_timing_provenance,
            "identity_resolution_state": self.identity_resolution_state,
            "identity_resolution_digest": self.identity_resolution_digest,
            "adapter_version": self.adapter_version,
            "request_started_at": (
                self.request_started_at.isoformat() if self.request_started_at else None
            ),
            "request_completed_at": (
                self.request_completed_at.isoformat()
                if self.request_completed_at
                else None
            ),
        }


@dataclass(frozen=True)
class CascadeDecisionTrace:
    fixture_key: str
    attempts: tuple[ProviderAttemptTrace, ...]
    selected_provider: str | None
    fallback_depth: int | None
    total_latency_ms: int
    fail_closed: bool
    no_bet: bool = True
    publication: bool = False
    configured_provider_order: tuple[str, ...] = ()
    cascade_trace_digest: str = ""

    def validate(self) -> None:
        if not self.fixture_key.strip() or self.total_latency_ms < 0:
            raise ProductionContractError(
                "cascade trace identity or latency is invalid"
            )
        if self.fallback_depth is not None and self.fallback_depth < 0:
            raise ProductionContractError("cascade fallback depth must be non-negative")
        if self.selected_provider is None and not self.fail_closed:
            raise ProductionContractError("unselected cascade must fail closed")
        if self.selected_provider is not None and self.fail_closed:
            raise ProductionContractError("selected cascade cannot be fail closed")
        if not self.no_bet or self.publication:
            raise ProductionContractError(
                "cascade trace violates shadow safety boundary"
            )
        if self.configured_provider_order and len(
            set(self.configured_provider_order)
        ) != len(self.configured_provider_order):
            raise ProductionContractError("cascade provider order contains duplicates")
        for attempt in self.attempts:
            attempt.validate()

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "fixture_key": self.fixture_key,
            "attempts": [attempt.as_payload() for attempt in self.attempts],
            "selected_provider": self.selected_provider,
            "fallback_depth": self.fallback_depth,
            "total_latency_ms": self.total_latency_ms,
            "fail_closed": self.fail_closed,
            "no_bet": self.no_bet,
            "publication": self.publication,
            "configured_provider_order": list(self.configured_provider_order),
            "cascade_trace_digest": self.cascade_trace_digest,
        }


@dataclass(frozen=True)
class CascadeResult:
    observation: NormalizedOddsObservation | None
    trace: CascadeDecisionTrace

    @property
    def accepted(self) -> bool:
        return self.observation is not None and not self.trace.fail_closed

    @property
    def observation_digest(self) -> str:
        if self.observation is None:
            return ""
        return digest_record(self.observation.as_payload())

    @property
    def cascade_trace_digest(self) -> str:
        payload = self.trace.as_payload()
        payload["cascade_trace_digest"] = ""
        return digest_record(payload)

    def validate(self) -> None:
        self.trace.validate()
        if self.observation is None:
            if not self.trace.fail_closed or self.trace.selected_provider is not None:
                raise ProductionContractError("empty cascade result must fail closed")
        else:
            self.observation.validate(require_fresh=False)
            if (
                self.trace.fail_closed
                or self.trace.selected_provider != self.observation.provider_identity
            ):
                raise ProductionContractError(
                    "cascade result and trace selection differ"
                )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        trace_payload = self.trace.as_payload()
        trace_payload["cascade_trace_digest"] = self.cascade_trace_digest
        return {
            "observation": self.observation.as_payload() if self.observation else None,
            "decision_trace": trace_payload,
            "accepted": self.accepted,
            "fail_closed": self.trace.fail_closed,
            "no_bet": True,
            "publication": False,
        }


BUILDER2_VALIDATION_CONTRACT_VERSION = "top5-provider-cascade-validation-v1"


def stable_request_identity(
    provider: str, fixture: Fixture, requested_at: datetime
) -> str:
    """Return a deterministic, secret-free identity for one logical request."""

    fixture.validate()
    requested_at = _utc(requested_at, "requested_at")
    encoded = json.dumps(
        (
            provider,
            fixture.league_code,
            fixture.fixture_key,
            fixture.home_team,
            fixture.away_team,
            fixture.kickoff.isoformat(),
            requested_at.isoformat(),
        ),
        separators=(",", ":"),
    ).encode("utf-8")
    return f"cascade-request:{hashlib.sha256(encoded).hexdigest()[:32]}"


def digest_record(record: object) -> str:
    """Hash a provider record without retaining its raw payload in evidence."""

    encoded = json.dumps(
        record, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
