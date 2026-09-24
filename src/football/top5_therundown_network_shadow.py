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
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Protocol
from urllib.error import URLError

import certifi
import requests

from src.football.odds.therundown import (
    THERUNDOWN_ADAPTER_VERSION,
    THERUNDOWN_BASE_URL,
    THERUNDOWN_PROVIDER_NAME,
    TheRundownExperimentalAdapter,
)
from src.football.production_contracts import Fixture
from src.football.provider_cascade.adapters import RawProviderResponse
from src.football.provider_cascade.contracts import (
    MARKET_PREMATCH_1X2,
    TOP5_LEAGUE_CODES,
    CascadeTimingPolicy,
    NetworkAuthorizationContract,
    ProviderConfig,
    QuotaSnapshot,
    TransportCapability,
    digest_record,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    CAPTURE_ATTESTATION_CONTRACT_VERSION,
    ObservationEvidenceKind,
    ProviderTimestampProvenance,
)
from src.football.top5_therundown_shadow_canary import (
    CANARY_EXECUTION_MODE,
    RECEIPT_SCHEMA_VERSION,
    THERUNDOWN_PROVIDER_IDENTITIES,
    CanaryContractError,
    CanaryOutcome,
    TheRundownCanaryNetworkTransport,
    TheRundownCanaryTargetV1,
)

