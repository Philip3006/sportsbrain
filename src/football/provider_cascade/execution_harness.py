"""Guarded, test-only execution harness for a Top-5 controlled shadow run.

The preparation contract is intentionally non-executable.  This module adds
the narrow runtime seam needed after an independently supplied CEO
authorization artifact exists.  It accepts only an injected transport; it
does not construct authorization, call a provider adapter, issue a
qualification receipt, create a prediction, publish, bet, or mutate runtime
state outside the supplied in-memory idempotency store.

The harness is suitable for contract tests and for a future separately
reviewed transport integration.  ``FakeControlledShadowTransport`` is the
only transport implementation in this package.  ``RealProviderTransport`` is
an explicit interface and is rejected by the harness unless a future caller
deliberately supplies a test-marked implementation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from math import isfinite
from threading import RLock
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from src.football.production_contracts import ProductionContractError
from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    NormalizedOddsObservation,
    ObservationCompleteness,
    ProviderState,
    QuotaSnapshot,
    TimingProvenance,
    TransportCapability,
)
from src.football.provider_cascade.preparation import (
    PREPARATION_READY,
    ControlledShadowRunPreparationV1,
    PreparationContractError,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    ObservationEvidenceKind,
    ProviderTimestampProvenance,
    RealProviderObservation,
)
from src.football.top5_provider_cascade_validation import (
    CASCADE_PROVIDER_ORDER,
    BudgetDecision,
    CascadeAttempt,
    CascadeEvidence,
    CascadeOutcome,
    CascadeProvenance,
    CascadeQuotaSnapshot,
    CascadeSafety,
    ExecutionMode,
    MarketPhase,
    RequestCostClassification,
    SkippedProvider,
    evidence_digest,
)
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA
from src.football.top5_shadow_provider_redundancy import ProviderReadinessState

AUTHORIZATION_SCHEMA_VERSION = "controlled-shadow-run-authorization-v1"
CAPTURE_ATTESTATION_SCHEMA_VERSION = "controlled-shadow-capture-attestation-v1"
CONTROLLED_SHADOW_RUN = "CONTROLLED_SHADOW_RUN"
NO_BET_BANNER = "NO BET"
NO_PUBLICATION_BANNER = "NO PUBLICATION"
NO_PRODUCTION_BANNER = "NO PRODUCTION ACTIVATION"
EXECUTION_BANNERS = (
    "CONTROLLED SHADOW ONLY",
    "CEO AUTHORIZATION REQUIRED",
    NO_BET_BANNER,
    NO_PUBLICATION_BANNER,
    NO_PRODUCTION_BANNER,
    "B2 QUALIFICATION REQUIRED AFTER CAPTURE",
)


class HarnessContractError(ProductionContractError):
    """Malformed or unsafe harness input."""


class HarnessExecutionBlocked(HarnessContractError):
    """Fail-closed execution refusal."""


class ExecutionStatus(str, Enum):
    OBSERVED = "OBSERVED"
    NO_OBSERVATION = "NO_OBSERVATION"
    REPLAYED = "REPLAYED"


class ProviderAction(str, Enum):
    FIXTURE_DISCOVERY = "FIXTURE_DISCOVERY"
    ODDS = "ODDS"
    ODDS_AND_EVENT_DISCOVERY = "ODDS_AND_EVENT_DISCOVERY"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarnessContractError(f"{name} must be non-empty text")
    return value.strip()


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if (
        len(text) < 40
        or len(text) > 64
        or any(char not in "0123456789abcdef" for char in text.lower())
    ):
        raise HarnessContractError(f"{name} must be a hexadecimal digest")
    return text.lower()


def _utc(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HarnessContractError(f"{name} must be an ISO timestamp") from exc
    else:
        raise HarnessContractError(f"{name} must be timezone-aware")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HarnessContractError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _nonnegative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HarnessContractError(f"{name} must be a non-negative integer")
    return value


def _nonnegative_number(value: object, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise HarnessContractError(f"{name} must be numeric") from exc
    if number < 0 or not isfinite(number):
        raise HarnessContractError(f"{name} must be finite and non-negative")
    return number


def _canonical(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc(value, "timestamp").isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        _canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise HarnessContractError(f"{name} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _scope_mapping(value: Mapping[str, object], name: str) -> dict[str, str]:
    return {key: _text(item, f"{name}[{key}]") for key, item in value.items()}


def _fixture_from_preparation(
    preparation: ControlledShadowRunPreparationV1,
) -> dict[str, object]:
    fixture = dict(preparation.fixture_identity)
    required = ("fixture_key", "league_code", "home_team", "away_team", "kickoff")
    for key in required:
        _text(fixture.get(key), f"preparation fixture.{key}")
    return fixture


def _as_preparation(
    value: ControlledShadowRunPreparationV1 | Mapping[str, object],
) -> ControlledShadowRunPreparationV1:
    if isinstance(value, ControlledShadowRunPreparationV1):
        preparation = value
    else:
        try:
            preparation = ControlledShadowRunPreparationV1.from_payload(value)
        except (PreparationContractError, TypeError, ValueError) as exc:
            raise HarnessContractError("preparation artifact is invalid") from exc
    try:
        preparation.validate()
    except PreparationContractError as exc:
        raise HarnessContractError("preparation artifact is invalid") from exc
    return preparation


@dataclass(frozen=True)
class ControlledShadowRunAuthorizationV1:
    """Caller-supplied, immutable authorization for exactly one prepared run.

    There is deliberately no ``issue`` or ``create`` method.  A CEO-controlled
    authority boundary supplies this object; the harness only validates it.
    """

    authorization_id: str
    controlled_shadow_run_id: str
    authorization_nonce: str
    ceo_authorization_identity: str
    preparation_id: str
    preparation_digest: str
    qualification_session_id: str
    fixture_key: str
    league: str
    home_team: str
    away_team: str
    kickoff: datetime
    configured_provider_order: tuple[str, ...]
    timing_policy_digest: str
    maximum_total_network_requests: int
    maximum_total_quota_cost_units: float
    per_provider_maximums: Mapping[str, Mapping[str, object]]
    adapter_version_scope: Mapping[str, str]
    adapter_source_sha_scope: Mapping[str, str]
    issued_at: datetime
    expires_at: datetime
    zero_monetary_spend: bool
    no_bet: bool
    publication: bool
    production_activation: bool
    issued_for: str = CONTROLLED_SHADOW_RUN
    schema_version: str = AUTHORIZATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "configured_provider_order", tuple(self.configured_provider_order)
        )
        object.__setattr__(
            self,
            "per_provider_maximums",
            MappingProxyType(
                {
                    str(provider): MappingProxyType(dict(limits))
                    for provider, limits in self.per_provider_maximums.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "adapter_version_scope",
            MappingProxyType(dict(self.adapter_version_scope)),
        )
        object.__setattr__(
            self,
            "adapter_source_sha_scope",
            MappingProxyType(dict(self.adapter_source_sha_scope)),
        )

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authorization_id": self.authorization_id,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "authorization_nonce": self.authorization_nonce,
            "ceo_authorization_identity": self.ceo_authorization_identity,
            "preparation_id": self.preparation_id,
            "preparation_digest": self.preparation_digest,
            "qualification_session_id": self.qualification_session_id,
            "fixture_key": self.fixture_key,
            "league": self.league,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": _utc(self.kickoff, "kickoff").isoformat(),
            "configured_provider_order": list(self.configured_provider_order),
            "timing_policy_digest": self.timing_policy_digest,
            "maximum_total_network_requests": self.maximum_total_network_requests,
            "maximum_total_quota_cost_units": self.maximum_total_quota_cost_units,
            "per_provider_maximums": {
                provider: dict(limits)
                for provider, limits in self.per_provider_maximums.items()
            },
            "adapter_version_scope": dict(self.adapter_version_scope),
            "adapter_source_sha_scope": dict(self.adapter_source_sha_scope),
            "issued_at": _utc(self.issued_at, "issued_at").isoformat(),
            "expires_at": _utc(self.expires_at, "expires_at").isoformat(),
            "zero_monetary_spend": self.zero_monetary_spend,
            "no_bet": self.no_bet,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "issued_for": self.issued_for,
        }

    @property
    def authorization_digest(self) -> str:
        return _digest(self._payload_without_digest())

    def validate(
        self,
        preparation: ControlledShadowRunPreparationV1
        | Mapping[str, object]
        | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        for name, value in (
            ("authorization_id", self.authorization_id),
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("authorization_nonce", self.authorization_nonce),
            ("ceo_authorization_identity", self.ceo_authorization_identity),
            ("preparation_id", self.preparation_id),
            ("qualification_session_id", self.qualification_session_id),
            ("fixture_key", self.fixture_key),
            ("league", self.league),
            ("home_team", self.home_team),
            ("away_team", self.away_team),
            ("timing_policy_digest", self.timing_policy_digest),
        ):
            _text(value, name)
        if self.schema_version != AUTHORIZATION_SCHEMA_VERSION:
            raise HarnessContractError("unsupported authorization schema")
        if self.issued_for != CONTROLLED_SHADOW_RUN:
            raise HarnessContractError(
                "authorization is not for a controlled shadow run"
            )
        _sha(self.preparation_digest, "preparation_digest")
        _sha(self.timing_policy_digest, "timing_policy_digest")
        _utc(self.kickoff, "kickoff")
        issued = _utc(self.issued_at, "issued_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= issued:
            raise HarnessContractError("authorization expiration must follow issuance")
        current = _utc(now or datetime.now(timezone.utc), "authorization now")
        if current < issued or current >= expires:
            raise HarnessExecutionBlocked("authorization is expired or not yet valid")
        if not self.configured_provider_order or len(
            set(self.configured_provider_order)
        ) != len(self.configured_provider_order):
            raise HarnessContractError("authorization provider order must be unique")
        if any(
            provider not in CASCADE_PROVIDER_ORDER
            for provider in self.configured_provider_order
        ):
            raise HarnessContractError("authorization contains an unsupported provider")
        _nonnegative_int(
            self.maximum_total_network_requests, "maximum_total_network_requests"
        )
        _nonnegative_number(
            self.maximum_total_quota_cost_units, "maximum_total_quota_cost_units"
        )
        for name, value, expected in (
            ("zero_monetary_spend", self.zero_monetary_spend, True),
            ("no_bet", self.no_bet, True),
            ("publication", self.publication, False),
            ("production_activation", self.production_activation, False),
        ):
            if value is not expected:
                raise HarnessExecutionBlocked(f"unsafe authorization flag: {name}")
        if not set(self.configured_provider_order).issubset(self.per_provider_maximums):
            raise HarnessContractError(
                "per-provider authorization maximums are incomplete"
            )
        expected_providers = set(self.configured_provider_order)
        if (
            set(self.adapter_version_scope) != expected_providers
            or set(self.adapter_source_sha_scope) != expected_providers
        ):
            raise HarnessContractError("adapter provenance scope is incomplete")
        for provider in self.configured_provider_order:
            limits = self.per_provider_maximums[provider]
            if not isinstance(limits, Mapping):
                raise HarnessContractError(
                    "per-provider authorization maximum is malformed"
                )
            _nonnegative_int(
                limits.get("network_requests"), f"{provider}.network_requests"
            )
            _nonnegative_number(
                limits.get("quota_cost_units"), f"{provider}.quota_cost_units"
            )
            _text(self.adapter_version_scope[provider], f"{provider}.adapter_version")
            _sha(
                self.adapter_source_sha_scope[provider],
                f"{provider}.adapter_source_sha",
            )
        if preparation is not None:
            plan = _as_preparation(preparation)
            if plan.preparation_status != PREPARATION_READY:
                raise HarnessExecutionBlocked(
                    "preparation is not READY_FOR_CEO_CONTROLLED_SHADOW_AUTHORIZATION"
                )
            fixture = _fixture_from_preparation(plan)
            exact_pairs = (
                (self.preparation_id, plan.preparation_id, "preparation ID"),
                (
                    self.preparation_digest,
                    plan.preparation_digest,
                    "preparation digest",
                ),
                (self.fixture_key, fixture["fixture_key"], "fixture key"),
                (self.league, fixture["league_code"], "league league"),
                (self.home_team, fixture["home_team"], "home team"),
                (self.away_team, fixture["away_team"], "away team"),
                (
                    _utc(self.kickoff, "kickoff"),
                    _utc(fixture["kickoff"], "preparation kickoff"),
                    "kickoff",
                ),
                (
                    self.configured_provider_order,
                    plan.configured_provider_order,
                    "provider order",
                ),
                (
                    self.timing_policy_digest,
                    plan.timing_policy_digest,
                    "timing policy digest",
                ),
                (
                    self.maximum_total_network_requests,
                    plan.maximum_total_network_requests,
                    "total request cap",
                ),
                (
                    self.maximum_total_quota_cost_units,
                    plan.maximum_total_quota_cost_units,
                    "total quota cap",
                ),
            )
            for actual, expected, label in exact_pairs:
                if actual != expected:
                    raise HarnessExecutionBlocked(
                        f"authorization does not match preparation: {label}"
                    )
            expected_limits = {
                provider: {
                    "network_requests": limits["network_requests"],
                    "quota_cost_units": limits["quota_cost_units"],
                }
                for provider, limits in plan.per_provider_maximums.items()
            }
            actual_limits = {
                provider: {
                    "network_requests": limits["network_requests"],
                    "quota_cost_units": limits["quota_cost_units"],
                }
                for provider, limits in self.per_provider_maximums.items()
            }
            if (
                set(self.per_provider_maximums) != set(expected_limits)
                or actual_limits != expected_limits
            ):
                raise HarnessExecutionBlocked(
                    "authorization does not match provider budgets"
                )

    def as_payload(self) -> dict[str, object]:
        self.validate(now=self.issued_at)
        return {
            **self._payload_without_digest(),
            "authorization_digest": self.authorization_digest,
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> ControlledShadowRunAuthorizationV1:
        if not isinstance(payload, Mapping):
            raise HarnessContractError("authorization artifact must be a mapping")
        try:
            raw_limits = _mapping(
                payload.get("per_provider_maximums"), "per_provider_maximums"
            )
            result = cls(
                authorization_id=_text(
                    payload.get("authorization_id"), "authorization_id"
                ),
                controlled_shadow_run_id=_text(
                    payload.get("controlled_shadow_run_id"), "controlled_shadow_run_id"
                ),
                authorization_nonce=_text(
                    payload.get("authorization_nonce"), "authorization_nonce"
                ),
                ceo_authorization_identity=_text(
                    payload.get("ceo_authorization_identity"),
                    "ceo_authorization_identity",
                ),
                preparation_id=_text(payload.get("preparation_id"), "preparation_id"),
                preparation_digest=_text(
                    payload.get("preparation_digest"), "preparation_digest"
                ),
                qualification_session_id=_text(
                    payload.get("qualification_session_id"), "qualification_session_id"
                ),
                fixture_key=_text(payload.get("fixture_key"), "fixture_key"),
                league=_text(payload.get("league"), "league"),
                home_team=_text(payload.get("home_team"), "home_team"),
                away_team=_text(payload.get("away_team"), "away_team"),
                kickoff=_utc(payload.get("kickoff"), "kickoff"),
                configured_provider_order=tuple(
                    payload.get("configured_provider_order", ())
                ),
                timing_policy_digest=_text(
                    payload.get("timing_policy_digest"), "timing_policy_digest"
                ),
                maximum_total_network_requests=_nonnegative_int(
                    payload.get("maximum_total_network_requests"),
                    "maximum_total_network_requests",
                ),
                maximum_total_quota_cost_units=_nonnegative_number(
                    payload.get("maximum_total_quota_cost_units"),
                    "maximum_total_quota_cost_units",
                ),
                per_provider_maximums={
                    provider: _mapping(limits, f"{provider}.limits")
                    for provider, limits in raw_limits.items()
                },
                adapter_version_scope=_scope_mapping(
                    _mapping(
                        payload.get("adapter_version_scope"), "adapter_version_scope"
                    ),
                    "adapter_version_scope",
                ),
                adapter_source_sha_scope=_scope_mapping(
                    _mapping(
                        payload.get("adapter_source_sha_scope"),
                        "adapter_source_sha_scope",
                    ),
                    "adapter_source_sha_scope",
                ),
                issued_at=_utc(payload.get("issued_at"), "issued_at"),
                expires_at=_utc(payload.get("expires_at"), "expires_at"),
                zero_monetary_spend=payload.get("zero_monetary_spend"),
                no_bet=payload.get("no_bet"),
                publication=payload.get("publication"),
                production_activation=payload.get("production_activation"),
                issued_for=payload.get("issued_for", CONTROLLED_SHADOW_RUN),
                schema_version=payload.get(
                    "schema_version", AUTHORIZATION_SCHEMA_VERSION
                ),
            )
            result.validate(now=result.issued_at)
            supplied_digest = payload.get("authorization_digest")
            if (
                supplied_digest is not None
                and supplied_digest != result.authorization_digest
            ):
                raise HarnessContractError("authorization digest mismatch")
            return result
        except (TypeError, ValueError, KeyError) as exc:
            raise HarnessContractError("authorization artifact is malformed") from exc


@dataclass(frozen=True)
class ProviderTransportRequest:
    """Secret-free request identity passed to an injected transport."""

    controlled_shadow_run_id: str
    provider: str
    action: ProviderAction | str
    request_identity: str
    fixture_key: str
    league: str
    home_team: str
    away_team: str
    kickoff: datetime
    market_type: str = MARKET_PREMATCH_1X2

    def validate(self) -> None:
        for name, value in (
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("provider", self.provider),
            ("request_identity", self.request_identity),
            ("fixture_key", self.fixture_key),
            ("league", self.league),
            ("home_team", self.home_team),
            ("away_team", self.away_team),
        ):
            _text(value, name)
        try:
            ProviderAction(self.action)
        except (TypeError, ValueError) as exc:
            raise HarnessContractError("transport action is unknown") from exc
        _utc(self.kickoff, "request kickoff")
        if self.market_type != MARKET_PREMATCH_1X2:
            raise HarnessContractError("only pre-match 1X2 may be requested")


@dataclass(frozen=True)
class ProviderTransportResponse:
    """Redacted response envelope supplied by a test or future adapter seam."""

    outcome: CascadeOutcome | str = CascadeOutcome.PROVIDER_UNAVAILABLE
    provider_event_id: str = ""
    provider_request_id: str = ""
    provider_record_id: str = ""
    identity_state: str = "RESOLVED"
    home_odds: float | None = None
    draw_odds: float | None = None
    away_odds: float | None = None
    bookmaker_identity: str | None = None
    source_identity: str | None = None
    source_timestamp: datetime | None = None
    adapter_version: str = ""
    adapter_source_sha: str = ""
    raw_response_digest: str = ""
    normalized_record_digest: str = ""
    source_timing_provenance: str = "SOURCE_TIMESTAMP"
    delayed_observation: bool = False
    delay_seconds: int | None = None
    pagination_total: int | None = None
    body_error_taxonomy: tuple[str, ...] = ()
    runner_mapping: Mapping[str, str] = field(default_factory=dict)
    app_session_prerequisites: bool | None = None
    failure_detail: str = ""
    evidence_kind: ObservationEvidenceKind | str = ObservationEvidenceKind.MOCK
    raw_metadata: Mapping[str, object] = field(default_factory=dict)


@runtime_checkable
class RealProviderTransport(Protocol):
    """Explicit future transport interface; no implementation is shipped here."""

    transport_capability: TransportCapability

    def execute(
        self, request: ProviderTransportRequest
    ) -> ProviderTransportResponse: ...


TransportResponseFactory = Callable[
    [ProviderTransportRequest], ProviderTransportResponse
]


class FakeControlledShadowTransport:
    """Deterministic injected transport used by acceptance tests only."""

    transport_capability = TransportCapability.TEST_INJECTED

    def __init__(
        self,
        responses: Mapping[
            tuple[str, str], ProviderTransportResponse | TransportResponseFactory
        ]
        | None = None,
        *,
        default: ProviderTransportResponse | TransportResponseFactory | None = None,
    ) -> None:
        self._responses = dict(responses or {})
        self._default = default or ProviderTransportResponse()
        self.calls: list[ProviderTransportRequest] = []

    @property
    def network_call_count(self) -> int:
        return len(self.calls)

    def execute(self, request: ProviderTransportRequest) -> ProviderTransportResponse:
        request.validate()
        self.calls.append(request)
        response = self._responses.get(
            (request.provider, ProviderAction(request.action).value), self._default
        )
        if callable(response):
            response = response(request)
        if not isinstance(response, ProviderTransportResponse):
            raise HarnessContractError("fake transport returned an invalid response")
        return response


@dataclass(frozen=True)
class ControlledShadowCaptureAttestationV1:
    """Deterministic run evidence; it is not authority and cannot qualify."""

    controlled_shadow_run_id: str
    authorization_id: str
    authorization_digest: str
    preparation_id: str
    preparation_digest: str
    qualification_session_id: str
    fixture_key: str
    league: str
    kickoff: datetime
    configured_provider_order: tuple[str, ...]
    attempted_providers: tuple[str, ...]
    provider_identity: str
    selected_provider: str | None
    provider_event_id: str
    provider_request_id: str
    observation_id: str | None
    observation_digest: str | None
    cascade_evidence_digest: str
    raw_response_digest: str
    normalized_record_digest: str
    adapter_version: str
    adapter_source_sha: str
    timing_policy_digest: str
    timing_evidence: Mapping[str, object]
    discovery_request_count: int
    odds_request_count: int
    network_request_count: int
    quota_cost_units: float
    failure_evidence: tuple[str, ...]
    zero_monetary_spend: bool
    no_bet: bool
    publication: bool
    production_activation: bool
    captured_at: datetime
    capture_digest: str = ""
    schema_version: str = CAPTURE_ATTESTATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "configured_provider_order",
            tuple(self.configured_provider_order),
        )
        object.__setattr__(
            self,
            "attempted_providers",
            tuple(self.attempted_providers),
        )
        object.__setattr__(
            self,
            "timing_evidence",
            MappingProxyType(dict(self.timing_evidence)),
        )
        if not self.capture_digest:
            object.__setattr__(self, "capture_digest", self.computed_capture_digest)

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "authorization_id": self.authorization_id,
            "authorization_digest": self.authorization_digest,
            "preparation_id": self.preparation_id,
            "preparation_digest": self.preparation_digest,
            "qualification_session_id": self.qualification_session_id,
            "fixture_key": self.fixture_key,
            "league": self.league,
            "kickoff": _utc(self.kickoff, "kickoff").isoformat(),
            "configured_provider_order": list(self.configured_provider_order),
            "attempted_providers": list(self.attempted_providers),
            "provider_identity": self.provider_identity,
            "selected_provider": self.selected_provider,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "observation_id": self.observation_id,
            "observation_digest": self.observation_digest,
            "cascade_evidence_digest": self.cascade_evidence_digest,
            "raw_response_digest": self.raw_response_digest,
            "normalized_record_digest": self.normalized_record_digest,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "timing_policy_digest": self.timing_policy_digest,
            "timing_evidence": dict(self.timing_evidence),
            "discovery_request_count": self.discovery_request_count,
            "odds_request_count": self.odds_request_count,
            "network_request_count": self.network_request_count,
            "quota_cost_units": self.quota_cost_units,
            "failure_evidence": list(self.failure_evidence),
            "zero_monetary_spend": self.zero_monetary_spend,
            "no_bet": self.no_bet,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "captured_at": _utc(self.captured_at, "captured_at").isoformat(),
        }

    @property
    def computed_capture_digest(self) -> str:
        return _digest(self._payload_without_digest())

    def validate(self) -> None:
        for name, value in (
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("authorization_id", self.authorization_id),
            ("preparation_id", self.preparation_id),
            ("qualification_session_id", self.qualification_session_id),
            ("fixture_key", self.fixture_key),
            ("league", self.league),
            ("provider_identity", self.provider_identity),
            ("provider_event_id", self.provider_event_id),
            ("provider_request_id", self.provider_request_id),
            ("adapter_version", self.adapter_version),
        ):
            _text(value, name)
        _sha(self.authorization_digest, "authorization_digest")
        _sha(self.preparation_digest, "preparation_digest")
        _sha(self.timing_policy_digest, "timing_policy_digest")
        _sha(self.cascade_evidence_digest, "cascade_evidence_digest")
        _sha(self.raw_response_digest, "raw_response_digest")
        _sha(self.normalized_record_digest, "normalized_record_digest")
        if self.observation_digest is not None:
            _sha(self.observation_digest, "observation_digest")
        _utc(self.kickoff, "kickoff")
        _utc(self.captured_at, "captured_at")
        if self.schema_version != CAPTURE_ATTESTATION_SCHEMA_VERSION:
            raise HarnessContractError("unsupported capture attestation schema")
        if not self.configured_provider_order or any(
            provider not in self.configured_provider_order
            for provider in self.attempted_providers
        ):
            raise HarnessContractError("attestation provider order is invalid")
        if self.provider_identity not in self.attempted_providers:
            raise HarnessContractError("attestation provider was not attempted")
        if (
            self.selected_provider is not None
            and self.selected_provider not in self.attempted_providers
        ):
            raise HarnessContractError("selected provider was not attempted")
        if (
            self.selected_provider is not None
            and self.provider_identity == self.selected_provider
            and self.observation_digest is None
        ):
            raise HarnessContractError("selected observation digest is required")
        for name, value in (
            ("discovery_request_count", self.discovery_request_count),
            ("odds_request_count", self.odds_request_count),
            ("network_request_count", self.network_request_count),
        ):
            _nonnegative_int(value, name)
        _nonnegative_number(self.quota_cost_units, "quota_cost_units")
        if (
            self.network_request_count
            != self.discovery_request_count + self.odds_request_count
        ):
            raise HarnessContractError("attestation request counters do not reconcile")
        if not isinstance(self.timing_evidence, Mapping):
            raise HarnessContractError("timing evidence must be a mapping")
        for name, value, expected in (
            ("zero_monetary_spend", self.zero_monetary_spend, True),
            ("no_bet", self.no_bet, True),
            ("publication", self.publication, False),
            ("production_activation", self.production_activation, False),
        ):
            if value is not expected:
                raise HarnessExecutionBlocked(f"unsafe attestation flag: {name}")
        if self.capture_digest and self.capture_digest != self.computed_capture_digest:
            raise HarnessContractError("capture digest mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            **self._payload_without_digest(),
            "capture_digest": self.computed_capture_digest,
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> ControlledShadowCaptureAttestationV1:
        if not isinstance(payload, Mapping):
            raise HarnessContractError("capture attestation must be a mapping")
        result = cls(
            controlled_shadow_run_id=payload.get("controlled_shadow_run_id", ""),
            authorization_id=payload.get("authorization_id", ""),
            authorization_digest=payload.get("authorization_digest", ""),
            preparation_id=payload.get("preparation_id", ""),
            preparation_digest=payload.get("preparation_digest", ""),
            qualification_session_id=payload.get("qualification_session_id", ""),
            fixture_key=payload.get("fixture_key", ""),
            league=payload.get("league", ""),
            kickoff=_utc(payload.get("kickoff"), "kickoff"),
            configured_provider_order=tuple(
                payload.get("configured_provider_order", ())
            ),
            attempted_providers=tuple(payload.get("attempted_providers", ())),
            provider_identity=payload.get("provider_identity", ""),
            selected_provider=payload.get("selected_provider"),
            provider_event_id=payload.get("provider_event_id", ""),
            provider_request_id=payload.get("provider_request_id", ""),
            observation_id=payload.get("observation_id"),
            observation_digest=payload.get("observation_digest"),
            cascade_evidence_digest=payload.get("cascade_evidence_digest", ""),
            raw_response_digest=payload.get("raw_response_digest", ""),
            normalized_record_digest=payload.get("normalized_record_digest", ""),
            adapter_version=payload.get("adapter_version", ""),
            adapter_source_sha=payload.get("adapter_source_sha", ""),
            timing_policy_digest=payload.get("timing_policy_digest", ""),
            timing_evidence=_mapping(payload.get("timing_evidence"), "timing_evidence"),
            discovery_request_count=payload.get("discovery_request_count", -1),
            odds_request_count=payload.get("odds_request_count", -1),
            network_request_count=payload.get("network_request_count", -1),
            quota_cost_units=payload.get("quota_cost_units", -1),
            failure_evidence=tuple(payload.get("failure_evidence", ())),
            zero_monetary_spend=payload.get("zero_monetary_spend"),
            no_bet=payload.get("no_bet"),
            publication=payload.get("publication"),
            production_activation=payload.get("production_activation"),
            captured_at=_utc(payload.get("captured_at"), "captured_at"),
            capture_digest=payload.get("capture_digest", ""),
            schema_version=payload.get(
                "schema_version", CAPTURE_ATTESTATION_SCHEMA_VERSION
            ),
        )
        result.validate()
        return result


@dataclass(frozen=True)
class ControlledShadowExecutionResult:
    status: ExecutionStatus
    controlled_shadow_run_id: str
    authorization: ControlledShadowRunAuthorizationV1
    cascade_evidence: CascadeEvidence
    attestations: tuple[ControlledShadowCaptureAttestationV1, ...]
    attestation: ControlledShadowCaptureAttestationV1
    observation: RealProviderObservation | None
    normalized_observation: NormalizedOddsObservation | None
    attempted_providers: tuple[str, ...]
    selected_provider: str | None
    discovery_request_count: int
    odds_request_count: int
    network_request_count: int
    quota_cost_units: float
    failure_evidence: tuple[str, ...]
    banners: tuple[str, ...] = EXECUTION_BANNERS

    @property
    def observed(self) -> bool:
        return self.observation is not None

    def validate(self) -> None:
        self.authorization.validate(now=self.authorization.issued_at)
        self.cascade_evidence.validate_structural()
        if not self.attestations:
            raise HarnessContractError("every run requires attempt attestations")
        for item in self.attestations:
            item.validate()
        self.attestation.validate()
        if (
            self.network_request_count
            != self.discovery_request_count + self.odds_request_count
        ):
            raise HarnessContractError("execution request counters do not reconcile")
        if self.selected_provider is None and self.observation is not None:
            raise HarnessContractError(
                "observation cannot exist without selected provider"
            )
        if self.observation is not None:
            self.observation.validate_structural()
        if self.normalized_observation is not None:
            self.normalized_observation.validate(require_fresh=False)
        if tuple(self.banners) != EXECUTION_BANNERS:
            raise HarnessContractError("execution safety banners are incomplete")


@dataclass(frozen=True)
class RunValidationReport:
    valid: bool
    controlled_shadow_run_id: str
    preparation_id: str
    preparation_digest: str
    configured_provider_order: tuple[str, ...]
    banners: tuple[str, ...] = EXECUTION_BANNERS

    def as_payload(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "preparation_id": self.preparation_id,
            "preparation_digest": self.preparation_digest,
            "configured_provider_order": list(self.configured_provider_order),
            "banners": list(self.banners),
        }


@dataclass(frozen=True)
class _Claim:
    fingerprint: str
    result: ControlledShadowExecutionResult | None = None


class InMemoryControlledShadowRuntimeState:
    """Atomic claim/completion abstraction for one-process tests."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._claims: dict[str, _Claim] = {}

    def claim(
        self, run_id: str, fingerprint: str
    ) -> ControlledShadowExecutionResult | None:
        with self._lock:
            existing = self._claims.get(run_id)
            if existing is None:
                self._claims[run_id] = _Claim(fingerprint=fingerprint)
                return None
            if existing.fingerprint != fingerprint:
                raise HarnessExecutionBlocked(
                    "conflicting replay for controlled shadow run"
                )
            if existing.result is None:
                raise HarnessExecutionBlocked(
                    "controlled shadow run is already claimed"
                )
            return existing.result

    def complete(
        self, run_id: str, fingerprint: str, result: ControlledShadowExecutionResult
    ) -> None:
        with self._lock:
            existing = self._claims.get(run_id)
            if existing is None or existing.fingerprint != fingerprint:
                raise HarnessExecutionBlocked(
                    "completion does not match the atomic run claim"
                )
            self._claims[run_id] = _Claim(fingerprint=fingerprint, result=result)


