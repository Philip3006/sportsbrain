"""Disabled-by-default TheRundown controlled-shadow canary contract.

This module is an isolated consumer seam for a future, separately authorized
TheRundown Top-5 shadow run.  It does not import a provider client, scheduler,
publisher, ledger, Builder-1 authority, or Builder-2 receipt issuer.  The
caller supplies the authorization and configuration; this module only
validates them and, when explicitly enabled, invokes a caller-supplied
transport once.

The only concrete transport shipped here is ``FakeTheRundownCanaryTransport``
for deterministic contract tests.  A future real adapter must implement the
sealed ``TheRundownCanaryNetworkTransport`` marker in a separately reviewed
change.  TEST_INJECTED evidence is always marked test-only and can never be
emitted as REAL_OBSERVED or as a canonical qualification artifact.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    TOP5_LEAGUE_CODES,
    TransportCapability,
)
from src.football.top5_builder2_qualification_receipt import RECEIPT_SCHEMA_VERSION
from src.football.top5_controlled_shadow_provider_qualification import (
    CAPTURE_ATTESTATION_CONTRACT_VERSION,
    ObservationEvidenceKind,
    ProviderTimestampProvenance,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key

THERUNDOWN_PROVIDER = "therundown"
CANARY_SCHEMA_VERSION = "top5-therundown-controlled-shadow-canary-v1"
CANARY_ATTESTATION_INPUT_SCHEMA_VERSION = (
    "top5-therundown-controlled-shadow-capture-attestation-input-v1"
)
CANARY_ACTION = "ODDS"
CANARY_EXECUTION_MODE = "SEQUENTIAL"
CANARY_MARKET_PHASE = "PRE_MATCH"
CANARY_OBSERVATION_SCHEMA_VERSION = "top5-real-provider-observation-v1"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_TEST_EVIDENCE_KINDS = frozenset(
    {
        ObservationEvidenceKind.MOCK,
        ObservationEvidenceKind.TEST_FIXTURE,
        ObservationEvidenceKind.OFFLINE_REPLAY,
    }
)


class CanaryContractError(ValueError):
    """Malformed canary input or an unsafe attempted transition."""


class CanaryExecutionBlocked(CanaryContractError):
    """Fail-closed refusal before or after a canary transport invocation."""


class CanaryOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    MALFORMED = "MALFORMED"
    STALE = "STALE"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"


class CanaryRunStatus(str, Enum):
    TEST_FIXTURE = "TEST_FIXTURE"
    REAL_OBSERVED = "REAL_OBSERVED"
    NO_OBSERVATION = "NO_OBSERVATION"


class CanaryLifecycleCompatibility(str, Enum):
    """Compatibility outcome without changing the active provider authority."""

    READY_FOR_EXTERNAL_VALIDATION = "READY_FOR_EXTERNAL_VALIDATION"
    BLOCKED_BY_CURRENT_PROVIDER_REPERTOIRE = "BLOCKED_BY_CURRENT_PROVIDER_REPERTOIRE"
    TEST_ONLY = "TEST_ONLY"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CanaryContractError(f"{name} is required")
    return value.strip()


def _sha(value: object, name: str) -> str:
    value = _text(value, name)
    if _SHA_RE.fullmatch(value) is None:
        raise CanaryContractError(f"{name} must be a hexadecimal digest")
    return value.lower()


def _utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise CanaryContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _nonnegative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CanaryContractError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: object, name: str) -> int:
    value = _nonnegative_int(value, name)
    if value == 0:
        raise CanaryContractError(f"{name} must be positive")
    return value


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise CanaryContractError(f"{name} must be a positive number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CanaryContractError(f"{name} must be a positive number") from exc
    if not isfinite(number) or number <= 0:
        raise CanaryContractError(f"{name} must be a positive number")
    return number


def _price(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise CanaryContractError(f"{name} must be a valid decimal price")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CanaryContractError(f"{name} must be a valid decimal price") from exc
    if not isfinite(number) or number <= 1.0:
        raise CanaryContractError(f"{name} must be a valid decimal price")
    return number


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc(value, "timestamp").isoformat()
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        _canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TheRundownCanaryTargetV1:
    """One exact provider/league/fixture/event authorization scope."""

    provider: str
    league: str
    fixture_key: str
    provider_event_id: str
    home_team: str
    away_team: str
    kickoff: datetime

    def validate(self) -> None:
        if _text(self.provider, "target provider") != THERUNDOWN_PROVIDER:
            raise CanaryContractError(
                "TheRundown canary target provider must be exactly therundown"
            )
        if _text(self.league, "target league") not in TOP5_LEAGUE_CODES:
            raise CanaryContractError("target league is outside the Top-5 scope")
        for name, value in (
            ("fixture_key", self.fixture_key),
            ("provider_event_id", self.provider_event_id),
            ("home_team", self.home_team),
            ("away_team", self.away_team),
        ):
            _text(value, name)
        kickoff = _utc(self.kickoff, "target kickoff")
        if self.fixture_key != make_fixture_key(
            self.league, self.home_team, self.away_team, kickoff
        ):
            raise CanaryContractError(
                "fixture key does not match the exact league/team/kickoff scope"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider": self.provider,
            "league": self.league,
            "fixture_key": self.fixture_key,
            "provider_event_id": self.provider_event_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": _utc(self.kickoff, "target kickoff").isoformat(),
        }


@dataclass(frozen=True)
class TheRundownCanaryPacingPolicyV1:
    """Explicit one-way pacing policy; retries are never implicit."""

    execution_mode: str = CANARY_EXECUTION_MODE
    minimum_interval_seconds: float = 1.0
    maximum_retries: int = 0

    def validate(self) -> None:
        if self.execution_mode != CANARY_EXECUTION_MODE:
            raise CanaryContractError("canary execution must be sequential")
        if isinstance(self.minimum_interval_seconds, bool):
            raise CanaryContractError("minimum pacing interval is invalid")
        try:
            interval = float(self.minimum_interval_seconds)
        except (TypeError, ValueError) as exc:
            raise CanaryContractError("minimum pacing interval is invalid") from exc
        if not isfinite(interval) or interval < 0:
            raise CanaryContractError("minimum pacing interval is invalid")
        if _nonnegative_int(self.maximum_retries, "maximum_retries") != 0:
            raise CanaryExecutionBlocked("retries are forbidden by the canary policy")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "execution_mode": self.execution_mode,
            "minimum_interval_seconds": float(self.minimum_interval_seconds),
            "maximum_retries": self.maximum_retries,
        }


@dataclass(frozen=True)
class TheRundownCanaryConfigurationV1:
    """Disabled-by-default configuration for one future canary run."""

    target: TheRundownCanaryTargetV1
    adapter_version: str
    adapter_source_sha: str
    maximum_request_count: int
    maximum_quota_cost_units: float
    request_quota_cost_units: float
    maximum_source_age_seconds: int
    pacing_policy: TheRundownCanaryPacingPolicyV1 = field(
        default_factory=TheRundownCanaryPacingPolicyV1
    )
    enabled: bool = False
    no_bet: bool = True
    publication: bool = False
    production_activation: bool = False
    monetary_spend_authorized: bool = False
    configuration_digest: str = ""

    @property
    def computed_configuration_digest(self) -> str:
        return _digest(self._payload_without_digest())

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": CANARY_SCHEMA_VERSION,
            "target": self.target.as_payload(),
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "maximum_request_count": self.maximum_request_count,
            "maximum_quota_cost_units": self.maximum_quota_cost_units,
            "request_quota_cost_units": self.request_quota_cost_units,
            "maximum_source_age_seconds": self.maximum_source_age_seconds,
            "pacing_policy": self.pacing_policy.as_payload(),
            "enabled": self.enabled,
            "no_bet": self.no_bet,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "monetary_spend_authorized": self.monetary_spend_authorized,
        }

    def validate(self) -> None:
        if not isinstance(self.enabled, bool):
            raise CanaryContractError("canary enabled flag must be boolean")
        self.target.validate()
        _text(self.adapter_version, "adapter_version")
        _sha(self.adapter_source_sha, "adapter_source_sha")
        _positive_int(self.maximum_request_count, "maximum_request_count")
        maximum_cost = _positive_number(
            self.maximum_quota_cost_units, "maximum_quota_cost_units"
        )
        request_cost = _positive_number(
            self.request_quota_cost_units, "request_quota_cost_units"
        )
        if request_cost > maximum_cost:
            raise CanaryExecutionBlocked(
                "configured request quota cost exceeds the authorized quota budget"
            )
        _positive_int(self.maximum_source_age_seconds, "maximum_source_age_seconds")
        self.pacing_policy.validate()
        for name, value, expected in (
            ("no_bet", self.no_bet, True),
            ("publication", self.publication, False),
            ("production_activation", self.production_activation, False),
            ("monetary_spend_authorized", self.monetary_spend_authorized, False),
        ):
            if value is not expected:
                raise CanaryExecutionBlocked(
                    f"unsafe canary configuration flag: {name}"
                )
        _sha(self.configuration_digest, "configuration_digest")
        if self.configuration_digest != self.computed_configuration_digest:
            raise CanaryContractError("configuration digest mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            **self._payload_without_digest(),
            "configuration_digest": self.computed_configuration_digest,
        }


@dataclass(frozen=True)
class TheRundownCanaryAuthorizationV1:
    """Externally supplied authorization; this module cannot issue one."""

    authorization_id: str
    ceo_authorization_identity: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    target: TheRundownCanaryTargetV1
    adapter_version: str
    adapter_source_sha: str
    configuration_digest: str
    maximum_request_count: int
    maximum_quota_cost_units: float
    request_quota_cost_units: float
    maximum_source_age_seconds: int
    issued_at: datetime
    expires_at: datetime
    pacing_policy: TheRundownCanaryPacingPolicyV1 = field(
        default_factory=TheRundownCanaryPacingPolicyV1
    )
    no_bet: bool = True
    publication: bool = False
    production_activation: bool = False
    monetary_spend_authorized: bool = False
    schema_version: str = CANARY_SCHEMA_VERSION

    @property
    def authorization_digest(self) -> str:
        return _digest(self._payload_without_digest())

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authorization_id": self.authorization_id,
            "ceo_authorization_identity": self.ceo_authorization_identity,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "target": self.target.as_payload(),
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "configuration_digest": self.configuration_digest,
            "maximum_request_count": self.maximum_request_count,
            "maximum_quota_cost_units": self.maximum_quota_cost_units,
            "request_quota_cost_units": self.request_quota_cost_units,
            "maximum_source_age_seconds": self.maximum_source_age_seconds,
            "issued_at": _utc(self.issued_at, "issued_at").isoformat(),
            "expires_at": _utc(self.expires_at, "expires_at").isoformat(),
            "pacing_policy": self.pacing_policy.as_payload(),
            "no_bet": self.no_bet,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "monetary_spend_authorized": self.monetary_spend_authorized,
        }

    def validate(
        self,
        configuration: TheRundownCanaryConfigurationV1 | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        for name, value in (
            ("authorization_id", self.authorization_id),
            ("ceo_authorization_identity", self.ceo_authorization_identity),
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("qualification_session_id", self.qualification_session_id),
        ):
            _text(value, name)
        if self.schema_version != CANARY_SCHEMA_VERSION:
            raise CanaryContractError("unsupported canary authorization schema")
        self.target.validate()
        _text(self.adapter_version, "adapter_version")
        _sha(self.adapter_source_sha, "adapter_source_sha")
        _sha(self.configuration_digest, "configuration_digest")
        _positive_int(self.maximum_request_count, "maximum_request_count")
        maximum_cost = _positive_number(
            self.maximum_quota_cost_units, "maximum_quota_cost_units"
        )
        if (
            _positive_number(self.request_quota_cost_units, "request_quota_cost_units")
            > maximum_cost
        ):
            raise CanaryExecutionBlocked(
                "authorized request quota cost exceeds the authorized quota budget"
            )
        _positive_int(self.maximum_source_age_seconds, "maximum_source_age_seconds")
        self.pacing_policy.validate()
        issued = _utc(self.issued_at, "issued_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= issued:
            raise CanaryContractError("authorization expiry must follow issue time")
        current = _utc(now or datetime.now(timezone.utc), "authorization now")
        if current < issued or current >= expires:
            raise CanaryExecutionBlocked(
                "canary authorization is expired or not yet valid"
            )
        for name, value, expected in (
            ("no_bet", self.no_bet, True),
            ("publication", self.publication, False),
            ("production_activation", self.production_activation, False),
            ("monetary_spend_authorized", self.monetary_spend_authorized, False),
        ):
            if value is not expected:
                raise CanaryExecutionBlocked(
                    f"unsafe canary authorization flag: {name}"
                )
        if configuration is not None:
            configuration.validate()
            pairs = (
                (self.target, configuration.target, "target scope"),
                (
                    self.adapter_version,
                    configuration.adapter_version,
                    "adapter version",
                ),
                (
                    self.adapter_source_sha.lower(),
                    configuration.adapter_source_sha.lower(),
                    "adapter source SHA",
                ),
                (
                    self.configuration_digest,
                    configuration.configuration_digest,
                    "configuration digest",
                ),
                (
                    self.maximum_request_count,
                    configuration.maximum_request_count,
                    "request budget",
                ),
                (
                    self.maximum_quota_cost_units,
                    configuration.maximum_quota_cost_units,
                    "quota budget",
                ),
                (
                    self.request_quota_cost_units,
                    configuration.request_quota_cost_units,
                    "request quota cost",
                ),
                (
                    self.maximum_source_age_seconds,
                    configuration.maximum_source_age_seconds,
                    "source age",
                ),
                (self.pacing_policy, configuration.pacing_policy, "pacing policy"),
                (self.no_bet, configuration.no_bet, "no-bet flag"),
                (self.publication, configuration.publication, "publication flag"),
                (
                    self.production_activation,
                    configuration.production_activation,
                    "activation flag",
                ),
                (
                    self.monetary_spend_authorized,
                    configuration.monetary_spend_authorized,
                    "spend flag",
                ),
            )
            for actual, expected, label in pairs:
                if actual != expected:
                    raise CanaryExecutionBlocked(
                        f"authorization does not match configuration: {label}"
                    )

    def as_payload(self) -> dict[str, object]:
        self.validate(now=self.issued_at)
        return {
            **self._payload_without_digest(),
            "authorization_digest": self.authorization_digest,
        }


@dataclass(frozen=True)
class TheRundownCanaryRequestV1:
    controlled_shadow_run_id: str
    authorization_id: str
    request_identity: str
    target: TheRundownCanaryTargetV1
    sequence: int = 0
    action: str = CANARY_ACTION
    market_type: str = MARKET_PREMATCH_1X2

    def validate(self) -> None:
        _text(self.controlled_shadow_run_id, "request controlled_shadow_run_id")
        _text(self.authorization_id, "request authorization_id")
        _text(self.request_identity, "request_identity")
        self.target.validate()
        if self.sequence != 0:
            raise CanaryExecutionBlocked("unexpected retry or non-sequential request")
        if self.action != CANARY_ACTION:
            raise CanaryContractError("only the odds action is supported")
        if self.market_type != MARKET_PREMATCH_1X2:
            raise CanaryContractError("only pre-match 1X2 is supported")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "authorization_id": self.authorization_id,
            "request_identity": self.request_identity,
            "target": self.target.as_payload(),
            "sequence": self.sequence,
            "action": self.action,
            "market_type": self.market_type,
        }


@dataclass(frozen=True)
class TheRundownCanaryResponseV1:
    """Redacted response envelope returned by a future adapter or test stub."""

    outcome: CanaryOutcome | str = CanaryOutcome.PROVIDER_UNAVAILABLE
    provider: str = THERUNDOWN_PROVIDER
    provider_event_id: str = ""
    provider_request_id: str = ""
    bookmaker_identity: str = ""
    source_identity: str = ""
    source_timestamp: datetime | None = None
    captured_at: datetime | None = None
    request_started_at: datetime | None = None
    request_finished_at: datetime | None = None
    home_odds: float | None = None
    draw_odds: float | None = None
    away_odds: float | None = None
    adapter_version: str = ""
    adapter_source_sha: str = ""
    raw_response_digest: str = ""
    normalized_record_digest: str = ""
    quota_before: int | None = None
    quota_after: int | None = None
    quota_cost_units: float = 0.0
    provider_timestamp_provenance: str = (
        ProviderTimestampProvenance.PROVIDER_SOURCE_TIMESTAMP.value
    )
    retry_count: int = 0
    evidence_kind: ObservationEvidenceKind | str = ObservationEvidenceKind.TEST_FIXTURE
    network_execution: bool = False
    no_bet: bool = True
    publication: bool = False
    production_activation: bool = False
    monetary_spend_authorized: bool = False
    authority_attempted: bool = False
    publication_attempted: bool = False
    activation_attempted: bool = False
    ledger_mutated: bool = False
    scheduler_registered: bool = False
    raw_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "raw_metadata", MappingProxyType(dict(self.raw_metadata))
        )


@runtime_checkable
class TheRundownCanaryTransport(Protocol):
    transport_capability: TransportCapability

    def execute(
        self, request: TheRundownCanaryRequestV1
    ) -> TheRundownCanaryResponseV1: ...


class TheRundownCanaryNetworkTransport:
    """Sealed marker for a future separately reviewed real adapter.

    No HTTP implementation is present in this PR.  Subclasses belong in a
    later, independently authorized change and must not be supplied by the
    default configuration.
    """

    transport_capability = TransportCapability.NETWORK_CAPABLE

    def execute(self, request: TheRundownCanaryRequestV1) -> TheRundownCanaryResponseV1:
        raise NotImplementedError(
            "TheRundown network transport is not implemented in this PR"
        )


TransportResponseFactory = Callable[
    [TheRundownCanaryRequestV1], TheRundownCanaryResponseV1
]


class FakeTheRundownCanaryTransport:
    """Deterministic test-only transport; it cannot claim real execution."""

    transport_capability = TransportCapability.TEST_INJECTED

    def __init__(
        self,
        response: TheRundownCanaryResponseV1 | TransportResponseFactory,
    ) -> None:
        self._response = response
        self.calls: list[TheRundownCanaryRequestV1] = []

    def execute(self, request: TheRundownCanaryRequestV1) -> TheRundownCanaryResponseV1:
        request.validate()
        self.calls.append(request)
        response = (
            self._response(request) if callable(self._response) else self._response
        )
        if not isinstance(response, TheRundownCanaryResponseV1):
            raise CanaryContractError("test transport returned an invalid response")
        return response


@dataclass(frozen=True)
class TheRundownCanaryEvidenceV1:
    """Serializable evidence inputs for later attestation/qualification.

    These are evidence inputs, not a Builder-2 receipt and not authority.  A
    test result has ``network_execution=False`` and therefore cannot validate
    as the canonical real capture attestation.
    """

    status: CanaryRunStatus
    evidence_kind: ObservationEvidenceKind
    network_execution: bool
    controlled_shadow_run_id: str
    authorization_id: str
    authorization_digest: str
    qualification_session_id: str
    configuration_digest: str
    target: TheRundownCanaryTargetV1
    request_identity: str
    observation_id: str
    provider_request_id: str
    provider_event_id: str
    observation_digest: str
    normalized_record_digest: str
    raw_response_digest: str
    cascade_evidence_digest: str
    adapter_version: str
    adapter_source_sha: str
    source_timestamp: datetime | None
    captured_at: datetime
    request_started_at: datetime
    request_finished_at: datetime
    bookmaker_identity: str
    source_identity: str
    provider_timestamp_provenance: str
    market_type: str
    market_phase: str
    home_odds: float | None
    draw_odds: float | None
    away_odds: float | None
    request_count: int
    quota_before: int | None
    quota_after: int | None
    quota_cost_units: float
    candidate_only: bool
    canonical_attestation_eligible: bool
    qualification_report_eligible: bool
    builder2_receipt_eligible: bool
    failure_reason: str | None

    @property
    def network_request_count(self) -> int:
        return 1 if self.network_execution else 0

    def _observation_input_without_digest(self) -> dict[str, object]:
        """Return the exact field shape expected by the current observation gate.

        The mapping is deliberately an input projection.  It is not parsed or
        promoted here: current Builder-2 validation must independently accept
        the provider, cascade, attestation, and qualification result.
        """

        return {
            "observation_id": self.observation_id,
            "qualification_session_id": self.qualification_session_id,
            "evidence_kind": self.evidence_kind.value,
            "provider_identity": self.target.provider,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "league": self.target.league,
            "fixture_key": self.target.fixture_key,
            "home_team": self.target.home_team,
            "away_team": self.target.away_team,
            "kickoff": self.target.kickoff.isoformat(),
            "market_type": MARKET_PREMATCH_1X2,
            "market_phase": CANARY_MARKET_PHASE,
            "home_odds": self.home_odds,
            "draw_odds": self.draw_odds,
            "away_odds": self.away_odds,
            "bookmaker_identity": self.bookmaker_identity,
            "source_identity": self.source_identity,
            "source_timestamp": self.source_timestamp.isoformat()
            if self.source_timestamp
            else None,
            "provider_timestamp_provenance": self.provider_timestamp_provenance,
            "captured_at": self.captured_at.isoformat(),
            "request_started_at": self.request_started_at.isoformat(),
            "request_finished_at": self.request_finished_at.isoformat(),
            "latency_ms": max(
                0,
                int(
                    (self.request_finished_at - self.request_started_at).total_seconds()
                    * 1000
                ),
            ),
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "raw_response_digest": self.raw_response_digest,
            "normalized_record_digest": self.normalized_record_digest,
            "cascade_evidence": {
                "contract_version": CANARY_SCHEMA_VERSION,
                "candidate_only": True,
                "provider_identity": self.target.provider,
                "request_identity": self.request_identity,
                "cascade_evidence_digest": self.cascade_evidence_digest,
                "network_request_count": self.network_request_count,
                "quota_cost_units": self.quota_cost_units,
            },
            "quota_before": self.quota_before,
            "quota_after": self.quota_after,
            "quota_cost_units": self.quota_cost_units,
            "network_request_count": self.network_request_count,
            "candidate_only": True,
            "monetary_spend_authorized": False,
            "delayed_observation": False,
            "synthetic_reconstruction": not self.network_execution,
            "no_bet": True,
            "publication_enabled": False,
            "production_activation": False,
            "ledger_mutated": False,
            "sealed_data_accessed": False,
            "research_mutated": False,
            "capture_attestation": self._capture_attestation_input(),
        }

    @property
    def observation_input_digest(self) -> str:
        return _digest(self._observation_input_without_digest())

    def observation_input(self) -> dict[str, object]:
        payload = self._observation_input_without_digest()
        payload["observation_digest"] = self.observation_input_digest
        return payload

    def canonical_capture_attestation_payload(self) -> dict[str, object]:
        """Return only fields serialized by the current canonical attestation."""

        return {
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "ceo_authorization_id": self.authorization_id,
            "qualification_session_id": self.qualification_session_id,
            "provider_identity": self.target.provider,
            "fixture_key": self.target.fixture_key,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "cascade_evidence_digest": self.cascade_evidence_digest,
            "raw_response_digest": self.raw_response_digest,
            "normalized_record_digest": self.normalized_record_digest,
            "captured_at": self.captured_at.isoformat(),
            "network_execution": self.network_execution,
            "no_bet": True,
            "publication": False,
            "monetary_spend_authorized": False,
            "schema_version": CAPTURE_ATTESTATION_CONTRACT_VERSION,
        }

    @property
    def canonical_capture_attestation_digest(self) -> str:
        return _digest(self.canonical_capture_attestation_payload())

    def _capture_attestation_input(self) -> dict[str, object]:
        return {
            "schema_version": CAPTURE_ATTESTATION_CONTRACT_VERSION,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "ceo_authorization_id": self.authorization_id,
            "qualification_session_id": self.qualification_session_id,
            "provider_identity": self.target.provider,
            "fixture_key": self.target.fixture_key,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "cascade_evidence_digest": self.cascade_evidence_digest,
            "raw_response_digest": self.raw_response_digest,
            "normalized_record_digest": self.normalized_record_digest,
            "captured_at": self.captured_at.isoformat(),
            "network_execution": self.network_execution,
            "no_bet": True,
            "publication": False,
            "production_activation": False,
            "monetary_spend_authorized": False,
            "candidate_only": self.candidate_only,
            "evidence_kind": self.evidence_kind.value,
            "attestation_input_schema": CANARY_ATTESTATION_INPUT_SCHEMA_VERSION,
        }

    def as_payload(self) -> dict[str, object]:
        self.target.validate()
        return {
            "schema_version": CANARY_SCHEMA_VERSION,
            "status": self.status.value,
            "evidence_kind": self.evidence_kind.value,
            "network_execution": self.network_execution,
            "safety": {
                "no_bet": True,
                "publication": False,
                "production_activation": False,
                "monetary_spend_authorized": False,
                "authority_attempted": False,
                "ledger_mutated": False,
                "scheduler_registered": False,
            },
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "authorization_id": self.authorization_id,
            "authorization_digest": self.authorization_digest,
            "qualification_session_id": self.qualification_session_id,
            "configuration_digest": self.configuration_digest,
            "candidate_only": self.candidate_only,
            "provider": self.target.provider,
            "league": self.target.league,
            "fixture_key": self.target.fixture_key,
            "provider_event_id": self.provider_event_id,
            "request_identity": self.request_identity,
            "provider_request_id": self.provider_request_id,
            "observation_id": self.observation_id,
            "observation_digest": self.observation_digest,
            "normalized_record_digest": self.normalized_record_digest,
            "raw_response_digest": self.raw_response_digest,
            "cascade_evidence_digest": self.cascade_evidence_digest,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "source_timestamp": self.source_timestamp.isoformat()
            if self.source_timestamp
            else None,
            "captured_at": self.captured_at.isoformat(),
            "request_started_at": self.request_started_at.isoformat(),
            "request_finished_at": self.request_finished_at.isoformat(),
            "bookmaker_identity": self.bookmaker_identity,
            "source_identity": self.source_identity,
            "provider_timestamp_provenance": self.provider_timestamp_provenance,
            "market_type": MARKET_PREMATCH_1X2,
            "market_phase": CANARY_MARKET_PHASE,
            "odds": {
                "home": self.home_odds,
                "draw": self.draw_odds,
                "away": self.away_odds,
            },
            "request_count": self.request_count,
            "network_request_count": self.network_request_count,
            "quota_before": self.quota_before,
            "quota_after": self.quota_after,
            "quota_cost_units": self.quota_cost_units,
            "observation_input_digest": self.observation_input_digest,
            "observation_input": self.observation_input(),
            "canonical_attestation_eligible": self.canonical_attestation_eligible,
            "qualification_report_eligible": self.qualification_report_eligible,
            "builder2_receipt_eligible": self.builder2_receipt_eligible,
            "capture_attestation_input": self._capture_attestation_input(),
            "qualification_input": {
                "schema_version": CANARY_OBSERVATION_SCHEMA_VERSION,
                "provider": self.target.provider,
                "provider_identity": self.target.provider,
                "league": self.target.league,
                "fixture_key": self.target.fixture_key,
                "provider_event_id": self.provider_event_id,
                "provider_request_id": self.provider_request_id,
                "bookmaker_identity": self.bookmaker_identity,
                "source_identity": self.source_identity,
                "real_observed": self.evidence_kind
                is ObservationEvidenceKind.REAL_OBSERVED,
                "candidate_only": True,
                "no_bet": True,
                "publication": False,
                "production_activation": False,
                "monetary_spend_authorized": False,
                "source_timestamp": self.source_timestamp.isoformat()
                if self.source_timestamp
                else None,
                "provider_timestamp_provenance": self.provider_timestamp_provenance,
                "observation_id": self.observation_id,
                "observation_digest": self.observation_input_digest,
                "network_request_count": self.network_request_count,
                "quota_before": self.quota_before,
                "quota_after": self.quota_after,
                "quota_cost_units": self.quota_cost_units,
                "observation": self.observation_input(),
            },
            "builder2_receipt_input": {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "eligible": False,
                "issuer_present": False,
                "reason": "canary emits evidence inputs only; Builder-2 remains the receipt authority",
                "required_binding_fields": [
                    "qualification_report_identity",
                    "qualification_report_digest",
                    "qualification_result_digest",
                    "qualification_status",
                    "qualification_session_id",
                    "controlled_shadow_run_id",
                    "ceo_authorization_id",
                    "fixture_key",
                    "provider_identity",
                    "provider_event_id",
                    "provider_request_id",
                    "observation_id",
                    "observation_digest",
                    "normalized_record_digest",
                    "cascade_evidence_digest",
                    "capture_attestation_digest",
                    "adapter_version",
                    "adapter_source_sha",
                ],
                "available_evidence": {
                    "qualification_session_id": self.qualification_session_id,
                    "controlled_shadow_run_id": self.controlled_shadow_run_id,
                    "ceo_authorization_id": self.authorization_id,
                    "fixture_key": self.target.fixture_key,
                    "provider_identity": self.target.provider,
                    "provider_event_id": self.provider_event_id,
                    "provider_request_id": self.provider_request_id,
                    "observation_id": self.observation_id,
                    "observation_digest": self.observation_digest,
                    "normalized_record_digest": self.normalized_record_digest,
                    "cascade_evidence_digest": self.cascade_evidence_digest,
                    "capture_attestation_digest": self.canonical_capture_attestation_digest,
                    "adapter_version": self.adapter_version,
                    "adapter_source_sha": self.adapter_source_sha,
                    "qualification_result_digest": None,
                    "qualification_status": None,
                    "accepted": False,
                    "prediction_input_allowed": False,
                    "no_bet": True,
                    "publication": False,
                    "production_activation": False,
                    "monetary_spend_authorized": False,
                },
            },
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True)
class TheRundownCanaryLifecycleArtifactV1:
    """Offline projection across the capture, qualification, and receipt seams.

    This object is a compatibility proof artifact only.  It contains the
    exact current downstream field shapes and the immutable safety bindings,
    but it never constructs a canonical observation, qualification report, or
    Builder-2 receipt.  The current contracts still reject TheRundown because
    it is not in the active Football provider repertoire.
    """

    result: TheRundownCanaryRunResultV1
    compatibility: CanaryLifecycleCompatibility
    dependency_blockers: tuple[str, ...]

    def validate(self) -> None:
        self.result.validate()
        evidence = self.result.evidence
        if evidence.candidate_only is not True:
            raise CanaryExecutionBlocked(
                "lifecycle artifact cannot clear candidate-only evidence"
            )
        if evidence.network_execution:
            if (
                self.compatibility
                is not CanaryLifecycleCompatibility.BLOCKED_BY_CURRENT_PROVIDER_REPERTOIRE
            ):
                raise CanaryContractError(
                    "TheRundown real-shaped evidence must report the current provider dependency"
                )
            if not self.dependency_blockers:
                raise CanaryContractError(
                    "provider compatibility blocker must be explicit"
                )
        elif self.compatibility is not CanaryLifecycleCompatibility.TEST_ONLY:
            raise CanaryContractError(
                "non-network canary evidence must remain test-only"
            )
        for name, value, expected in (
            ("no_bet", True, True),
            ("publication", False, False),
            ("production_activation", False, False),
            ("monetary_spend_authorized", False, False),
        ):
            if value is not expected:
                raise CanaryExecutionBlocked(
                    f"lifecycle safety binding is unsafe: {name}"
                )

    @property
    def capture_attestation_input(self) -> Mapping[str, object]:
        return self.result.evidence.as_payload()["capture_attestation_input"]

    @property
    def canonical_capture_attestation(self) -> Mapping[str, object]:
        return self.result.evidence.canonical_capture_attestation_payload()

    @property
    def qualification_input(self) -> Mapping[str, object]:
        payload = self.result.evidence.as_payload()["qualification_input"]
        if not isinstance(payload, Mapping):
            raise CanaryContractError("qualification input projection is malformed")
        return payload

    @property
    def builder2_receipt_input(self) -> Mapping[str, object]:
        payload = self.result.evidence.as_payload()["builder2_receipt_input"]
        if not isinstance(payload, Mapping):
            raise CanaryContractError("Builder-2 receipt input projection is malformed")
        return payload

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": CANARY_SCHEMA_VERSION,
            "lifecycle_contract": "authorized-capture-to-qualification-to-receipt-input-v1",
            "compatibility": self.compatibility.value,
            "dependency_blockers": list(self.dependency_blockers),
            "run": self.result.evidence.as_payload(),
            "capture_attestation_input": dict(self.capture_attestation_input),
            "canonical_capture_attestation": self.result.evidence.canonical_capture_attestation_payload(),
            "capture_attestation_digest": self.result.evidence.canonical_capture_attestation_digest,
            "qualification_input": dict(self.qualification_input),
            "builder2_receipt_input": dict(self.builder2_receipt_input),
            "safety": {
                "no_bet": True,
                "publication": False,
                "production_activation": False,
                "monetary_spend_authorized": False,
                "authority_changed": False,
                "scheduler_registered": False,
                "ledger_mutated": False,
            },
        }


@dataclass(frozen=True)
class TheRundownCanaryRunResultV1:
    status: CanaryRunStatus
    request: TheRundownCanaryRequestV1
    response: TheRundownCanaryResponseV1
    evidence: TheRundownCanaryEvidenceV1
    request_count: int
    quota_cost_units: float

    @property
    def observed(self) -> bool:
        return self.status is CanaryRunStatus.REAL_OBSERVED

    def validate(self) -> None:
        self.request.validate()
        if self.request_count != 1:
            raise CanaryContractError("canary request count must be exactly one")
        if self.quota_cost_units < 0 or not isfinite(self.quota_cost_units):
            raise CanaryContractError("canary quota cost is invalid")
        if (
            self.evidence.network_execution
            and self.status is not CanaryRunStatus.REAL_OBSERVED
        ):
            raise CanaryExecutionBlocked(
                "network execution cannot produce non-real status"
            )
        if (
            not self.evidence.network_execution
            and self.status is CanaryRunStatus.REAL_OBSERVED
        ):
            raise CanaryExecutionBlocked(
                "non-network execution cannot produce REAL_OBSERVED"
            )
        if (
            self.evidence.request_identity != self.request.request_identity
            or self.evidence.controlled_shadow_run_id
            != self.request.controlled_shadow_run_id
            or self.evidence.target != self.request.target
            or self.evidence.request_count != self.request_count
            or self.evidence.quota_cost_units != self.quota_cost_units
        ):
            raise CanaryExecutionBlocked("canary evidence is not bound to the request")

    def lifecycle_artifact(self) -> TheRundownCanaryLifecycleArtifactV1:
        """Project one offline result into the downstream contract boundary."""

        if self.status is CanaryRunStatus.TEST_FIXTURE:
            compatibility = CanaryLifecycleCompatibility.TEST_ONLY
            blockers = (
                "TEST_INJECTED evidence cannot validate as canonical REAL_OBSERVED",
                "a Builder-2 receipt requires an independently accepted real qualification result",
            )
        elif self.status is CanaryRunStatus.REAL_OBSERVED:
            compatibility = (
                CanaryLifecycleCompatibility.BLOCKED_BY_CURRENT_PROVIDER_REPERTOIRE
            )
            blockers = (
                "current B1/B2 contracts accept only the active Football provider repertoire",
                "TheRundown is intentionally outside the active Football provider repertoire",
                "a canonical CascadeEvidence digest must be supplied by a separately reviewed provider integration",
            )
        else:
            compatibility = CanaryLifecycleCompatibility.TEST_ONLY
            blockers = (
                "no provider observation exists",
                "qualification and receipt preconditions are not satisfied",
            )
        artifact = TheRundownCanaryLifecycleArtifactV1(
            result=self,
            compatibility=compatibility,
            dependency_blockers=blockers,
        )
        artifact.validate()
        return artifact


class TheRundownControlledShadowCanary:
    """Single-shot, disabled-by-default canary executor."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def validate_contract(
        self,
        configuration: TheRundownCanaryConfigurationV1,
        authorization: TheRundownCanaryAuthorizationV1,
    ) -> None:
        configuration.validate()
        authorization.validate(configuration, now=self.clock())

    def run(
        self,
        configuration: TheRundownCanaryConfigurationV1,
        authorization: TheRundownCanaryAuthorizationV1,
        *,
        transport: TheRundownCanaryTransport,
    ) -> TheRundownCanaryRunResultV1:
        self.validate_contract(configuration, authorization)
        if configuration.enabled is not True:
            raise CanaryExecutionBlocked(
                "TheRundown controlled-shadow canary is disabled by default"
            )
        capability = getattr(transport, "transport_capability", None)
        if capability in {
            TransportCapability.NETWORK_CAPABLE,
            TransportCapability.NETWORK_CAPABLE.value,
        }:
            if not isinstance(transport, TheRundownCanaryNetworkTransport):
                raise CanaryExecutionBlocked(
                    "NETWORK_CAPABLE canary transport must use the reviewed marker"
                )
        elif capability is not TransportCapability.TEST_INJECTED:
            raise CanaryExecutionBlocked("canary transport capability is invalid")
        elif not isinstance(transport, FakeTheRundownCanaryTransport):
            raise CanaryExecutionBlocked(
                "TEST_INJECTED canary transport must be the test-only transport"
            )
        request = TheRundownCanaryRequestV1(
            controlled_shadow_run_id=authorization.controlled_shadow_run_id,
            authorization_id=authorization.authorization_id,
            request_identity=(
                "therundown-canary-request:"
                f"{_digest((authorization.authorization_digest, authorization.target.as_payload()))[:32]}"
            ),
            target=authorization.target,
        )
        request.validate()
        if authorization.maximum_request_count < 1:
            raise CanaryExecutionBlocked("request budget is exhausted before the call")
        if (
            authorization.request_quota_cost_units
            > authorization.maximum_quota_cost_units
        ):
            raise CanaryExecutionBlocked("quota budget is exhausted before the call")
        try:
            response = transport.execute(request)
        except Exception as exc:
            raise CanaryExecutionBlocked(
                f"TheRundown canary transport failed: {type(exc).__name__}"
            ) from exc
        if not isinstance(response, TheRundownCanaryResponseV1):
            raise CanaryExecutionBlocked(
                "canary transport returned an invalid response"
            )
        outcome = CanaryOutcome(response.outcome)
        self._validate_response(
            response,
            request,
            authorization,
            capability=capability,
            now=self.clock(),
        )
        if outcome is not CanaryOutcome.SUCCESS:
            evidence = self._build_evidence(
                response, request, authorization, status=CanaryRunStatus.NO_OBSERVATION
            )
            result = TheRundownCanaryRunResultV1(
                CanaryRunStatus.NO_OBSERVATION,
                request,
                response,
                evidence,
                1,
                response.quota_cost_units,
            )
            result.validate()
            return result
        status = (
            CanaryRunStatus.REAL_OBSERVED
            if capability is TransportCapability.NETWORK_CAPABLE
            else CanaryRunStatus.TEST_FIXTURE
        )
        evidence = self._build_evidence(response, request, authorization, status=status)
        result = TheRundownCanaryRunResultV1(
            status, request, response, evidence, 1, response.quota_cost_units
        )
        result.validate()
        return result

    @staticmethod
    def _validate_response(
        response: TheRundownCanaryResponseV1,
        request: TheRundownCanaryRequestV1,
        authorization: TheRundownCanaryAuthorizationV1,
        *,
        capability: TransportCapability,
        now: datetime,
    ) -> None:
        try:
            outcome = CanaryOutcome(response.outcome)
        except (TypeError, ValueError) as exc:
            raise CanaryExecutionBlocked("canary response outcome is invalid") from exc
        if response.provider != authorization.target.provider:
            raise CanaryExecutionBlocked("provider response does not match exact scope")
        if (
            response.provider_event_id
            and response.provider_event_id != authorization.target.provider_event_id
        ):
            raise CanaryExecutionBlocked(
                "provider event identity does not match exact scope"
            )
        if (
            response.provider_request_id
            and response.provider_request_id != request.request_identity
        ):
            raise CanaryExecutionBlocked(
                "provider request identity does not match exact scope"
            )
        if response.retry_count != 0:
            raise CanaryExecutionBlocked("unexpected provider retry detected")
        for name, value, expected in (
            ("no_bet", response.no_bet, True),
            ("publication", response.publication, False),
            ("production_activation", response.production_activation, False),
            ("monetary_spend_authorized", response.monetary_spend_authorized, False),
            ("authority_attempted", response.authority_attempted, False),
            ("publication_attempted", response.publication_attempted, False),
            ("activation_attempted", response.activation_attempted, False),
            ("ledger_mutated", response.ledger_mutated, False),
            ("scheduler_registered", response.scheduler_registered, False),
        ):
            if value is not expected:
                raise CanaryExecutionBlocked(f"unsafe provider response flag: {name}")
        if response.quota_cost_units < 0 or not isfinite(response.quota_cost_units):
            raise CanaryExecutionBlocked("provider response quota cost is invalid")
        for name, value in (
            ("quota_before", response.quota_before),
            ("quota_after", response.quota_after),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise CanaryExecutionBlocked(f"provider response {name} is invalid")
        if (
            response.quota_before is not None
            and response.quota_after is not None
            and response.quota_after > response.quota_before
        ):
            raise CanaryExecutionBlocked(
                "provider response quota increased unexpectedly"
            )
        if response.quota_cost_units != authorization.request_quota_cost_units:
            raise CanaryExecutionBlocked(
                "provider response quota cost does not match preflight"
            )
        try:
            evidence_kind = ObservationEvidenceKind(response.evidence_kind)
        except (TypeError, ValueError) as exc:
            raise CanaryExecutionBlocked(
                "provider response evidence kind is invalid"
            ) from exc
        if capability is TransportCapability.TEST_INJECTED:
            if (
                response.network_execution
                or evidence_kind is ObservationEvidenceKind.REAL_OBSERVED
            ):
                raise CanaryExecutionBlocked("TEST_INJECTED cannot emit REAL_OBSERVED")
            if evidence_kind not in _TEST_EVIDENCE_KINDS:
                raise CanaryExecutionBlocked(
                    "test evidence kind is not explicitly non-real"
                )
        elif capability is TransportCapability.NETWORK_CAPABLE:
            if (
                not response.network_execution
                or evidence_kind is not ObservationEvidenceKind.REAL_OBSERVED
            ):
                raise CanaryExecutionBlocked("network success must be REAL_OBSERVED")
        else:
            raise CanaryExecutionBlocked("canary transport capability is invalid")
        if outcome is not CanaryOutcome.SUCCESS:
            if (
                response.network_execution
                or evidence_kind is ObservationEvidenceKind.REAL_OBSERVED
            ):
                raise CanaryExecutionBlocked(
                    "failed response cannot claim real evidence"
                )
            return
        if response.provider_event_id != authorization.target.provider_event_id:
            raise CanaryExecutionBlocked(
                "successful response is missing exact provider event identity"
            )
        if response.provider_request_id != request.request_identity:
            raise CanaryExecutionBlocked(
                "successful response is missing exact request identity"
            )
        if (
            not response.bookmaker_identity.strip()
            or not response.source_identity.strip()
        ):
            raise CanaryExecutionBlocked("bookmaker and source provenance are required")
        if response.source_timestamp is None or response.captured_at is None:
            raise CanaryExecutionBlocked("source and capture timestamps are required")
        if (
            response.provider_timestamp_provenance
            != ProviderTimestampProvenance.PROVIDER_SOURCE_TIMESTAMP.value
        ):
            raise CanaryExecutionBlocked(
                "provider source timestamp provenance is required"
            )
        if response.quota_before is None or response.quota_after is None:
            raise CanaryExecutionBlocked("quota evidence is required for success")
        if response.request_started_at is None or response.request_finished_at is None:
            raise CanaryExecutionBlocked("request timing provenance is required")
        source = _utc(response.source_timestamp, "source_timestamp")
        captured = _utc(response.captured_at, "captured_at")
        started = _utc(response.request_started_at, "request_started_at")
        finished = _utc(response.request_finished_at, "request_finished_at")
        current = _utc(now, "response validation now")
        if (
            source > captured
            or captured > current
            or finished < started
            or finished > current
            or not started <= captured <= finished
        ):
            raise CanaryExecutionBlocked(
                "provider timestamps are not ordered or are in the future"
            )
        if (
            captured - source
        ).total_seconds() > authorization.maximum_source_age_seconds:
            raise CanaryExecutionBlocked("provider source timestamp is stale")
        if response.adapter_version != authorization.adapter_version:
            raise CanaryExecutionBlocked(
                "adapter version is outside exact authorization scope"
            )
        if (
            response.adapter_source_sha.lower()
            != authorization.adapter_source_sha.lower()
        ):
            raise CanaryExecutionBlocked(
                "adapter source SHA is outside exact authorization scope"
            )
        _sha(response.raw_response_digest, "raw_response_digest")
        _sha(response.normalized_record_digest, "normalized_record_digest")
        _price(response.home_odds, "home_odds")
        _price(response.draw_odds, "draw_odds")
        _price(response.away_odds, "away_odds")

    @staticmethod
    def _build_evidence(
        response: TheRundownCanaryResponseV1,
        request: TheRundownCanaryRequestV1,
        authorization: TheRundownCanaryAuthorizationV1,
        *,
        status: CanaryRunStatus,
    ) -> TheRundownCanaryEvidenceV1:
        try:
            evidence_kind = ObservationEvidenceKind(response.evidence_kind)
        except (TypeError, ValueError):
            evidence_kind = ObservationEvidenceKind.MOCK
        captured_at = response.captured_at or authorization.issued_at
        started_at = response.request_started_at or captured_at
        finished_at = response.request_finished_at or captured_at
        raw_digest = response.raw_response_digest or _digest(
            {"request": request.as_payload(), "outcome": str(response.outcome)}
        )
        normalized_digest = response.normalized_record_digest or _digest(
            {
                "request": request.as_payload(),
                "odds": [response.home_odds, response.draw_odds, response.away_odds],
            }
        )
        provider_event_id = response.provider_event_id or "unresolved-event"
        provider_request_id = response.provider_request_id or request.request_identity
        observation_id = f"therundown-observation:{_digest((request.request_identity, provider_event_id, provider_request_id))[:32]}"
        observation_digest = _digest(
            {
                "request": request.as_payload(),
                "provider_event_id": provider_event_id,
                "provider_request_id": provider_request_id,
                "raw_response_digest": raw_digest,
                "normalized_record_digest": normalized_digest,
            }
        )
        cascade_trace = {
            "execution_mode": CANARY_EXECUTION_MODE,
            "sequence": request.sequence,
            "provider": request.target.provider,
            "league": request.target.league,
            "fixture_key": request.target.fixture_key,
            "provider_event_id": provider_event_id,
            "request_identity": request.request_identity,
            "outcome": CanaryOutcome(response.outcome).value
            if response.outcome in {item.value for item in CanaryOutcome}
            else str(response.outcome),
            "network_called": bool(response.network_execution),
            "retry_count": response.retry_count,
            "request_count": 1,
            "quota_cost_units": response.quota_cost_units,
        }
        cascade_digest = _digest(cascade_trace)
        network = status is CanaryRunStatus.REAL_OBSERVED
        evidence = TheRundownCanaryEvidenceV1(
            status=status,
            evidence_kind=evidence_kind,
            network_execution=network,
            controlled_shadow_run_id=authorization.controlled_shadow_run_id,
            authorization_id=authorization.authorization_id,
            authorization_digest=authorization.authorization_digest,
            qualification_session_id=authorization.qualification_session_id,
            configuration_digest=authorization.configuration_digest,
            target=request.target,
            request_identity=request.request_identity,
            observation_id=observation_id,
            provider_request_id=provider_request_id,
            provider_event_id=provider_event_id,
            observation_digest=observation_digest,
            normalized_record_digest=normalized_digest,
            raw_response_digest=raw_digest,
            cascade_evidence_digest=cascade_digest,
            adapter_version=response.adapter_version or authorization.adapter_version,
            adapter_source_sha=response.adapter_source_sha
            or authorization.adapter_source_sha,
            source_timestamp=response.source_timestamp,
            captured_at=_utc(captured_at, "captured_at"),
            request_started_at=_utc(started_at, "request_started_at"),
            request_finished_at=_utc(finished_at, "request_finished_at"),
            bookmaker_identity=response.bookmaker_identity,
            source_identity=response.source_identity,
            provider_timestamp_provenance=response.provider_timestamp_provenance,
            market_type=MARKET_PREMATCH_1X2,
            market_phase=CANARY_MARKET_PHASE,
            home_odds=response.home_odds,
            draw_odds=response.draw_odds,
            away_odds=response.away_odds,
            request_count=1,
            quota_before=response.quota_before,
            quota_after=response.quota_after,
            quota_cost_units=response.quota_cost_units,
            candidate_only=True,
            canonical_attestation_eligible=network,
            qualification_report_eligible=network,
            builder2_receipt_eligible=False,
            failure_reason=None
            if status is not CanaryRunStatus.NO_OBSERVATION
            else str(response.outcome),
        )
        return replace(evidence, observation_digest=evidence.observation_input_digest)


__all__ = [
    "CANARY_ACTION",
    "CANARY_ATTESTATION_INPUT_SCHEMA_VERSION",
    "CANARY_EXECUTION_MODE",
    "CANARY_MARKET_PHASE",
    "CANARY_OBSERVATION_SCHEMA_VERSION",
    "CANARY_SCHEMA_VERSION",
    "THERUNDOWN_PROVIDER",
    "CanaryContractError",
    "CanaryExecutionBlocked",
    "CanaryLifecycleCompatibility",
    "CanaryOutcome",
    "CanaryRunStatus",
    "FakeTheRundownCanaryTransport",
    "TheRundownCanaryAuthorizationV1",
    "TheRundownCanaryConfigurationV1",
    "TheRundownCanaryEvidenceV1",
    "TheRundownCanaryLifecycleArtifactV1",
    "TheRundownCanaryNetworkTransport",
    "TheRundownCanaryPacingPolicyV1",
    "TheRundownCanaryRequestV1",
    "TheRundownCanaryResponseV1",
    "TheRundownCanaryRunResultV1",
    "TheRundownCanaryTargetV1",
    "TheRundownCanaryTransport",
    "TheRundownControlledShadowCanary",
]