NETWORK_SHADOW_SCHEMA_VERSION = "top5-therundown-network-shadow-v1"
NETWORK_REQUEST_SCHEMA_VERSION = "top5-therundown-network-request-v1"
NETWORK_RESPONSE_SCHEMA_VERSION = "top5-therundown-network-response-v1"
NETWORK_RUN_SCHEMA_VERSION = "top5-therundown-network-run-v1"
QUOTA_HEADROOM_SCHEMA_VERSION = "top5-therundown-quota-headroom-v1"
QUOTA_PROOF_SCHEMA_VERSION = "top5-therundown-quota-proof-v1"
QUOTA_PROOF_AUTHORIZATION_SCHEMA_VERSION = (
    "top5-therundown-dated-snapshot-quota-proof-authorization-v1"
)
TOP5_CONTROLLED_SHADOW_REQUEST_COUNT = 5
THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST = 55
TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET = (
    TOP5_CONTROLLED_SHADOW_REQUEST_COUNT * THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST
)
TOP5_CONTROLLED_SHADOW_QUOTA_BUDGET = float(TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET)
TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS = 1.1
QUOTA_PROOF_MAX_REQUEST_COUNT = 1
# The dated-snapshot proof request was observed to bill 56 datapoints in the
# authorized real response. Keep this independent from the five-league
# Discovery request-cost assumption above, which remains 55.
THERUNDOWN_DATED_SNAPSHOT_QUOTA_PROOF_DATAPOINTS_PER_REQUEST = 56
QUOTA_PROOF_MAX_DATAPOINTS = (
    THERUNDOWN_DATED_SNAPSHOT_QUOTA_PROOF_DATAPOINTS_PER_REQUEST
)
QUOTA_PROOF_MINIMUM_REMAINING_DATAPOINTS = TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET
QUOTA_PROOF_MAXIMUM_AGE_SECONDS = 300
QUOTA_PROOF_SPORT_ID = 3
QUOTA_PROOF_MARKET_IDS = ("1",)
QUOTA_PROOF_AFFILIATE_IDS = ("19",)
QUOTA_PROOF_SNAPSHOT_MAX_OFFSET_DAYS = 1
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

    def __init__(
        self,
        message: str,
        *,
        diagnostic: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic = dict(diagnostic) if diagnostic is not None else None


def _transport_failure_metadata(exc: BaseException) -> dict[str, object]:
    """Normalize transport causes without retaining exception text."""

    reason = getattr(exc, "reason", None) if isinstance(exc, URLError) else None
    root = reason if reason is not None else exc
    reason_class = type(root).__name__ if root is not None else None
    errno_value = getattr(root, "errno", None)
    safe_errno = (
        int(errno_value)
        if isinstance(errno_value, int) and not isinstance(errno_value, bool)
        else None
    )
    class_name = reason_class.casefold() if reason_class else ""
    reason_text = reason.casefold() if isinstance(reason, str) else ""
    if isinstance(root, socket.gaierror):
        category = "dns"
    elif isinstance(root, ssl.SSLCertVerificationError):
        category = "tls_certificate"
    elif isinstance(root, ConnectionRefusedError):
        category = "tcp_refused"
    elif isinstance(root, (TimeoutError, socket.timeout)):
        category = "timeout"
    elif isinstance(exc, requests.exceptions.SSLError):
        category = "tls_certificate"
    elif isinstance(exc, requests.exceptions.ProxyError):
        category = "proxy"
    elif isinstance(exc, requests.exceptions.Timeout):
        category = "timeout"
    elif isinstance(exc, URLError) and (
        "proxy" in class_name or "proxy" in reason_text or "tunnel" in reason_text
    ):
        category = "proxy"
    elif isinstance(exc, URLError):
        category = "generic_urllib"
    else:
        category = "generic"
    return {
        "transport_reason_class": reason_class,
        "transport_reason_category": category,
        "transport_errno": safe_errno,
    }


class NetworkShadowRunStatus(str, Enum):
    COMPLETED_NETWORK = "COMPLETED_NETWORK"
    COMPLETED_REPLAY = "COMPLETED_REPLAY"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class TheRundownQuotaHeadroomEvidenceV1:
    """Non-billable, caller-supplied quota evidence required before request 1."""

    provider: str
    account_scope: str
    observed_remaining_datapoints: int
    observed_at: datetime
    provenance_source: str
    provenance_digest: str
    authorization_package_digest: str
    authorization_id: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    ceo_authorization_identity: str
    evidence_digest: str
    schema_version: str = QUOTA_HEADROOM_SCHEMA_VERSION

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "account_scope": self.account_scope,
            "observed_remaining_datapoints": self.observed_remaining_datapoints,
            "observed_at": _utc(self.observed_at, "quota observed_at").isoformat(),
            "provenance_source": self.provenance_source,
            "provenance_digest": self.provenance_digest,
            "authorization_package_digest": self.authorization_package_digest,
            "authorization_id": self.authorization_id,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "ceo_authorization_identity": self.ceo_authorization_identity,
        }

    @property
    def computed_evidence_digest(self) -> str:
        return _digest(self._payload_without_digest())

    def validate(
        self,
        *,
        expected_provider: str = THERUNDOWN_PROVIDER_NAME,
        expected_package_digest: str | None = None,
        expected_authorization: TheRundownNetworkAuthorizationV1 | None = None,
        now: datetime | None = None,
        maximum_age_seconds: int = 300,
    ) -> None:
        if self.schema_version != QUOTA_HEADROOM_SCHEMA_VERSION:
            raise NetworkShadowContractError("unsupported quota headroom schema")
        if self.provider != expected_provider:
            raise NetworkShadowExecutionBlocked(
                "quota headroom provider does not match the run"
            )
        _text(self.account_scope, "quota account_scope")
        _positive_int(
            self.observed_remaining_datapoints, "observed_remaining_datapoints"
        )
        if self.observed_remaining_datapoints < TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET:
            raise NetworkShadowExecutionBlocked(
                "quota headroom is below the five-league budget"
            )
        observed = _utc(self.observed_at, "quota observed_at")
        current = _utc(now or datetime.now(timezone.utc), "quota validation now")
        if observed > current:
            raise NetworkShadowExecutionBlocked("quota headroom is from the future")
        if (current - observed).total_seconds() > maximum_age_seconds:
            raise NetworkShadowExecutionBlocked("quota headroom evidence is stale")
        _text(self.provenance_source, "quota provenance_source")
        _sha(self.provenance_digest, "quota provenance_digest")
        _sha(self.authorization_package_digest, "authorization_package_digest")
        _text(self.authorization_id, "quota authorization_id")
        _text(self.controlled_shadow_run_id, "quota controlled_shadow_run_id")
        _text(self.qualification_session_id, "quota qualification_session_id")
        _text(self.ceo_authorization_identity, "quota ceo_authorization_identity")
        _sha(self.evidence_digest, "quota evidence_digest")
        if self.evidence_digest.lower() != self.computed_evidence_digest:
            raise NetworkShadowContractError("quota headroom evidence digest mismatch")
        if (
            expected_package_digest is not None
            and self.authorization_package_digest.lower()
            != expected_package_digest.lower()
        ):
            raise NetworkShadowExecutionBlocked(
                "quota headroom package binding does not match"
            )
        if expected_authorization is not None:
            for name, actual, expected in (
                (
                    "authorization_id",
                    self.authorization_id,
                    expected_authorization.authorization_id,
                ),
                (
                    "controlled_shadow_run_id",
                    self.controlled_shadow_run_id,
                    expected_authorization.controlled_shadow_run_id,
                ),
                (
                    "qualification_session_id",
                    self.qualification_session_id,
                    expected_authorization.qualification_session_id,
                ),
                (
                    "ceo_authorization_identity",
                    self.ceo_authorization_identity,
                    expected_authorization.ceo_authorization_identity,
                ),
            ):
                if actual != expected:
                    raise NetworkShadowExecutionBlocked(
                        f"quota headroom authorization binding mismatch: {name}"
                    )
            if (
                expected_authorization.quota_headroom_evidence_digest.lower()
                != self.evidence_digest.lower()
            ):
                raise NetworkShadowExecutionBlocked(
                    "authorization is not bound to quota headroom evidence"
                )

    def as_payload(self) -> dict[str, object]:
        self.validate(now=self.observed_at, maximum_age_seconds=2**31 - 1)
        return {
            **self._payload_without_digest(),
            "evidence_digest": self.evidence_digest,
        }

    @classmethod
    def from_payload(cls, raw: object) -> TheRundownQuotaHeadroomEvidenceV1:
        if not isinstance(raw, Mapping):
            raise NetworkShadowContractError(
                "quota headroom evidence must be an object"
            )
        try:
            observed_at = datetime.fromisoformat(
                str(raw.get("observed_at", "")).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise NetworkShadowContractError("quota observed_at is invalid") from exc
        return cls(
            provider=str(raw.get("provider", "")),
            account_scope=str(raw.get("account_scope", "")),
            observed_remaining_datapoints=raw.get("observed_remaining_datapoints", 0),  # type: ignore[arg-type]
            observed_at=observed_at,
            provenance_source=str(raw.get("provenance_source", "")),
            provenance_digest=str(raw.get("provenance_digest", "")),
            authorization_package_digest=str(
                raw.get("authorization_package_digest", "")
            ),
            authorization_id=str(raw.get("authorization_id", "")),
            controlled_shadow_run_id=str(raw.get("controlled_shadow_run_id", "")),
            qualification_session_id=str(raw.get("qualification_session_id", "")),
            ceo_authorization_identity=str(raw.get("ceo_authorization_identity", "")),
            evidence_digest=str(raw.get("evidence_digest", "")),
            schema_version=str(
                raw.get("schema_version", QUOTA_HEADROOM_SCHEMA_VERSION)
            ),
        )


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


def _validate_controlled_shadow_budget(
    maximum_request_count: int,
    maximum_datapoints: int,
    maximum_quota_cost_units: float,
    request_quota_cost_units: float,
) -> None:
    if maximum_request_count != TOP5_CONTROLLED_SHADOW_REQUEST_COUNT:
        raise NetworkShadowExecutionBlocked(
            "controlled shadow request budget must be exactly five"
        )
    if maximum_datapoints != TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET:
        raise NetworkShadowExecutionBlocked(
            "controlled shadow datapoint budget must be exactly 275"
        )
    if maximum_quota_cost_units != TOP5_CONTROLLED_SHADOW_QUOTA_BUDGET:
        raise NetworkShadowExecutionBlocked(
            "controlled shadow quota budget must be exactly 275"
        )
    if request_quota_cost_units != THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST:
        raise NetworkShadowExecutionBlocked(
            "controlled shadow request billing budget must be exactly 55"
        )


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
    provider_affiliate_ids: tuple[str, ...] = QUOTA_PROOF_AFFILIATE_IDS

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
            "provider_affiliate_ids": list(self.provider_affiliate_ids),
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
        if self.provider_affiliate_ids != QUOTA_PROOF_AFFILIATE_IDS:
            raise NetworkShadowExecutionBlocked(
                "provider affiliate scope is outside the reviewed bounded source"
            )
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
        _validate_controlled_shadow_budget(
            self.maximum_request_count,
            self.maximum_datapoints,
            self.maximum_quota_cost_units,
            self.request_quota_cost_units,
        )
        _positive_int(self.maximum_source_age_seconds, "maximum_source_age_seconds")
        interval = _number(self.minimum_interval_seconds, "minimum_interval_seconds")
        if interval < TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS:
            raise NetworkShadowExecutionBlocked(
                "network shadow pacing must be at least 1.1 seconds"
            )
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
    quota_headroom_evidence_digest: str = ""
    minimum_interval_seconds: float = 1.0
    maximum_retries: int = 0
    no_bet: bool = True
    publication: bool = False
    production_activation: bool = False
    monetary_spend_authorized: bool = False
    schema_version: str = NETWORK_SHADOW_SCHEMA_VERSION
    provider_affiliate_ids: tuple[str, ...] = QUOTA_PROOF_AFFILIATE_IDS

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
            "quota_headroom_evidence_digest": self.quota_headroom_evidence_digest,
            "minimum_interval_seconds": self.minimum_interval_seconds,
            "maximum_retries": self.maximum_retries,
            "no_bet": self.no_bet,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "monetary_spend_authorized": self.monetary_spend_authorized,
            "provider_affiliate_ids": list(self.provider_affiliate_ids),
        }

    def validate(
        self,
        configuration: TheRundownNetworkConfigurationV1 | None = None,
        *,
        now: datetime | None = None,
        require_quota_headroom: bool = False,
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
        if self.provider not in THERUNDOWN_PROVIDER_IDENTITIES:
            raise NetworkShadowContractError(
                "network provider is not an allowed TheRundown identity"
            )
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
        if self.provider_affiliate_ids != QUOTA_PROOF_AFFILIATE_IDS:
            raise NetworkShadowExecutionBlocked(
                "authorization provider affiliate scope is outside the reviewed bounded source"
            )
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
        _validate_controlled_shadow_budget(
            self.maximum_request_count,
            self.maximum_datapoints,
            self.maximum_quota_cost_units,
            self.request_quota_cost_units,
        )
        _positive_int(self.maximum_source_age_seconds, "maximum_source_age_seconds")
        if require_quota_headroom:
            _sha(
                self.quota_headroom_evidence_digest,
                "quota_headroom_evidence_digest",
            )
        interval = _number(self.minimum_interval_seconds, "minimum_interval_seconds")
        if interval < TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS:
            raise NetworkShadowExecutionBlocked(
                "network shadow pacing must be at least 1.1 seconds"
            )
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
                    self.provider_affiliate_ids,
                    configuration.provider_affiliate_ids,
                    "provider affiliate scope",
                ),
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
    """Normalized response with provider-billed ``X-Datapoints`` accounting.

    ``datapoint_count`` and ``quota_cost_units`` both represent the provider's
    billed ``X-Datapoints`` value for this response.  They are not counts of
    normalized observations or bookmaker rows.  The payload adapter must bind
    them to the response header before the executor can consume the response.
    """

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
    content_type: str | None = None
    body_length: int | None = None
    body_digest: str | None = None
    transport_reason_class: str | None = None
    transport_reason_category: str | None = None
    transport_errno: int | None = None

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

    def safe_failure_diagnostic(self, failure_classification: str) -> dict[str, object]:
        """Return response metadata safe for a consumed-proof failure artifact."""

        return {
            "failure_classification": failure_classification,
            "request_started_at": _utc(
                self.started_at, "HTTP diagnostic request start"
            ).isoformat(),
            "request_finished_at": _utc(
                self.finished_at, "HTTP diagnostic request finish"
            ).isoformat(),
            "http_status": self.status_code,
            "safe_response_headers": _safe_quota_proof_headers(self.headers),
            "transport_exception_class": self.error_detail,
            "transport_reason_class": self.transport_reason_class,
            "transport_reason_category": self.transport_reason_category,
            "transport_errno": self.transport_errno,
            "content_type": self.content_type,
            "response_body_length": self.body_length,
            "response_body_digest": self.body_digest,
        }


_QUOTA_PROOF_SAFE_HEADERS = frozenset(
    {
        "x-datapoints",
        "x-datapoints-used",
        "x-datapoints-remaining",
        "x-datapoints-limit",
        "x-datapoints-period",
        "x-datapoints-reset",
        "x-tier",
        "x-rate-limit",
        "x-rate-limit-remaining",
        "x-rate-limit-reset",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "x-data-delay-seconds",
        "x-history-access",
        "x-live-odds-access",
        "x-websocket-access",
    }
)


def _lower_headers(headers: Mapping[str, object]) -> dict[str, str]:
    return {
        str(key).casefold(): str(value).strip()
        for key, value in headers.items()
        if str(value).strip()
    }


