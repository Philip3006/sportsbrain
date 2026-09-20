"""Read-only production verification and publication-gate contracts for Top-5.

This module consumes operator-supplied evidence.  It has no provider client,
network, scheduler, publisher, ledger, Cloudflare, or filesystem writer
dependency.  A future activation runner may collect the evidence, but this
module only validates it and returns a deterministic decision.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
from types import MappingProxyType

from src.football.production_contracts import ProductionContractError, _utc
from src.football.provider_cascade.contracts import (
    CANDIDATE_ONLY_PROVIDER_IDENTITIES,
)

ACTIVE_FOOTBALL_AUTHORITY = "the_odds_api"
CANDIDATE_PROVIDER = next(iter(CANDIDATE_ONLY_PROVIDER_IDENTITIES))
_HEALTHY_RUNTIME_STATES = frozenset({"ok", "degraded"})
_WRITER_STATES = frozenset({"active", "healthy", "read_only"})


class VerificationStatus(str, Enum):
    PRODUCTION_VERIFIED = "PRODUCTION_VERIFIED"
    ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"
    VERIFICATION_BLOCKED = "VERIFICATION_BLOCKED"


class RollbackTrigger(str, Enum):
    ROUTING_MISMATCH = "routing_mismatch"
    IDENTITY_PROVENANCE_MISMATCH = "identity_provenance_mismatch"
    STALE_OR_POST_KICKOFF_DATA = "stale_or_post_kickoff_data"
    INCOMPLETE_1X2 = "incomplete_1x2"
    UNEXPECTED_SCHEDULER_ACTIVITY = "unexpected_scheduler_activity"
    HEALTH_FAILURE = "health_failure"
    UNCONTROLLED_RETRIES = "uncontrolled_retries"
    UNEXPECTED_QUOTA_CONSUMPTION = "unexpected_quota_consumption"
    PUBLICATION_LEAKAGE = "publication_leakage"
    FINANCIAL_LEDGER_MUTATION = "financial_ledger_mutation"
    SEALED_PARTITION_MUTATION = "sealed_partition_mutation"
    UNAPPROVED_SIGNAL_TIME_BEHAVIOR = "unapproved_signal_time_behavior"
    ACTIVATION_IDENTITY_MISMATCH = "activation_identity_mismatch"


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProductionContractError(f"{name} must be a nonblank string")
    return value.strip()


def _nonnegative_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProductionContractError(f"{name} must be a non-negative integer")
    return value


def _nonnegative_number(name: str, value: object) -> float:
    if isinstance(value, bool):
        raise ProductionContractError(f"{name} must be a non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProductionContractError(f"{name} must be a non-negative number") from exc
    if not isfinite(number) or number < 0:
        raise ProductionContractError(f"{name} must be a non-negative number")
    return number


def _parse_datetime(name: str, value: object) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, name)
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
        except ValueError as exc:
            raise ProductionContractError(f"{name} must be an ISO-8601 timestamp") from exc
    raise ProductionContractError(f"{name} must be an ISO-8601 timestamp")


def _string_map(name: str, values: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(values, Mapping) or not values:
        raise ProductionContractError(f"{name} must be a non-empty mapping")
    return {_text(f"{name} key", key): _text(f"{name}[{key}]", value) for key, value in values.items()}


def _count_map(name: str, values: Mapping[str, object]) -> dict[str, int]:
    if not isinstance(values, Mapping):
        raise ProductionContractError(f"{name} must be a mapping")
    return {_text(f"{name} key", key): _nonnegative_int(f"{name}[{key}]", value) for key, value in values.items()}


def _number_map(name: str, values: Mapping[str, object]) -> dict[str, float]:
    if not isinstance(values, Mapping):
        raise ProductionContractError(f"{name} must be a mapping")
    return {_text(f"{name} key", key): _nonnegative_number(f"{name}[{key}]", value) for key, value in values.items()}


@dataclass(frozen=True)
class ActivationIdentity:
    """Immutable identity that must remain bound from activation to verify."""

    activation_id: str
    activation_digest: str
    source_sha: str
    research_sha: str
    model_artifact_hash: str
    candidate_id: str

    def validate(self) -> None:
        for name, value in (
            ("activation_id", self.activation_id),
            ("activation_digest", self.activation_digest),
            ("source_sha", self.source_sha),
            ("research_sha", self.research_sha),
            ("model_artifact_hash", self.model_artifact_hash),
            ("candidate_id", self.candidate_id),
        ):
            _text(name, value)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> ActivationIdentity:
        return cls(**{name: _text(name, values.get(name)) for name in (
            "activation_id", "activation_digest", "source_sha", "research_sha",
            "model_artifact_hash", "candidate_id",
        )})

    def as_payload(self) -> dict[str, str]:
        self.validate()
        return {
            "activation_id": self.activation_id,
            "activation_digest": self.activation_digest,
            "source_sha": self.source_sha,
            "research_sha": self.research_sha,
            "model_artifact_hash": self.model_artifact_hash,
            "candidate_id": self.candidate_id,
        }


@dataclass(frozen=True)
class PreActivationBaseline:
    """Read-only baseline of the runtime that must remain safe and attributable."""

    captured_at: datetime
    activation: ActivationIdentity
    worker_health: str
    pwa_health: str
    football_scheduler_state: str
    active_provider_order: tuple[str, ...]
    launchd_expectations: Mapping[str, str]
    github_workflow_expectations: Mapping[str, str]
    runtime_writer_state: Mapping[str, str]
    ledger_writer_state: Mapping[str, str]
    football_health_artifacts: Mapping[str, str]
    request_counts: Mapping[str, int] = field(default_factory=dict)
    quota_remaining: Mapping[str, int] = field(default_factory=dict)
    spend_units: Mapping[str, float] = field(default_factory=dict)
    expected_fixture_identities: Mapping[str, tuple[str, str, str]] = field(default_factory=dict)
    publication_enabled: bool = False
    betting_enabled: bool = False
    ledger_mutations: int = 0
    sealed_partition_mutations: int = 0

    def validate(self) -> None:
        _utc(self.captured_at, "captured_at")
        self.activation.validate()
        for name, value in (("worker_health", self.worker_health), ("pwa_health", self.pwa_health)):
            if value not in _HEALTHY_RUNTIME_STATES:
                raise ProductionContractError(f"{name} is not an acceptable baseline state")
        _text("football_scheduler_state", self.football_scheduler_state)
        if not self.active_provider_order or any(not provider.strip() for provider in self.active_provider_order):
            raise ProductionContractError("active provider order is required")
        normalized = tuple(provider.casefold() for provider in self.active_provider_order)
        if ACTIVE_FOOTBALL_AUTHORITY not in normalized:
            raise ProductionContractError("The Odds API must remain in active football authority")
        if any(provider in CANDIDATE_ONLY_PROVIDER_IDENTITIES for provider in normalized):
            raise ProductionContractError("candidate provider cannot be active football authority")
        _string_map("launchd_expectations", self.launchd_expectations)
        _string_map("github_workflow_expectations", self.github_workflow_expectations)
        for name, values in (("runtime_writer_state", self.runtime_writer_state), ("ledger_writer_state", self.ledger_writer_state)):
            states = _string_map(name, values)
            if any(state not in _WRITER_STATES for state in states.values()):
                raise ProductionContractError(f"{name} contains an unexpected writer state")
        if not _string_map("football_health_artifacts", self.football_health_artifacts):
            raise ProductionContractError("football health artifacts are required")
        for name, values in (("request_counts", self.request_counts), ("quota_remaining", self.quota_remaining)):
            _count_map(name, values)
        _number_map("spend_units", self.spend_units)
        for key, value in self.expected_fixture_identities.items():
            _text("expected fixture key", key)
            if not isinstance(value, tuple) or len(value) != 3 or any(not item.strip() for item in value):
                raise ProductionContractError("expected fixture identity must be event/home/away")
        if self.publication_enabled or self.betting_enabled:
            raise ProductionContractError("baseline must be unpublished and no-bet")
        _nonnegative_int("ledger_mutations", self.ledger_mutations)
        _nonnegative_int("sealed_partition_mutations", self.sealed_partition_mutations)
        if self.ledger_mutations or self.sealed_partition_mutations:
            raise ProductionContractError("baseline must start without ledger or sealed mutations")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> PreActivationBaseline:
        expected = {}
        for key, identity in (values.get("expected_fixture_identities") or {}).items():
            if isinstance(identity, Mapping):
                expected[key] = (identity.get("event_identity"), identity.get("home_team"), identity.get("away_team"))
            else:
                expected[key] = tuple(identity)
        return cls(
            captured_at=_parse_datetime("captured_at", values.get("captured_at")),
            activation=ActivationIdentity.from_mapping(values.get("activation") or {}),
            worker_health=values.get("worker_health"),
            pwa_health=values.get("pwa_health"),
            football_scheduler_state=values.get("football_scheduler_state"),
            active_provider_order=tuple(values.get("active_provider_order") or ()),
            launchd_expectations=values.get("launchd_expectations") or {},
            github_workflow_expectations=values.get("github_workflow_expectations") or {},
            runtime_writer_state=values.get("runtime_writer_state") or {},
            ledger_writer_state=values.get("ledger_writer_state") or {},
            football_health_artifacts=values.get("football_health_artifacts") or {},
            request_counts=values.get("request_counts") or {},
            quota_remaining=values.get("quota_remaining") or {},
            spend_units=values.get("spend_units") or {},
            expected_fixture_identities=expected,
            publication_enabled=values.get("publication_enabled", False),
            betting_enabled=values.get("betting_enabled", False),
            ledger_mutations=values.get("ledger_mutations", 0),
            sealed_partition_mutations=values.get("sealed_partition_mutations", 0),
        )


@dataclass(frozen=True)
class RoutingEvidence:
    expected_provider: str
    selected_provider: str
    active_provider_order: tuple[str, ...]
    observed_providers: tuple[str, ...]
    fallback_provider: str | None = None
    allowed_fallback_providers: tuple[str, ...] = ()
    observed_candidate_ids: tuple[str, ...] = ()

    def validate(self) -> None:
        for name, value in (("expected_provider", self.expected_provider), ("selected_provider", self.selected_provider)):
            _text(name, value)
        if not self.active_provider_order or not self.observed_providers:
            raise ProductionContractError("routing evidence requires provider order and observations")
        if any(not value.strip() for value in (*self.active_provider_order, *self.observed_providers, *self.allowed_fallback_providers)):
            raise ProductionContractError("routing evidence contains a blank provider")
        if self.fallback_provider is not None:
            _text("fallback_provider", self.fallback_provider)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> RoutingEvidence:
        return cls(
            expected_provider=values.get("expected_provider"),
            selected_provider=values.get("selected_provider"),
            active_provider_order=tuple(values.get("active_provider_order") or ()),
            observed_providers=tuple(values.get("observed_providers") or ()),
            fallback_provider=values.get("fallback_provider"),
            allowed_fallback_providers=tuple(values.get("allowed_fallback_providers") or ()),
            observed_candidate_ids=tuple(values.get("observed_candidate_ids") or ()),
        )


@dataclass(frozen=True)
class MatchObservation:
    fixture_key: str
    event_identity: str
    home_team: str
    away_team: str
    kickoff_at: datetime
    observed_at: datetime
    source_sha: str
    config_digest: str
    market_kind: str
    odds: Mapping[str, float]

    def validate(self, *, now: datetime, max_age_seconds: int) -> None:
        for name, value in (
            ("fixture_key", self.fixture_key), ("event_identity", self.event_identity),
            ("home_team", self.home_team), ("away_team", self.away_team),
            ("source_sha", self.source_sha), ("config_digest", self.config_digest),
        ):
            _text(name, value)
        if self.home_team == self.away_team:
            raise ProductionContractError("observation participants must be distinct")
        kickoff = _utc(self.kickoff_at, "kickoff_at")
        observed = _utc(self.observed_at, "observed_at")
        current = _utc(now, "now")
        if self.market_kind != "signal_time":
            raise ProductionContractError("observation is not pre-match signal_time")
        if kickoff <= observed or kickoff <= current:
            raise ProductionContractError("observation is stale or post-kickoff")
        if (current - observed).total_seconds() > max_age_seconds:
            raise ProductionContractError("observation is stale")
        required = {"home", "draw", "away"}
        if set(self.odds) != required:
            raise ProductionContractError("observation requires complete pre-match regulation 1X2")
        for market, value in self.odds.items():
            if not isfinite(float(value)) or float(value) <= 1.0:
                raise ProductionContractError(f"invalid 1X2 odds for {market}")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> MatchObservation:
        return cls(
            fixture_key=values.get("fixture_key"), event_identity=values.get("event_identity"),
            home_team=values.get("home_team"), away_team=values.get("away_team"),
            kickoff_at=_parse_datetime("kickoff_at", values.get("kickoff_at")),
            observed_at=_parse_datetime("observed_at", values.get("observed_at")),
            source_sha=values.get("source_sha"), config_digest=values.get("config_digest"),
            market_kind=values.get("market_kind"), odds=values.get("odds") or {},
        )


@dataclass(frozen=True)
class RuntimeEvidence:
    scan_refresh_exit_code: int | None
    worker_health: str
    pwa_health: str
    football_scheduler_state: str
    runtime_writer_state: Mapping[str, str]
    ledger_writer_state: Mapping[str, str]
    football_health_artifacts: Mapping[str, str]
    unexpected_scheduler_runs: int = 0
    scheduler_expanded: bool = False
    retry_count: int = 0
    retry_budget: int = 0
    publication_enabled: bool = False
    betting_enabled: bool = False
    financial_mutations: int = 0
    ledger_mutations: int = 0
    sealed_partition_mutations: int = 0
    unapproved_signal_time_behavior: bool = False

    def validate(self) -> None:
        if self.scan_refresh_exit_code is not None:
            _nonnegative_int("scan_refresh_exit_code", self.scan_refresh_exit_code)
        _text("worker_health", self.worker_health)
        _text("pwa_health", self.pwa_health)
        _text("football_scheduler_state", self.football_scheduler_state)
        for name, values in (("runtime_writer_state", self.runtime_writer_state), ("ledger_writer_state", self.ledger_writer_state)):
            if any(state not in _WRITER_STATES for state in _string_map(name, values).values()):
                raise ProductionContractError(f"{name} contains an unexpected writer state")
        if not _string_map("football_health_artifacts", self.football_health_artifacts):
            raise ProductionContractError("football health artifacts are missing")
        for name, value in (("unexpected_scheduler_runs", self.unexpected_scheduler_runs), ("retry_count", self.retry_count), ("retry_budget", self.retry_budget), ("financial_mutations", self.financial_mutations), ("ledger_mutations", self.ledger_mutations), ("sealed_partition_mutations", self.sealed_partition_mutations)):
            _nonnegative_int(name, value)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> RuntimeEvidence:
        return cls(
            scan_refresh_exit_code=values.get("scan_refresh_exit_code"),
            worker_health=values.get("worker_health"), pwa_health=values.get("pwa_health"),
            football_scheduler_state=values.get("football_scheduler_state"),
            runtime_writer_state=values.get("runtime_writer_state") or {},
            ledger_writer_state=values.get("ledger_writer_state") or {},
            football_health_artifacts=values.get("football_health_artifacts") or {},
            unexpected_scheduler_runs=values.get("unexpected_scheduler_runs", 0),
            scheduler_expanded=values.get("scheduler_expanded", False),
            retry_count=values.get("retry_count", 0), retry_budget=values.get("retry_budget", 0),
            publication_enabled=values.get("publication_enabled", False),
            betting_enabled=values.get("betting_enabled", False),
            financial_mutations=values.get("financial_mutations", 0),
            ledger_mutations=values.get("ledger_mutations", 0),
            sealed_partition_mutations=values.get("sealed_partition_mutations", 0),
            unapproved_signal_time_behavior=values.get("unapproved_signal_time_behavior", False),
        )


@dataclass(frozen=True)
class ResourceEvidence:
    request_counts: Mapping[str, int]
    request_budgets: Mapping[str, int]
    quota_before: Mapping[str, int]
    quota_after: Mapping[str, int]
    spend_delta: Mapping[str, float]
    spend_budgets: Mapping[str, float]

    def validate(self) -> None:
        counts = _count_map("request_counts", self.request_counts)
        budgets = _count_map("request_budgets", self.request_budgets)
        before = _count_map("quota_before", self.quota_before)
        after = _count_map("quota_after", self.quota_after)
        spend = _number_map("spend_delta", self.spend_delta)
        spend_budgets = _number_map("spend_budgets", self.spend_budgets)
        providers = set(counts) | set(before) | set(after) | set(spend)
        if not providers:
            raise ProductionContractError("request/quota accounting is required")
        if not providers.issubset(set(budgets) | set(spend_budgets)):
            raise ProductionContractError("request/quota accounting lacks a provider budget")
        for provider in providers:
            if after.get(provider, before.get(provider, 0)) > before.get(provider, 0):
                raise ProductionContractError("quota increased during verification")
            if before.get(provider, 0) - after.get(provider, 0) != counts.get(provider, 0):
                raise ProductionContractError("quota delta does not equal request count")
            if counts.get(provider, 0) > budgets.get(provider, 0):
                raise ProductionContractError("request count exceeds budget")
            if spend.get(provider, 0.0) > spend_budgets.get(provider, 0.0):
                raise ProductionContractError("provider spend exceeds budget")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> ResourceEvidence:
        return cls(
            request_counts=values.get("request_counts") or {}, request_budgets=values.get("request_budgets") or {},
            quota_before=values.get("quota_before") or {}, quota_after=values.get("quota_after") or {},
            spend_delta=values.get("spend_delta") or {}, spend_budgets=values.get("spend_budgets") or {},
        )


@dataclass(frozen=True)
class VerificationReport:
    status: VerificationStatus
    checked_at: datetime
    activation: ActivationIdentity
    triggers: tuple[RollbackTrigger, ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    checks: Mapping[str, str] = field(default_factory=dict)

    @property
    def report_id(self) -> str:
        payload = {
            "status": self.status.value,
            "activation": self.activation.as_payload(),
            "triggers": [trigger.value for trigger in self.triggers],
            "blocked_reasons": list(self.blocked_reasons),
            "checks": dict(self.checks),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]

    def as_payload(self) -> dict[str, object]:
        self.activation.validate()
        return {
            "status": self.status.value,
            "report_id": self.report_id,
            "checked_at": _utc(self.checked_at, "checked_at").isoformat(),
            "activation": self.activation.as_payload(),
            "triggers": [trigger.value for trigger in self.triggers],
            "blocked_reasons": list(self.blocked_reasons),
            "checks": dict(self.checks),
        }


def verification_report_from_mapping(values: Mapping[str, object]) -> VerificationReport:
    """Parse a previously captured report without trusting its status."""

    report = VerificationReport(
        status=VerificationStatus(values.get("status")),
        checked_at=_parse_datetime("checked_at", values.get("checked_at")),
        activation=ActivationIdentity.from_mapping(values.get("activation") or {}),
        triggers=tuple(RollbackTrigger(trigger) for trigger in values.get("triggers", ())),
        blocked_reasons=tuple(values.get("blocked_reasons", ())),
        checks=values.get("checks") or {},
    )
    report.activation.validate()
    return report


@dataclass(frozen=True)
class PublicationGateResult:
    eligible: bool
    failures: tuple[str, ...]
    verification_report_id: str

    def as_payload(self) -> dict[str, object]:
        return {
            "publication_eligible": self.eligible,
            "failures": list(self.failures),
            "verification_report_id": self.verification_report_id,
            "publication_enabled": False,
        }


def _report(
    status: VerificationStatus,
    checked_at: datetime,
    activation: ActivationIdentity,
    *,
    triggers: Sequence[RollbackTrigger] = (),
    blocked: Sequence[str] = (),
    checks: Mapping[str, str] | None = None,
) -> VerificationReport:
    unique_triggers = tuple(dict.fromkeys(RollbackTrigger(trigger) for trigger in triggers))
    unique_blocked = tuple(dict.fromkeys(str(reason) for reason in blocked if str(reason).strip()))
    return VerificationReport(status, _utc(checked_at, "checked_at"), activation, unique_triggers, unique_blocked, MappingProxyType(dict(checks or {})))


def verify_production(
    baseline: PreActivationBaseline,
    observed_activation: ActivationIdentity,
    routing: RoutingEvidence,
    observations: Sequence[MatchObservation],
    runtime: RuntimeEvidence,
    resources: ResourceEvidence,
    *,
    checked_at: datetime,
    max_observation_age_seconds: int = 900,
) -> VerificationReport:
    """Evaluate supplied post-activation evidence without performing any action."""

    try:
        baseline.validate()
        observed_activation.validate()
        routing.validate()
        runtime.validate()
    except (ProductionContractError, TypeError, ValueError) as exc:
        return _report(VerificationStatus.VERIFICATION_BLOCKED, checked_at, observed_activation, blocked=(str(exc),))

    triggers: list[RollbackTrigger] = []
    blocked: list[str] = []
    checks: dict[str, str] = {}

    if observed_activation != baseline.activation:
        triggers.append(RollbackTrigger.ACTIVATION_IDENTITY_MISMATCH)
        checks["activation_identity"] = "FAIL"
    else:
        checks["activation_identity"] = "PASS"

    normalized_order = tuple(provider.casefold() for provider in routing.active_provider_order)
    expected = routing.expected_provider.casefold()
    routing_providers = (
        routing.selected_provider,
        *routing.active_provider_order,
        *routing.observed_providers,
        *(routing.allowed_fallback_providers or ()),
    )
    if routing.fallback_provider is not None:
        routing_providers += (routing.fallback_provider,)
    candidate_provider_present = any(
        provider.casefold()
        in {candidate.casefold() for candidate in CANDIDATE_ONLY_PROVIDER_IDENTITIES}
        for provider in routing_providers
    )
    if (
        routing.selected_provider.casefold() != expected
        or not normalized_order
        or normalized_order[0] != expected
        or normalized_order != tuple(provider.casefold() for provider in baseline.active_provider_order)
        or any(provider.casefold() not in normalized_order for provider in routing.observed_providers)
        or (routing.fallback_provider is not None and routing.fallback_provider.casefold() not in tuple(p.casefold() for p in routing.allowed_fallback_providers))
        or candidate_provider_present
        or routing.observed_candidate_ids
    ):
        triggers.append(RollbackTrigger.ROUTING_MISMATCH)
        checks["routing"] = "FAIL"
    else:
        checks["routing"] = "PASS"

    seen_identities: dict[str, tuple[str, str, str]] = {}
    if not observations:
        blocked.append("no post-activation match observations supplied")
        checks["data"] = "BLOCKED"
    else:
        data_failed = False
        for observation in observations:
            try:
                observation.validate(now=checked_at, max_age_seconds=max_observation_age_seconds)
            except (ProductionContractError, TypeError, ValueError) as exc:
                message = str(exc)
                data_failed = True
                if "1X2" in message or "odds" in message:
                    triggers.append(RollbackTrigger.INCOMPLETE_1X2)
                elif "stale" in message or "kickoff" in message or "signal_time" in message:
                    triggers.append(RollbackTrigger.STALE_OR_POST_KICKOFF_DATA)
                else:
                    triggers.append(RollbackTrigger.IDENTITY_PROVENANCE_MISMATCH)
            identity = (observation.event_identity, observation.home_team, observation.away_team)
            previous = seen_identities.setdefault(observation.fixture_key, identity)
            if previous != identity:
                triggers.append(RollbackTrigger.IDENTITY_PROVENANCE_MISMATCH)
            expected_fixture = baseline.expected_fixture_identities.get(observation.fixture_key)
            if expected_fixture is not None and expected_fixture != identity:
                triggers.append(RollbackTrigger.IDENTITY_PROVENANCE_MISMATCH)
            if observation.source_sha != baseline.activation.source_sha or observation.config_digest != baseline.activation.activation_digest:
                triggers.append(RollbackTrigger.IDENTITY_PROVENANCE_MISMATCH)
        checks["data"] = "FAIL" if data_failed or any(trigger in triggers for trigger in (
            RollbackTrigger.IDENTITY_PROVENANCE_MISMATCH,
            RollbackTrigger.STALE_OR_POST_KICKOFF_DATA,
            RollbackTrigger.INCOMPLETE_1X2,
        )) else "PASS"

    if runtime.unexpected_scheduler_runs or runtime.scheduler_expanded or runtime.football_scheduler_state != baseline.football_scheduler_state:
        triggers.append(RollbackTrigger.UNEXPECTED_SCHEDULER_ACTIVITY)
    if runtime.retry_count > runtime.retry_budget:
        triggers.append(RollbackTrigger.UNCONTROLLED_RETRIES)
    if runtime.publication_enabled or runtime.betting_enabled:
        triggers.append(RollbackTrigger.PUBLICATION_LEAKAGE)
    if runtime.financial_mutations or runtime.ledger_mutations:
        triggers.append(RollbackTrigger.FINANCIAL_LEDGER_MUTATION)
    if runtime.sealed_partition_mutations:
        triggers.append(RollbackTrigger.SEALED_PARTITION_MUTATION)
    if runtime.unapproved_signal_time_behavior:
        triggers.append(RollbackTrigger.UNAPPROVED_SIGNAL_TIME_BEHAVIOR)
    if (
        set(runtime.runtime_writer_state) != set(baseline.runtime_writer_state)
        or set(runtime.ledger_writer_state) != set(baseline.ledger_writer_state)
        or any(runtime.runtime_writer_state.get(name) not in {"active", "healthy"} for name in baseline.runtime_writer_state)
        or any(runtime.ledger_writer_state.get(name) not in {"active", "healthy"} for name in baseline.ledger_writer_state)
    ):
        triggers.append(RollbackTrigger.HEALTH_FAILURE)
    if runtime.scan_refresh_exit_code != 0 or runtime.worker_health not in _HEALTHY_RUNTIME_STATES or runtime.pwa_health not in _HEALTHY_RUNTIME_STATES or any(status not in _HEALTHY_RUNTIME_STATES for status in runtime.football_health_artifacts.values()):
        triggers.append(RollbackTrigger.HEALTH_FAILURE)
    checks["runtime"] = "FAIL" if any(trigger in triggers for trigger in (
        RollbackTrigger.UNEXPECTED_SCHEDULER_ACTIVITY, RollbackTrigger.UNCONTROLLED_RETRIES,
        RollbackTrigger.HEALTH_FAILURE, RollbackTrigger.PUBLICATION_LEAKAGE,
        RollbackTrigger.FINANCIAL_LEDGER_MUTATION, RollbackTrigger.SEALED_PARTITION_MUTATION,
        RollbackTrigger.UNAPPROVED_SIGNAL_TIME_BEHAVIOR,
    )) else "PASS"

    try:
        resources.validate()
    except (ProductionContractError, TypeError, ValueError) as exc:
        triggers.append(RollbackTrigger.UNEXPECTED_QUOTA_CONSUMPTION)
        checks["resources"] = "FAIL"
        blocked.append(str(exc))
    else:
        checks["resources"] = "PASS"
        if any(
            provider.casefold() != routing.expected_provider.casefold()
            for provider, count in resources.request_counts.items()
            if count
        ) or any(
            provider.casefold() != routing.expected_provider.casefold()
            for provider, spend in resources.spend_delta.items()
            if spend
        ):
            triggers.append(RollbackTrigger.UNEXPECTED_QUOTA_CONSUMPTION)

    status = VerificationStatus.ROLLBACK_REQUIRED if triggers else VerificationStatus.VERIFICATION_BLOCKED if blocked else VerificationStatus.PRODUCTION_VERIFIED
    return _report(status, checked_at, observed_activation, triggers=triggers, blocked=blocked, checks=checks)


def evaluate_publication_gate(
    report: VerificationReport,
    current_activation: ActivationIdentity,
    *,
    ceo_publication_authorization: str | None,
) -> PublicationGateResult:
    """Check eligibility only; this function never enables or publishes anything."""

    failures: list[str] = []
    try:
        report.activation.validate()
        current_activation.validate()
    except ProductionContractError as exc:
        failures.append(str(exc))
    if report.status is not VerificationStatus.PRODUCTION_VERIFIED:
        failures.append("production verification is not PRODUCTION_VERIFIED")
    if report.activation != current_activation:
        failures.append("activation identity or digest does not match verified runtime")
    if report.triggers:
        failures.append("a rollback trigger is active")
    if ceo_publication_authorization is None or not ceo_publication_authorization.strip():
        failures.append("separate CEO publication authorization is required")
    return PublicationGateResult(not failures, tuple(dict.fromkeys(failures)), report.report_id)


def rollback_decision(report: VerificationReport) -> dict[str, object]:
    """Return a safe, non-mutating rollback decision for an operator."""

    required = report.status is VerificationStatus.ROLLBACK_REQUIRED
    return {
        "decision": "ROLLBACK_REQUIRED" if required else "NO_ROLLBACK_DECISION",
        "report_id": report.report_id,
        "leave_active_authority": ACTIVE_FOOTBALL_AUTHORITY,
        "disable_candidate": True,
        "publication_enabled": False,
        "no_bet": True,
        "ledger_mutation": False,
        "scheduler_mutation": False,
    }
