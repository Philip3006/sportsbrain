"""Disabled network-capable TheRundown controlled-shadow execution seam.

This module is the next, separately reviewable layer after the offline canary.
It owns the exact five-league authorization/request/quota boundary and a
network-capable HTTP seam, but it does not register a provider, issue a
qualification receipt, or enable live execution by default.

The live HTTP client is only reachable when the caller explicitly constructs
an executor with ``allow_live_network=True`` and supplies a complete,
unexpired CEO authorization.  Tests use ``TheRundownReplayTransportV1`` or an
in-memory HTTP client; no test performs an internet request.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    TOP5_LEAGUE_CODES,
    TransportCapability,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    CAPTURE_ATTESTATION_CONTRACT_VERSION,
    ObservationEvidenceKind,
    ProviderTimestampProvenance,
)
from src.football.top5_therundown_shadow_canary import (
    CANARY_EXECUTION_MODE,
    RECEIPT_SCHEMA_VERSION,
    THERUNDOWN_PROVIDER,
    CanaryContractError,
    CanaryOutcome,
    TheRundownCanaryNetworkTransport,
    TheRundownCanaryTargetV1,
)

NETWORK_SHADOW_SCHEMA_VERSION = "top5-therundown-network-shadow-v1"
NETWORK_REQUEST_SCHEMA_VERSION = "top5-therundown-network-request-v1"
NETWORK_RESPONSE_SCHEMA_VERSION = "top5-therundown-network-response-v1"
NETWORK_RUN_SCHEMA_VERSION = "top5-therundown-network-run-v1"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_SAFE_EVIDENCE_KINDS = frozenset(
    {
        ObservationEvidenceKind.MOCK,
        ObservationEvidenceKind.TEST_FIXTURE,
        ObservationEvidenceKind.OFFLINE_REPLAY,
    }
)


class NetworkShadowContractError(CanaryContractError):
    """Malformed network-shadow input or response."""


class NetworkShadowExecutionBlocked(NetworkShadowContractError):
    """Fail-closed refusal before or during a network-shadow run."""


class NetworkShadowRunStatus(str, Enum):
    COMPLETED_NETWORK = "COMPLETED_NETWORK"
    COMPLETED_REPLAY = "COMPLETED_REPLAY"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NetworkShadowContractError(f"{name} is required")
    return value.strip()


def _sha(value: object, name: str) -> str:
    value = _text(value, name)
    if _SHA_RE.fullmatch(value) is None:
        raise NetworkShadowContractError(f"{name} must be a hexadecimal digest")
    return value.lower()


def _utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise NetworkShadowContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _nonnegative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise NetworkShadowContractError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: object, name: str) -> int:
    value = _nonnegative_int(value, name)
    if value == 0:
        raise NetworkShadowContractError(f"{name} must be positive")
    return value


def _number(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise NetworkShadowContractError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise NetworkShadowContractError(f"{name} must be numeric") from exc
    if not isfinite(number) or (positive and number <= 0) or number < 0:
        raise NetworkShadowContractError(f"{name} is outside the allowed range")
    return number


def _price(value: object, name: str) -> float:
    number = _number(value, name, positive=True)
    if number <= 1.0:
        raise NetworkShadowContractError(f"{name} must be a valid decimal price")
    return number


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc(value, "timestamp").isoformat()
    return value


def _digest(value: object) -> str:
    payload = json.dumps(
        _canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TheRundownNetworkParticipantScopeV1:
    fixture_key: str
    home_participant_id: str
    away_participant_id: str

    def validate(self) -> None:
        _text(self.fixture_key, "participant fixture_key")
        _text(self.home_participant_id, "home_participant_id")
        _text(self.away_participant_id, "away_participant_id")
        if self.home_participant_id == self.away_participant_id:
            raise NetworkShadowContractError("participant IDs must be distinct")

    def as_payload(self) -> dict[str, str]:
        self.validate()
        return {
            "fixture_key": self.fixture_key,
            "home_participant_id": self.home_participant_id,
            "away_participant_id": self.away_participant_id,
        }


@dataclass(frozen=True)
class TheRundownNetworkRequestScopeV1:
    fixture_key: str
    request_identity: str

    def validate(self) -> None:
        _text(self.fixture_key, "request scope fixture_key")
        _text(self.request_identity, "request scope request_identity")

    def as_payload(self) -> dict[str, str]:
        self.validate()
        return {
            "fixture_key": self.fixture_key,
            "request_identity": self.request_identity,
        }


@dataclass(frozen=True)
class TheRundownNetworkConfigurationV1:
    """Five-league configuration; disabled by default."""

    targets: tuple[TheRundownCanaryTargetV1, ...]
    participant_scope: tuple[TheRundownNetworkParticipantScopeV1, ...]
    request_scope: tuple[TheRundownNetworkRequestScopeV1, ...]
    adapter_version: str
    adapter_source_sha: str
    maximum_request_count: int
    maximum_datapoints: int
    maximum_quota_cost_units: float
    request_quota_cost_units: float
    maximum_source_age_seconds: int
    minimum_interval_seconds: float = 1.0
    maximum_retries: int = 0
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
            "schema_version": NETWORK_SHADOW_SCHEMA_VERSION,
            "targets": [target.as_payload() for target in self.targets],
            "participant_scope": [item.as_payload() for item in self.participant_scope],
            "request_scope": [item.as_payload() for item in self.request_scope],
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "maximum_request_count": self.maximum_request_count,
            "maximum_datapoints": self.maximum_datapoints,
            "maximum_quota_cost_units": self.maximum_quota_cost_units,
            "request_quota_cost_units": self.request_quota_cost_units,
            "maximum_source_age_seconds": self.maximum_source_age_seconds,
            "minimum_interval_seconds": self.minimum_interval_seconds,
            "maximum_retries": self.maximum_retries,
            "enabled": self.enabled,
            "no_bet": self.no_bet,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "monetary_spend_authorized": self.monetary_spend_authorized,
        }

    def validate(self) -> None:
        if self.enabled not in (True, False):
            raise NetworkShadowContractError("enabled must be boolean")
        if len(self.targets) != len(TOP5_LEAGUE_CODES):
            raise NetworkShadowContractError("exactly five Top-5 targets are required")
        for target in self.targets:
            target.validate()
        leagues = tuple(target.league for target in self.targets)
        if set(leagues) != set(TOP5_LEAGUE_CODES):
            raise NetworkShadowContractError(
                "authorization must cover all five leagues"
            )
        fixtures = tuple(target.fixture_key for target in self.targets)
        events = tuple(target.provider_event_id for target in self.targets)
        if len(set(fixtures)) != len(fixtures) or len(set(events)) != len(events):
            raise NetworkShadowContractError(
                "target fixture/event scope must be unique"
            )
        for item in self.participant_scope:
            item.validate()
        participant_keys = tuple(item.fixture_key for item in self.participant_scope)
        if set(participant_keys) != set(fixtures):
            raise NetworkShadowContractError("participant scope must match targets")
        for item in self.request_scope:
            item.validate()
        request_keys = tuple(item.fixture_key for item in self.request_scope)
        if set(request_keys) != set(fixtures):
            raise NetworkShadowContractError("request scope must match targets")
        request_ids = tuple(item.request_identity for item in self.request_scope)
        if len(set(request_ids)) != len(request_ids):
            raise NetworkShadowContractError("request identities must be unique")
        _text(self.adapter_version, "adapter_version")
        _sha(self.adapter_source_sha, "adapter_source_sha")
        _nonnegative_int(self.maximum_request_count, "maximum_request_count")
        _nonnegative_int(self.maximum_datapoints, "maximum_datapoints")
        _number(
            self.maximum_quota_cost_units, "maximum_quota_cost_units", positive=True
        )
        _number(
            self.request_quota_cost_units, "request_quota_cost_units", positive=True
        )
        _positive_int(self.maximum_source_age_seconds, "maximum_source_age_seconds")
        _number(self.minimum_interval_seconds, "minimum_interval_seconds")
        if _nonnegative_int(self.maximum_retries, "maximum_retries") != 0:
            raise NetworkShadowExecutionBlocked("network shadow retries are forbidden")
        for name, value, expected in (
            ("no_bet", self.no_bet, True),
            ("publication", self.publication, False),
            ("production_activation", self.production_activation, False),
            ("monetary_spend_authorized", self.monetary_spend_authorized, False),
        ):
            if value is not expected:
                raise NetworkShadowExecutionBlocked(
                    f"unsafe configuration flag: {name}"
                )
        _sha(self.configuration_digest, "configuration_digest")
        if self.configuration_digest != self.computed_configuration_digest:
            raise NetworkShadowContractError("configuration digest mismatch")

    def participant_for(self, fixture_key: str) -> TheRundownNetworkParticipantScopeV1:
        for item in self.participant_scope:
            if item.fixture_key == fixture_key:
                return item
        raise NetworkShadowContractError("participant scope is missing")

    def request_for(self, fixture_key: str) -> TheRundownNetworkRequestScopeV1:
        for item in self.request_scope:
            if item.fixture_key == fixture_key:
                return item
        raise NetworkShadowContractError("request scope is missing")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            **self._payload_without_digest(),
            "configuration_digest": self.computed_configuration_digest,
        }


@dataclass(frozen=True)
class TheRundownNetworkAuthorizationV1:
    """Caller-supplied CEO authority; this module cannot create it."""

    authorization_id: str
    ceo_authorization_identity: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    provider: str
    targets: tuple[TheRundownCanaryTargetV1, ...]
    participant_scope: tuple[TheRundownNetworkParticipantScopeV1, ...]
    request_scope: tuple[TheRundownNetworkRequestScopeV1, ...]
    adapter_version: str
    adapter_source_sha: str
    configuration_digest: str
    maximum_request_count: int
    maximum_datapoints: int
    maximum_quota_cost_units: float
    request_quota_cost_units: float
    maximum_source_age_seconds: int
    issued_at: datetime
    expires_at: datetime
    minimum_interval_seconds: float = 1.0
    maximum_retries: int = 0
    no_bet: bool = True
    publication: bool = False
    production_activation: bool = False
    monetary_spend_authorized: bool = False
    schema_version: str = NETWORK_SHADOW_SCHEMA_VERSION

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
            "provider": self.provider,
            "targets": [target.as_payload() for target in self.targets],
            "participant_scope": [item.as_payload() for item in self.participant_scope],
            "request_scope": [item.as_payload() for item in self.request_scope],
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "configuration_digest": self.configuration_digest,
            "maximum_request_count": self.maximum_request_count,
            "maximum_datapoints": self.maximum_datapoints,
            "maximum_quota_cost_units": self.maximum_quota_cost_units,
            "request_quota_cost_units": self.request_quota_cost_units,
            "maximum_source_age_seconds": self.maximum_source_age_seconds,
            "issued_at": _utc(self.issued_at, "issued_at").isoformat(),
            "expires_at": _utc(self.expires_at, "expires_at").isoformat(),
            "minimum_interval_seconds": self.minimum_interval_seconds,
            "maximum_retries": self.maximum_retries,
            "no_bet": self.no_bet,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "monetary_spend_authorized": self.monetary_spend_authorized,
        }

    def validate(
        self,
        configuration: TheRundownNetworkConfigurationV1 | None = None,
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
        if self.schema_version != NETWORK_SHADOW_SCHEMA_VERSION:
            raise NetworkShadowContractError("unsupported network authorization schema")
        if self.provider != THERUNDOWN_PROVIDER:
            raise NetworkShadowContractError("network provider must be therundown")
        if len(self.targets) != len(TOP5_LEAGUE_CODES):
            raise NetworkShadowContractError("authorization must cover five targets")
        for target in self.targets:
            target.validate()
        if {target.league for target in self.targets} != set(TOP5_LEAGUE_CODES):
            raise NetworkShadowContractError(
                "authorization must cover all five leagues"
            )
        target_keys = {target.fixture_key for target in self.targets}
        if {item.fixture_key for item in self.participant_scope} != target_keys:
            raise NetworkShadowContractError("participant authorization scope mismatch")
        if {item.fixture_key for item in self.request_scope} != target_keys:
            raise NetworkShadowContractError("request authorization scope mismatch")
        for item in self.participant_scope:
            item.validate()
        for item in self.request_scope:
            item.validate()
        _text(self.adapter_version, "adapter_version")
        _sha(self.adapter_source_sha, "adapter_source_sha")
        _sha(self.configuration_digest, "configuration_digest")
        _nonnegative_int(self.maximum_request_count, "maximum_request_count")
        _nonnegative_int(self.maximum_datapoints, "maximum_datapoints")
        _number(
            self.maximum_quota_cost_units, "maximum_quota_cost_units", positive=True
        )
        request_cost = _number(
            self.request_quota_cost_units, "request_quota_cost_units", positive=True
        )
        _positive_int(self.maximum_source_age_seconds, "maximum_source_age_seconds")
        _number(self.minimum_interval_seconds, "minimum_interval_seconds")
        if self.maximum_retries != 0:
            raise NetworkShadowExecutionBlocked("network shadow retries are forbidden")
        issued = _utc(self.issued_at, "issued_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= issued:
            raise NetworkShadowContractError(
                "authorization expiry must follow issue time"
            )
        current = _utc(now or datetime.now(timezone.utc), "authorization now")
        if current < issued or current >= expires:
            raise NetworkShadowExecutionBlocked(
                "network authorization is expired or not yet valid"
            )
        if self.maximum_request_count < len(self.targets):
            raise NetworkShadowExecutionBlocked(
                "request budget is below five-league scope"
            )
        if self.maximum_datapoints < len(self.targets):
            raise NetworkShadowExecutionBlocked(
                "datapoint budget is below five-league scope"
            )
        if self.maximum_quota_cost_units < request_cost * len(self.targets):
            raise NetworkShadowExecutionBlocked(
                "quota budget is below five-league scope"
            )
        for name, value, expected in (
            ("no_bet", self.no_bet, True),
            ("publication", self.publication, False),
            ("production_activation", self.production_activation, False),
            ("monetary_spend_authorized", self.monetary_spend_authorized, False),
        ):
            if value is not expected:
                raise NetworkShadowExecutionBlocked(
                    f"unsafe authorization flag: {name}"
                )
        if configuration is not None:
            configuration.validate()
            pairs = (
                (self.targets, configuration.targets, "target scope"),
                (
                    self.participant_scope,
                    configuration.participant_scope,
                    "participant scope",
                ),
                (self.request_scope, configuration.request_scope, "request scope"),
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
                    self.maximum_datapoints,
                    configuration.maximum_datapoints,
                    "datapoint budget",
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
                (
                    self.minimum_interval_seconds,
                    configuration.minimum_interval_seconds,
                    "pacing interval",
                ),
                (self.maximum_retries, configuration.maximum_retries, "retry policy"),
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
                    raise NetworkShadowExecutionBlocked(
                        f"authorization does not match configuration: {label}"
                    )

    def request_for(
        self,
        target: TheRundownCanaryTargetV1,
        configuration: TheRundownNetworkConfigurationV1,
    ) -> TheRundownNetworkRequestV1:
        request_scope = configuration.request_for(target.fixture_key)
        participant = configuration.participant_for(target.fixture_key)
        return TheRundownNetworkRequestV1(
            controlled_shadow_run_id=self.controlled_shadow_run_id,
            qualification_session_id=self.qualification_session_id,
            authorization_id=self.authorization_id,
            ceo_authorization_identity=self.ceo_authorization_identity,
            authorization_digest=self.authorization_digest,
            authorization_expires_at=self.expires_at,
            configuration_digest=self.configuration_digest,
            target=target,
            request_identity=request_scope.request_identity,
            home_participant_id=participant.home_participant_id,
            away_participant_id=participant.away_participant_id,
        )

    def as_payload(self) -> dict[str, object]:
        self.validate(now=self.issued_at)
        return {
            **self._payload_without_digest(),
            "authorization_digest": self.authorization_digest,
        }


@dataclass(frozen=True)
class TheRundownNetworkRequestV1:
    controlled_shadow_run_id: str
    qualification_session_id: str
    authorization_id: str
    ceo_authorization_identity: str
    authorization_digest: str
    authorization_expires_at: datetime
    configuration_digest: str
    target: TheRundownCanaryTargetV1
    request_identity: str
    home_participant_id: str
    away_participant_id: str
    sequence: int = 0
    market_type: str = MARKET_PREMATCH_1X2
    execution_mode: str = CANARY_EXECUTION_MODE

    def validate(self, *, now: datetime | None = None) -> None:
        for name, value in (
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("qualification_session_id", self.qualification_session_id),
            ("authorization_id", self.authorization_id),
            ("ceo_authorization_identity", self.ceo_authorization_identity),
            ("request_identity", self.request_identity),
            ("home_participant_id", self.home_participant_id),
            ("away_participant_id", self.away_participant_id),
        ):
            _text(value, name)
        _sha(self.authorization_digest, "authorization_digest")
        _sha(self.configuration_digest, "configuration_digest")
        self.target.validate()
        if self.sequence != 0:
            raise NetworkShadowExecutionBlocked(
                "unexpected retry or non-sequential request"
            )
        if self.market_type != MARKET_PREMATCH_1X2:
            raise NetworkShadowContractError("only pre-match 1X2 is supported")
        if self.execution_mode != CANARY_EXECUTION_MODE:
            raise NetworkShadowExecutionBlocked("network execution must be sequential")
        if self.home_participant_id == self.away_participant_id:
            raise NetworkShadowContractError("participant IDs must be distinct")
        current = _utc(now or datetime.now(timezone.utc), "request now")
        if current >= _utc(self.authorization_expires_at, "authorization expiry"):
            raise NetworkShadowExecutionBlocked("request authorization has expired")

    def as_payload(self) -> dict[str, object]:
        self.validate(
            now=self.authorization_expires_at.replace(microsecond=0)
            - timedelta(seconds=1)
        )
        return {
            "schema_version": NETWORK_REQUEST_SCHEMA_VERSION,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "authorization_id": self.authorization_id,
            "ceo_authorization_identity": self.ceo_authorization_identity,
            "authorization_digest": self.authorization_digest,
            "authorization_expires_at": _utc(
                self.authorization_expires_at, "authorization expiry"
            ).isoformat(),
            "configuration_digest": self.configuration_digest,
            "target": self.target.as_payload(),
            "request_identity": self.request_identity,
            "home_participant_id": self.home_participant_id,
            "away_participant_id": self.away_participant_id,
            "sequence": self.sequence,
            "market_type": self.market_type,
            "execution_mode": self.execution_mode,
        }


@dataclass(frozen=True)
class TheRundownNetworkResponseV1:
    """Normalized provider response plus rate-limit/tier/delay evidence."""

    outcome: CanaryOutcome | str
    provider: str
    league: str
    fixture_key: str
    provider_event_id: str
    provider_request_id: str
    home_team: str
    away_team: str
    home_participant_id: str
    away_participant_id: str
    bookmaker_identity: str
    source_identity: str
    source_timestamp: datetime | None
    captured_at: datetime | None
    request_started_at: datetime | None
    request_finished_at: datetime | None
    home_odds: float | None
    draw_odds: float | None
    away_odds: float | None
    adapter_version: str
    adapter_source_sha: str
    raw_response_digest: str
    provider_record_digest: str
    normalized_record_digest: str
    cascade_evidence_digest: str
    quota_before: int | None
    quota_after: int | None
    quota_cost_units: float
    datapoint_count: int
    rate_limit_remaining: int | None
    rate_limit_reset_at: datetime | None
    account_tier: str
    provider_delay_seconds: float | None
    provider_timestamp_provenance: str = (
        ProviderTimestampProvenance.PROVIDER_SOURCE_TIMESTAMP.value
    )
    http_status: int | None = None
    retry_count: int = 0
    # Safe defaults are deliberate: callers must opt into the real-evidence
    # boundary through a successful NETWORK_CAPABLE transport response.
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
    error_detail: str | None = None
    raw_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "raw_metadata", MappingProxyType(dict(self.raw_metadata))
        )

    @classmethod
    def from_payload(cls, payload: object) -> TheRundownNetworkResponseV1:
        raw = payload if isinstance(payload, Mapping) else {}

        def dt(name: str) -> datetime | None:
            value = raw.get(name)
            if value is None or isinstance(value, datetime):
                return value
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    return None
            return None

        return cls(
            outcome=raw.get("outcome", CanaryOutcome.MALFORMED),
            provider=raw.get("provider", ""),
            league=raw.get("league", ""),
            fixture_key=raw.get("fixture_key", ""),
            provider_event_id=raw.get("provider_event_id", ""),
            provider_request_id=raw.get("provider_request_id", ""),
            home_team=raw.get("home_team", ""),
            away_team=raw.get("away_team", ""),
            home_participant_id=raw.get("home_participant_id", ""),
            away_participant_id=raw.get("away_participant_id", ""),
            bookmaker_identity=raw.get("bookmaker_identity", ""),
            source_identity=raw.get("source_identity", ""),
            source_timestamp=dt("source_timestamp"),
            captured_at=dt("captured_at"),
            request_started_at=dt("request_started_at"),
            request_finished_at=dt("request_finished_at"),
            home_odds=raw.get("home_odds"),
            draw_odds=raw.get("draw_odds"),
            away_odds=raw.get("away_odds"),
            adapter_version=raw.get("adapter_version", ""),
            adapter_source_sha=raw.get("adapter_source_sha", ""),
            raw_response_digest=raw.get("raw_response_digest", ""),
            provider_record_digest=raw.get("provider_record_digest", ""),
            normalized_record_digest=raw.get("normalized_record_digest", ""),
            cascade_evidence_digest=raw.get("cascade_evidence_digest", ""),
            quota_before=raw.get("quota_before"),
            quota_after=raw.get("quota_after"),
            quota_cost_units=raw.get("quota_cost_units", 0.0),
            datapoint_count=raw.get("datapoint_count", 0),
            rate_limit_remaining=raw.get("rate_limit_remaining"),
            rate_limit_reset_at=dt("rate_limit_reset_at"),
            account_tier=raw.get("account_tier", ""),
            provider_delay_seconds=raw.get("provider_delay_seconds"),
            provider_timestamp_provenance=raw.get(
                "provider_timestamp_provenance",
                ProviderTimestampProvenance.PROVIDER_SOURCE_TIMESTAMP.value,
            ),
            http_status=raw.get("http_status"),
            retry_count=raw.get("retry_count", 0),
            evidence_kind=raw.get(
                "evidence_kind", ObservationEvidenceKind.TEST_FIXTURE
            ),
            network_execution=raw.get("network_execution", False),
            no_bet=raw.get("no_bet", True),
            publication=raw.get("publication", False),
            production_activation=raw.get("production_activation", False),
            monetary_spend_authorized=raw.get("monetary_spend_authorized", False),
            authority_attempted=raw.get("authority_attempted", False),
            publication_attempted=raw.get("publication_attempted", False),
            activation_attempted=raw.get("activation_attempted", False),
            ledger_mutated=raw.get("ledger_mutated", False),
            scheduler_registered=raw.get("scheduler_registered", False),
            error_detail=raw.get("error_detail"),
            raw_metadata=raw.get("raw_metadata", {}),
        )


@dataclass(frozen=True)
class TheRundownNetworkHttpRequestV1:
    method: str
    endpoint: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    timeout_seconds: float

    def validate(self) -> None:
        if self.method != "GET":
            raise NetworkShadowContractError("only GET provider requests are supported")
        _text(self.endpoint, "HTTP endpoint")
        if not self.endpoint.startswith(
            ("https://", "http://127.0.0.1", "http://localhost")
        ):
            raise NetworkShadowContractError("provider endpoint must be HTTPS")
        _number(self.timeout_seconds, "timeout_seconds", positive=True)


@dataclass(frozen=True)
class TheRundownNetworkHttpResponseV1:
    status_code: int | None
    payload: object | None
    headers: Mapping[str, str]
    started_at: datetime
    finished_at: datetime
    timed_out: bool = False
    error_detail: str | None = None

    def validate(self) -> None:
        if self.status_code is not None and (
            not isinstance(self.status_code, int) or isinstance(self.status_code, bool)
        ):
            raise NetworkShadowContractError("HTTP status code is invalid")
        started = _utc(self.started_at, "HTTP start")
        finished = _utc(self.finished_at, "HTTP finish")
        if finished < started:
            raise NetworkShadowContractError("HTTP timestamps are not ordered")
        if not isinstance(self.timed_out, bool):
            raise NetworkShadowContractError("HTTP timeout flag is invalid")


class TheRundownNetworkHttpClient(Protocol):
    def execute(
        self, request: TheRundownNetworkHttpRequestV1
    ) -> TheRundownNetworkHttpResponseV1: ...


class TheRundownNetworkPayloadAdapter(Protocol):
    def build_request(
        self, request: TheRundownNetworkRequestV1, *, endpoint: str, api_key: str
    ) -> TheRundownNetworkHttpRequestV1: ...

    def decode_response(
        self,
        request: TheRundownNetworkRequestV1,
        response: TheRundownNetworkHttpResponseV1,
    ) -> TheRundownNetworkResponseV1: ...


class TheRundownCanonicalPayloadAdapterV1:
    """Adapter seam for a reviewed provider-specific JSON normalization."""

    def build_request(
        self, request: TheRundownNetworkRequestV1, *, endpoint: str, api_key: str
    ) -> TheRundownNetworkHttpRequestV1:
        http_request = TheRundownNetworkHttpRequestV1(
            method="GET",
            endpoint=endpoint,
            query={
                "league": request.target.league,
                "fixture_key": request.target.fixture_key,
                "provider_event_id": request.target.provider_event_id,
                "market": MARKET_PREMATCH_1X2,
                "pre_match": "true",
            },
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "X-SportsBrain-Request-Identity": request.request_identity,
            },
            timeout_seconds=10.0,
        )
        http_request.validate()
        return http_request

    def decode_response(
        self,
        request: TheRundownNetworkRequestV1,
        response: TheRundownNetworkHttpResponseV1,
    ) -> TheRundownNetworkResponseV1:
        response.validate()
        status = response.status_code
        if response.timed_out:
            outcome = CanaryOutcome.TIMEOUT
        elif status in (401, 403):
            outcome = CanaryOutcome.AUTH_FAILED
        elif status == 429:
            outcome = CanaryOutcome.RATE_LIMITED
        elif status is None or status >= 500:
            outcome = CanaryOutcome.PROVIDER_UNAVAILABLE
        elif status < 200 or status >= 300:
            outcome = CanaryOutcome.MALFORMED
        else:
            outcome = CanaryOutcome.SUCCESS
        raw = response.payload if isinstance(response.payload, Mapping) else {}
        if outcome is not CanaryOutcome.SUCCESS:
            return _failure_response(request, outcome, response)
        decoded = TheRundownNetworkResponseV1.from_payload(raw)
        if decoded.outcome is not CanaryOutcome.SUCCESS:
            return decoded
        return decoded


class TheRundownUrlLibHttpClientV1:
    """Concrete HTTP client; only called by an explicitly live-enabled run."""

    def execute(
        self, request: TheRundownNetworkHttpRequestV1
    ) -> TheRundownNetworkHttpResponseV1:
        request.validate()
        started = datetime.now(timezone.utc)
        url = request.endpoint
        if request.query:
            url = f"{url}?{urlencode(dict(request.query))}"
        http_request = Request(
            url, headers=dict(request.headers), method=request.method
        )
        try:
            with urlopen(http_request, timeout=request.timeout_seconds) as response:
                body = response.read()
                status_code = int(response.status)
                headers = {
                    str(key): str(value) for key, value in response.headers.items()
                }
        except Exception as exc:  # noqa: BLE001 - transport boundary fails closed
            finished = datetime.now(timezone.utc)
            return TheRundownNetworkHttpResponseV1(
                status_code=None,
                payload=None,
                headers={},
                started_at=started,
                finished_at=finished,
                timed_out=type(exc).__name__ == "TimeoutError",
                error_detail=type(exc).__name__,
            )
        finished = datetime.now(timezone.utc)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        return TheRundownNetworkHttpResponseV1(
            status_code=status_code,
            payload=payload,
            headers=headers,
            started_at=started,
            finished_at=finished,
        )


def _failure_response(
    request: TheRundownNetworkRequestV1,
    outcome: CanaryOutcome,
    response: TheRundownNetworkHttpResponseV1,
) -> TheRundownNetworkResponseV1:
    return TheRundownNetworkResponseV1(
        outcome=outcome,
        provider=THERUNDOWN_PROVIDER,
        league=request.target.league,
        fixture_key=request.target.fixture_key,
        provider_event_id=request.target.provider_event_id,
        provider_request_id=request.request_identity,
        home_team=request.target.home_team,
        away_team=request.target.away_team,
        home_participant_id=request.home_participant_id,
        away_participant_id=request.away_participant_id,
        bookmaker_identity="",
        source_identity="",
        source_timestamp=None,
        captured_at=response.finished_at,
        request_started_at=response.started_at,
        request_finished_at=response.finished_at,
        home_odds=None,
        draw_odds=None,
        away_odds=None,
        adapter_version="",
        adapter_source_sha="0" * 64,
        raw_response_digest="0" * 64,
        provider_record_digest="0" * 64,
        normalized_record_digest="0" * 64,
        cascade_evidence_digest="0" * 64,
        quota_before=None,
        quota_after=None,
        quota_cost_units=0.0,
        datapoint_count=0,
        rate_limit_remaining=None,
        rate_limit_reset_at=None,
        account_tier="",
        provider_delay_seconds=None,
        http_status=response.status_code,
        retry_count=0,
        evidence_kind=ObservationEvidenceKind.MOCK,
        network_execution=True,
        error_detail=response.error_detail,
    )


class TheRundownHttpNetworkTransportV1(TheRundownCanaryNetworkTransport):
    """Network-capable transport with no implicit credentials or retries."""

    test_only = False

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None,
        adapter: TheRundownNetworkPayloadAdapter,
        http_client: TheRundownNetworkHttpClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.endpoint = _text(endpoint, "endpoint")
        self.api_key = api_key
        self.adapter = adapter
        self.http_client = http_client or TheRundownUrlLibHttpClientV1()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.calls: list[TheRundownNetworkRequestV1] = []

    def execute(
        self, request: TheRundownNetworkRequestV1
    ) -> TheRundownNetworkResponseV1:
        request.validate(now=self.clock())
        self.calls.append(request)
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            return _failure_response(
                request,
                CanaryOutcome.AUTH_FAILED,
                TheRundownNetworkHttpResponseV1(
                    status_code=401,
                    payload=None,
                    headers={},
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    error_detail="credential_missing",
                ),
            )
        http_request = self.adapter.build_request(
            request, endpoint=self.endpoint, api_key=self.api_key
        )
        response = self.http_client.execute(http_request)
        decoded = self.adapter.decode_response(request, response)
        if not isinstance(decoded, TheRundownNetworkResponseV1):
            raise NetworkShadowExecutionBlocked(
                "HTTP adapter returned invalid response"
            )
        if decoded.outcome is CanaryOutcome.SUCCESS:
            return _copy_response(decoded, network_execution=True)
        return decoded


def _copy_response(
    response: TheRundownNetworkResponseV1, **changes: object
) -> TheRundownNetworkResponseV1:
    values = {key: getattr(response, key) for key in response.__dataclass_fields__}
    values.update(changes)
    return TheRundownNetworkResponseV1(**values)


class TheRundownReplayTransportV1(TheRundownCanaryNetworkTransport):
    """Deterministic test transport; it can never emit genuine evidence."""

    test_only = True

    def __init__(
        self,
        responses: Mapping[str, TheRundownNetworkResponseV1]
        | Callable[[TheRundownNetworkRequestV1], TheRundownNetworkResponseV1],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.responses = responses
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.calls: list[TheRundownNetworkRequestV1] = []

    def execute(
        self, request: TheRundownNetworkRequestV1
    ) -> TheRundownNetworkResponseV1:
        request.validate(now=self.clock())
        self.calls.append(request)
        response = (
            self.responses(request)
            if callable(self.responses)
            else self.responses[request.target.fixture_key]
        )
        if not isinstance(response, TheRundownNetworkResponseV1):
            raise NetworkShadowExecutionBlocked("replay returned invalid response")
        if (
            response.network_execution
            or response.evidence_kind == ObservationEvidenceKind.REAL_OBSERVED
        ):
            raise NetworkShadowExecutionBlocked("replay cannot emit real evidence")
        return _copy_response(
            response,
            provider=request.target.provider,
            league=request.target.league,
            fixture_key=request.target.fixture_key,
            provider_event_id=request.target.provider_event_id,
            provider_request_id=request.request_identity,
            home_team=request.target.home_team,
            away_team=request.target.away_team,
            home_participant_id=request.home_participant_id,
            away_participant_id=request.away_participant_id,
            network_execution=False,
            evidence_kind=ObservationEvidenceKind.TEST_FIXTURE,
        )


@dataclass(frozen=True)
class TheRundownNetworkShadowCaptureV1:
    target: TheRundownCanaryTargetV1
    request: TheRundownNetworkRequestV1
    response: TheRundownNetworkResponseV1
    evidence_kind: ObservationEvidenceKind
    network_execution: bool
    observation_id: str | None
    observation_digest: str | None
    capture_attestation_digest: str | None
    capture_attestation_input: Mapping[str, object] | None
    observation_input: Mapping[str, object] | None
    failure_reason: str | None
    candidate_only: bool = True
    receipt_eligible: bool = False

    @property
    def canonical_capture_attestation(self) -> Mapping[str, object]:
        return self.capture_attestation_input or {}

    @property
    def qualification_input(self) -> dict[str, object]:
        return {
            "schema_version": "top5-real-provider-observation-v1",
            "provider": self.target.provider,
            "provider_identity": self.target.provider,
            "league": self.target.league,
            "fixture_key": self.target.fixture_key,
            "provider_event_id": self.response.provider_event_id,
            "provider_request_id": self.response.provider_request_id,
            "bookmaker_identity": self.response.bookmaker_identity,
            "source_identity": self.response.source_identity,
            "real_observed": self.evidence_kind
            is ObservationEvidenceKind.REAL_OBSERVED,
            "candidate_only": True,
            "no_bet": True,
            "publication": False,
            "production_activation": False,
            "monetary_spend_authorized": False,
            "source_timestamp": self.response.source_timestamp.isoformat()
            if self.response.source_timestamp
            else None,
            "provider_timestamp_provenance": self.response.provider_timestamp_provenance,
            "observation_id": self.observation_id,
            "observation_digest": self.observation_digest,
            "network_request_count": 1 if self.network_execution else 0,
            "quota_before": self.response.quota_before,
            "quota_after": self.response.quota_after,
            "quota_cost_units": self.response.quota_cost_units,
            "configuration_digest": self.request.configuration_digest,
            "ceo_authorization_identity": self.request.ceo_authorization_identity,
            "observation": self.observation_input,
            "cascade_evidence_digest": self.response.cascade_evidence_digest,
            "cascade_evidence_available": False,
        }

    @property
    def builder2_receipt_input(self) -> dict[str, object]:
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "eligible": False,
            "issuer_present": False,
            "reason": "transport emits evidence inputs only; Builder-2 remains the receipt authority",
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
                "qualification_session_id": self.request.qualification_session_id,
                "controlled_shadow_run_id": self.request.controlled_shadow_run_id,
                "ceo_authorization_id": self.request.authorization_id,
                "ceo_authorization_identity": self.request.ceo_authorization_identity,
                "configuration_digest": self.request.configuration_digest,
                "fixture_key": self.target.fixture_key,
                "provider_identity": self.target.provider,
                "provider_event_id": self.response.provider_event_id,
                "provider_request_id": self.response.provider_request_id,
                "observation_id": self.observation_id,
                "observation_digest": self.observation_digest,
                "normalized_record_digest": self.response.normalized_record_digest,
                "cascade_evidence_digest": self.response.cascade_evidence_digest,
                "capture_attestation_digest": self.capture_attestation_digest,
                "adapter_version": self.response.adapter_version,
                "adapter_source_sha": self.response.adapter_source_sha,
                "qualification_result_digest": None,
                "qualification_status": None,
                "accepted": False,
                "prediction_input_allowed": False,
                "no_bet": True,
                "publication": False,
                "production_activation": False,
                "monetary_spend_authorized": False,
            },
        }

    def validate(self) -> None:
        self.target.validate()
        self.request.validate(
            now=self.request.authorization_expires_at.replace(microsecond=0)
            - timedelta(seconds=1)
        )
        if self.candidate_only is not True or self.receipt_eligible is not False:
            raise NetworkShadowExecutionBlocked("capture cannot carry authority")
        if (
            self.network_execution
            and self.evidence_kind is not ObservationEvidenceKind.REAL_OBSERVED
        ):
            raise NetworkShadowExecutionBlocked(
                "network capture evidence kind is not real"
            )
        if (
            not self.network_execution
            and self.evidence_kind is ObservationEvidenceKind.REAL_OBSERVED
        ):
            raise NetworkShadowExecutionBlocked("replay capture cannot be real")
        if self.evidence_kind is ObservationEvidenceKind.REAL_OBSERVED and (
            not self.observation_input or not self.capture_attestation_input
        ):
            raise NetworkShadowExecutionBlocked("real capture evidence is incomplete")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": NETWORK_SHADOW_SCHEMA_VERSION,
            "provider": self.target.provider,
            "league": self.target.league,
            "fixture_key": self.target.fixture_key,
            "provider_event_id": self.target.provider_event_id,
            "request_identity": self.request.request_identity,
            "ceo_authorization_identity": self.request.ceo_authorization_identity,
            "authorization_digest": self.request.authorization_digest,
            "configuration_digest": self.request.configuration_digest,
            "participant_scope": {
                "home_participant_id": self.request.home_participant_id,
                "away_participant_id": self.request.away_participant_id,
            },
            "evidence_kind": self.evidence_kind.value,
            "network_execution": self.network_execution,
            "candidate_only": True,
            "receipt_eligible": False,
            "observation_id": self.observation_id,
            "observation_digest": self.observation_digest,
            "capture_attestation_digest": self.capture_attestation_digest,
            "capture_attestation_input": dict(self.capture_attestation_input)
            if self.capture_attestation_input
            else None,
            "observation_input": dict(self.observation_input)
            if self.observation_input
            else None,
            "failure_reason": self.failure_reason,
            "qualification_input": self.qualification_input,
            "builder2_receipt_input": self.builder2_receipt_input,
            "safety": {
                "no_bet": True,
                "publication": False,
                "production_activation": False,
                "monetary_spend_authorized": False,
                "authority_changed": False,
                "scheduler_registered": False,
                "ledger_mutated": False,
            },
            "response": _response_payload(self.response),
        }


@dataclass(frozen=True)
class TheRundownNetworkShadowRunResultV1:
    status: NetworkShadowRunStatus
    controlled_shadow_run_id: str
    qualification_session_id: str
    authorization_id: str
    captures: tuple[TheRundownNetworkShadowCaptureV1, ...]
    failures: tuple[str, ...]
    request_count: int
    datapoint_count: int
    quota_cost_units: float
    receipt_eligible: bool = False
    authority_changed: bool = False
    publication: bool = False
    production_activation: bool = False
    monetary_spend_authorized: bool = False

    @property
    def all_five_succeeded(self) -> bool:
        return (
            len(self.captures) == len(TOP5_LEAGUE_CODES)
            and not self.failures
            and all(item.failure_reason is None for item in self.captures)
        )

    def validate(self) -> None:
        for name, value in (
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("qualification_session_id", self.qualification_session_id),
            ("authorization_id", self.authorization_id),
        ):
            _text(value, name)
        for capture in self.captures:
            capture.validate()
        _nonnegative_int(self.request_count, "request_count")
        _nonnegative_int(self.datapoint_count, "datapoint_count")
        _number(self.quota_cost_units, "quota_cost_units")
        for name, value, expected in (
            ("receipt_eligible", self.receipt_eligible, False),
            ("authority_changed", self.authority_changed, False),
            ("publication", self.publication, False),
            ("production_activation", self.production_activation, False),
            ("monetary_spend_authorized", self.monetary_spend_authorized, False),
        ):
            if value is not expected:
                raise NetworkShadowExecutionBlocked(f"unsafe run result flag: {name}")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": NETWORK_RUN_SCHEMA_VERSION,
            "status": self.status.value,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "authorization_id": self.authorization_id,
            "captures": [item.as_payload() for item in self.captures],
            "failures": list(self.failures),
            "request_count": self.request_count,
            "datapoint_count": self.datapoint_count,
            "quota_cost_units": self.quota_cost_units,
            "receipt_eligible": False,
            "authority_changed": False,
            "publication": False,
            "production_activation": False,
            "monetary_spend_authorized": False,
        }


def _response_payload(response: TheRundownNetworkResponseV1) -> dict[str, object]:
    payload = {key: getattr(response, key) for key in response.__dataclass_fields__}
    payload["outcome"] = CanaryOutcome(response.outcome).value
    for key in (
        "source_timestamp",
        "captured_at",
        "request_started_at",
        "request_finished_at",
        "rate_limit_reset_at",
    ):
        value = payload[key]
        payload[key] = value.isoformat() if isinstance(value, datetime) else value
    payload["evidence_kind"] = ObservationEvidenceKind(response.evidence_kind).value
    payload["raw_metadata"] = dict(response.raw_metadata)
    return payload


class TheRundownNetworkShadowExecutorV1:
    """Sequential five-league executor with a live-network kill switch."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        pacer: Callable[[float], None] | None = None,
        allow_live_network: bool = False,
    ) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.pacer = pacer or time.sleep
        self.allow_live_network = allow_live_network

    def run(
        self,
        configuration: TheRundownNetworkConfigurationV1,
        authorization: TheRundownNetworkAuthorizationV1,
        *,
        transport: TheRundownCanaryNetworkTransport,
    ) -> TheRundownNetworkShadowRunResultV1:
        configuration.validate()
        authorization.validate(configuration, now=self.clock())
        if configuration.enabled is not True:
            raise NetworkShadowExecutionBlocked("network shadow is disabled by default")
        if (
            getattr(transport, "transport_capability", None)
            is not TransportCapability.NETWORK_CAPABLE
        ):
            raise NetworkShadowExecutionBlocked("transport is not NETWORK_CAPABLE")
        test_only = bool(getattr(transport, "test_only", False))
        if not test_only and self.allow_live_network is not True:
            raise NetworkShadowExecutionBlocked("live network execution is disabled")
        captures: list[TheRundownNetworkShadowCaptureV1] = []
        failures: list[str] = []
        request_count = 0
        datapoints = 0
        quota_units = 0.0
        for index, target in enumerate(configuration.targets):
            if index:
                self.pacer(configuration.minimum_interval_seconds)
            if request_count + 1 > authorization.maximum_request_count:
                failures.append(f"{target.league}:REQUEST_BUDGET_EXCEEDED")
                break
            request = authorization.request_for(target, configuration)
            request.validate(now=self.clock())
            try:
                response = transport.execute(request)
            except Exception as exc:  # noqa: BLE001 - transport boundary fails closed
                failures.append(f"{target.league}:TRANSPORT_ERROR:{type(exc).__name__}")
                request_count += 1
                continue
            request_count += 1
            try:
                self._validate_response(
                    response,
                    request,
                    authorization,
                    test_only=test_only,
                    now=self.clock(),
                )
            except Exception as exc:  # noqa: BLE001 - malformed provider data fails closed
                failures.append(f"{target.league}:{exc}")
                if "budget" in str(exc) or "overrun" in str(exc):
                    break
                captures.append(_failure_capture(target, request, response, str(exc)))
                continue
            next_datapoints = datapoints + response.datapoint_count
            next_quota_units = quota_units + response.quota_cost_units
            if next_datapoints > authorization.maximum_datapoints:
                failures.append(f"{target.league}:DATAPOINT_BUDGET_OVERRUN")
                break
            if next_quota_units > authorization.maximum_quota_cost_units:
                failures.append(f"{target.league}:QUOTA_BUDGET_OVERRUN")
                break
            datapoints = next_datapoints
            quota_units = next_quota_units
            if response.outcome is not CanaryOutcome.SUCCESS:
                captures.append(
                    _failure_capture(
                        target,
                        request,
                        response,
                        CanaryOutcome(response.outcome).value,
                    )
                )
                failures.append(
                    f"{target.league}:{CanaryOutcome(response.outcome).value}"
                )
                continue
            capture = _build_capture(target, request, response, test_only=test_only)
            captures.append(capture)
        status = (
            NetworkShadowRunStatus.COMPLETED_REPLAY
            if test_only and not failures and len(captures) == len(TOP5_LEAGUE_CODES)
            else NetworkShadowRunStatus.COMPLETED_NETWORK
            if not test_only
            and not failures
            and len(captures) == len(TOP5_LEAGUE_CODES)
            else NetworkShadowRunStatus.PARTIAL
            if captures or failures
            else NetworkShadowRunStatus.BLOCKED
        )
        result = TheRundownNetworkShadowRunResultV1(
            status=status,
            controlled_shadow_run_id=authorization.controlled_shadow_run_id,
            qualification_session_id=authorization.qualification_session_id,
            authorization_id=authorization.authorization_id,
            captures=tuple(captures),
            failures=tuple(failures),
            request_count=request_count,
            datapoint_count=datapoints,
            quota_cost_units=quota_units,
        )
        result.validate()
        return result

    @staticmethod
    def _validate_response(
        response: TheRundownNetworkResponseV1,
        request: TheRundownNetworkRequestV1,
        authorization: TheRundownNetworkAuthorizationV1,
        *,
        test_only: bool,
        now: datetime,
    ) -> None:
        if not isinstance(response, TheRundownNetworkResponseV1):
            raise NetworkShadowExecutionBlocked("transport returned invalid response")
        try:
            outcome = CanaryOutcome(response.outcome)
        except (TypeError, ValueError) as exc:
            raise NetworkShadowExecutionBlocked("response outcome is invalid") from exc
        if outcome is CanaryOutcome.SUCCESS and (
            response.home_participant_id != request.home_participant_id
            or response.away_participant_id != request.away_participant_id
        ):
            raise NetworkShadowExecutionBlocked("participant mismatch")
        for name, value, expected in (
            ("provider", response.provider, request.target.provider),
            ("league", response.league, request.target.league),
            ("fixture_key", response.fixture_key, request.target.fixture_key),
            (
                "provider_event_id",
                response.provider_event_id,
                request.target.provider_event_id,
            ),
            (
                "provider_request_id",
                response.provider_request_id,
                request.request_identity,
            ),
            ("home_team", response.home_team, request.target.home_team),
            ("away_team", response.away_team, request.target.away_team),
        ):
            if outcome is CanaryOutcome.SUCCESS and value != expected:
                raise NetworkShadowExecutionBlocked(f"{name} mismatch")
        if response.retry_count != 0:
            raise NetworkShadowExecutionBlocked("unexpected retries are forbidden")
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
                raise NetworkShadowExecutionBlocked(f"unsafe response flag: {name}")
        _nonnegative_int(response.datapoint_count, "datapoint_count")
        _number(response.quota_cost_units, "quota_cost_units")
        if response.datapoint_count == 0 and outcome is CanaryOutcome.SUCCESS:
            raise NetworkShadowExecutionBlocked("successful response has no datapoints")
        if response.quota_cost_units > authorization.request_quota_cost_units:
            raise NetworkShadowExecutionBlocked("quota budget overrun")
        if response.quota_before is not None:
            _nonnegative_int(response.quota_before, "quota_before")
        if response.quota_after is not None:
            _nonnegative_int(response.quota_after, "quota_after")
        if (
            response.quota_before is not None
            and response.quota_after is not None
            and response.quota_after > response.quota_before
        ):
            raise NetworkShadowExecutionBlocked("quota-after exceeds quota-before")
        if outcome is not CanaryOutcome.SUCCESS:
            if test_only and response.network_execution:
                raise NetworkShadowExecutionBlocked(
                    "replay cannot claim network execution"
                )
            return
        try:
            evidence_kind = ObservationEvidenceKind(response.evidence_kind)
        except (TypeError, ValueError) as exc:
            raise NetworkShadowExecutionBlocked(
                "response evidence kind is invalid"
            ) from exc
        if test_only:
            if response.network_execution or evidence_kind not in _SAFE_EVIDENCE_KINDS:
                raise NetworkShadowExecutionBlocked("replay cannot emit real evidence")
        elif (
            not response.network_execution
            or evidence_kind is not ObservationEvidenceKind.REAL_OBSERVED
        ):
            raise NetworkShadowExecutionBlocked("live response must be REAL_OBSERVED")
        if (
            not response.bookmaker_identity.strip()
            or not response.source_identity.strip()
        ):
            raise NetworkShadowExecutionBlocked(
                "bookmaker/source provenance is missing"
            )
        if response.source_timestamp is None or response.captured_at is None:
            raise NetworkShadowExecutionBlocked("source/capture timestamp is missing")
        if response.request_started_at is None or response.request_finished_at is None:
            raise NetworkShadowExecutionBlocked("request timing provenance is missing")
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
        ):
            raise NetworkShadowExecutionBlocked("response timestamps are not ordered")
        if (
            captured - source
        ).total_seconds() > authorization.maximum_source_age_seconds:
            raise NetworkShadowExecutionBlocked("stale odds")
        if (
            response.home_team != request.target.home_team
            or response.away_team != request.target.away_team
        ):
            raise NetworkShadowExecutionBlocked("participant mismatch")
        _price(response.home_odds, "home_odds")
        _price(response.draw_odds, "draw_odds")
        _price(response.away_odds, "away_odds")
        if (
            response.provider_timestamp_provenance
            != ProviderTimestampProvenance.PROVIDER_SOURCE_TIMESTAMP.value
        ):
            raise NetworkShadowExecutionBlocked(
                "provider timestamp provenance is unsupported"
            )
        _text(response.adapter_version, "adapter_version")
        for name, value in (
            ("adapter_source_sha", response.adapter_source_sha),
            ("raw_response_digest", response.raw_response_digest),
            ("provider_record_digest", response.provider_record_digest),
            ("normalized_record_digest", response.normalized_record_digest),
            ("cascade_evidence_digest", response.cascade_evidence_digest),
        ):
            _sha(value, name)
        if response.adapter_version != authorization.adapter_version:
            raise NetworkShadowExecutionBlocked("adapter version mismatch")
        if (
            response.adapter_source_sha.lower()
            != authorization.adapter_source_sha.lower()
        ):
            raise NetworkShadowExecutionBlocked("adapter source SHA mismatch")
        if response.quota_before is None or response.quota_after is None:
            raise NetworkShadowExecutionBlocked(
                "quota-before/after evidence is missing"
            )
        if response.rate_limit_remaining is None:
            raise NetworkShadowExecutionBlocked("rate-limit evidence is missing")
        _nonnegative_int(response.rate_limit_remaining, "rate_limit_remaining")
        if response.rate_limit_reset_at is None:
            raise NetworkShadowExecutionBlocked("rate-limit reset evidence is missing")
        _utc(response.rate_limit_reset_at, "rate_limit_reset_at")
        _text(response.account_tier, "account_tier")
        if response.provider_delay_seconds is None:
            raise NetworkShadowExecutionBlocked("provider delay evidence is missing")
        _number(response.provider_delay_seconds, "provider_delay_seconds")