@dataclass
class _BudgetLedger:
    maximum_requests: int
    maximum_cost: float
    per_provider: Mapping[str, Mapping[str, object]]
    requests: int = 0
    cost: float = 0.0
    provider_requests: dict[str, int] = field(default_factory=dict)
    provider_cost: dict[str, float] = field(default_factory=dict)

    def reserve(self, provider: str, cost: float) -> bool:
        limits = self.per_provider[provider]
        request_limit = _nonnegative_int(
            limits["network_requests"], f"{provider}.network_requests"
        )
        cost_limit = _nonnegative_number(
            limits["quota_cost_units"], f"{provider}.quota_cost_units"
        )
        current_requests = self.provider_requests.get(provider, 0)
        current_cost = self.provider_cost.get(provider, 0.0)
        if self.requests + 1 > self.maximum_requests:
            return False
        if self.cost + cost > self.maximum_cost + 1e-9:
            return False
        if current_requests + 1 > request_limit:
            return False
        if current_cost + cost > cost_limit + 1e-9:
            return False
        self.requests += 1
        self.cost += cost
        self.provider_requests[provider] = current_requests + 1
        self.provider_cost[provider] = current_cost + cost
        return True


@dataclass
class _ProviderRun:
    provider: str
    attempt_response: ProviderTransportResponse
    attempt_request: ProviderTransportRequest
    attempt_start: datetime
    attempt_end: datetime
    network_called: bool
    request_count_for_attempt: int
    quota_cost_for_attempt: float
    discovery_count: int
    odds_count: int
    failure_evidence: list[str]
    selected: bool = False