def _safe_quota_proof_headers(headers: Mapping[str, object]) -> dict[str, str]:
    lowered = _lower_headers(headers)
    return {
        key: value
        for key, value in sorted(lowered.items())
        if key in _QUOTA_PROOF_SAFE_HEADERS
    }


def _content_type(headers: Mapping[str, object]) -> str | None:
    for key, value in headers.items():
        if str(key).casefold() == "content-type":
            normalized = str(value).strip()
            return normalized or None
    return None


def _decode_json_body(body: bytes) -> object | None:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _header_int(headers: Mapping[str, str], name: str) -> int:
    raw = headers.get(name)
    if raw is None:
        raise NetworkShadowExecutionBlocked(f"quota proof header missing: {name}")
    try:
        value = int(raw)
    except ValueError as exc:
        raise NetworkShadowExecutionBlocked(
            f"quota proof header malformed: {name}"
        ) from exc
    if value < 0:
        raise NetworkShadowExecutionBlocked(f"quota proof header is negative: {name}")
    return value


def _header_timestamp(headers: Mapping[str, str], name: str) -> datetime:
    raw = headers.get(name)
    if raw is None:
        raise NetworkShadowExecutionBlocked(f"quota proof header missing: {name}")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NetworkShadowExecutionBlocked(
            f"quota proof header malformed: {name}"
        ) from exc
    return _utc(value, f"quota proof {name}")


def _quota_proof_snapshot_date(value: object, name: str = "snapshot_date") -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise NetworkShadowContractError(f"quota proof {name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise NetworkShadowContractError(
            f"quota proof {name} must be YYYY-MM-DD"
        ) from exc


def _quota_proof_request_shape_digest(*, sport_id: int, snapshot_date: date) -> str:
    return _digest(
        {
            "method": "GET",
            "endpoint": f"{THERUNDOWN_BASE_URL}/sports/{sport_id}/events/{snapshot_date.isoformat()}",
            "query": {
                "affiliate_ids": ",".join(QUOTA_PROOF_AFFILIATE_IDS),
                "hide_closed": "true",
                "main_line": "true",
                "market_ids": ",".join(QUOTA_PROOF_MARKET_IDS),
            },
        }
    )