def _build_capture(
    target: TheRundownCanaryTargetV1,
    request: TheRundownNetworkRequestV1,
    response: TheRundownNetworkResponseV1,
    *,
    test_only: bool,
) -> TheRundownNetworkShadowCaptureV1:
    evidence_kind = (
        ObservationEvidenceKind.TEST_FIXTURE
        if test_only
        else ObservationEvidenceKind.REAL_OBSERVED
    )
    network = not test_only
    captured = (
        response.captured_at
        or response.request_finished_at
        or datetime.now(timezone.utc)
    )
    started = response.request_started_at or captured
    finished = response.request_finished_at or captured
    capture_attestation = {
        "schema_version": CAPTURE_ATTESTATION_CONTRACT_VERSION,
        "controlled_shadow_run_id": request.controlled_shadow_run_id,
        "ceo_authorization_id": request.authorization_id,
        "qualification_session_id": request.qualification_session_id,
        "provider_identity": target.provider,
        "fixture_key": target.fixture_key,
        "provider_event_id": response.provider_event_id,
        "provider_request_id": response.provider_request_id,
        "adapter_version": response.adapter_version,
        "adapter_source_sha": response.adapter_source_sha,
        "cascade_evidence_digest": response.cascade_evidence_digest,
        "raw_response_digest": response.raw_response_digest,
        "normalized_record_digest": response.normalized_record_digest,
        "captured_at": _utc(captured, "captured_at").isoformat(),
        "network_execution": network,
        "no_bet": True,
        "publication": False,
        "monetary_spend_authorized": False,
    }
    observation = {
        "schema_version": "top5-real-provider-observation-v1",
        "observation_id": f"therundown-observation:{_digest((request.request_identity, response.provider_event_id))[:32]}",
        "qualification_session_id": request.qualification_session_id,
        "evidence_kind": evidence_kind.value,
        "provider_identity": target.provider,
        "provider_event_id": response.provider_event_id,
        "provider_request_id": response.provider_request_id,
        "league": target.league,
        "fixture_key": target.fixture_key,
        "home_team": target.home_team,
        "away_team": target.away_team,
        "kickoff": target.kickoff.isoformat(),
        "market_type": MARKET_PREMATCH_1X2,
        "market_phase": "PRE_MATCH",
        "home_odds": response.home_odds,
        "draw_odds": response.draw_odds,
        "away_odds": response.away_odds,
        "bookmaker_identity": response.bookmaker_identity,
        "source_identity": response.source_identity,
        "source_timestamp": response.source_timestamp.isoformat()
        if response.source_timestamp
        else None,
        "provider_timestamp_provenance": ProviderTimestampProvenance.PROVIDER_SOURCE_TIMESTAMP.value,
        "captured_at": _utc(captured, "captured_at").isoformat(),
        "request_started_at": _utc(started, "request_started_at").isoformat(),
        "request_finished_at": _utc(finished, "request_finished_at").isoformat(),
        "latency_ms": max(
            0,
            int(
                (_utc(finished, "finished") - _utc(started, "started")).total_seconds()
                * 1000
            ),
        ),
        "adapter_version": response.adapter_version,
        "adapter_source_sha": response.adapter_source_sha,
        "raw_response_digest": response.raw_response_digest,
        "normalized_record_digest": response.normalized_record_digest,
        "cascade_evidence": {
            "candidate_only": True,
            "provider": target.provider,
            "request_identity": request.request_identity,
            "cascade_evidence_digest": response.cascade_evidence_digest,
        },
        "quota_before": response.quota_before,
        "quota_after": response.quota_after,
        "quota_cost_units": response.quota_cost_units,
        "network_request_count": 0 if test_only else 1,
        "monetary_spend_authorized": False,
        "delayed_observation": False,
        "synthetic_reconstruction": test_only,
        "no_bet": True,
        "publication_enabled": False,
        "production_activation": False,
        "ledger_mutated": False,
        "sealed_data_accessed": False,
        "research_mutated": False,
        "capture_attestation": capture_attestation,
        "candidate_only": True,
    }
    observation_digest = _digest(observation)
    attestation_digest = _digest(capture_attestation)
    observation["observation_digest"] = observation_digest
    return TheRundownNetworkShadowCaptureV1(
        target=target,
        request=request,
        response=response,
        evidence_kind=evidence_kind,
        network_execution=network,
        observation_id=str(observation["observation_id"]),
        observation_digest=observation_digest,
        capture_attestation_digest=attestation_digest,
        capture_attestation_input=capture_attestation,
        observation_input=observation,
        failure_reason=None,
    )


