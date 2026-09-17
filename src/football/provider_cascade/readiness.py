"""Persistent, secret-free provider quota and release-day readiness contracts.

This module is a read/write seam for operator-owned runtime state and a
zero-network decision boundary.  It does not create provider authority,
issue a controlled-shadow authorization, perform quota revalidation, or call
an adapter.  A caller must supply any current run authorization and all
readiness evidence explicitly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.football.production_contracts import ProductionContractError, _utc
from src.football.provider_cascade.contracts import (
    NetworkAuthorizationContract,
    ProviderCascadeConfig,
    ProviderConfig,
    ProviderState,
    QuotaSnapshot,
)
from src.football.provider_cascade.health import ProviderHealthRegistry
from src.football.top5_shadow_provider_redundancy import ProviderReadinessState
from src.runtime.paths import runtime_state_path
from src.utils.atomic_io import atomic_write_json

QUOTA_STATE_SCHEMA_VERSION = "top5-provider-quota-state-v1"
RELEASE_PREFLIGHT_READY = "READY_FOR_PROVIDER_RUN"
RELEASE_PREFLIGHT_BLOCKED = "BLOCKED"
DEFAULT_QUOTA_FRESHNESS_SECONDS = 31 * 24 * 60 * 60

_SOURCE_TIMESTAMP_CAPABILITY = {
    "the_odds_api": True,
}
_PARSER_CONTRACT = {
    "the_odds_api": "SUPPORTED",
}
_QUOTA_HEADER_CONTRACT = {
    "the_odds_api": "SUPPORTED",
}
_HEALTH_CONTRACT = {
    "the_odds_api": "SUPPORTED",
}
_IDENTITY_REQUIRED = frozenset()
_READY_STATES = frozenset(
    {
        ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION.value,
        ProviderReadinessState.REAL_OBSERVATION_VALIDATED.value,
    }
)
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")


def next_month_start(at: datetime) -> datetime:
    """Return the UTC calendar boundary used by The Odds API monthly quota."""

    current = _utc(at, "monthly quota observation")
    if current.month == 12:
        return datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)


def quota_reset_revalidation_eligible(
    quota: QuotaSnapshot,
    *,
    now: datetime,
) -> bool:
    """Return whether a known zero may be probed once by a future caller."""

    current = _utc(now, "quota revalidation now")
    return (
        quota.remaining == 0
        and quota.reset_at is not None
        and current >= quota.reset_at
    )


def _optional_datetime(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc(value, name)
    if not isinstance(value, str):
        raise ProductionContractError(f"{name} must be an ISO timestamp")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
    except ValueError as exc:
        raise ProductionContractError(f"{name} must be an ISO timestamp") from exc


def _optional_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProductionContractError(f"{name} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ProductionContractError(f"{name} must be a non-negative integer") from exc
    if result < 0:
        raise ProductionContractError(f"{name} must be a non-negative integer")
    return result


def _quota_from_payload(value: object) -> QuotaSnapshot:
    if isinstance(value, QuotaSnapshot):
        return value
    if not isinstance(value, Mapping):
        raise ProductionContractError("persisted quota must be a mapping")
    return QuotaSnapshot(
        used=_optional_int(value.get("used"), "quota.used"),
        remaining=_optional_int(value.get("remaining"), "quota.remaining"),
        reset_at=_optional_datetime(value.get("reset_at"), "quota.reset_at"),
        rate_limit=_optional_int(value.get("rate_limit"), "quota.rate_limit"),
        rate_remaining=_optional_int(
            value.get("rate_remaining"), "quota.rate_remaining"
        ),
        rate_reset_at=_optional_datetime(
            value.get("rate_reset_at"), "quota.rate_reset_at"
        ),
    )


@dataclass(frozen=True)
class PersistedProviderQuotaState:
    """One redacted provider quota observation stored outside source control."""

    provider: str
    observed_at: datetime | None
    quota: QuotaSnapshot
    state: str
    source: str

    def validate(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ProductionContractError("persisted quota provider is required")
        if not isinstance(self.state, str) or not self.state.strip():
            raise ProductionContractError("persisted quota state is required")
        if (
            not isinstance(self.source, str)
            or _SAFE_TEXT.fullmatch(self.source) is None
        ):
            raise ProductionContractError("persisted quota source is invalid")
        if self.observed_at is not None:
            _utc(self.observed_at, "persisted quota observed_at")
        self.quota.__post_init__()

    @classmethod
    def from_payload(cls, value: object) -> PersistedProviderQuotaState:
        if not isinstance(value, Mapping):
            raise ProductionContractError("persisted provider quota must be a mapping")
        quota_value = value.get("quota")
        if quota_value is None:
            quota_value = {
                key: value.get(key)
                for key in (
                    "used",
                    "remaining",
                    "reset_at",
                    "rate_limit",
                    "rate_remaining",
                    "rate_reset_at",
                )
                if key in value
            }
        result = cls(
            provider=str(value.get("provider", "")),
            observed_at=_optional_datetime(
                value.get("observed_at"), "persisted quota observed_at"
            ),
            quota=_quota_from_payload(quota_value or {}),
            state=str(value.get("state", "")),
            source=str(value.get("source", "")),
        )
        result.validate()
        return result

    def as_payload(self) -> dict[str, object]:
        self.validate()
        quota = self.quota.as_payload()
        return {
            "provider": self.provider,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "used": quota["used"],
            "remaining": quota["remaining"],
            "reset_at": quota["reset_at"],
            "rate_limit": quota["rate_limit"],
            "rate_remaining": quota["rate_remaining"],
            "rate_reset_at": quota["rate_reset_at"],
            "state": self.state,
            "source": self.source,
        }


class QuotaStateStore:
    """Atomic runtime store for redacted Top-5 quota evidence."""

    def __init__(self, path: str | Path | None = None) -> None:
        selected = (
            runtime_state_path(
                "data/cache/top5_provider_quota.json", require_external=True
            )
            if path is None
            else Path(path).expanduser()
        )
        resolved = selected.resolve()
        active_root = Path(__file__).resolve().parents[3]
        if resolved == active_root or active_root in resolved.parents:
            raise ProductionContractError(
                "Top-5 quota state must be outside the active checkout"
            )
        self.path = selected

    def load(self) -> dict[str, PersistedProviderQuotaState]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, TypeError, ValueError) as exc:
            raise ProductionContractError(
                "persisted Top-5 quota state is unreadable"
            ) from exc
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema_version") != QUOTA_STATE_SCHEMA_VERSION
        ):
            raise ProductionContractError("persisted Top-5 quota schema is unsupported")
        providers = raw.get("providers", {})
        if not isinstance(providers, Mapping):
            raise ProductionContractError(
                "persisted Top-5 quota providers are malformed"
            )
        result: dict[str, PersistedProviderQuotaState] = {}
        for name, value in providers.items():
            state = PersistedProviderQuotaState.from_payload(value)
            if state.provider != str(name):
                raise ProductionContractError("persisted quota provider key mismatch")
            result[state.provider] = state
        return result

    def save(self, states: Mapping[str, PersistedProviderQuotaState]) -> None:
        normalized: dict[str, dict[str, object]] = {}
        for name, state in states.items():
            if state.provider != name:
                raise ProductionContractError("quota state provider key mismatch")
            state.validate()
            normalized[name] = state.as_payload()
        atomic_write_json(
            self.path,
            {"schema_version": QUOTA_STATE_SCHEMA_VERSION, "providers": normalized},
            sort_keys=True,
        )

    def update(
        self,
        provider: str,
        *,
        quota: QuotaSnapshot,
        observed_at: datetime,
        state: str,
        source: str,
    ) -> PersistedProviderQuotaState:
        current = self.load()
        item = PersistedProviderQuotaState(
            provider=provider,
            observed_at=_utc(observed_at, "quota observed_at"),
            quota=quota,
            state=state,
            source=source,
        )
        item.validate()
        current[provider] = item
        self.save(current)
        return item


def _config_quota_state(config: ProviderConfig) -> PersistedProviderQuotaState:
    return PersistedProviderQuotaState(
        provider=config.name,
        observed_at=None,
        quota=config.initial_quota,
        state=(
            ProviderState.QUOTA_EXHAUSTED.value
            if config.initial_quota.remaining == 0
            else "CONFIGURED"
        ),
        source="operator_config",
    )


def _credential_present(
    config: ProviderConfig, credentials: Mapping[str, bool] | None
) -> bool:
    if credentials is not None and config.name in credentials:
        return credentials[config.name] is True
    if config.credential_available is not None:
        return config.credential_available is True
    if not config.credentials_required:
        return True
    import os

    return bool(config.credential_env) and all(
        bool(os.getenv(name, "").strip()) for name in config.credential_env
    )


def _identity_ready(provider: str, value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return provider not in _IDENTITY_REQUIRED
    return str(value).upper() in {"RESOLVED", "READY", "KNOWN", "TRUE"}


def _readiness_value(provider: str, value: object) -> str:
    if value is None:
        return ProviderReadinessState.CONTRACT_SUPPORTED.value
    try:
        return ProviderReadinessState(value).value
    except (TypeError, ValueError):
        return ProviderReadinessState.CONTRACT_SUPPORTED.value


def _contract_value(defaults: Mapping[str, str], provider: str, value: object) -> str:
    if value is None:
        return defaults.get(provider, "NOT_SUPPORTED")
    text = str(value).strip().upper()
    return text if text else "NOT_SUPPORTED"


def _health_value(
    health: ProviderHealthRegistry | None, provider: str, name: str
) -> Any:
    if health is None:
        return None
    record = health.get(provider)
    return getattr(record, name, None)


@dataclass(frozen=True)
class ProviderReadinessRecord:
    provider: str
    state: str
    credential_present: bool
    authorized_for_current_run: bool
    quota_known: bool
    remaining: int | None
    reset_at: datetime | None
    quota_evidence_age_seconds: float | None
    quota_observed_at: datetime | None
    rate_limit_state: str
    last_success_observation_time: datetime | None
    last_failure_class: str | None
    fallback_eligibility: bool
    source_timestamp_capability: bool
    identity_readiness: str
    parser_contract: str
    quota_header_contract: str
    health_contract: str
    quota_revalidation_eligible: bool
    candidate_only: bool

    def validate(self) -> None:
        if not self.provider.strip() or not self.state.strip():
            raise ProductionContractError("provider readiness identity is missing")
        if self.remaining is not None and self.remaining < 0:
            raise ProductionContractError("provider readiness remaining is invalid")
        if (
            self.quota_evidence_age_seconds is not None
            and self.quota_evidence_age_seconds < 0
        ):
            raise ProductionContractError("provider quota evidence age is invalid")
        if self.fallback_eligibility and not (
            self.credential_present
            and self.authorized_for_current_run
            and self.source_timestamp_capability
        ):
            raise ProductionContractError(
                "ineligible provider marked fallback-eligible"
            )
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                self.parser_contract,
                self.quota_header_contract,
                self.health_contract,
            )
        ):
            raise ProductionContractError("provider readiness contracts are missing")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider": self.provider,
            "state": self.state,
            "credential_present": self.credential_present,
            "authorized_for_current_run": self.authorized_for_current_run,
            "quota_known": self.quota_known,
            "remaining": self.remaining,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "quota_evidence_age_seconds": self.quota_evidence_age_seconds,
            "quota_observed_at": (
                self.quota_observed_at.isoformat() if self.quota_observed_at else None
            ),
            "rate_limit_state": self.rate_limit_state,
            "last_success_observation_time": (
                self.last_success_observation_time.isoformat()
                if self.last_success_observation_time
                else None
            ),
            "last_failure_class": self.last_failure_class,
            "fallback_eligibility": self.fallback_eligibility,
            "source_timestamp_capability": self.source_timestamp_capability,
            "identity_readiness": self.identity_readiness,
            "parser_contract": self.parser_contract,
            "quota_header_contract": self.quota_header_contract,
            "health_contract": self.health_contract,
            "quota_revalidation_eligible": self.quota_revalidation_eligible,
            "candidate_only": self.candidate_only,
        }


@dataclass(frozen=True)
class ProviderReadinessView:
    generated_at: datetime
    providers: tuple[ProviderReadinessRecord, ...]
    status: str
    blockers: tuple[str, ...] = ()
    network_called: bool = False

    def validate(self) -> None:
        _utc(self.generated_at, "provider readiness generated_at")
        if self.status not in {RELEASE_PREFLIGHT_READY, RELEASE_PREFLIGHT_BLOCKED}:
            raise ProductionContractError("provider readiness status is invalid")
        if self.network_called:
            raise ProductionContractError("provider readiness cannot call the network")
        if len({item.provider for item in self.providers}) != len(self.providers):
            raise ProductionContractError("provider readiness contains duplicates")
        for item in self.providers:
            item.validate()

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "generated_at": self.generated_at.isoformat(),
            "status": self.status,
            "blockers": list(self.blockers),
            "network_called": False,
            "providers": [item.as_payload() for item in self.providers],
        }


def build_provider_readiness_view(
    config: ProviderCascadeConfig,
    *,
    authorization: NetworkAuthorizationContract | None = None,
    quota_state_store: QuotaStateStore | None = None,
    credentials: Mapping[str, bool] | None = None,
    identity_readiness: Mapping[str, object] | None = None,
    readiness_states: Mapping[str, object] | None = None,
    parser_readiness: Mapping[str, object] | None = None,
    quota_header_readiness: Mapping[str, object] | None = None,
    health_readiness: Mapping[str, object] | None = None,
    health: ProviderHealthRegistry | None = None,
    now: datetime | None = None,
    quota_freshness_seconds: int = DEFAULT_QUOTA_FRESHNESS_SECONDS,
) -> ProviderReadinessView:
    """Build one deterministic secret-free provider readiness snapshot."""

    config.validate()
    if (
        isinstance(quota_freshness_seconds, bool)
        or not isinstance(quota_freshness_seconds, int)
        or quota_freshness_seconds <= 0
    ):
        raise ProductionContractError("quota freshness window is invalid")
    current = _utc(now or datetime.now(timezone.utc), "provider readiness now")
    persisted = quota_state_store.load() if quota_state_store is not None else {}
    identities = identity_readiness or {}
    states = readiness_states or {}
    parsers = parser_readiness or {}
    quota_headers = quota_header_readiness or {}
    health_contracts = health_readiness or {}
    records: list[ProviderReadinessRecord] = []
    for provider_name in config.provider_order:
        provider = config.providers[provider_name]
        quota_state = persisted.get(provider_name, _config_quota_state(provider))
        quota = quota_state.quota
        credential = _credential_present(provider, credentials)
        authorized = authorization is not None and authorization.permits(provider_name)
        reset_eligible = (
            quota_state.observed_at is not None
            and quota_state.observed_at <= current
            and quota_reset_revalidation_eligible(quota, now=current)
        )
        age = (
            (current - quota_state.observed_at).total_seconds()
            if quota_state.observed_at is not None
            else None
        )
        quota_fresh = age is not None and 0 <= age <= quota_freshness_seconds
        observed_state = str(quota_state.state)
        if not credential:
            observed_state = ProviderState.CREDENTIAL_MISSING.value
        elif quota.remaining == 0:
            observed_state = (
                "QUOTA_REVALIDATION_ELIGIBLE"
                if reset_eligible
                else ProviderState.QUOTA_EXHAUSTED.value
            )
        rate_state = "UNKNOWN"
        if quota.rate_remaining is not None:
            rate_state = (
                ProviderState.RATE_LIMITED.value
                if quota.rate_remaining == 0
                else ProviderState.AVAILABLE.value
            )
        health_failure = _health_value(health, provider_name, "last_failure_class")
        health_success = _health_value(health, provider_name, "last_success_at")
        source_capable = _SOURCE_TIMESTAMP_CAPABILITY.get(provider_name, False)
        identity_value = identities.get(provider_name)
        identity_state = (
            str(identity_value)
            if identity_value is not None
            else (
                "NOT_REQUIRED"
                if provider_name not in _IDENTITY_REQUIRED
                else "UNRESOLVED"
            )
        )
        ready_state = _readiness_value(provider_name, states.get(provider_name))
        parser_contract = _contract_value(
            _PARSER_CONTRACT, provider_name, parsers.get(provider_name)
        )
        quota_header_contract = _contract_value(
            _QUOTA_HEADER_CONTRACT, provider_name, quota_headers.get(provider_name)
        )
        health_contract = _contract_value(
            _HEALTH_CONTRACT, provider_name, health_contracts.get(provider_name)
        )
        fallback_eligible = (
            provider.enabled
            and credential
            and authorized
            and source_capable
            and _identity_ready(provider_name, identity_value)
            and ready_state in _READY_STATES
            and parser_contract == "SUPPORTED"
            and quota_header_contract == "SUPPORTED"
            and health_contract == "SUPPORTED"
            and (
                observed_state in {ProviderState.AVAILABLE.value, "CONFIGURED"}
                or reset_eligible
            )
            and quota.remaining is not None
            and (quota.remaining > 0 or reset_eligible)
            and (quota_fresh or reset_eligible)
            and (quota.rate_remaining is None or quota.rate_remaining > 0)
        )
        records.append(
            ProviderReadinessRecord(
                provider=provider_name,
                state=observed_state,
                credential_present=credential,
                authorized_for_current_run=authorized,
                quota_known=quota.remaining is not None,
                remaining=quota.remaining,
                reset_at=quota.reset_at,
                quota_evidence_age_seconds=max(0.0, age) if age is not None else None,
                quota_observed_at=quota_state.observed_at,
                rate_limit_state=rate_state,
                last_success_observation_time=health_success,
                last_failure_class=(
                    health_failure.value
                    if isinstance(health_failure, ProviderState)
                    else (str(health_failure) if health_failure else None)
                ),
                fallback_eligibility=fallback_eligible,
                source_timestamp_capability=source_capable,
                identity_readiness=identity_state,
                parser_contract=parser_contract,
                quota_header_contract=quota_header_contract,
                health_contract=health_contract,
                quota_revalidation_eligible=reset_eligible,
                candidate_only=provider.candidate_only,
            )
        )
    view = ProviderReadinessView(
        generated_at=current,
        providers=tuple(records),
        status=(
            RELEASE_PREFLIGHT_READY
            if any(item.fallback_eligibility for item in records)
            else RELEASE_PREFLIGHT_BLOCKED
        ),
        blockers=(),
    )
    view.validate()
    return view


@dataclass(frozen=True)
class ReleasePreflightResult:
    status: str
    blockers: tuple[str, ...]
    provider_blockers: Mapping[str, tuple[str, ...]]
    view: ProviderReadinessView
    network_called: bool = False

    def validate(self) -> None:
        if self.status not in {RELEASE_PREFLIGHT_READY, RELEASE_PREFLIGHT_BLOCKED}:
            raise ProductionContractError("release preflight status is invalid")
        if self.network_called:
            raise ProductionContractError("release preflight cannot call the network")
        self.view.validate()
        for provider, blockers in self.provider_blockers.items():
            if not provider.strip() or len(set(blockers)) != len(blockers):
                raise ProductionContractError(
                    "release preflight blockers are malformed"
                )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "status": self.status,
            "blockers": list(self.blockers),
            "provider_blockers": {
                provider: list(blockers)
                for provider, blockers in sorted(self.provider_blockers.items())
            },
            "network_called": False,
            "providers": [item.as_payload() for item in self.view.providers],
        }


def release_day_preflight(
    config: ProviderCascadeConfig,
    *,
    authorization: NetworkAuthorizationContract | None,
    quota_state_store: QuotaStateStore | None = None,
    credentials: Mapping[str, bool] | None = None,
    identity_readiness: Mapping[str, object] | None = None,
    readiness_states: Mapping[str, object] | None = None,
    parser_readiness: Mapping[str, object] | None = None,
    quota_header_readiness: Mapping[str, object] | None = None,
    health_readiness: Mapping[str, object] | None = None,
    health: ProviderHealthRegistry | None = None,
    now: datetime | None = None,
    quota_freshness_seconds: int = DEFAULT_QUOTA_FRESHNESS_SECONDS,
) -> ReleasePreflightResult:
    """Check whether a future authorized run may proceed, with zero calls."""

    if (
        isinstance(quota_freshness_seconds, bool)
        or not isinstance(quota_freshness_seconds, int)
        or quota_freshness_seconds <= 0
    ):
        raise ProductionContractError("quota freshness window is invalid")
    view = build_provider_readiness_view(
        config,
        authorization=authorization,
        quota_state_store=quota_state_store,
        credentials=credentials,
        identity_readiness=identity_readiness,
        readiness_states=readiness_states,
        parser_readiness=parser_readiness,
        quota_header_readiness=quota_header_readiness,
        health_readiness=health_readiness,
        health=health,
        now=now,
        quota_freshness_seconds=quota_freshness_seconds,
    )
    provider_blockers: dict[str, tuple[str, ...]] = {}
    global_blockers: list[str] = []
    eligible_provider_count = 0
    if config.global_request_budget <= 0 or config.per_run_cap <= 0:
        global_blockers.append("REQUEST_BUDGET_INVALID")
    for record, provider in zip(view.providers, config.provider_order, strict=True):
        config_entry = config.providers[provider]
        blockers: list[str] = []
        if not config_entry.enabled:
            continue
        if not record.credential_present:
            blockers.append("CREDENTIAL_MISSING")
        if not record.authorized_for_current_run:
            blockers.append("PROVIDER_NOT_AUTHORIZED")
        if not record.source_timestamp_capability:
            blockers.append("SOURCE_TIMESTAMP_UNSUPPORTED")
        if record.identity_readiness not in {
            "NOT_REQUIRED",
            "RESOLVED",
            "READY",
            "KNOWN",
            "TRUE",
        }:
            blockers.append("IDENTITY_UNRESOLVED")
        readiness = _readiness_value(provider, (readiness_states or {}).get(provider))
        if readiness not in _READY_STATES:
            blockers.append("PROVIDER_NOT_READY")
        if (
            record.state not in {ProviderState.AVAILABLE.value, "CONFIGURED"}
            and not record.quota_revalidation_eligible
        ):
            blockers.append("PROVIDER_STATE_BLOCKED")
        if record.parser_contract != "SUPPORTED":
            blockers.append("PARSER_CONTRACT_UNSUPPORTED")
        if record.quota_header_contract != "SUPPORTED":
            blockers.append("QUOTA_HEADER_CONTRACT_UNSUPPORTED")
        if record.health_contract != "SUPPORTED":
            blockers.append("HEALTH_CONTRACT_UNSUPPORTED")
        if (
            not record.quota_known
            or record.quota_evidence_age_seconds is None
            or record.quota_evidence_age_seconds > quota_freshness_seconds
        ):
            blockers.append("QUOTA_STATE_STALE")
        if record.remaining == 0 and not record.quota_revalidation_eligible:
            blockers.append("QUOTA_EXHAUSTED")
        if record.rate_limit_state == ProviderState.RATE_LIMITED.value:
            blockers.append("RATE_LIMITED")
        if blockers:
            provider_blockers[provider] = tuple(dict.fromkeys(blockers))
        else:
            eligible_provider_count += 1
    if eligible_provider_count == 0:
        global_blockers.append("NO_ELIGIBLE_PROVIDER")
    blockers = tuple(dict.fromkeys(global_blockers))
    status = RELEASE_PREFLIGHT_READY if not blockers else RELEASE_PREFLIGHT_BLOCKED
    result = ReleasePreflightResult(
        status=status,
        blockers=blockers,
        provider_blockers=provider_blockers,
        view=view,
        network_called=False,
    )
    result.validate()
    return result


__all__ = [
    "DEFAULT_QUOTA_FRESHNESS_SECONDS",
    "QUOTA_STATE_SCHEMA_VERSION",
    "RELEASE_PREFLIGHT_BLOCKED",
    "RELEASE_PREFLIGHT_READY",
    "PersistedProviderQuotaState",
    "ProviderReadinessRecord",
    "ProviderReadinessView",
    "QuotaStateStore",
    "ReleasePreflightResult",
    "build_provider_readiness_view",
    "next_month_start",
    "quota_reset_revalidation_eligible",
    "release_day_preflight",
]