@dataclass(frozen=True)
class TheRundownQuotaProofAuthorizationV1:
    """Proof-only CEO authorization; it cannot authorize league execution."""

    proof_authorization_id: str
    ceo_proof_authorization_identity: str
    proof_id: str
    provider: str
    sport_id: int
    snapshot_date: date
    request_shape_digest: str
    adapter_version: str
    adapter_source_sha: str
    issued_at: datetime
    expires_at: datetime
    authorization_digest: str
    maximum_request_count: int = QUOTA_PROOF_MAX_REQUEST_COUNT
    maximum_datapoints: int = QUOTA_PROOF_MAX_DATAPOINTS
    retry_count: int = 0
    five_league_execution_authorized: bool = False
    provider_authority_granted: bool = False
    activation_authorized: bool = False
    publication_authorized: bool = False
    betting_authorized: bool = False
    schema_version: str = QUOTA_PROOF_AUTHORIZATION_SCHEMA_VERSION

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "proof_authorization_id": self.proof_authorization_id,
            "ceo_proof_authorization_identity": self.ceo_proof_authorization_identity,
            "proof_id": self.proof_id,
            "provider": self.provider,
            "sport_id": self.sport_id,
            "snapshot_date": self.snapshot_date.isoformat(),
            "request_shape_digest": self.request_shape_digest,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "issued_at": _utc(
                self.issued_at, "proof authorization issued_at"
            ).isoformat(),
            "expires_at": _utc(
                self.expires_at, "proof authorization expires_at"
            ).isoformat(),
            "maximum_request_count": self.maximum_request_count,
            "maximum_datapoints": self.maximum_datapoints,
            "retry_count": self.retry_count,
            "five_league_execution_authorized": self.five_league_execution_authorized,
            "provider_authority_granted": self.provider_authority_granted,
            "activation_authorized": self.activation_authorized,
            "publication_authorized": self.publication_authorized,
            "betting_authorized": self.betting_authorized,
        }

    @property
    def computed_authorization_digest(self) -> str:
        return _digest(self._payload_without_digest())

    def validate(self, *, now: datetime | None = None) -> None:
        if self.schema_version != QUOTA_PROOF_AUTHORIZATION_SCHEMA_VERSION:
            raise NetworkShadowContractError(
                "unsupported quota proof authorization schema"
            )
        for value, name in (
            (self.proof_authorization_id, "proof_authorization_id"),
            (
                self.ceo_proof_authorization_identity,
                "ceo_proof_authorization_identity",
            ),
            (self.proof_id, "proof_id"),
            (self.request_shape_digest, "request_shape_digest"),
            (self.adapter_version, "adapter_version"),
            (self.adapter_source_sha, "adapter_source_sha"),
        ):
            _text(value, f"proof authorization {name}")
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise NetworkShadowExecutionBlocked(
                "proof authorization provider is not TheRundown"
            )
        if self.sport_id != QUOTA_PROOF_SPORT_ID:
            raise NetworkShadowExecutionBlocked("quota proof sport is unsupported")
        snapshot = _quota_proof_snapshot_date(self.snapshot_date)
        issued = _utc(self.issued_at, "proof authorization issued_at")
        expires = _utc(self.expires_at, "proof authorization expires_at")
        issue_date = issued.date()
        if snapshot < issue_date or snapshot > issue_date + timedelta(
            days=QUOTA_PROOF_SNAPSHOT_MAX_OFFSET_DAYS
        ):
            raise NetworkShadowExecutionBlocked(
                "quota proof snapshot date must be current or next UTC date at issue"
            )
        if snapshot < expires.date():
            raise NetworkShadowExecutionBlocked(
                "quota proof snapshot date would become historical before authorization expiry"
            )
        _sha(self.request_shape_digest, "proof authorization request-shape digest")
        _sha(self.adapter_source_sha, "proof authorization adapter source SHA")
        if self.request_shape_digest != _quota_proof_request_shape_digest(
            sport_id=self.sport_id, snapshot_date=snapshot
        ):
            raise NetworkShadowExecutionBlocked(
                "proof authorization request-shape digest mismatch"
            )
        if self.maximum_request_count != QUOTA_PROOF_MAX_REQUEST_COUNT:
            raise NetworkShadowExecutionBlocked(
                "proof authorization request count must be exactly one"
            )
        if self.maximum_datapoints != QUOTA_PROOF_MAX_DATAPOINTS:
            raise NetworkShadowExecutionBlocked(
                "proof authorization datapoint cap must be exactly 56"
            )
        if self.retry_count != 0:
            raise NetworkShadowExecutionBlocked(
                "proof authorization retries must be zero"
            )
        if any(
            (
                self.five_league_execution_authorized,
                self.provider_authority_granted,
                self.activation_authorized,
                self.publication_authorized,
                self.betting_authorized,
            )
        ):
            raise NetworkShadowExecutionBlocked(
                "proof authorization contains a forbidden authority"
            )
        current = _utc(now or issued, "proof authorization validation now")
        if expires <= issued or current < issued or current >= expires:
            raise NetworkShadowExecutionBlocked(
                "proof authorization is outside its issue/expiry window"
            )
        current_date = current.date()
        if snapshot < current_date or snapshot > current_date + timedelta(days=1):
            raise NetworkShadowExecutionBlocked(
                "quota proof snapshot date is historical or outside the bounded window"
            )
        _sha(self.authorization_digest, "proof authorization digest")
        if self.authorization_digest.lower() != self.computed_authorization_digest:
            raise NetworkShadowContractError("proof authorization digest mismatch")

    def as_payload(self) -> dict[str, object]:
        return {
            **self._payload_without_digest(),
            "authorization_digest": self.authorization_digest,
        }

    @classmethod
    def from_payload(cls, raw: object) -> TheRundownQuotaProofAuthorizationV1:
        if not isinstance(raw, Mapping):
            raise NetworkShadowContractError("proof authorization must be an object")
        try:
            issued_at = datetime.fromisoformat(
                str(raw.get("issued_at", "")).replace("Z", "+00:00")
            )
            expires_at = datetime.fromisoformat(
                str(raw.get("expires_at", "")).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise NetworkShadowContractError(
                "proof authorization timestamps are invalid"
            ) from exc
        return cls(
            proof_authorization_id=str(raw.get("proof_authorization_id", "")),
            ceo_proof_authorization_identity=str(
                raw.get("ceo_proof_authorization_identity", "")
            ),
            proof_id=str(raw.get("proof_id", "")),
            provider=str(raw.get("provider", "")),
            sport_id=raw.get("sport_id", 0),  # type: ignore[arg-type]
            snapshot_date=_quota_proof_snapshot_date(raw.get("snapshot_date", "")),
            request_shape_digest=str(raw.get("request_shape_digest", "")),
            adapter_version=str(raw.get("adapter_version", "")),
            adapter_source_sha=str(raw.get("adapter_source_sha", "")),
            issued_at=issued_at,
            expires_at=expires_at,
            authorization_digest=str(raw.get("authorization_digest", "")),
            maximum_request_count=raw.get("maximum_request_count", 0),  # type: ignore[arg-type]
            maximum_datapoints=raw.get("maximum_datapoints", 0),  # type: ignore[arg-type]
            retry_count=raw.get("retry_count", 0),  # type: ignore[arg-type]
            five_league_execution_authorized=raw.get(
                "five_league_execution_authorized", True
            ),  # type: ignore[arg-type]
            provider_authority_granted=raw.get("provider_authority_granted", True),  # type: ignore[arg-type]
            activation_authorized=raw.get("activation_authorized", True),  # type: ignore[arg-type]
            publication_authorized=raw.get("publication_authorized", True),  # type: ignore[arg-type]
            betting_authorized=raw.get("betting_authorized", True),  # type: ignore[arg-type]
            schema_version=str(
                raw.get("schema_version", QUOTA_PROOF_AUTHORIZATION_SCHEMA_VERSION)
            ),
        )

    def request_for_proof(
        self, *, proof_configuration_digest: str, now: datetime | None = None
    ) -> TheRundownQuotaProofRequestV1:
        self.validate()
        _sha(proof_configuration_digest, "proof configuration digest")
        request = TheRundownQuotaProofRequestV1(
            proof_id=self.proof_id,
            provider=self.provider,
            sport_id=self.sport_id,
            snapshot_date=self.snapshot_date,
            authorization_package_digest=self.authorization_digest,
            configuration_digest=proof_configuration_digest,
            authorization_id=self.proof_authorization_id,
            controlled_shadow_run_id="quota-proof-only",
            qualification_session_id="quota-proof-only",
            ceo_authorization_identity=self.ceo_proof_authorization_identity,
            adapter_version=self.adapter_version,
            adapter_source_sha=self.adapter_source_sha,
            endpoint=f"{THERUNDOWN_BASE_URL}/sports/{self.sport_id}/events/{self.snapshot_date.isoformat()}",
            query={
                "affiliate_ids": ",".join(QUOTA_PROOF_AFFILIATE_IDS),
                "hide_closed": "true",
                "main_line": "true",
                "market_ids": ",".join(QUOTA_PROOF_MARKET_IDS),
            },
            request_shape_digest=self.request_shape_digest,
            maximum_datapoints=self.maximum_datapoints,
            request_count=self.maximum_request_count,
            retry_count=self.retry_count,
        )
        request.validate(now=now)
        return request


@dataclass(frozen=True)
class TheRundownQuotaProofRequestV1:
    """One provider-native, account-bound request used only to prove headroom."""

    proof_id: str
    provider: str
    sport_id: int
    snapshot_date: date
    authorization_package_digest: str
    configuration_digest: str
    authorization_id: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    ceo_authorization_identity: str
    adapter_version: str
    adapter_source_sha: str
    endpoint: str
    query: Mapping[str, str]
    request_shape_digest: str
    maximum_datapoints: int = QUOTA_PROOF_MAX_DATAPOINTS
    request_count: int = QUOTA_PROOF_MAX_REQUEST_COUNT
    retry_count: int = 0
    timeout_seconds: float = 30.0

    @property
    def computed_request_shape_digest(self) -> str:
        return _quota_proof_request_shape_digest(
            sport_id=self.sport_id, snapshot_date=self.snapshot_date
        )

    def validate(self, *, now: datetime | None = None) -> None:
        if self.proof_id.strip() == "":
            raise NetworkShadowContractError("quota proof_id is required")
        if self.provider != THERUNDOWN_PROVIDER_NAME:
            raise NetworkShadowExecutionBlocked(
                "quota proof provider is not TheRundown"
            )
        for value, name in (
            (self.authorization_package_digest, "authorization_package_digest"),
            (self.configuration_digest, "configuration_digest"),
            (self.authorization_id, "authorization_id"),
            (self.controlled_shadow_run_id, "controlled_shadow_run_id"),
            (self.qualification_session_id, "qualification_session_id"),
            (self.ceo_authorization_identity, "ceo_authorization_identity"),
            (self.adapter_version, "adapter_version"),
            (self.adapter_source_sha, "adapter_source_sha"),
        ):
            _text(value, f"quota proof {name}")
        _sha(self.authorization_package_digest, "quota proof package digest")
        _sha(self.configuration_digest, "quota proof configuration digest")
        _sha(self.adapter_source_sha, "quota proof adapter source SHA")
        if self.sport_id != QUOTA_PROOF_SPORT_ID:
            raise NetworkShadowExecutionBlocked("quota proof sport is unsupported")
        snapshot = _quota_proof_snapshot_date(self.snapshot_date)
        current_date = _utc(
            now or datetime.now(timezone.utc), "quota proof request now"
        ).date()
        if snapshot < current_date or snapshot > current_date + timedelta(days=1):
            raise NetworkShadowExecutionBlocked(
                "quota proof snapshot date is historical or outside the bounded window"
            )
        expected_endpoint = f"{THERUNDOWN_BASE_URL}/sports/{self.sport_id}/events/{snapshot.isoformat()}"
        if self.endpoint != expected_endpoint:
            raise NetworkShadowExecutionBlocked(
                "quota proof endpoint is not the reviewed TheRundown event route"
            )
        expected_query = {
            "affiliate_ids": ",".join(QUOTA_PROOF_AFFILIATE_IDS),
            "hide_closed": "true",
            "main_line": "true",
            "market_ids": ",".join(QUOTA_PROOF_MARKET_IDS),
        }
        if dict(self.query) != expected_query:
            raise NetworkShadowExecutionBlocked(
                "quota proof request shape is outside the bounded contract"
            )
        if self.maximum_datapoints != QUOTA_PROOF_MAX_DATAPOINTS:
            raise NetworkShadowExecutionBlocked(
                "quota proof datapoint cap must be exactly 56"
            )
        if self.request_count != QUOTA_PROOF_MAX_REQUEST_COUNT:
            raise NetworkShadowExecutionBlocked(
                "quota proof request count must be exactly one"
            )
        if self.retry_count != 0:
            raise NetworkShadowExecutionBlocked("quota proof retries must be zero")
        if self.request_shape_digest.lower() != self.computed_request_shape_digest:
            raise NetworkShadowExecutionBlocked(
                "quota proof request-shape digest mismatch"
            )
        _number(self.timeout_seconds, "quota proof timeout_seconds", positive=True)

    def as_http_request(
        self, api_key: str, *, now: datetime | None = None
    ) -> TheRundownNetworkHttpRequestV1:
        self.validate(now=now)
        _text(api_key, "TheRundown API credential")
        return TheRundownNetworkHttpRequestV1(
            method="GET",
            endpoint=self.endpoint,
            query=dict(self.query),
            headers={
                "Accept": "application/json",
                "X-SportsBrain-Request-Identity": self.proof_id,
                "X-TheRundown-Key": api_key,
            },
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class TheRundownQuotaProofEvidenceV1:
    """Validated response evidence; never a caller-authored quota assertion."""

    proof_id: str
    provider: str
    sport_id: int
    snapshot_date: date
    account_scope: str
    authorization_package_digest: str
    configuration_digest: str
    authorization_id: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    ceo_authorization_identity: str
    request_shape_digest: str
    credential_binding_digest: str
    request_started_at: datetime
    response_finished_at: datetime
    billed_datapoints: int
    remaining_datapoints: int
    quota_used_datapoints: int
    quota_limit_datapoints: int
    quota_period: str
    quota_reset_at: datetime
    raw_header_evidence: Mapping[str, str]
    response_digest: str
    evidence_digest: str
    status_code: int = 200
    request_count: int = 1
    retry_count: int = 0
    no_retry: bool = True
    execution_phase: str = "quota_proof"
    schema_version: str = QUOTA_PROOF_SCHEMA_VERSION

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "execution_phase": self.execution_phase,
            "proof_id": self.proof_id,
            "provider": self.provider,
            "sport_id": self.sport_id,
            "snapshot_date": self.snapshot_date.isoformat(),
            "account_scope": self.account_scope,
            "authorization_package_digest": self.authorization_package_digest,
            "configuration_digest": self.configuration_digest,
            "authorization_id": self.authorization_id,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "ceo_authorization_identity": self.ceo_authorization_identity,
            "request_shape_digest": self.request_shape_digest,
            "credential_binding_digest": self.credential_binding_digest,
            "request_started_at": _utc(
                self.request_started_at, "quota proof request_started_at"
            ).isoformat(),
            "response_finished_at": _utc(
                self.response_finished_at, "quota proof response_finished_at"
            ).isoformat(),
            "billed_datapoints": self.billed_datapoints,
            "remaining_datapoints": self.remaining_datapoints,
            "quota_used_datapoints": self.quota_used_datapoints,
            "quota_limit_datapoints": self.quota_limit_datapoints,
            "quota_period": self.quota_period,
            "quota_reset_at": _utc(
                self.quota_reset_at, "quota proof quota_reset_at"
            ).isoformat(),
            "raw_header_evidence": dict(self.raw_header_evidence),
            "response_digest": self.response_digest,
            "status_code": self.status_code,
            "request_count": self.request_count,
            "retry_count": self.retry_count,
            "no_retry": self.no_retry,
        }

    @property
    def computed_evidence_digest(self) -> str:
        return _digest(self._payload_without_digest())

    def validate(
        self,
        *,
        request: TheRundownQuotaProofRequestV1,
        now: datetime,
        request_now: datetime | None = None,
        require_provider_delay: bool = False,
    ) -> None:
        request.validate(now=request_now or now)
        if self.schema_version != QUOTA_PROOF_SCHEMA_VERSION:
            raise NetworkShadowContractError("unsupported quota proof schema")
        if self.execution_phase != "quota_proof":
            raise NetworkShadowExecutionBlocked(
                "quota proof evidence has an invalid execution phase"
            )
        if self.proof_id != request.proof_id or self.provider != request.provider:
            raise NetworkShadowExecutionBlocked(
                "quota proof identity is not bound to its request"
            )
        for value, expected, name in (
            (
                self.authorization_package_digest,
                request.authorization_package_digest,
                "authorization package",
            ),
            (self.configuration_digest, request.configuration_digest, "configuration"),
            (self.authorization_id, request.authorization_id, "authorization"),
            (
                self.controlled_shadow_run_id,
                request.controlled_shadow_run_id,
                "controlled-shadow run",
            ),
            (
                self.qualification_session_id,
                request.qualification_session_id,
                "qualification session",
            ),
            (
                self.ceo_authorization_identity,
                request.ceo_authorization_identity,
                "CEO authorization",
            ),
            (self.request_shape_digest, request.request_shape_digest, "request shape"),
        ):
            if value != expected:
                raise NetworkShadowExecutionBlocked(
                    f"quota proof {name} binding mismatch"
                )
        if (
            self.sport_id != request.sport_id
            or self.snapshot_date != request.snapshot_date
        ):
            raise NetworkShadowExecutionBlocked(
                "quota proof dated snapshot binding mismatch"
            )
        _text(self.account_scope, "quota proof account_scope")
        _sha(self.credential_binding_digest, "quota proof credential binding")
        _sha(self.response_digest, "quota proof response digest")
        _sha(self.evidence_digest, "quota proof evidence digest")
        if self.status_code < 200 or self.status_code >= 300:
            raise NetworkShadowExecutionBlocked("quota proof HTTP response is not 2xx")
        if (
            self.billed_datapoints <= 0
            or self.billed_datapoints > request.maximum_datapoints
        ):
            raise NetworkShadowExecutionBlocked(
                "quota proof billed datapoints exceed the per-request cap"
            )
        if self.remaining_datapoints < QUOTA_PROOF_MINIMUM_REMAINING_DATAPOINTS:
            raise NetworkShadowExecutionBlocked(
                "quota proof remaining datapoints are below the five-league budget"
            )
        for value, name in (
            (self.quota_used_datapoints, "quota_used_datapoints"),
            (self.quota_limit_datapoints, "quota_limit_datapoints"),
        ):
            _nonnegative_int(value, f"quota proof {name}")
        if self.quota_limit_datapoints == 0:
            raise NetworkShadowExecutionBlocked("quota proof quota limit is zero")
        if (
            self.quota_used_datapoints + self.remaining_datapoints
            != self.quota_limit_datapoints
        ):
            raise NetworkShadowExecutionBlocked(
                "quota proof quota headers are contradictory"
            )
        if self.billed_datapoints > self.quota_used_datapoints:
            raise NetworkShadowExecutionBlocked(
                "quota proof billed datapoints exceed provider usage"
            )
        if self.quota_period not in {"daily", "weekly", "monthly"}:
            raise NetworkShadowExecutionBlocked("quota proof period is unsupported")
        started = _utc(self.request_started_at, "quota proof request start")
        finished = _utc(self.response_finished_at, "quota proof response finish")
        current = _utc(now, "quota proof validation now")
        if finished < started or finished > current:
            raise NetworkShadowExecutionBlocked(
                "quota proof response timestamps are invalid"
            )
        if (current - finished).total_seconds() > QUOTA_PROOF_MAXIMUM_AGE_SECONDS:
            raise NetworkShadowExecutionBlocked("quota proof response is stale")
        if _utc(self.quota_reset_at, "quota proof quota reset") <= finished:
            raise NetworkShadowExecutionBlocked(
                "quota proof reset boundary is not future-dated"
            )
        required_headers = {
            "x-datapoints",
            "x-datapoints-used",
            "x-datapoints-remaining",
            "x-datapoints-limit",
            "x-datapoints-period",
            "x-datapoints-reset",
            "x-tier",
            "x-rate-limit",
        }
        if require_provider_delay:
            required_headers.add("x-data-delay-seconds")
        headers = _lower_headers(self.raw_header_evidence)
        if not required_headers.issubset(headers):
            raise NetworkShadowExecutionBlocked(
                "quota proof provider billing/rate/tier evidence is incomplete"
            )
        if int(headers["x-datapoints"]) != self.billed_datapoints:
            raise NetworkShadowExecutionBlocked(
                "quota proof billed header does not match evidence"
            )
        if int(headers["x-datapoints-used"]) != self.quota_used_datapoints:
            raise NetworkShadowExecutionBlocked(
                "quota proof used header does not match evidence"
            )
        if int(headers["x-datapoints-remaining"]) != self.remaining_datapoints:
            raise NetworkShadowExecutionBlocked(
                "quota proof remaining header does not match evidence"
            )
        if int(headers["x-datapoints-limit"]) != self.quota_limit_datapoints:
            raise NetworkShadowExecutionBlocked(
                "quota proof limit header does not match evidence"
            )
        if headers["x-datapoints-period"] != self.quota_period:
            raise NetworkShadowExecutionBlocked(
                "quota proof period header does not match evidence"
            )
        if headers["x-tier"].casefold() != "free":
            raise NetworkShadowExecutionBlocked(
                "quota proof account tier is not the approved free tier"
            )
        if (
            self.request_count != 1
            or self.retry_count != 0
            or self.no_retry is not True
        ):
            raise NetworkShadowExecutionBlocked(
                "quota proof must be exactly one request with zero retries"
            )
        if self.evidence_digest.lower() != self.computed_evidence_digest:
            raise NetworkShadowContractError("quota proof evidence digest mismatch")

    def as_payload(self) -> dict[str, object]:
        return {
            **self._payload_without_digest(),
            "evidence_digest": self.evidence_digest,
        }

    @classmethod
    def from_http_response(
        cls,
        request: TheRundownQuotaProofRequestV1,
        response: TheRundownNetworkHttpResponseV1,
        *,
        api_key: str,
        now: datetime,
        request_now: datetime | None = None,
    ) -> TheRundownQuotaProofEvidenceV1:
        request.validate(now=request_now or now)
        response.validate()
        if (
            response.timed_out
            or response.status_code is None
            or response.error_detail not in (None, "HTTPError")
        ):
            raise NetworkShadowExecutionBlocked(
                "quota proof provider transport failed",
                diagnostic=response.safe_failure_diagnostic("transport_failure"),
            )
        if not 200 <= response.status_code < 300:
            raise NetworkShadowExecutionBlocked(
                f"quota proof provider request failed with HTTP {response.status_code}",
                diagnostic=response.safe_failure_diagnostic("http_status_failure"),
            )
        if response.payload is None or not isinstance(response.payload, Mapping):
            raise NetworkShadowExecutionBlocked(
                "quota proof response body is missing or malformed",
                diagnostic=response.safe_failure_diagnostic("response_body_malformed"),
            )
        events = response.payload.get("events")
        if not isinstance(events, list) or not events:
            raise NetworkShadowExecutionBlocked(
                "quota proof dated snapshot contains no events",
                diagnostic=response.safe_failure_diagnostic("empty_snapshot"),
            )
        headers = _safe_quota_proof_headers(response.headers)
        billed = _header_int(headers, "x-datapoints")
        used = _header_int(headers, "x-datapoints-used")
        remaining = _header_int(headers, "x-datapoints-remaining")
        limit = _header_int(headers, "x-datapoints-limit")
        period = headers.get("x-datapoints-period", "").casefold()
        _text(period, "quota proof x-datapoints-period")
        reset = _header_timestamp(headers, "x-datapoints-reset")
        _header_int(headers, "x-rate-limit")
        if "x-data-delay-seconds" in headers:
            _header_int(headers, "x-data-delay-seconds")
        if not headers.get("x-tier"):
            raise NetworkShadowExecutionBlocked("quota proof x-tier is missing")
        finished = _utc(response.finished_at, "quota proof response finish")
        started = _utc(response.started_at, "quota proof request start")
        credential_binding = sha256(api_key.encode("utf-8")).hexdigest()
        response_digest = _digest(
            {
                "status_code": response.status_code,
                "payload": response.payload,
                "safe_headers": headers,
                "started_at": started,
                "finished_at": finished,
            }
        )
        evidence = cls(
            proof_id=request.proof_id,
            provider=request.provider,
            sport_id=request.sport_id,
            snapshot_date=request.snapshot_date,
            account_scope=f"credential:{credential_binding}",
            authorization_package_digest=request.authorization_package_digest,
            configuration_digest=request.configuration_digest,
            authorization_id=request.authorization_id,
            controlled_shadow_run_id=request.controlled_shadow_run_id,
            qualification_session_id=request.qualification_session_id,
            ceo_authorization_identity=request.ceo_authorization_identity,
            request_shape_digest=request.request_shape_digest,
            credential_binding_digest=credential_binding,
            request_started_at=started,
            response_finished_at=finished,
            billed_datapoints=billed,
            remaining_datapoints=remaining,
            quota_used_datapoints=used,
            quota_limit_datapoints=limit,
            quota_period=period,
            quota_reset_at=reset,
            raw_header_evidence=headers,
            response_digest=response_digest,
            evidence_digest="0" * 64,
            status_code=response.status_code,
        )
        evidence = replace(evidence, evidence_digest=evidence.computed_evidence_digest)
        evidence.validate(request=request, now=now, request_now=request_now)
        return evidence

    @classmethod
    def from_payload(cls, raw: object) -> TheRundownQuotaProofEvidenceV1:
        if not isinstance(raw, Mapping):
            raise NetworkShadowContractError("quota proof evidence must be an object")
        try:
            timestamps = {
                name: datetime.fromisoformat(
                    str(raw.get(name, "")).replace("Z", "+00:00")
                )
                for name in (
                    "request_started_at",
                    "response_finished_at",
                    "quota_reset_at",
                )
            }
        except ValueError as exc:
            raise NetworkShadowContractError(
                "quota proof timestamp is invalid"
            ) from exc
        return cls(
            proof_id=str(raw.get("proof_id", "")),
            provider=str(raw.get("provider", "")),
            sport_id=raw.get("sport_id", 0),  # type: ignore[arg-type]
            snapshot_date=_quota_proof_snapshot_date(raw.get("snapshot_date", "")),
            account_scope=str(raw.get("account_scope", "")),
            authorization_package_digest=str(
                raw.get("authorization_package_digest", "")
            ),
            configuration_digest=str(raw.get("configuration_digest", "")),
            authorization_id=str(raw.get("authorization_id", "")),
            controlled_shadow_run_id=str(raw.get("controlled_shadow_run_id", "")),
            qualification_session_id=str(raw.get("qualification_session_id", "")),
            ceo_authorization_identity=str(raw.get("ceo_authorization_identity", "")),
            request_shape_digest=str(raw.get("request_shape_digest", "")),
            credential_binding_digest=str(raw.get("credential_binding_digest", "")),
            request_started_at=timestamps["request_started_at"],
            response_finished_at=timestamps["response_finished_at"],
            billed_datapoints=raw.get("billed_datapoints", 0),  # type: ignore[arg-type]
            remaining_datapoints=raw.get("remaining_datapoints", 0),  # type: ignore[arg-type]
            quota_used_datapoints=raw.get("quota_used_datapoints", 0),  # type: ignore[arg-type]
            quota_limit_datapoints=raw.get("quota_limit_datapoints", 0),  # type: ignore[arg-type]
            quota_period=str(raw.get("quota_period", "")),
            quota_reset_at=timestamps["quota_reset_at"],
            raw_header_evidence=dict(raw.get("raw_header_evidence", {})),  # type: ignore[arg-type]
            response_digest=str(raw.get("response_digest", "")),
            evidence_digest=str(raw.get("evidence_digest", "")),
            status_code=raw.get("status_code", 0),  # type: ignore[arg-type]
            request_count=raw.get("request_count", 0),  # type: ignore[arg-type]
            retry_count=raw.get("retry_count", 0),  # type: ignore[arg-type]
            no_retry=raw.get("no_retry", False),  # type: ignore[arg-type]
            execution_phase=str(raw.get("execution_phase", "")),
            schema_version=str(raw.get("schema_version", "")),
        )


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
    """Bridge actual TheRundown JSON through the reviewed candidate adapter.

    The legacy mapping path remains available for deterministic contract
    fixtures.  Actual ``events[]`` payloads require the reviewed adapter
    configuration explicitly, so a generic payload cannot silently become
    real evidence.
    """

    _SAFE_PROVIDER_HEADERS = frozenset(
        {
            "x-datapoints",
            "x-datapoints-limit",
            "x-datapoints-period",
            "x-datapoints-remaining",
            "x-datapoints-reset",
            "x-datapoints-used",
            "x-data-delay-seconds",
            "x-history-access",
            "x-live-odds-access",
            "x-rate-limit",
            "x-rate-limit-remaining",
            "x-rate-limit-reset",
            "x-ratelimit-limit",
            "x-ratelimit-remaining",
            "x-ratelimit-reset",
            "x-tier",
            "x-websocket-access",
        }
    )

    _INTEGER_PROVIDER_HEADERS = frozenset(
        {
            "x-datapoints",
            "x-datapoints-limit",
            "x-datapoints-remaining",
            "x-datapoints-used",
            "x-data-delay-seconds",
            "x-rate-limit",
            "x-rate-limit-remaining",
            "x-rate-limit-reset",
            "x-ratelimit-limit",
            "x-ratelimit-remaining",
            "x-ratelimit-reset",
        }
    )

    def __init__(
        self,
        *,
        adapter_version: str = THERUNDOWN_ADAPTER_VERSION,
        adapter_source_sha: str | None = None,
        maximum_source_age_seconds: int = 300,
        kickoff_tolerance_seconds: int = 60,
        initial_quota: QuotaSnapshot | None = None,
    ) -> None:
        self.adapter_version = adapter_version
        self.adapter_source_sha = adapter_source_sha
        self.maximum_source_age_seconds = maximum_source_age_seconds
        self.kickoff_tolerance_seconds = kickoff_tolerance_seconds
        self.initial_quota = initial_quota or QuotaSnapshot()

    @classmethod
    def _safe_provider_headers(cls, headers: Mapping[str, str]) -> dict[str, object]:
        evidence: dict[str, object] = {}
        for key, raw_value in headers.items():
            name = str(key).casefold()
            if name not in cls._SAFE_PROVIDER_HEADERS:
                continue
            value = str(raw_value).strip()
            if name in cls._INTEGER_PROVIDER_HEADERS:
                try:
                    evidence[name] = int(value)
                except ValueError:
                    continue
            else:
                evidence[name] = value
        return evidence

    @staticmethod
    def _provider_fixture_key(request: TheRundownNetworkRequestV1) -> str:
        return f"therundown:{request.target.league}:{request.target.provider_event_id}"

    def _reviewed_config(self, request: TheRundownNetworkRequestV1) -> ProviderConfig:
        return ProviderConfig(
            name=THERUNDOWN_PROVIDER_NAME,
            league_allowlist=frozenset({request.target.league}),
            market_allowlist=(MARKET_PREMATCH_1X2,),
            credentials_required=False,
            credential_available=True,
            candidate_only=True,
            quality_eligible=False,
            shadow_only=True,
            adapter_version=self.adapter_version,
            initial_quota=self.initial_quota,
        )

    def _decode_reviewed_events(
        self,
        request: TheRundownNetworkRequestV1,
        response: TheRundownNetworkHttpResponseV1,
        billing: Mapping[str, int],
    ) -> TheRundownNetworkResponseV1:
        if request.target.provider != THERUNDOWN_PROVIDER_NAME:
            raise NetworkShadowExecutionBlocked(
                "actual TheRundown payload requires therundown_experimental identity"
            )
        if not self.adapter_source_sha:
            raise NetworkShadowExecutionBlocked(
                "reviewed adapter source SHA is required for actual payloads"
            )
        raw = response.payload
        if not isinstance(raw, Mapping) or not isinstance(raw.get("events"), list):
            raise NetworkShadowExecutionBlocked(
                "actual TheRundown payload must contain events[]"
            )
        provider_fixture_key = self._provider_fixture_key(request)
        provider_fixture = Fixture(
            provider_fixture_key,
            request.target.league,
            request.target.home_team,
            request.target.away_team,
            request.target.kickoff,
        )
        raw_response = RawProviderResponse(
            status_code=response.status_code,
            payload=raw,
            headers=response.headers,
            started_at=response.started_at,
            completed_at=response.finished_at,
            latency_ms=round(
                (response.finished_at - response.started_at).total_seconds() * 1000
            ),
        )
        reviewed_adapter = TheRundownExperimentalAdapter(
            transport=lambda _request, _timeout: raw_response
        )
        observations = reviewed_adapter.fetch_observations(
            provider_fixture,
            self._reviewed_config(request),
            request_identity=request.request_identity,
            requested_at=response.started_at,
            provider_priority=0,
            provider_fixture_id=request.target.provider_event_id,
            timing_policy=CascadeTimingPolicy(
                maximum_odds_age_seconds=self.maximum_source_age_seconds,
                kickoff_tolerance_seconds=self.kickoff_tolerance_seconds,
            ),
            authorization=NetworkAuthorizationContract(
                controlled_shadow_run_ref=request.controlled_shadow_run_id,
                authorized_providers=(THERUNDOWN_PROVIDER_NAME,),
            ),
        )
        if not observations:
            raise NetworkShadowExecutionBlocked(
                "reviewed TheRundown adapter returned no complete bookmaker observations"
            )
        raw_digest = digest_record(raw)
        normalized_digests = tuple(
            digest_record(observation.as_payload()) for observation in observations
        )
        safe_headers = self._safe_provider_headers(response.headers)
        cascade_evidence = {
            "candidate_only": True,
            "provider": request.target.provider,
            "league": request.target.league,
            "fixture_key": request.target.fixture_key,
            "provider_event_id": request.target.provider_event_id,
            "provider_request_id": request.request_identity,
            "raw_response_digest": raw_digest,
            "normalized_record_digests": list(normalized_digests),
            "bookmaker_observations": len(observations),
        }
        cascade_digest = _digest(cascade_evidence)
        primary = observations[0]
        provider_billing = dict(safe_headers)
        provider_billing["provider_fixture_key"] = provider_fixture_key
        provider_billing["canonical_fixture_key"] = request.target.fixture_key
        provider_billing["normalized_observations"] = [
            observation.as_payload() for observation in observations
        ]
        provider_billing["cascade_evidence"] = cascade_evidence
        provider_billing["cascade_evidence_digest"] = cascade_digest
        return TheRundownNetworkResponseV1(
            outcome=CanaryOutcome.SUCCESS,
            provider=request.target.provider,
            league=request.target.league,
            fixture_key=request.target.fixture_key,
            provider_event_id=primary.provider_fixture_id,
            provider_request_id=request.request_identity,
            home_team=request.target.home_team,
            away_team=request.target.away_team,
            home_participant_id=str(
                primary.metadata.get("participant_ids", {}).get("home", "")
            ),
            away_participant_id=str(
                primary.metadata.get("participant_ids", {}).get("away", "")
            ),
            bookmaker_identity=primary.bookmaker_identity,
            source_identity=primary.source_provenance,
            source_timestamp=primary.source_timestamp,
            captured_at=primary.captured_at,
            request_started_at=primary.request_started_at,
            request_finished_at=primary.request_completed_at,
            home_odds=primary.home_odds,
            draw_odds=primary.draw_odds,
            away_odds=primary.away_odds,
            adapter_version=self.adapter_version,
            adapter_source_sha=self.adapter_source_sha,
            raw_response_digest=raw_digest,
            provider_record_digest=primary.raw_record_digest,
            normalized_record_digest=normalized_digests[0],
            cascade_evidence_digest=cascade_digest,
            quota_before=billing["x-datapoints-remaining"] + billing["x-datapoints"],
            quota_after=billing["x-datapoints-remaining"],
            quota_cost_units=float(billing["x-datapoints"]),
            datapoint_count=billing["x-datapoints"],
            rate_limit_remaining=safe_headers.get("x-rate-limit-remaining"),
            rate_limit_reset_at=None,
            account_tier=str(safe_headers.get("x-tier", "")),
            provider_delay_seconds=float(safe_headers["x-data-delay-seconds"]),
            http_status=response.status_code,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            network_execution=True,
            raw_metadata={
                "provider_billing": provider_billing,
                "provider_fixture_key": provider_fixture_key,
                "canonical_fixture_key": request.target.fixture_key,
                "normalized_observations": [
                    observation.as_payload() for observation in observations
                ],
                "cascade_evidence": cascade_evidence,
            },
        )

    @staticmethod
    def _billing_headers(headers: Mapping[str, str]) -> dict[str, int]:
        lowered = {
            str(key).casefold(): str(value).strip() for key, value in headers.items()
        }
        values: dict[str, int] = {}
        for name in (
            "x-datapoints",
            "x-datapoints-used",
            "x-datapoints-remaining",
            "x-datapoints-limit",
        ):
            raw = lowered.get(name)
            if raw is None:
                raise NetworkShadowExecutionBlocked(
                    f"provider billing header is missing: {name}"
                )
            try:
                parsed = int(raw)
            except ValueError as exc:
                raise NetworkShadowExecutionBlocked(
                    f"provider billing header is invalid: {name}"
                ) from exc
            if parsed < 0:
                raise NetworkShadowExecutionBlocked(
                    f"provider billing header is negative: {name}"
                )
            values[name] = parsed
        if (
            values["x-datapoints-used"] + values["x-datapoints-remaining"]
            != values["x-datapoints-limit"]
        ):
            raise NetworkShadowExecutionBlocked(
                "provider quota counters do not reconcile"
            )
        if values["x-datapoints"] > values["x-datapoints-used"]:
            raise NetworkShadowExecutionBlocked(
                "provider billed datapoints exceed used quota"
            )
        return values

    def build_request(
        self, request: TheRundownNetworkRequestV1, *, endpoint: str, api_key: str
    ) -> TheRundownNetworkHttpRequestV1:
        http_request = TheRundownNetworkHttpRequestV1(
            method="GET",
            endpoint=endpoint,
            query={
                "affiliate_ids": ",".join(QUOTA_PROOF_AFFILIATE_IDS),
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
        billing = self._billing_headers(response.headers)
        payload = dict(raw)
        if isinstance(raw, Mapping) and isinstance(raw.get("events"), list):
            return self._decode_reviewed_events(request, response, billing)
        payload_datapoints = payload.get("datapoint_count")
        if (
            payload_datapoints is not None
            and payload_datapoints != billing["x-datapoints"]
        ):
            raise NetworkShadowExecutionBlocked(
                "payload and provider billed datapoints disagree"
            )
        expected_quota_before = (
            billing["x-datapoints-remaining"] + billing["x-datapoints"]
        )
        expected_quota_after = billing["x-datapoints-remaining"]
        for name, expected in (
            ("quota_before", expected_quota_before),
            ("quota_after", expected_quota_after),
        ):
            if payload.get(name) is not None and payload[name] != expected:
                raise NetworkShadowExecutionBlocked(
                    f"payload and provider quota evidence disagree: {name}"
                )
        payload.update(
            {
                "datapoint_count": billing["x-datapoints"],
                "quota_cost_units": float(billing["x-datapoints"]),
                "quota_before": expected_quota_before,
                "quota_after": expected_quota_after,
                "http_status": response.status_code,
                "network_execution": True,
                "evidence_kind": ObservationEvidenceKind.REAL_OBSERVED,
                "raw_metadata": {
                    **(
                        dict(payload.get("raw_metadata", {}))
                        if isinstance(payload.get("raw_metadata"), Mapping)
                        else {}
                    ),
                    "provider_billing": billing,
                },
            }
        )
        decoded = TheRundownNetworkResponseV1.from_payload(payload)
        if decoded.outcome is not CanaryOutcome.SUCCESS:
            return decoded
        return decoded


class TheRundownRequestsHttpClientV1:
    """Concrete requests client; only called by an explicitly live-enabled run."""

    @staticmethod
    def _certifi_ca_bundle() -> str:
        """Return the reviewed certificate bundle used by requests."""

        bundle = certifi.where()
        if not isinstance(bundle, str) or not bundle:
            raise NetworkShadowContractError(
                "TheRundown certifi CA bundle is unavailable"
            )
        return bundle

    def execute(
        self, request: TheRundownNetworkHttpRequestV1
    ) -> TheRundownNetworkHttpResponseV1:
        request.validate()
        started = datetime.now(timezone.utc)
        try:
            response = requests.request(
                method=request.method,
                url=request.endpoint,
                params=dict(request.query),
                headers=dict(request.headers),
                timeout=request.timeout_seconds,
                verify=self._certifi_ca_bundle(),
                allow_redirects=False,
            )
            body = bytes(response.content)
            status_code = int(response.status_code)
            raw_headers = {
                str(key): str(value) for key, value in response.headers.items()
            }
            finished = datetime.now(timezone.utc)
            return TheRundownNetworkHttpResponseV1(
                status_code=status_code,
                payload=_decode_json_body(body),
                headers=_safe_quota_proof_headers(raw_headers),
                started_at=started,
                finished_at=finished,
                error_detail="HTTPError" if status_code >= 300 else None,
                content_type=_content_type(raw_headers),
                body_length=len(body),
                body_digest=sha256(body).hexdigest(),
            )
        except Exception as exc:  # noqa: BLE001 - transport boundary fails closed
            finished = datetime.now(timezone.utc)
            transport = _transport_failure_metadata(exc)
            return TheRundownNetworkHttpResponseV1(
                status_code=None,
                payload=None,
                headers={},
                started_at=started,
                finished_at=finished,
                timed_out=type(exc).__name__ == "TimeoutError",
                error_detail=type(exc).__name__,
                **transport,
            )


class TheRundownUrlLibHttpClientV1(TheRundownRequestsHttpClientV1):
    """Compatibility name for callers of the pre-requests transport."""


def execute_therundown_quota_proof(
    request: TheRundownQuotaProofRequestV1,
    *,
    api_key: str,
    http_client: TheRundownNetworkHttpClient | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> TheRundownQuotaProofEvidenceV1:
    """Perform exactly one proof request and never continue into league calls.

    ``now`` is the pre-request instant used for request validation. Response
    evidence is validated against a fresh post-response instant, supplied by
    ``clock`` when deterministic control is required.
    """

    preflight_now = _utc(now or datetime.now(timezone.utc), "quota proof preflight now")
    request.validate(now=preflight_now)
    _text(api_key, "TheRundown API credential")
    client = http_client or TheRundownRequestsHttpClientV1()
    try:
        response = client.execute(request.as_http_request(api_key, now=preflight_now))
    except Exception as exc:
        diagnostic_now = _utc(
            preflight_now, "quota proof transport diagnostic now"
        ).isoformat()
        transport = _transport_failure_metadata(exc)
        raise NetworkShadowExecutionBlocked(
            "quota proof provider transport failed",
            diagnostic={
                "failure_classification": "transport_failure",
                "request_started_at": diagnostic_now,
                "request_finished_at": diagnostic_now,
                "http_status": None,
                "safe_response_headers": {},
                "transport_exception_class": type(exc).__name__,
                **transport,
                "content_type": None,
                "response_body_length": None,
                "response_body_digest": None,
            },
        ) from exc
    try:
        response_validation_now = _utc(
            clock() if clock is not None else datetime.now(timezone.utc),
            "quota proof response validation now",
        )
        return TheRundownQuotaProofEvidenceV1.from_http_response(
            request,
            response,
            api_key=api_key,
            now=response_validation_now,
            request_now=preflight_now,
        )
    except NetworkShadowExecutionBlocked as exc:
        if exc.diagnostic is not None:
            raise
        raise NetworkShadowExecutionBlocked(
            str(exc),
            diagnostic=response.safe_failure_diagnostic(
                "quota_proof_validation_failure"
            ),
        ) from exc


def _failure_response(
    request: TheRundownNetworkRequestV1,
    outcome: CanaryOutcome,
    response: TheRundownNetworkHttpResponseV1,
) -> TheRundownNetworkResponseV1:
    return TheRundownNetworkResponseV1(
        outcome=outcome,
        provider=request.target.provider,
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
        self.http_client = http_client or TheRundownRequestsHttpClientV1()
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
        fail_closed_immediately: bool = False,
    ) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.pacer = pacer or time.sleep
        self.allow_live_network = allow_live_network
        self.fail_closed_immediately = fail_closed_immediately

    def run(
        self,
        configuration: TheRundownNetworkConfigurationV1,
        authorization: TheRundownNetworkAuthorizationV1,
        *,
        transport: TheRundownCanaryNetworkTransport,
        quota_headroom: TheRundownQuotaHeadroomEvidenceV1 | None = None,
        post_capture_validator: Callable[[TheRundownNetworkShadowCaptureV1], None]
        | None = None,
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
        if not test_only:
            authorization.validate(
                configuration,
                now=self.clock(),
                require_quota_headroom=True,
            )
            if quota_headroom is None:
                raise NetworkShadowExecutionBlocked(
                    "quota headroom evidence is required before network execution"
                )
            quota_headroom.validate(
                expected_provider=authorization.provider,
                expected_authorization=authorization,
                now=self.clock(),
            )
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
                if self.fail_closed_immediately:
                    break
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
                if self.fail_closed_immediately:
                    break
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
                if self.fail_closed_immediately:
                    break
                continue
            capture = _build_capture(target, request, response, test_only=test_only)
            if post_capture_validator is not None:
                try:
                    post_capture_validator(capture)
                except Exception as exc:  # noqa: BLE001 - safety hook fails closed
                    failures.append(f"{target.league}:POST_CAPTURE_VALIDATION:{exc}")
                    break
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
        if response.quota_cost_units != float(response.datapoint_count):
            raise NetworkShadowExecutionBlocked(
                "provider billing units do not reconcile"
            )
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
            provider_billing = response.raw_metadata.get("provider_billing")
            if not isinstance(provider_billing, Mapping):
                raise NetworkShadowExecutionBlocked("rate-limit evidence is missing")
            limit = provider_billing.get("x-rate-limit")
            if limit is None:
                limit = provider_billing.get("x-ratelimit-limit")
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                raise NetworkShadowExecutionBlocked("rate-limit evidence is missing")
        else:
            _nonnegative_int(response.rate_limit_remaining, "rate_limit_remaining")
            if response.rate_limit_reset_at is None:
                raise NetworkShadowExecutionBlocked(
                    "rate-limit reset evidence is missing"
                )
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
        "datapoint_count": response.datapoint_count,
        "provider_billing": response.raw_metadata.get("provider_billing", {}),
        "normalized_observations": response.raw_metadata.get(
            "normalized_observations", []
        ),
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
    "QUOTA_HEADROOM_SCHEMA_VERSION",
    "QUOTA_PROOF_AUTHORIZATION_SCHEMA_VERSION",
    "QUOTA_PROOF_MAXIMUM_AGE_SECONDS",
    "QUOTA_PROOF_MAX_DATAPOINTS",
    "QUOTA_PROOF_MAX_REQUEST_COUNT",
    "QUOTA_PROOF_MINIMUM_REMAINING_DATAPOINTS",
    "QUOTA_PROOF_SCHEMA_VERSION",
    "THERUNDOWN_DATED_SNAPSHOT_QUOTA_PROOF_DATAPOINTS_PER_REQUEST",
    "THERUNDOWN_OBSERVED_DATAPOINTS_PER_REQUEST",
    "TOP5_CONTROLLED_SHADOW_DATAPOINT_BUDGET",
    "TOP5_CONTROLLED_SHADOW_MINIMUM_INTERVAL_SECONDS",
    "TOP5_CONTROLLED_SHADOW_QUOTA_BUDGET",
    "TOP5_CONTROLLED_SHADOW_REQUEST_COUNT",
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
    "TheRundownQuotaHeadroomEvidenceV1",
    "TheRundownQuotaProofAuthorizationV1",
    "TheRundownQuotaProofEvidenceV1",
    "TheRundownQuotaProofRequestV1",
    "TheRundownReplayTransportV1",
    "TheRundownRequestsHttpClientV1",
    "TheRundownUrlLibHttpClientV1",
    "execute_therundown_quota_proof",
]