def _failure_capture(
    target: TheRundownCanaryTargetV1,
    request: TheRundownNetworkRequestV1,
    response: TheRundownNetworkResponseV1,
    reason: str,
) -> TheRundownNetworkShadowCaptureV1:
    return TheRundownNetworkShadowCaptureV1(
        target=target,
        request=request,
        response=response,
        evidence_kind=ObservationEvidenceKind.MOCK,
        network_execution=False,
        observation_id=None,
        observation_digest=None,
        capture_attestation_digest=None,
        capture_attestation_input=None,
        observation_input=None,
        failure_reason=reason,
    )


__all__ = [
    "NETWORK_REQUEST_SCHEMA_VERSION",
    "NETWORK_RESPONSE_SCHEMA_VERSION",
    "NETWORK_RUN_SCHEMA_VERSION",
    "NETWORK_SHADOW_SCHEMA_VERSION",
    "NetworkShadowContractError",
    "NetworkShadowExecutionBlocked",
    "NetworkShadowRunStatus",
    "TheRundownCanonicalPayloadAdapterV1",
    "TheRundownHttpNetworkTransportV1",
    "TheRundownNetworkAuthorizationV1",
    "TheRundownNetworkConfigurationV1",
    "TheRundownNetworkHttpClient",
    "TheRundownNetworkHttpRequestV1",
    "TheRundownNetworkHttpResponseV1",
    "TheRundownNetworkParticipantScopeV1",
    "TheRundownNetworkPayloadAdapter",
    "TheRundownNetworkRequestScopeV1",
    "TheRundownNetworkRequestV1",
    "TheRundownNetworkResponseV1",
    "TheRundownNetworkShadowCaptureV1",
    "TheRundownNetworkShadowExecutorV1",
    "TheRundownNetworkShadowRunResultV1",
    "TheRundownReplayTransportV1",
    "TheRundownUrlLibHttpClientV1",
]