def _outcome(
    value: object, default: CascadeOutcome = CascadeOutcome.PROVIDER_UNAVAILABLE
) -> CascadeOutcome:
    try:
        return CascadeOutcome(value)
    except (TypeError, ValueError):
        return default


def _failure_outcome(response: ProviderTransportResponse) -> CascadeOutcome:
    outcome = _outcome(response.outcome)
    if outcome is CascadeOutcome.SUCCESS:
        return CascadeOutcome.MALFORMED
    return outcome


class ControlledShadowExecutionHarness:
    """Sequential, fail-closed executor behind an injected transport."""

    def __init__(
        self,
        *,
        runtime_state: InMemoryControlledShadowRuntimeState | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime_state = runtime_state or InMemoryControlledShadowRuntimeState()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def validate_run(
        self,
        preparation: ControlledShadowRunPreparationV1 | Mapping[str, object],
        authorization: ControlledShadowRunAuthorizationV1 | Mapping[str, object],
    ) -> RunValidationReport:
        plan = _as_preparation(preparation)
        auth = (
            authorization
            if isinstance(authorization, ControlledShadowRunAuthorizationV1)
            else ControlledShadowRunAuthorizationV1.from_payload(authorization)
        )
        auth.validate(plan, now=self.clock())
        if plan.builder2_receipt_issuer_exposed:
            raise HarnessExecutionBlocked("preparation exposes a qualification issuer")
        if plan.builder2_receipt_required_before_capture:
            raise HarnessExecutionBlocked(
                "qualification receipt cannot be required before capture"
            )
        return RunValidationReport(
            valid=True,
            controlled_shadow_run_id=auth.controlled_shadow_run_id,
            preparation_id=plan.preparation_id,
            preparation_digest=plan.preparation_digest,
            configured_provider_order=tuple(plan.configured_provider_order),
        )

    def dry_run(
        self,
        preparation: ControlledShadowRunPreparationV1 | Mapping[str, object],
        authorization: ControlledShadowRunAuthorizationV1 | Mapping[str, object],
    ) -> RunValidationReport:
        """Validate only; this method never asks a transport to do anything."""

        return self.validate_run(preparation, authorization)

    def execute(
        self,
        preparation: ControlledShadowRunPreparationV1 | Mapping[str, object],
        authorization: ControlledShadowRunAuthorizationV1 | Mapping[str, object],
        *,
        transport: RealProviderTransport,
    ) -> ControlledShadowExecutionResult:
        plan = _as_preparation(preparation)
        auth = (
            authorization
            if isinstance(authorization, ControlledShadowRunAuthorizationV1)
            else ControlledShadowRunAuthorizationV1.from_payload(authorization)
        )
        now = self.clock()
        self.validate_run(plan, auth)
        capability = getattr(transport, "transport_capability", None)
        if capability is not TransportCapability.TEST_INJECTED:
            raise HarnessExecutionBlocked(
                "only an explicitly injected test transport is accepted"
            )
        fingerprint = _digest(
            {
                "authorization": auth.authorization_digest,
                "preparation": plan.preparation_digest,
                "fixture": _fixture_from_preparation(plan),
            }
        )
        replay = self.runtime_state.claim(auth.controlled_shadow_run_id, fingerprint)
        if replay is not None:
            return ControlledShadowExecutionResult(
                status=ExecutionStatus.REPLAYED,
                controlled_shadow_run_id=replay.controlled_shadow_run_id,
                authorization=replay.authorization,
                cascade_evidence=replay.cascade_evidence,
                attestations=replay.attestations,
                attestation=replay.attestation,
                observation=replay.observation,
                normalized_observation=replay.normalized_observation,
                attempted_providers=replay.attempted_providers,
                selected_provider=replay.selected_provider,
                discovery_request_count=replay.discovery_request_count,
                odds_request_count=replay.odds_request_count,
                network_request_count=replay.network_request_count,
                quota_cost_units=replay.quota_cost_units,
                failure_evidence=replay.failure_evidence,
            )
        result = self._execute_claimed(plan, auth, transport, now)
        self.runtime_state.complete(auth.controlled_shadow_run_id, fingerprint, result)
        return result

    def _execute_claimed(
        self,
        plan: ControlledShadowRunPreparationV1,
        auth: ControlledShadowRunAuthorizationV1,
        transport: RealProviderTransport,
        run_start: datetime,
    ) -> ControlledShadowExecutionResult:
        manifests = {item.provider: item for item in plan.providers}
        budget = _BudgetLedger(
            maximum_requests=auth.maximum_total_network_requests,
            maximum_cost=auth.maximum_total_quota_cost_units,
            per_provider=auth.per_provider_maximums,
        )
        attempts: list[CascadeAttempt] = []
        skipped: list[SkippedProvider] = []
        attempt_runs: list[_ProviderRun] = []
        attempted_providers: list[str] = []
        failures: list[str] = []
        discovery_count = 0
        odds_count = 0
        selected_provider: str | None = None
        selected_response: ProviderTransportResponse | None = None
        selected_request: ProviderTransportRequest | None = None
        step = 0

        for order_index, provider in enumerate(auth.configured_provider_order):
            if selected_provider is not None:
                skipped.append(
                    SkippedProvider(
                        provider,
                        order_index,
                        "prior provider accepted exact observation",
                    )
                )
                continue
            manifest = manifests.get(provider)
            if manifest is None or not manifest.enabled:
                response = ProviderTransportResponse(
                    outcome=CascadeOutcome.CONFIG_DISABLED,
                    failure_detail="provider is not enabled in preparation",
                )
                run = self._synthetic_run(
                    plan,
                    auth,
                    provider,
                    ProviderAction.ODDS,
                    response,
                    run_start,
                    step,
                    0,
                    0,
                    0,
                )
            elif manifest.credential_present is not True:
                response = ProviderTransportResponse(
                    outcome=CascadeOutcome.CREDENTIAL_MISSING,
                    failure_detail="credential presence was not explicitly true",
                )
                run = self._synthetic_run(
                    plan,
                    auth,
                    provider,
                    ProviderAction.ODDS,
                    response,
                    run_start,
                    step,
                    0,
                    0,
                    0,
                )
            elif (
                not manifest.executable or manifest.expected_network_request_count == 0
            ):
                response = ProviderTransportResponse(
                    outcome=(
                        CascadeOutcome.QUOTA_EXHAUSTED
                        if "QUOTA_EXHAUSTED"
                        in manifest.expected_failure_classifications
                        else CascadeOutcome.PROVIDER_UNAVAILABLE
                    ),
                    failure_detail=manifest.reason_executable_or_blocked,
                )
                run = self._synthetic_run(
                    plan,
                    auth,
                    provider,
                    ProviderAction.ODDS,
                    response,
                    run_start,
                    step,
                    0,
                    0,
                    0,
                    quota_override=manifest.current_known_quota,
                )
            else:
                run = self._run_provider(
                    plan,
                    auth,
                    manifest,
                    transport,
                    budget,
                    run_start,
                    step,
                )
            step += 1
            attempted_providers.append(provider)
            discovery_count += run.discovery_count
            odds_count += run.odds_count
            failures.extend(run.failure_evidence)
            attempt_runs.append(run)
            attempt = self._to_cascade_attempt(
                plan, auth, run, order_index, len(attempts)
            )
            attempts.append(attempt)
            if (
                _outcome(run.attempt_response.outcome) is CascadeOutcome.SUCCESS
                and run.odds_count
            ):
                selected_provider = provider
                selected_response = run.attempt_response
                selected_request = run.attempt_request
                run.selected = True

        if not attempts:
            raise HarnessExecutionBlocked(
                "no configured provider was available for a cascade attempt"
            )
        evidence = self._build_cascade_evidence(
            plan, auth, attempts, skipped, run_start, selected_provider
        )
        evidence.validate_structural()
        cascade_digest = evidence_digest(evidence)
        observation: RealProviderObservation | None = None
        normalized: NormalizedOddsObservation | None = None
        if (
            selected_provider is not None
            and selected_response is not None
            and selected_request is not None
        ):
            observation, normalized = self._build_observation(
                plan,
                auth,
                selected_provider,
                selected_response,
                selected_request,
                evidence,
                cascade_digest,
                run_start,
                attempts,
            )
        total_requests = discovery_count + odds_count
        total_cost = budget.cost
        if selected_provider is None:
            failures.append(
                "NO_OBSERVATION: all configured providers failed or were preflight-rejected"
            )
        attestations = tuple(
            self._build_attestation(
                plan,
                auth,
                run,
                attempted_providers,
                selected_provider,
                observation,
                evidence,
                cascade_digest,
                run_start,
                discovery_count,
                odds_count,
                total_requests,
                total_cost,
                tuple(failures),
            )
            for run in attempt_runs
        )
        final_attestation = attestations[-1]
        result = ControlledShadowExecutionResult(
            status=ExecutionStatus.OBSERVED
            if observation is not None
            else ExecutionStatus.NO_OBSERVATION,
            controlled_shadow_run_id=auth.controlled_shadow_run_id,
            authorization=auth,
            cascade_evidence=evidence,
            attestations=attestations,
            attestation=final_attestation,
            observation=observation,
            normalized_observation=normalized,
            attempted_providers=tuple(attempted_providers),
            selected_provider=selected_provider,
            discovery_request_count=discovery_count,
            odds_request_count=odds_count,
            network_request_count=total_requests,
            quota_cost_units=total_cost,
            failure_evidence=tuple(dict.fromkeys(failures)),
        )
        result.validate()
        return result

    def _run_provider(
        self,
        plan: ControlledShadowRunPreparationV1,
        auth: ControlledShadowRunAuthorizationV1,
        manifest: object,
        transport: RealProviderTransport,
        budget: _BudgetLedger,
        run_start: datetime,
        step: int,
    ) -> _ProviderRun:
        provider = manifest.provider
        actions = tuple(
            item for item in manifest.planned_actions if item.network_request_count
        )
        if not actions:
            return self._synthetic_run(
                plan,
                auth,
                provider,
                ProviderAction.ODDS,
                ProviderTransportResponse(
                    outcome=CascadeOutcome.PROVIDER_UNAVAILABLE,
                    failure_detail="no executable action was planned",
                ),
                run_start,
                step,
                0,
                0,
                0,
            )
        discovery_done = 0
        odds_done = 0
        failure_evidence: list[str] = []
        last_request: ProviderTransportRequest | None = None
        last_response: ProviderTransportResponse | None = None
        last_start = run_start + timedelta(seconds=step * 2)
        last_end = last_start
        action_step = 0
        for action in actions:
            action_kind = (
                ProviderAction.ODDS
                if action.action_class in {"ODDS", "ODDS_AND_EVENT_DISCOVERY"}
                else ProviderAction.FIXTURE_DISCOVERY
            )
            request_id = f"harness-request:{_digest((auth.controlled_shadow_run_id, provider, action_kind.value, action.sequence))[:32]}"
            request = ProviderTransportRequest(
                controlled_shadow_run_id=auth.controlled_shadow_run_id,
                provider=provider,
                action=action_kind,
                request_identity=request_id,
                fixture_key=plan.fixture_identity["fixture_key"],
                league=plan.league,
                home_team=plan.fixture_identity["home_team"],
                away_team=plan.fixture_identity["away_team"],
                kickoff=plan.kickoff,
            )
            request.validate()
            cost = float(action.quota_cost_units)
            quota = dict(manifest.current_known_quota)
            remaining = quota.get("remaining")
            if (
                remaining is None
                or (isinstance(remaining, int) and remaining < 1)
                or not budget.reserve(provider, cost)
            ):
                outcome = (
                    CascadeOutcome.QUOTA_EXHAUSTED
                    if remaining == 0
                    else CascadeOutcome.BUDGET_REJECTED
                )
                failure_evidence.append(
                    f"{provider}:{action_kind.value}:{outcome.value}:preflight_rejected"
                )
                synthetic = ProviderTransportResponse(
                    outcome=outcome,
                    provider_request_id=request_id,
                    failure_detail="atomic pre-call budget or quota check rejected the request",
                )
                last_request = request
                last_response = synthetic
                last_start = run_start + timedelta(seconds=(step + action_step) * 2)
                last_end = last_start
                action_step += 1
                break
            started = run_start + timedelta(seconds=(step + action_step) * 2)
            try:
                response = transport.execute(request)
            except Exception as exc:  # noqa: BLE001 - fail closed on adapter boundary
                response = ProviderTransportResponse(
                    outcome=CascadeOutcome.PROVIDER_UNAVAILABLE,
                    provider_request_id=request_id,
                    failure_detail=f"transport failure: {type(exc).__name__}",
                )
            finished = started + timedelta(milliseconds=1)
            last_request = request
            last_response = response
            last_start = started
            last_end = finished
            if (
                response.adapter_version
                and response.adapter_version != auth.adapter_version_scope[provider]
            ):
                failure_evidence.append(
                    f"{provider}:{action_kind.value}:ADAPTER_VERSION_MISMATCH"
                )
                last_response = ProviderTransportResponse(
                    **{
                        **response.__dict__,
                        "outcome": CascadeOutcome.MALFORMED,
                        "failure_detail": "response adapter version is outside the authorized scope",
                    }
                )
                break
            if (
                response.adapter_source_sha
                and response.adapter_source_sha.lower()
                != auth.adapter_source_sha_scope[provider].lower()
            ):
                failure_evidence.append(
                    f"{provider}:{action_kind.value}:ADAPTER_SOURCE_SHA_MISMATCH"
                )
                last_response = ProviderTransportResponse(
                    **{
                        **response.__dict__,
                        "outcome": CascadeOutcome.MALFORMED,
                        "failure_detail": "response adapter source is outside the authorized scope",
                    }
                )
                break
            if action_kind is ProviderAction.FIXTURE_DISCOVERY:
                discovery_done += 1
                if _outcome(
                    response.outcome
                ) is not CascadeOutcome.SUCCESS or response.identity_state not in {
                    "RESOLVED",
                    "UNIQUE",
                }:
                    failure_evidence.append(
                        f"{provider}:FIXTURE_DISCOVERY:{response.identity_state or _outcome(response.outcome).value}"
                    )
                    break
            else:
                odds_done += 1
                if _outcome(response.outcome) is not CascadeOutcome.SUCCESS:
                    failure_evidence.append(
                        f"{provider}:ODDS:{_failure_outcome(response).value}"
                    )
                elif (
                    response.pagination_total is not None
                    and response.pagination_total > 1
                ):
                    failure_evidence.append(f"{provider}:ODDS:PAGINATION_RISK")
                    last_response = ProviderTransportResponse(
                        **{
                            **response.__dict__,
                            "outcome": CascadeOutcome.MALFORMED,
                            "failure_detail": "pagination total exceeds one",
                        }
                    )
                elif response.body_error_taxonomy:
                    failure_evidence.append(
                        f"{provider}:ODDS:BODY_ERROR:{','.join(response.body_error_taxonomy)}"
                    )
                    last_response = ProviderTransportResponse(
                        **{
                            **response.__dict__,
                            "outcome": CascadeOutcome.MALFORMED,
                            "failure_detail": "provider body error taxonomy is non-empty",
                        }
                    )
                elif response.source_timestamp is None:
                    failure_evidence.append(f"{provider}:ODDS:SOURCE_TIMESTAMP_MISSING")
                    last_response = ProviderTransportResponse(
                        **{
                            **response.__dict__,
                            "outcome": CascadeOutcome.STALE,
                            "failure_detail": "capture time cannot substitute for provider freshness",
                        }
                    )
                elif provider == "betfair_delayed" and (
                    response.delayed_observation is not True
                    or response.delay_seconds is None
                    or response.delay_seconds < 0
                    or response.app_session_prerequisites is not True
                    or not response.runner_mapping
                ):
                    failure_evidence.append(
                        f"{provider}:ODDS:BETFAIR_PREREQUISITES_OR_DELAYED_RUNNER_EVIDENCE_MISSING"
                    )
                    last_response = ProviderTransportResponse(
                        **{
                            **response.__dict__,
                            "outcome": CascadeOutcome.MALFORMED,
                            "failure_detail": "Betfair delayed app/session, runner mapping, and delay evidence are required",
                        }
                    )
                break
            action_step += 1
        if last_request is None or last_response is None:
            raise HarnessExecutionBlocked(
                "provider execution produced no auditable response"
            )
        if (
            _outcome(last_response.outcome) is not CascadeOutcome.SUCCESS
            and not failure_evidence
        ):
            failure_evidence.append(
                f"{provider}:{_failure_outcome(last_response).value}"
            )
        return _ProviderRun(
            provider,
            last_response,
            last_request,
            last_start,
            last_end,
            bool(discovery_done or odds_done),
            discovery_done + odds_done,
            budget.provider_cost.get(provider, 0.0),
            discovery_done,
            odds_done,
            failure_evidence,
        )

    def _synthetic_run(
        self,
        plan: ControlledShadowRunPreparationV1,
        auth: ControlledShadowRunAuthorizationV1,
        provider: str,
        action: ProviderAction,
        response: ProviderTransportResponse,
        run_start: datetime,
        step: int,
        discovery_count: int,
        odds_count: int,
        cost: float,
        *,
        quota_override: Mapping[str, object] | None = None,
    ) -> _ProviderRun:
        request = ProviderTransportRequest(
            controlled_shadow_run_id=auth.controlled_shadow_run_id,
            provider=provider,
            action=action,
            request_identity=f"harness-preflight:{_digest((auth.controlled_shadow_run_id, provider, step))[:32]}",
            fixture_key=plan.fixture_identity["fixture_key"],
            league=plan.league,
            home_team=plan.fixture_identity["home_team"],
            away_team=plan.fixture_identity["away_team"],
            kickoff=plan.kickoff,
        )
        when = run_start + timedelta(seconds=step * 2)
        detail = response.failure_detail or _failure_outcome(response).value
        quota = quota_override or {}
        if quota.get("remaining") == 0:
            detail = f"{detail}; known quota remaining=0; zero transport calls"
        return _ProviderRun(
            provider,
            response,
            request,
            when,
            when,
            False,
            0,
            cost,
            discovery_count,
            odds_count,
            [f"{provider}:{_outcome(response.outcome).value}:{detail}"],
        )

    def _to_cascade_attempt(
        self,
        plan: ControlledShadowRunPreparationV1,
        auth: ControlledShadowRunAuthorizationV1,
        run: _ProviderRun,
        order_index: int,
        attempt_index: int,
    ) -> CascadeAttempt:
        response = run.attempt_response
        provider = run.provider
        manifest = next(item for item in plan.providers if item.provider == provider)
        outcome = _outcome(response.outcome)
        source_timestamp = response.source_timestamp or (
            run.attempt_end - timedelta(seconds=1)
            if outcome is CascadeOutcome.SUCCESS
            else run.attempt_end
        )
        adapter_version = (
            response.adapter_version or auth.adapter_version_scope[provider]
        )
        raw_digest = response.raw_response_digest or _digest(
            {
                "provider": provider,
                "request": run.attempt_request.request_identity,
                "response": response.failure_detail,
                "outcome": outcome.value,
            }
        )
        request_id = (
            response.provider_request_id or run.attempt_request.request_identity
        )
        provider_record_id = (
            response.provider_record_id
            or response.provider_event_id
            or f"{provider}:attempt:{attempt_index}"
        )
        before = manifest.current_known_quota
        quota_before = CascadeQuotaSnapshot(
            True, before.get("used"), before.get("remaining")
        )
        quota_after = (
            quota_before
            if not run.network_called
            else CascadeQuotaSnapshot(
                True, before.get("used"), max(0, int(before.get("remaining") or 0) - 1)
            )
        )
        return CascadeAttempt(
            league=plan.league,
            fixture_key=plan.fixture_identity["fixture_key"],
            home_team=plan.fixture_identity["home_team"],
            away_team=plan.fixture_identity["away_team"],
            kickoff=plan.kickoff,
            configured_provider_order=tuple(auth.configured_provider_order),
            provider_attempt_index=attempt_index,
            fallback_depth=attempt_index,
            provider_identity=provider,
            network_called=run.network_called,
            start_timestamp=run.attempt_start,
            end_timestamp=run.attempt_end,
            capture_timestamp=run.attempt_end,
            outcome=outcome,
            failure_classification=outcome
            if outcome is not CascadeOutcome.SUCCESS
            else None,
            market_type="h2h_1x2",
            home_odds=response.home_odds,
            draw_odds=response.draw_odds,
            away_odds=response.away_odds,
            bookmaker_identity=response.bookmaker_identity,
            source_identity=response.source_identity or provider,
            market_phase=MarketPhase.PRE_MATCH,
            source_timestamp=source_timestamp,
            request_latency_ms=max(
                0, int((run.attempt_end - run.attempt_start).total_seconds() * 1000)
            ),
            quota_before=quota_before,
            quota_after=quota_after,
            preflight_allowed=run.network_called,
            budget_decision=BudgetDecision.ALLOWED
            if run.network_called
            else BudgetDecision.REJECTED,
            request_cost_classification=RequestCostClassification.QUOTA_CONSUMING_REQUEST,
            network_request_count=int(run.network_called),
            quota_cost_units=run.quota_cost_for_attempt if run.network_called else 0.0,
            credentials_available=manifest.credential_present,
            provider_record_id=provider_record_id,
            adapter_version=adapter_version,
            raw_record_digest=raw_digest,
            request_identity=request_id,
            provider_readiness_state=(
                manifest.readiness_state
                if outcome is CascadeOutcome.SUCCESS
                else ProviderReadinessState.CONTRACT_SUPPORTED.value
            ),
            source_timing_provenance=response.source_timing_provenance
            or TimingProvenance.SOURCE_TIMESTAMP.value,
        )

    def _build_cascade_evidence(
        self,
        plan: ControlledShadowRunPreparationV1,
        auth: ControlledShadowRunAuthorizationV1,
        attempts: Sequence[CascadeAttempt],
        skipped: Sequence[SkippedProvider],
        run_start: datetime,
        selected_provider: str | None,
    ) -> CascadeEvidence:
        source_sha = _digest(auth.adapter_source_sha_scope)
        artifact_sha = _digest(
            {
                "run": auth.controlled_shadow_run_id,
                "preparation": plan.preparation_digest,
                "attempts": [item.as_payload() for item in attempts],
            }
        )
        provenance = CascadeProvenance(
            evidence_id=f"controlled-shadow-evidence:{auth.controlled_shadow_run_id}",
            artifact_id=f"controlled-shadow-artifact:{auth.controlled_shadow_run_id}",
            artifact_sha=artifact_sha,
            source_sha=source_sha,
            research_sha=FROZEN_RESEARCH_SHA,
            candidate_id="controlled-shadow-candidate-only",
            model_identity="unbound-controlled-shadow-model-slot",
            generated_at=run_start,
        )
        return CascadeEvidence(
            provenance=provenance,
            configured_provider_order=tuple(auth.configured_provider_order),
            execution_mode=ExecutionMode.SEQUENTIAL,
            attempts=tuple(attempts),
            skipped_providers=tuple(skipped),
            selected_provider=selected_provider,
            prediction_input_allowed=False,
            safety=CascadeSafety(True, False, False, False, False, False, False),
        )

    def _build_observation(
        self,
        plan: ControlledShadowRunPreparationV1,
        auth: ControlledShadowRunAuthorizationV1,
        provider: str,
        response: ProviderTransportResponse,
        request: ProviderTransportRequest,
        evidence: CascadeEvidence,
        cascade_digest: str,
        run_start: datetime,
        attempts: Sequence[CascadeAttempt],
    ) -> tuple[RealProviderObservation, NormalizedOddsObservation]:
        if _outcome(response.outcome) is not CascadeOutcome.SUCCESS:
            raise HarnessExecutionBlocked(
                "a failed provider cannot produce an observation"
            )
        if any(
            value is None
            for value in (response.home_odds, response.draw_odds, response.away_odds)
        ):
            raise HarnessExecutionBlocked(
                "complete 1X2 odds are required for an observation"
            )
        captured_at = attempts[-1].capture_timestamp
        source_timestamp = response.source_timestamp or captured_at - timedelta(
            seconds=1
        )
        adapter_version = (
            response.adapter_version or auth.adapter_version_scope[provider]
        )
        adapter_sha = (
            response.adapter_source_sha or auth.adapter_source_sha_scope[provider]
        )
        raw_digest = response.raw_response_digest or _digest(
            {
                "provider": provider,
                "request": request.request_identity,
                "odds": [response.home_odds, response.draw_odds, response.away_odds],
            }
        )
        normalized_digest = response.normalized_record_digest or _digest(
            {
                "provider": provider,
                "fixture": plan.fixture_identity["fixture_key"],
                "odds": [response.home_odds, response.draw_odds, response.away_odds],
                "source": source_timestamp.isoformat(),
            }
        )
        provider_event_id = (
            response.provider_event_id
            or f"{provider}:event:{_digest(request.request_identity)[:16]}"
        )
        provider_request_id = response.provider_request_id or request.request_identity
        observation_id = f"controlled-shadow-observation:{_digest((auth.controlled_shadow_run_id, provider_request_id, normalized_digest))[:32]}"
        attestation = self._canonical_attestation_payload(
            auth,
            provider,
            plan.fixture_identity["fixture_key"],
            provider_event_id,
            provider_request_id,
            adapter_version,
            adapter_sha,
            cascade_digest,
            raw_digest,
            normalized_digest,
            captured_at,
        )
        observation = RealProviderObservation(
            observation_id=observation_id,
            qualification_session_id=auth.qualification_session_id,
            evidence_kind=response.evidence_kind,
            provider_identity=provider,
            provider_event_id=provider_event_id,
            provider_request_id=provider_request_id,
            league=plan.league,
            fixture_key=plan.fixture_identity["fixture_key"],
            home_team=plan.fixture_identity["home_team"],
            away_team=plan.fixture_identity["away_team"],
            kickoff=plan.kickoff,
            market_type="h2h_1x2",
            market_phase=MarketPhase.PRE_MATCH.value,
            home_odds=response.home_odds,
            draw_odds=response.draw_odds,
            away_odds=response.away_odds,
            bookmaker_identity=response.bookmaker_identity or "injected-bookmaker",
            source_identity=response.source_identity or provider,
            source_timestamp=source_timestamp,
            provider_timestamp_provenance=ProviderTimestampProvenance.PROVIDER_SOURCE_TIMESTAMP,
            captured_at=captured_at,
            request_started_at=attempts[-1].start_timestamp,
            request_finished_at=attempts[-1].end_timestamp,
            latency_ms=attempts[-1].request_latency_ms,
            adapter_version=adapter_version,
            adapter_source_sha=adapter_sha,
            raw_response_digest=raw_digest,
            normalized_record_digest=normalized_digest,
            cascade_evidence=evidence,
            quota_before=attempts[-1].quota_before.quota_remaining
            if attempts[-1].quota_before
            else None,
            quota_after=attempts[-1].quota_after.quota_remaining
            if attempts[-1].quota_after
            else None,
            quota_cost_units=attempts[-1].quota_cost_units or 0.0,
            network_request_count=1,
            delayed_observation=response.delayed_observation,
            capture_attestation=attestation,
        )
        observation.validate_structural()
        normalized = NormalizedOddsObservation(
            league_code=plan.league,
            fixture_key=plan.fixture_identity["fixture_key"],
            provider_fixture_id=provider_event_id,
            home_team=plan.fixture_identity["home_team"],
            away_team=plan.fixture_identity["away_team"],
            kickoff_utc=plan.kickoff,
            market_type=MARKET_PREMATCH_1X2,
            home_odds=response.home_odds,
            draw_odds=response.draw_odds,
            away_odds=response.away_odds,
            provider_identity=provider,
            bookmaker_identity=response.bookmaker_identity or "injected-bookmaker",
            source_timestamp=source_timestamp,
            captured_at=captured_at,
            request_identity=provider_request_id,
            request_started_at=attempts[-1].start_timestamp,
            request_completed_at=attempts[-1].end_timestamp,
            latency_ms=attempts[-1].request_latency_ms,
            provider_priority=auth.configured_provider_order.index(provider),
            fallback_depth=auth.configured_provider_order.index(provider),
            quota_state_before=QuotaSnapshot(
                remaining=attempts[-1].quota_before.quota_remaining
                if attempts[-1].quota_before
                else None
            ),
            quota_state_after=QuotaSnapshot(
                remaining=attempts[-1].quota_after.quota_remaining
                if attempts[-1].quota_after
                else None
            ),
            source_provenance=f"controlled-shadow:{provider}",
            raw_record_digest=raw_digest,
            adapter_version=adapter_version,
            completeness=ObservationCompleteness.COMPLETE,
            error_classification=ProviderState.AVAILABLE,
            candidate_only=True,
            delayed=response.delayed_observation,
            delay_seconds=response.delay_seconds,
            metadata={
                "controlled_shadow_run_id": auth.controlled_shadow_run_id,
                "capture_attestation_digest": _digest(attestation),
                **(
                    {"delay_semantics": "official delayed exchange snapshot"}
                    if response.delayed_observation
                    else {}
                ),
            },
            source_timing_provenance=TimingProvenance.SOURCE_TIMESTAMP,
        )
        normalized.validate(require_fresh=False)
        return observation, normalized

    @staticmethod
    def _canonical_attestation_payload(
        auth: ControlledShadowRunAuthorizationV1,
        provider: str,
        fixture_key: str,
        event_id: str,
        request_id: str,
        adapter_version: str,
        adapter_sha: str,
        cascade_digest: str,
        raw_digest: str,
        normalized_digest: str,
        captured_at: datetime,
    ) -> dict[str, object]:
        return {
            "controlled_shadow_run_id": auth.controlled_shadow_run_id,
            "ceo_authorization_id": auth.authorization_id,
            "qualification_session_id": auth.qualification_session_id,
            "provider_identity": provider,
            "fixture_key": fixture_key,
            "provider_event_id": event_id,
            "provider_request_id": request_id,
            "adapter_version": adapter_version,
            "adapter_source_sha": adapter_sha,
            "cascade_evidence_digest": cascade_digest,
            "raw_response_digest": raw_digest,
            "normalized_record_digest": normalized_digest,
            "captured_at": captured_at,
            "network_execution": True,
            "no_bet": True,
            "publication": False,
            "monetary_spend_authorized": False,
        }

    def _build_attestation(
        self,
        plan: ControlledShadowRunPreparationV1,
        auth: ControlledShadowRunAuthorizationV1,
        run: _ProviderRun,
        attempted_providers: Sequence[str],
        selected_provider: str | None,
        observation: RealProviderObservation | None,
        evidence: CascadeEvidence,
        cascade_digest: str,
        run_start: datetime,
        discovery_count: int,
        odds_count: int,
        total_requests: int,
        total_cost: float,
        failures: tuple[str, ...],
    ) -> ControlledShadowCaptureAttestationV1:
        response = run.attempt_response
        provider = run.provider
        event_id = (
            response.provider_event_id
            or f"not-executed:{_digest(run.attempt_request.request_identity)[:16]}"
        )
        request_id = (
            response.provider_request_id or run.attempt_request.request_identity
        )
        raw_digest = response.raw_response_digest or _digest(
            {
                "provider": provider,
                "request": request_id,
                "outcome": _outcome(response.outcome).value,
            }
        )
        normalized_digest = response.normalized_record_digest or _digest(
            {
                "provider": provider,
                "request": request_id,
                "observation": bool(observation),
            }
        )
        adapter_version = (
            response.adapter_version or auth.adapter_version_scope[provider]
        )
        adapter_sha = (
            response.adapter_source_sha or auth.adapter_source_sha_scope[provider]
        )
        observation_digest = (
            _digest(observation.as_payload())
            if observation is not None and provider == selected_provider
            else None
        )
        attestation = ControlledShadowCaptureAttestationV1(
            controlled_shadow_run_id=auth.controlled_shadow_run_id,
            authorization_id=auth.authorization_id,
            authorization_digest=auth.authorization_digest,
            preparation_id=plan.preparation_id,
            preparation_digest=plan.preparation_digest,
            qualification_session_id=auth.qualification_session_id,
            fixture_key=plan.fixture_identity["fixture_key"],
            league=plan.league,
            kickoff=plan.kickoff,
            configured_provider_order=tuple(auth.configured_provider_order),
            attempted_providers=tuple(attempted_providers),
            provider_identity=provider,
            selected_provider=selected_provider,
            provider_event_id=event_id,
            provider_request_id=request_id,
            observation_id=observation.observation_id
            if observation is not None
            else None,
            observation_digest=observation_digest,
            cascade_evidence_digest=cascade_digest,
            raw_response_digest=raw_digest,
            normalized_record_digest=(
                observation.normalized_record_digest
                if observation is not None and provider == selected_provider
                else normalized_digest
            ),
            adapter_version=adapter_version,
            adapter_source_sha=adapter_sha,
            timing_policy_digest=auth.timing_policy_digest,
            timing_evidence={
                "run_started_at": run_start.isoformat(),
                "attempt_started_at": run.attempt_start.isoformat(),
                "attempt_finished_at": run.attempt_end.isoformat(),
                "source_timing_provenance": response.source_timing_provenance,
            },
            discovery_request_count=discovery_count,
            odds_request_count=odds_count,
            network_request_count=total_requests,
            quota_cost_units=total_cost,
            failure_evidence=failures,
            zero_monetary_spend=True,
            no_bet=True,
            publication=False,
            production_activation=False,
            captured_at=run.attempt_end,
        )
        attestation.validate()
        return attestation


__all__ = [
    "AUTHORIZATION_SCHEMA_VERSION",
    "CAPTURE_ATTESTATION_SCHEMA_VERSION",
    "EXECUTION_BANNERS",
    "ControlledShadowCaptureAttestationV1",
    "ControlledShadowExecutionHarness",
    "ControlledShadowExecutionResult",
    "ControlledShadowRunAuthorizationV1",
    "FakeControlledShadowTransport",
    "HarnessContractError",
    "HarnessExecutionBlocked",
    "InMemoryControlledShadowRuntimeState",
    "ProviderAction",
    "ProviderTransportRequest",
    "ProviderTransportResponse",
    "RealProviderTransport",
    "RunValidationReport",
]
