"""Controlled Shadow Provider Qualification and Real Observation Gate.

This module is a deliberately side-effect-free validation boundary.  It
consumes serialized evidence produced by an independently controlled source,
validates exact fixture identity, pre-match 1X2 quality, source-time
provenance, cascade causality, and safety, and emits qualification metrics.

It never imports a provider client, scheduler, publisher, model registry,
ledger, Cloudflare binding, or Builder 4 execution implementation.  A
``REAL_OBSERVED`` record is evidence of a completed observation; this module
does not perform or authorize that observation.  Offline fixtures are marked
``TEST_FIXTURE`` and can never count as real evidence.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from math import isfinite
from statistics import median

from src.football.production_contracts import ProductionContractError
from src.football.top5_provider_cascade_validation import (
    CASCADE_PROVIDER_ORDER,
    CascadeEvidence,
    CascadeOutcome,
    CascadeValidationPolicy,
    ExpectedCascadeFixture,
    RequestCostClassification,
    cascade_to_shadow_observation_evidence,
    validate_cascade_evidence,
)
from src.football.top5_shadow_provider_redundancy import (
    ProviderReadinessState,
    make_fixture_key,
)

QUALIFICATION_CONTRACT_VERSION = "top5-controlled-shadow-provider-qualification-v1"
TOP5_LEAGUES = ("BL1", "EPL", "LL", "SA", "L1")
QUALIFICATION_ARCHIVE_ROOT = "top5-provider-qualification"
NO_PRODUCTION_SIGNAL_TIME_VALUES = "NO PRODUCTION SIGNAL-TIME VALUES APPROVED"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_KNOWN_PROVIDERS = frozenset(CASCADE_PROVIDER_ORDER)
_SAFE_SOURCE_WORDS = frozenset({"consensus", "authority", "selected", "best"})


class QualificationContractError(ProductionContractError):
    """Malformed qualification input or an unsafe attempted transition."""


class ObservationEvidenceKind(str, Enum):
    TEST_FIXTURE = "TEST_FIXTURE"
    MOCK = "MOCK"
    OFFLINE_REPLAY = "OFFLINE_REPLAY"
    REAL_OBSERVED = "REAL_OBSERVED"


class ProviderTimestampProvenance(str, Enum):
    PROVIDER_SOURCE_TIMESTAMP = "PROVIDER_SOURCE_TIMESTAMP"
    BOOKMAKER_UPDATE_TIMESTAMP = "BOOKMAKER_UPDATE_TIMESTAMP"
    EXCHANGE_PUBLISH_TIMESTAMP = "EXCHANGE_PUBLISH_TIMESTAMP"
    CAPTURE_TIME_ONLY = "CAPTURE_TIME_ONLY"
    UNKNOWN = "UNKNOWN"


class ProviderQualificationStatus(str, Enum):
    NOT_OBSERVED = "NOT_OBSERVED"
    OBSERVED_REJECTED = "OBSERVED_REJECTED"
    OBSERVED_VALID_CONTRACT = "OBSERVED_VALID_CONTRACT"
    REAL_OBSERVATION_VALIDATED = "REAL_OBSERVATION_VALIDATED"


class QualificationCode(str, Enum):
    INVALID_OBSERVATION = "INVALID_OBSERVATION"
    EVIDENCE_NOT_REAL = "EVIDENCE_NOT_REAL"
    REAL_PROVENANCE_MISSING = "REAL_PROVENANCE_MISSING"
    PROVIDER_EVENT_ID_MISSING = "PROVIDER_EVENT_ID_MISSING"
    PROVIDER_REQUEST_ID_MISSING = "PROVIDER_REQUEST_ID_MISSING"
    RAW_DIGEST_MISSING = "RAW_DIGEST_MISSING"
    NORMALIZED_DIGEST_MISSING = "NORMALIZED_DIGEST_MISSING"
    ADAPTER_PROVENANCE_MISSING = "ADAPTER_PROVENANCE_MISSING"
    PROVIDER_EVENT_ID_MISMATCH = "PROVIDER_EVENT_ID_MISMATCH"
    PROVIDER_REQUEST_ID_MISMATCH = "PROVIDER_REQUEST_ID_MISMATCH"
    RAW_DIGEST_MISMATCH = "RAW_DIGEST_MISMATCH"
    ADAPTER_VERSION_MISMATCH = "ADAPTER_VERSION_MISMATCH"
    SOURCE_TIMESTAMP_MISMATCH = "SOURCE_TIMESTAMP_MISMATCH"
    SOURCE_TIME_REQUIRED = "SOURCE_TIME_REQUIRED"
    CAPTURE_TIME_ONLY = "CAPTURE_TIME_ONLY"
    UNKNOWN_SOURCE_TIME = "UNKNOWN_SOURCE_TIME"
    STALE_OBSERVATION = "STALE_OBSERVATION"
    FUTURE_SOURCE_TIMESTAMP = "FUTURE_SOURCE_TIMESTAMP"
    MINIMUM_LEAD_NOT_MET = "MINIMUM_LEAD_NOT_MET"
    MAXIMUM_LEAD_EXCEEDED = "MAXIMUM_LEAD_EXCEEDED"
    KICKOFF_OUTSIDE_TOLERANCE = "KICKOFF_OUTSIDE_TOLERANCE"
    WRONG_LEAGUE = "WRONG_LEAGUE"
    WRONG_FIXTURE = "WRONG_FIXTURE"
    TEAM_ALIAS_MISMATCH = "TEAM_ALIAS_MISMATCH"
    HOME_AWAY_INVERSION = "HOME_AWAY_INVERSION"
    PROVIDER_FIXTURE_COLLISION = "PROVIDER_FIXTURE_COLLISION"
    DUPLICATE_OBSERVATION = "DUPLICATE_OBSERVATION"
    CONFLICTING_DUPLICATE = "CONFLICTING_DUPLICATE"
    WRONG_MARKET = "WRONG_MARKET"
    IN_PLAY_MARKET = "IN_PLAY_MARKET"
    CLOSING_MARKET = "CLOSING_MARKET"
    PARTIAL_MARKET = "PARTIAL_MARKET"
    MISSING_DRAW = "MISSING_DRAW"
    MALFORMED_ODDS = "MALFORMED_ODDS"
    SYNTHETIC_RECONSTRUCTION = "SYNTHETIC_RECONSTRUCTION"
    BETFAIR_DELAY_NOT_DISCLOSED = "BETFAIR_DELAY_NOT_DISCLOSED"
    PROVIDER_NOT_READY = "PROVIDER_NOT_READY"
    AUTHORIZATION_MISSING = "AUTHORIZATION_MISSING"
    AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
    AUTHORIZATION_SCOPE_MISMATCH = "AUTHORIZATION_SCOPE_MISMATCH"
    PAID_SPEND_AUTHORIZATION = "PAID_SPEND_AUTHORIZATION"
    NETWORK_CALL_NOT_ALLOWED = "NETWORK_CALL_NOT_ALLOWED"
    MODEL_BOUND = "MODEL_BOUND"
    NO_BET_VIOLATION = "NO_BET_VIOLATION"
    PUBLICATION_ENABLED = "PUBLICATION_ENABLED"
    PRODUCTION_ACTIVATION = "PRODUCTION_ACTIVATION"
    LEDGER_MUTATION = "LEDGER_MUTATION"
    SEALED_DATA_ACCESS = "SEALED_DATA_ACCESS"
    RESEARCH_MUTATION = "RESEARCH_MUTATION"
    TIMEOUT = "TIMEOUT"
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    HTTP_401 = "HTTP_401"
    HTTP_403 = "HTTP_403"
    HTTP_429 = "HTTP_429"
    HTTP_5XX = "HTTP_5XX"
    OFFLINE_REPLAY = "OFFLINE_REPLAY"
    UNRESOLVED = "UNRESOLVED"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualificationContractError(f"{name} is required")
    return value.strip()


def _sha(value: object, name: str) -> str:
    value = _text(value, name)
    if _SHA_RE.fullmatch(value) is None:
        raise QualificationContractError(f"{name} must be a 40-64 character SHA")
    return value.lower()


def _utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise QualificationContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _enum(enum_type: type[Enum], value: object, name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise QualificationContractError(f"{name} is invalid") from exc


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence):
        raise QualificationContractError("expected a sequence")
    return tuple(_text(item, "sequence item") for item in value)


def _number(value: object, name: str, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QualificationContractError(f"{name} must be numeric") from exc
    if not isfinite(number) or number < minimum:
        raise QualificationContractError(f"{name} must be finite and non-negative")
    return number


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat()


def _json_digest(payload: object) -> str:
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class QualificationTimingPolicy:
    """Caller-supplied experiment timing; deliberately has no defaults."""

    maximum_odds_age_seconds: int
    kickoff_tolerance_seconds: int
    minimum_lead_seconds: int
    maximum_lead_seconds: int

    def validate(self) -> None:
        for name, value in (
            ("maximum_odds_age_seconds", self.maximum_odds_age_seconds),
            ("kickoff_tolerance_seconds", self.kickoff_tolerance_seconds),
            ("minimum_lead_seconds", self.minimum_lead_seconds),
            ("maximum_lead_seconds", self.maximum_lead_seconds),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise QualificationContractError(
                    f"{name} must be a non-negative integer"
                )
        if self.maximum_odds_age_seconds == 0:
            raise QualificationContractError(
                "maximum_odds_age_seconds must be positive"
            )
        if self.minimum_lead_seconds > self.maximum_lead_seconds:
            raise QualificationContractError("minimum lead cannot exceed maximum lead")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "maximum_odds_age_seconds": self.maximum_odds_age_seconds,
            "kickoff_tolerance_seconds": self.kickoff_tolerance_seconds,
            "minimum_lead_seconds": self.minimum_lead_seconds,
            "maximum_lead_seconds": self.maximum_lead_seconds,
            "production_signal_time_values_approved": False,
            "note": NO_PRODUCTION_SIGNAL_TIME_VALUES,
        }


@dataclass(frozen=True)
class MinimumSamplePolicy:
    """Caller-supplied sample threshold; it never authorizes production."""

    minimum_real_observations: int
    minimum_distinct_fixtures: int = 1

    def validate(self) -> None:
        for name, value in (
            ("minimum_real_observations", self.minimum_real_observations),
            ("minimum_distinct_fixtures", self.minimum_distinct_fixtures),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise QualificationContractError(f"{name} must be a positive integer")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "minimum_real_observations": self.minimum_real_observations,
            "minimum_distinct_fixtures": self.minimum_distinct_fixtures,
            "production_approval": False,
        }


@dataclass(frozen=True)
class CEOAuthorization:
    """A future, caller-supplied authorization envelope; never created here."""

    authorization_id: str
    provider_scope: tuple[str, ...]
    league_scope: tuple[str, ...]
    fixture_scope: tuple[str, ...]
    maximum_network_requests: int
    monetary_spend_authorized: bool
    issued_at: datetime
    expires_at: datetime
    single_use: bool = True

    def validate(self) -> None:
        _text(self.authorization_id, "authorization_id")
        providers = _strings(self.provider_scope)
        leagues = _strings(self.league_scope)
        fixtures = _strings(self.fixture_scope)
        if not providers or any(item not in _KNOWN_PROVIDERS for item in providers):
            raise QualificationContractError("authorization provider scope is invalid")
        if not leagues or any(item not in TOP5_LEAGUES for item in leagues):
            raise QualificationContractError("authorization league scope is invalid")
        if not fixtures:
            raise QualificationContractError("authorization fixture scope is required")
        if len(providers) != len(set(providers)) or len(leagues) != len(set(leagues)):
            raise QualificationContractError("authorization scopes must be unique")
        if (
            not isinstance(self.maximum_network_requests, int)
            or isinstance(self.maximum_network_requests, bool)
            or self.maximum_network_requests <= 0
        ):
            raise QualificationContractError("authorization request budget is invalid")
        if self.monetary_spend_authorized is not False:
            raise QualificationContractError(
                "monetary spend authorization is forbidden"
            )
        start = _utc(self.issued_at, "issued_at")
        end = _utc(self.expires_at, "expires_at")
        if end <= start:
            raise QualificationContractError(
                "authorization expiry must follow issue time"
            )
        if not isinstance(self.single_use, bool):
            raise QualificationContractError("single_use must be boolean")

    def allows(self, observation: RealProviderObservation) -> QualificationCode | None:
        self.validate()
        capture = _utc(observation.captured_at, "captured_at")
        if not (
            _utc(self.issued_at, "issued_at")
            <= capture
            <= _utc(self.expires_at, "expires_at")
        ):
            return QualificationCode.AUTHORIZATION_EXPIRED
        if (
            observation.provider_identity not in self.provider_scope
            or observation.league not in self.league_scope
            or observation.fixture_key not in self.fixture_scope
        ):
            return QualificationCode.AUTHORIZATION_SCOPE_MISMATCH
        return None

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "authorization_id": self.authorization_id,
            "provider_scope": list(self.provider_scope),
            "league_scope": list(self.league_scope),
            "fixture_scope": list(self.fixture_scope),
            "maximum_network_requests": self.maximum_network_requests,
            "monetary_spend_authorized": False,
            "issued_at": _iso(self.issued_at),
            "expires_at": _iso(self.expires_at),
            "single_use": self.single_use,
        }


@dataclass(frozen=True)
class QualificationArchive:
    """Deterministic namespace separate from replay, prediction, and ledger."""

    session_id: str
    root: str = QUALIFICATION_ARCHIVE_ROOT

    def paths(self) -> dict[str, str]:
        _text(self.session_id, "session_id")
        base = f"{self.root}/{self.session_id}"
        return {
            "manifest": f"{base}/manifest.json",
            "observations": f"{base}/observations.jsonl",
            "validation": f"{base}/validation.jsonl",
            "rejections": f"{base}/rejections.jsonl",
            "coverage": f"{base}/coverage.json",
            "freshness": f"{base}/freshness.json",
        }


@dataclass(frozen=True)
class ProviderQualificationSession:
    qualification_session_id: str
    schema_version: str
    created_at: datetime
    provider_identity: str
    league_scope: tuple[str, ...]
    fixture_scope: tuple[str, ...]
    configured_provider_order: tuple[str, ...]
    adapter_version: str
    adapter_source_sha: str
    qualification_state: ProviderReadinessState | str
    real_observation_count: int = 0
    accepted_observation_count: int = 0
    rejected_observation_count: int = 0
    network_request_count: int = 0
    quota_units_observed: float = 0.0
    monetary_spend_authorized: bool = False
    no_bet: bool = True
    publication_enabled: bool = False
    production_activation: bool = False
    ledger_mutated: bool = False
    sealed_data_accessed: bool = False
    research_mutated: bool = False

    def validate(self) -> None:
        _text(self.qualification_session_id, "qualification_session_id")
        if self.schema_version != QUALIFICATION_CONTRACT_VERSION:
            raise QualificationContractError("unsupported qualification schema")
        _utc(self.created_at, "created_at")
        provider = _text(self.provider_identity, "provider_identity")
        if provider != "top5_cascade" and provider not in _KNOWN_PROVIDERS:
            raise QualificationContractError("session provider is not a candidate")
        leagues = _strings(self.league_scope)
        if not leagues or any(item not in TOP5_LEAGUES for item in leagues):
            raise QualificationContractError("session league scope is invalid")
        fixtures = _strings(self.fixture_scope)
        if not fixtures:
            raise QualificationContractError("session fixture scope is required")
        order = _strings(self.configured_provider_order)
        if (
            not order
            or len(order) != len(set(order))
            or any(item not in _KNOWN_PROVIDERS for item in order)
        ):
            raise QualificationContractError("session provider order is invalid")
        _text(self.adapter_version, "adapter_version")
        _sha(self.adapter_source_sha, "adapter_source_sha")
        _enum(ProviderReadinessState, self.qualification_state, "qualification_state")
        for name, value in (
            ("real_observation_count", self.real_observation_count),
            ("accepted_observation_count", self.accepted_observation_count),
            ("rejected_observation_count", self.rejected_observation_count),
            ("network_request_count", self.network_request_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise QualificationContractError(f"{name} must be non-negative")
        _number(self.quota_units_observed, "quota_units_observed")
        if self.monetary_spend_authorized is not False:
            raise QualificationContractError("session monetary spend must remain false")
        for name, value, expected in (
            ("no_bet", self.no_bet, True),
            ("publication_enabled", self.publication_enabled, False),
            ("production_activation", self.production_activation, False),
            ("ledger_mutated", self.ledger_mutated, False),
            ("sealed_data_accessed", self.sealed_data_accessed, False),
            ("research_mutated", self.research_mutated, False),
        ):
            if value is not expected:
                raise QualificationContractError(
                    f"session safety flag {name} is unsafe"
                )
        if (
            ProviderReadinessState(self.qualification_state)
            is ProviderReadinessState.REAL_OBSERVATION_VALIDATED
            and self.accepted_observation_count == 0
        ):
            raise QualificationContractError(
                "real validation cannot be asserted without accepted real evidence"
            )

    @property
    def archive(self) -> QualificationArchive:
        return QualificationArchive(self.qualification_session_id)

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "qualification_session_id": self.qualification_session_id,
            "schema_version": self.schema_version,
            "created_at": _iso(self.created_at),
            "provider_identity": self.provider_identity,
            "league_scope": list(self.league_scope),
            "fixture_scope": list(self.fixture_scope),
            "configured_provider_order": list(self.configured_provider_order),
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "qualification_state": ProviderReadinessState(
                self.qualification_state
            ).value,
            "real_observation_count": self.real_observation_count,
            "accepted_observation_count": self.accepted_observation_count,
            "rejected_observation_count": self.rejected_observation_count,
            "network_request_count": self.network_request_count,
            "quota_units_observed": self.quota_units_observed,
            "monetary_spend_authorized": False,
            "safety": {
                "no_bet": True,
                "publication_enabled": False,
                "production_activation": False,
                "ledger_mutated": False,
                "sealed_data_accessed": False,
                "research_mutated": False,
            },
            "archive": self.archive.paths(),
        }


@dataclass(frozen=True)
class RealProviderObservation:
    """Immutable normalized observation envelope, including its cascade."""

    observation_id: str
    qualification_session_id: str
    evidence_kind: ObservationEvidenceKind | str
    provider_identity: str
    provider_event_id: str
    provider_request_id: str
    league: str
    fixture_key: str
    home_team: str
    away_team: str
    kickoff: datetime
    market_type: str
    market_phase: str
    home_odds: float | None
    draw_odds: float | None
    away_odds: float | None
    bookmaker_identity: str
    source_identity: str
    source_timestamp: datetime | None
    provider_timestamp_provenance: ProviderTimestampProvenance | str
    captured_at: datetime
    request_started_at: datetime
    request_finished_at: datetime
    latency_ms: int
    adapter_version: str
    adapter_source_sha: str
    raw_response_digest: str
    normalized_record_digest: str
    cascade_evidence: CascadeEvidence | Mapping[str, object]
    quota_before: int | None
    quota_after: int | None
    quota_cost_units: float
    network_request_count: int
    monetary_spend_authorized: bool = False
    delayed_observation: bool = False
    synthetic_reconstruction: bool = False
    no_bet: bool = True
    publication_enabled: bool = False
    production_activation: bool = False
    ledger_mutated: bool = False
    sealed_data_accessed: bool = False
    research_mutated: bool = False

    @property
    def provider_timestamp(self) -> datetime | None:
        return self.source_timestamp

    def validate_structural(self) -> None:
        for name, value in (
            ("observation_id", self.observation_id),
            ("qualification_session_id", self.qualification_session_id),
            ("provider_identity", self.provider_identity),
            ("provider_event_id", self.provider_event_id),
            ("provider_request_id", self.provider_request_id),
            ("league", self.league),
            ("fixture_key", self.fixture_key),
            ("home_team", self.home_team),
            ("away_team", self.away_team),
            ("market_type", self.market_type),
            ("market_phase", self.market_phase),
            ("bookmaker_identity", self.bookmaker_identity),
            ("source_identity", self.source_identity),
            ("adapter_version", self.adapter_version),
            ("adapter_source_sha", self.adapter_source_sha),
            ("raw_response_digest", self.raw_response_digest),
            ("normalized_record_digest", self.normalized_record_digest),
        ):
            _text(value, name)
        _enum(ObservationEvidenceKind, self.evidence_kind, "evidence_kind")
        if self.provider_identity not in _KNOWN_PROVIDERS:
            raise QualificationContractError("unknown provider candidate")
        if self.league not in TOP5_LEAGUES:
            raise QualificationContractError(
                "league is outside the Top-5 qualification scope"
            )
        if any(
            token in self.source_identity.lower().split()
            for token in _SAFE_SOURCE_WORDS
        ):
            raise QualificationContractError(
                "source identity cannot infer authority or consensus"
            )
        _utc(self.kickoff, "kickoff")
        _utc(self.captured_at, "captured_at")
        start = _utc(self.request_started_at, "request_started_at")
        end = _utc(self.request_finished_at, "request_finished_at")
        if end < start or not start <= _utc(self.captured_at, "captured_at") <= end:
            raise QualificationContractError("request timing is not ordered")
        if (
            not isinstance(self.latency_ms, int)
            or isinstance(self.latency_ms, bool)
            or self.latency_ms < 0
        ):
            raise QualificationContractError("latency_ms must be non-negative")
        _sha(self.adapter_source_sha, "adapter_source_sha")
        _sha(self.raw_response_digest, "raw_response_digest")
        _sha(self.normalized_record_digest, "normalized_record_digest")
        _enum(
            ProviderTimestampProvenance,
            self.provider_timestamp_provenance,
            "provider_timestamp_provenance",
        )
        if self.source_timestamp is not None:
            _utc(self.source_timestamp, "source_timestamp")
        if not isinstance(
            self.network_request_count, int
        ) or self.network_request_count not in {0, 1}:
            raise QualificationContractError(
                "network_request_count must be exactly 0 or 1"
            )
        if self.network_request_count != 1:
            raise QualificationContractError(
                "a serialized provider observation must document one request"
            )
        _number(self.quota_cost_units, "quota_cost_units")
        for name, value in (
            ("quota_before", self.quota_before),
            ("quota_after", self.quota_after),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise QualificationContractError(f"{name} must be non-negative or null")
        for name, value, expected in (
            ("monetary_spend_authorized", self.monetary_spend_authorized, False),
            ("synthetic_reconstruction", self.synthetic_reconstruction, False),
            ("no_bet", self.no_bet, True),
            ("publication_enabled", self.publication_enabled, False),
            ("production_activation", self.production_activation, False),
            ("ledger_mutated", self.ledger_mutated, False),
            ("sealed_data_accessed", self.sealed_data_accessed, False),
            ("research_mutated", self.research_mutated, False),
        ):
            if value is not expected:
                raise QualificationContractError(f"unsafe observation flag: {name}")
        if (
            self.provider_identity == "betfair_delayed"
            and self.delayed_observation is not True
        ):
            raise QualificationContractError(
                "Betfair Delayed observations must disclose delay explicitly"
            )

    @classmethod
    def from_payload(cls, payload: object) -> RealProviderObservation:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(
            observation_id=raw.get("observation_id", ""),
            qualification_session_id=raw.get("qualification_session_id", ""),
            evidence_kind=raw.get("evidence_kind", ""),
            provider_identity=raw.get("provider_identity", ""),
            provider_event_id=raw.get("provider_event_id", ""),
            provider_request_id=raw.get("provider_request_id", ""),
            league=raw.get("league", ""),
            fixture_key=raw.get("fixture_key", ""),
            home_team=raw.get("home_team", ""),
            away_team=raw.get("away_team", ""),
            kickoff=_parse_datetime(raw.get("kickoff")),
            market_type=raw.get("market_type", ""),
            market_phase=raw.get("market_phase", ""),
            home_odds=raw.get("home_odds"),
            draw_odds=raw.get("draw_odds"),
            away_odds=raw.get("away_odds"),
            bookmaker_identity=raw.get("bookmaker_identity", ""),
            source_identity=raw.get("source_identity", ""),
            source_timestamp=_parse_datetime(raw.get("source_timestamp")),
            provider_timestamp_provenance=raw.get("provider_timestamp_provenance", ""),
            captured_at=_parse_datetime(raw.get("captured_at")),
            request_started_at=_parse_datetime(raw.get("request_started_at")),
            request_finished_at=_parse_datetime(raw.get("request_finished_at")),
            latency_ms=raw.get("latency_ms", -1),
            adapter_version=raw.get("adapter_version", ""),
            adapter_source_sha=raw.get("adapter_source_sha", ""),
            raw_response_digest=raw.get("raw_response_digest", ""),
            normalized_record_digest=raw.get("normalized_record_digest", ""),
            cascade_evidence=raw.get("cascade_evidence", {}),
            quota_before=raw.get("quota_before"),
            quota_after=raw.get("quota_after"),
            quota_cost_units=raw.get("quota_cost_units", -1),
            network_request_count=raw.get("network_request_count", -1),
            monetary_spend_authorized=raw.get("monetary_spend_authorized"),
            delayed_observation=raw.get("delayed_observation", False),
            synthetic_reconstruction=raw.get("synthetic_reconstruction", False),
            no_bet=raw.get("no_bet"),
            publication_enabled=raw.get("publication_enabled"),
            production_activation=raw.get("production_activation"),
            ledger_mutated=raw.get("ledger_mutated"),
            sealed_data_accessed=raw.get("sealed_data_accessed"),
            research_mutated=raw.get("research_mutated"),
        )

    def as_payload(self) -> dict[str, object]:
        self.validate_structural()
        cascade = self.cascade_evidence
        cascade_payload = (
            cascade.as_payload()
            if isinstance(cascade, CascadeEvidence)
            else dict(cascade)
        )
        return {
            "observation_id": self.observation_id,
            "qualification_session_id": self.qualification_session_id,
            "evidence_kind": ObservationEvidenceKind(self.evidence_kind).value,
            "provider_identity": self.provider_identity,
            "provider_event_id": self.provider_event_id,
            "provider_request_id": self.provider_request_id,
            "league": self.league,
            "fixture_key": self.fixture_key,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": _iso(self.kickoff),
            "market_type": self.market_type,
            "market_phase": self.market_phase,
            "home_odds": self.home_odds,
            "draw_odds": self.draw_odds,
            "away_odds": self.away_odds,
            "bookmaker_identity": self.bookmaker_identity,
            "source_identity": self.source_identity,
            "source_timestamp": _iso(self.source_timestamp)
            if self.source_timestamp
            else None,
            "provider_timestamp_provenance": ProviderTimestampProvenance(
                self.provider_timestamp_provenance
            ).value,
            "captured_at": _iso(self.captured_at),
            "request_started_at": _iso(self.request_started_at),
            "request_finished_at": _iso(self.request_finished_at),
            "latency_ms": self.latency_ms,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "raw_response_digest": self.raw_response_digest,
            "normalized_record_digest": self.normalized_record_digest,
            "cascade_evidence": cascade_payload,
            "quota_before": self.quota_before,
            "quota_after": self.quota_after,
            "quota_cost_units": self.quota_cost_units,
            "network_request_count": self.network_request_count,
            "monetary_spend_authorized": False,
            "delayed_observation": self.delayed_observation,
            "synthetic_reconstruction": False,
            "no_bet": True,
            "publication_enabled": False,
            "production_activation": False,
            "ledger_mutated": False,
            "sealed_data_accessed": False,
            "research_mutated": False,
        }


def _fixture_key(league: str, home: str, away: str, kickoff: datetime) -> str:
    return make_fixture_key(league, home, away, kickoff)


@dataclass(frozen=True)
class FreshnessSummary:
    n: int
    minimum_seconds: float | None
    median_seconds: float | None
    maximum_seconds: float | None

    @classmethod
    def from_values(cls, values: Sequence[float]) -> FreshnessSummary:
        ordered = tuple(float(item) for item in values)
        return cls(
            n=len(ordered),
            minimum_seconds=min(ordered) if ordered else None,
            median_seconds=float(median(ordered)) if ordered else None,
            maximum_seconds=max(ordered) if ordered else None,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "n": self.n,
            "min_seconds": self.minimum_seconds,
            "median_seconds": self.median_seconds,
            "max_seconds": self.maximum_seconds,
        }


@dataclass(frozen=True)
class ProviderLeagueMetrics:
    provider_identity: str
    league: str
    observed_count: int
    accepted_count: int
    rejected_count: int
    coverage_denominator: int
    coverage_rate: float | None
    freshness: FreshnessSummary
    capture_latency_ms: FreshnessSummary
    time_to_kickoff_seconds: FreshnessSummary
    failure_counts: Mapping[str, int]

    def as_payload(self) -> dict[str, object]:
        return {
            "provider_identity": self.provider_identity,
            "league": self.league,
            "observed_count": self.observed_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "coverage_denominator": self.coverage_denominator,
            "coverage_rate": self.coverage_rate,
            "freshness": self.freshness.as_payload(),
            "capture_latency_ms": self.capture_latency_ms.as_payload(),
            "time_to_kickoff": self.time_to_kickoff_seconds.as_payload(),
            "failure_counts": dict(self.failure_counts),
        }


@dataclass(frozen=True)
class ObservationValidationResult:
    observation_id: str
    provider_identity: str
    league: str
    status: ProviderQualificationStatus
    accepted: bool
    real_observed: bool
    failure_codes: tuple[str, ...] = ()
    cascade_errors: tuple[str, ...] = ()
    duplicate_suppressed: bool = False
    source_age_seconds: float | None = None
    capture_latency_ms: float | None = None
    time_to_kickoff_seconds: float | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "provider_identity": self.provider_identity,
            "league": self.league,
            "status": self.status.value,
            "accepted": self.accepted,
            "real_observed": self.real_observed,
            "failure_codes": list(self.failure_codes),
            "cascade_errors": list(self.cascade_errors),
            "duplicate_suppressed": self.duplicate_suppressed,
            "source_age_seconds": self.source_age_seconds,
            "capture_latency_ms": self.capture_latency_ms,
            "time_to_kickoff_seconds": self.time_to_kickoff_seconds,
        }


@dataclass(frozen=True)
class ProviderQualificationReport:
    qualification_status: ProviderQualificationStatus
    session: ProviderQualificationSession
    results: tuple[ObservationValidationResult, ...]
    provider_statuses: Mapping[str, ProviderQualificationStatus]
    coverage: tuple[ProviderLeagueMetrics, ...]
    freshness: tuple[ProviderLeagueMetrics, ...]
    failure_counts: Mapping[str, int]
    unresolved: tuple[str, ...]
    minimum_sample_policy: MinimumSamplePolicy | None = None
    production_sample_sufficient: bool | None = None
    signal_time_note: str = NO_PRODUCTION_SIGNAL_TIME_VALUES
    production_activation_authorized: bool = False
    recommendation: None = None

    @property
    def accepted_observation_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.observation_id
                for item in self.results
                if item.accepted and item.real_observed
            )
        )

    @property
    def archive_paths(self) -> Mapping[str, str]:
        return self.session.archive.paths()

    def validate(self) -> None:
        self.session.validate()
        if self.minimum_sample_policy is not None:
            self.minimum_sample_policy.validate()
        if self.signal_time_note != NO_PRODUCTION_SIGNAL_TIME_VALUES:
            raise QualificationContractError(
                "production signal-time approval cannot be asserted"
            )
        if self.production_activation_authorized or self.recommendation is not None:
            raise QualificationContractError(
                "qualification cannot authorize or recommend activation"
            )
        for provider, status in self.provider_statuses.items():
            if provider not in _KNOWN_PROVIDERS:
                raise QualificationContractError("report contains an unknown provider")
            ProviderQualificationStatus(status)

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "contract_version": QUALIFICATION_CONTRACT_VERSION,
            "qualification_status": self.qualification_status.value,
            "session": self.session.as_payload(),
            "results": [item.as_payload() for item in self.results],
            "provider_statuses": {
                key: value.value for key, value in self.provider_statuses.items()
            },
            "coverage": [item.as_payload() for item in self.coverage],
            "freshness": [item.as_payload() for item in self.freshness],
            "failure_counts": dict(self.failure_counts),
            "unresolved": list(self.unresolved),
            "minimum_sample_policy": (
                self.minimum_sample_policy.as_payload()
                if self.minimum_sample_policy
                else None
            ),
            "production_sample_sufficient": self.production_sample_sufficient,
            "signal_time_note": self.signal_time_note,
            "production_activation_authorized": False,
            "recommendation": None,
        }


def _fixture_codes(
    observation: RealProviderObservation,
    expected: ExpectedCascadeFixture,
    timing: QualificationTimingPolicy,
) -> list[QualificationCode]:
    errors: list[QualificationCode] = []
    if observation.league != expected.league:
        errors.append(QualificationCode.WRONG_LEAGUE)
    if observation.fixture_key != expected.fixture_key:
        errors.append(QualificationCode.WRONG_FIXTURE)
    if (observation.home_team, observation.away_team) == (
        expected.away_team,
        expected.home_team,
    ):
        errors.append(QualificationCode.HOME_AWAY_INVERSION)
    elif (observation.home_team, observation.away_team) != (
        expected.home_team,
        expected.away_team,
    ):
        errors.append(QualificationCode.TEAM_ALIAS_MISMATCH)
    kickoff_delta = abs(
        (
            _utc(observation.kickoff, "kickoff")
            - _utc(expected.kickoff, "expected kickoff")
        ).total_seconds()
    )
    if kickoff_delta > timing.kickoff_tolerance_seconds:
        errors.append(QualificationCode.KICKOFF_OUTSIDE_TOLERANCE)
    return errors


def _quality_codes(
    observation: RealProviderObservation, timing: QualificationTimingPolicy
) -> tuple[list[QualificationCode], float | None, float, float]:
    errors: list[QualificationCode] = []
    if observation.market_type != "h2h_1x2":
        errors.append(QualificationCode.WRONG_MARKET)
    if observation.market_phase == "IN_PLAY":
        errors.append(QualificationCode.IN_PLAY_MARKET)
    elif observation.market_phase == "CLOSING":
        errors.append(QualificationCode.CLOSING_MARKET)
    source_age: float | None = None
    if observation.source_timestamp is None:
        errors.append(QualificationCode.SOURCE_TIME_REQUIRED)
    else:
        source_age = (
            _utc(observation.captured_at, "captured_at")
            - _utc(observation.source_timestamp, "source_timestamp")
        ).total_seconds()
        if source_age < 0:
            errors.append(QualificationCode.FUTURE_SOURCE_TIMESTAMP)
        elif source_age > timing.maximum_odds_age_seconds:
            errors.append(QualificationCode.STALE_OBSERVATION)
    provenance = ProviderTimestampProvenance(observation.provider_timestamp_provenance)
    if provenance is ProviderTimestampProvenance.CAPTURE_TIME_ONLY:
        errors.append(QualificationCode.CAPTURE_TIME_ONLY)
    elif provenance is ProviderTimestampProvenance.UNKNOWN:
        errors.append(QualificationCode.UNKNOWN_SOURCE_TIME)
    if observation.home_odds is None or observation.away_odds is None:
        errors.append(QualificationCode.PARTIAL_MARKET)
    if observation.draw_odds is None:
        errors.append(QualificationCode.MISSING_DRAW)
    odds = (observation.home_odds, observation.draw_odds, observation.away_odds)
    if any(
        value is not None and (not isfinite(float(value)) or float(value) <= 1.0)
        for value in odds
    ):
        errors.append(QualificationCode.MALFORMED_ODDS)
    if observation.synthetic_reconstruction:
        errors.append(QualificationCode.SYNTHETIC_RECONSTRUCTION)
    lead = (
        _utc(observation.kickoff, "kickoff")
        - _utc(observation.captured_at, "captured_at")
    ).total_seconds()
    if lead < timing.minimum_lead_seconds:
        errors.append(QualificationCode.MINIMUM_LEAD_NOT_MET)
    if lead > timing.maximum_lead_seconds:
        errors.append(QualificationCode.MAXIMUM_LEAD_EXCEEDED)
    capture_latency = (
        _utc(observation.request_finished_at, "request_finished_at")
        - _utc(observation.request_started_at, "request_started_at")
    ).total_seconds() * 1000
    return errors, source_age, capture_latency, lead


def _cascade_codes(
    observation: RealProviderObservation,
    expected: ExpectedCascadeFixture,
    timing: QualificationTimingPolicy,
    provider_readiness: Mapping[str, ProviderReadinessState],
) -> tuple[object, list[str], list[QualificationCode]]:
    record = (
        observation.cascade_evidence
        if isinstance(observation.cascade_evidence, CascadeEvidence)
        else CascadeEvidence.from_payload(observation.cascade_evidence)
    )
    policy = CascadeValidationPolicy(
        maximum_odds_age_seconds=timing.maximum_odds_age_seconds,
        kickoff_tolerance_seconds=timing.kickoff_tolerance_seconds,
        configured_provider_order=tuple(record.configured_provider_order),
        provider_readiness=provider_readiness,
        expected_fixture=expected,
    )
    report = validate_cascade_evidence(record, policy)
    cascade_errors = [item.value for item in report.errors]
    errors: list[QualificationCode] = []
    if not report.accepted:
        errors.append(QualificationCode.UNRESOLVED)
    try:
        selected = next(
            item
            for item in record.attempts
            if item.provider_identity == record.selected_provider
        )
    except StopIteration:
        selected = None
    if selected is not None:
        checks = (
            (
                selected.provider_identity != observation.provider_identity,
                QualificationCode.PROVIDER_EVENT_ID_MISMATCH,
            ),
            (
                selected.provider_record_id != observation.provider_event_id,
                QualificationCode.PROVIDER_EVENT_ID_MISMATCH,
            ),
            (
                selected.request_identity != observation.provider_request_id,
                QualificationCode.PROVIDER_REQUEST_ID_MISMATCH,
            ),
            (
                selected.raw_record_digest.lower()
                != observation.raw_response_digest.lower(),
                QualificationCode.RAW_DIGEST_MISMATCH,
            ),
            (
                selected.adapter_version != observation.adapter_version,
                QualificationCode.ADAPTER_VERSION_MISMATCH,
            ),
        )
        errors.extend(code for mismatch, code in checks if mismatch)
        if selected.source_timestamp != observation.source_timestamp:
            errors.append(QualificationCode.SOURCE_TIMESTAMP_MISMATCH)
        if (
            _enum(CascadeOutcome, selected.outcome, "selected outcome")
            is not CascadeOutcome.SUCCESS
        ):
            errors.append(QualificationCode.PROVIDER_NOT_READY)
        if selected.request_cost_classification == RequestCostClassification.UNKNOWN:
            errors.append(QualificationCode.UNRESOLVED)
    return record, cascade_errors, errors


def _exception_codes(exc: Exception) -> list[QualificationCode]:
    text = str(exc).lower()
    codes: list[QualificationCode] = [QualificationCode.INVALID_OBSERVATION]
    if "betfair" in text:
        codes.append(QualificationCode.BETFAIR_DELAY_NOT_DISCLOSED)
    return codes


def _validate_one(
    observation: RealProviderObservation,
    session: ProviderQualificationSession,
    expected: ExpectedCascadeFixture,
    timing: QualificationTimingPolicy,
    provider_readiness: Mapping[str, ProviderReadinessState],
    authorization: CEOAuthorization | None,
) -> ObservationValidationResult:
    errors: list[QualificationCode] = []
    cascade_errors: list[str] = []
    source_age = capture_latency = lead = None
    real = (
        ObservationEvidenceKind(observation.evidence_kind)
        is ObservationEvidenceKind.REAL_OBSERVED
    )
    try:
        if real:
            if not observation.provider_event_id:
                errors.append(QualificationCode.PROVIDER_EVENT_ID_MISSING)
            if not observation.provider_request_id:
                errors.append(QualificationCode.PROVIDER_REQUEST_ID_MISSING)
            if (
                not isinstance(observation.raw_response_digest, str)
                or not observation.raw_response_digest
            ):
                errors.append(QualificationCode.RAW_DIGEST_MISSING)
            if (
                not isinstance(observation.normalized_record_digest, str)
                or not observation.normalized_record_digest
            ):
                errors.append(QualificationCode.NORMALIZED_DIGEST_MISSING)
            if not observation.adapter_version or not observation.adapter_source_sha:
                errors.append(QualificationCode.ADAPTER_PROVENANCE_MISSING)
        observation.validate_structural()
        errors.extend(_fixture_codes(observation, expected, timing))
        timing_errors, source_age, capture_latency, lead = _quality_codes(
            observation, timing
        )
        errors.extend(timing_errors)
        if real:
            if authorization is None:
                errors.append(QualificationCode.AUTHORIZATION_MISSING)
            else:
                try:
                    authorization_code = authorization.allows(observation)
                    if authorization_code:
                        errors.append(authorization_code)
                except QualificationContractError:
                    errors.append(QualificationCode.AUTHORIZATION_EXPIRED)
        else:
            errors.append(QualificationCode.EVIDENCE_NOT_REAL)
        record, cascade_errors, cascade_quality_errors = _cascade_codes(
            observation, expected, timing, provider_readiness
        )
        errors.extend(cascade_quality_errors)
        if not cascade_errors:
            selected = next(
                item
                for item in record.attempts
                if item.provider_identity == record.selected_provider
            )
            if (
                selected.bookmaker_identity != observation.bookmaker_identity
                or selected.source_identity != observation.source_identity
            ):
                errors.append(QualificationCode.UNRESOLVED)
            if (selected.home_odds, selected.draw_odds, selected.away_odds) != (
                observation.home_odds,
                observation.draw_odds,
                observation.away_odds,
            ):
                errors.append(QualificationCode.UNRESOLVED)
        if observation.provider_identity not in session.configured_provider_order:
            errors.append(QualificationCode.AUTHORIZATION_SCOPE_MISMATCH)
        if (
            observation.league not in session.league_scope
            or observation.fixture_key not in session.fixture_scope
        ):
            errors.append(QualificationCode.AUTHORIZATION_SCOPE_MISMATCH)
    except (
        QualificationContractError,
        ValueError,
        TypeError,
        AttributeError,
        StopIteration,
    ) as exc:
        errors.extend(_exception_codes(exc))
    errors = list(dict.fromkeys(errors))
    accepted_contract = not errors or errors == [QualificationCode.EVIDENCE_NOT_REAL]
    if errors and errors != [QualificationCode.EVIDENCE_NOT_REAL]:
        status = ProviderQualificationStatus.OBSERVED_REJECTED
        accepted = False
    elif real:
        status = ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
        accepted = True
    else:
        status = ProviderQualificationStatus.OBSERVED_VALID_CONTRACT
        accepted = accepted_contract
    return ObservationValidationResult(
        observation_id=observation.observation_id,
        provider_identity=observation.provider_identity,
        league=observation.league,
        status=status,
        accepted=accepted,
        real_observed=real,
        failure_codes=tuple(item.value for item in errors),
        cascade_errors=tuple(cascade_errors),
        source_age_seconds=source_age,
        capture_latency_ms=capture_latency,
        time_to_kickoff_seconds=lead,
    )


def _metrics(
    observations: Sequence[RealProviderObservation],
    results: Sequence[ObservationValidationResult],
) -> tuple[ProviderLeagueMetrics, ...]:
    grouped: dict[
        tuple[str, str],
        list[tuple[RealProviderObservation, ObservationValidationResult]],
    ] = defaultdict(list)
    for observation, result in zip(observations, results, strict=True):
        grouped[(observation.provider_identity, observation.league)].append(
            (observation, result)
        )
    output: list[ProviderLeagueMetrics] = []
    for provider in CASCADE_PROVIDER_ORDER:
        for league in TOP5_LEAGUES:
            pairs = grouped.get((provider, league), [])
            accepted = [
                result
                for _, result in pairs
                if result.accepted and result.real_observed
            ]
            ages = [
                result.source_age_seconds
                for _, result in pairs
                if result.accepted
                and result.real_observed
                and result.source_age_seconds is not None
            ]
            latencies = [
                result.capture_latency_ms
                for _, result in pairs
                if result.accepted
                and result.real_observed
                and result.capture_latency_ms is not None
            ]
            leads = [
                result.time_to_kickoff_seconds
                for _, result in pairs
                if result.accepted
                and result.real_observed
                and result.time_to_kickoff_seconds is not None
            ]
            failures = Counter(
                code for _, result in pairs for code in result.failure_codes
            )
            denominator = len(pairs)
            output.append(
                ProviderLeagueMetrics(
                    provider_identity=provider,
                    league=league,
                    observed_count=denominator,
                    accepted_count=len(accepted),
                    rejected_count=denominator - len(accepted),
                    coverage_denominator=denominator,
                    coverage_rate=(len(accepted) / denominator)
                    if denominator
                    else None,
                    freshness=FreshnessSummary.from_values(ages),
                    capture_latency_ms=FreshnessSummary.from_values(latencies),
                    time_to_kickoff_seconds=FreshnessSummary.from_values(leads),
                    failure_counts=dict(sorted(failures.items())),
                )
            )
    return tuple(output)


def qualify_provider_observations(
    observations: Sequence[RealProviderObservation | Mapping[str, object]],
    session: ProviderQualificationSession,
    expected_fixture: ExpectedCascadeFixture,
    timing_policy: QualificationTimingPolicy,
    provider_readiness: Mapping[str, ProviderReadinessState],
    authorization: CEOAuthorization | None = None,
    *,
    minimum_sample_policy: MinimumSamplePolicy | None = None,
) -> ProviderQualificationReport:
    """Validate already-captured observations; this function performs no I/O."""

    session.validate()
    expected_fixture.validate()
    timing_policy.validate()
    if minimum_sample_policy is not None:
        minimum_sample_policy.validate()
    if not isinstance(provider_readiness, Mapping):
        raise QualificationContractError("explicit provider readiness is required")
    for provider, state in provider_readiness.items():
        if provider not in _KNOWN_PROVIDERS:
            raise QualificationContractError(
                "provider readiness contains an unknown candidate"
            )
        ProviderReadinessState(state)
    if authorization is not None:
        authorization.validate()
        if authorization.monetary_spend_authorized:
            raise QualificationContractError("paid-spend authorization is forbidden")
    parsed: list[RealProviderObservation] = []
    invalid_results: list[ObservationValidationResult] = []
    for index, raw in enumerate(observations):
        try:
            observation = (
                raw
                if isinstance(raw, RealProviderObservation)
                else RealProviderObservation.from_payload(raw)
            )
            parsed.append(observation)
        except (
            QualificationContractError,
            TypeError,
            ValueError,
            AttributeError,
        ) as exc:
            observation_id = (
                raw.get("observation_id", f"invalid-{index}")
                if isinstance(raw, Mapping)
                else f"invalid-{index}"
            )
            invalid_results.append(
                ObservationValidationResult(
                    str(observation_id),
                    "",
                    "",
                    ProviderQualificationStatus.OBSERVED_REJECTED,
                    False,
                    False,
                    tuple(item.value for item in _exception_codes(exc)),
                )
            )
    unique: list[RealProviderObservation] = []
    duplicate_results: list[ObservationValidationResult] = []
    conflicting_duplicate_ids: set[str] = set()
    by_id: dict[str, RealProviderObservation] = {}
    for observation in parsed:
        previous = by_id.get(observation.observation_id)
        if previous is None:
            by_id[observation.observation_id] = observation
            unique.append(observation)
        elif previous.as_payload() == observation.as_payload():
            result = _validate_one(
                previous,
                session,
                expected_fixture,
                timing_policy,
                provider_readiness,
                authorization,
            )
            duplicate_results.append(
                replace(
                    result,
                    duplicate_suppressed=True,
                    failure_codes=tuple(
                        dict.fromkeys(
                            (
                                *result.failure_codes,
                                QualificationCode.DUPLICATE_OBSERVATION.value,
                            )
                        )
                    ),
                )
            )
        else:
            conflicting_duplicate_ids.add(observation.observation_id)
            duplicate_results.append(
                ObservationValidationResult(
                    observation.observation_id,
                    observation.provider_identity,
                    observation.league,
                    ProviderQualificationStatus.OBSERVED_REJECTED,
                    False,
                    ObservationEvidenceKind(observation.evidence_kind)
                    is ObservationEvidenceKind.REAL_OBSERVED,
                    (QualificationCode.CONFLICTING_DUPLICATE.value,),
                )
            )
            by_id[observation.observation_id] = observation
    event_fixtures: dict[tuple[str, str], tuple[str, str, str]] = {}
    collision_ids: set[str] = set()
    for observation in unique:
        key = (observation.provider_identity, observation.provider_event_id)
        fixture = (
            observation.league,
            observation.fixture_key,
            observation.observation_id,
        )
        previous = event_fixtures.get(key)
        if previous is not None and previous[:2] != fixture[:2]:
            collision_ids.update((previous[2], observation.observation_id))
        event_fixtures[key] = fixture
    results: list[ObservationValidationResult] = []
    for observation in unique:
        result = _validate_one(
            observation,
            session,
            expected_fixture,
            timing_policy,
            provider_readiness,
            authorization,
        )
        if observation.observation_id in conflicting_duplicate_ids:
            result = replace(
                result,
                accepted=False,
                status=ProviderQualificationStatus.OBSERVED_REJECTED,
                failure_codes=tuple(
                    dict.fromkeys(
                        (
                            *result.failure_codes,
                            QualificationCode.CONFLICTING_DUPLICATE.value,
                        )
                    )
                ),
            )
        if observation.observation_id in collision_ids:
            result = replace(
                result,
                accepted=False,
                status=ProviderQualificationStatus.OBSERVED_REJECTED,
                failure_codes=tuple(
                    dict.fromkeys(
                        (
                            *result.failure_codes,
                            QualificationCode.PROVIDER_FIXTURE_COLLISION.value,
                        )
                    )
                ),
            )
        results.append(result)
    all_results = tuple(results + duplicate_results + invalid_results)
    all_observations = tuple(unique)
    status_by_provider: dict[str, ProviderQualificationStatus] = {}
    for provider in CASCADE_PROVIDER_ORDER:
        provider_results = [
            item for item in all_results if item.provider_identity == provider
        ]
        if not provider_results:
            status_by_provider[provider] = ProviderQualificationStatus.NOT_OBSERVED
        elif any(item.accepted and item.real_observed for item in provider_results):
            status_by_provider[provider] = (
                ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
            )
        elif any(item.accepted for item in provider_results):
            status_by_provider[provider] = (
                ProviderQualificationStatus.OBSERVED_VALID_CONTRACT
            )
        else:
            status_by_provider[provider] = ProviderQualificationStatus.OBSERVED_REJECTED
    real_count = sum(
        ObservationEvidenceKind(item.evidence_kind)
        is ObservationEvidenceKind.REAL_OBSERVED
        for item in all_observations
    )
    accepted_real_count = sum(item.accepted and item.real_observed for item in results)
    rejected_count = sum(not item.accepted for item in all_results)
    network_count = sum(item.network_request_count for item in all_observations)
    quota_units = sum(item.quota_cost_units for item in all_observations)
    state = ProviderReadinessState(session.qualification_state)
    if (
        accepted_real_count
        and state is ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION
    ):
        state = ProviderReadinessState.REAL_OBSERVATION_VALIDATED
    updated_session = replace(
        session,
        qualification_state=state,
        real_observation_count=real_count,
        accepted_observation_count=accepted_real_count,
        rejected_observation_count=rejected_count,
        network_request_count=network_count,
        quota_units_observed=quota_units,
    )
    updated_session.validate()
    if not all_results:
        overall = ProviderQualificationStatus.NOT_OBSERVED
    elif accepted_real_count:
        overall = ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
    elif any(item.accepted for item in all_results):
        overall = ProviderQualificationStatus.OBSERVED_VALID_CONTRACT
    else:
        overall = ProviderQualificationStatus.OBSERVED_REJECTED
    failures = Counter(code for item in all_results for code in item.failure_codes)
    coverage = _metrics(all_observations, results)
    unresolved = [NO_PRODUCTION_SIGNAL_TIME_VALUES]
    if minimum_sample_policy is None:
        unresolved.append("MINIMUM_SAMPLE_POLICY_UNRESOLVED")
        sample_sufficient: bool | None = None
    else:
        sample_sufficient = (
            accepted_real_count >= minimum_sample_policy.minimum_real_observations
        )
        if not sample_sufficient:
            unresolved.append("INSUFFICIENT_PRODUCTION_SAMPLE")
    if not authorization and real_count:
        unresolved.append(QualificationCode.AUTHORIZATION_MISSING.value)
    report = ProviderQualificationReport(
        overall,
        updated_session,
        all_results,
        status_by_provider,
        coverage,
        coverage,
        dict(sorted(failures.items())),
        tuple(dict.fromkeys(unresolved)),
        minimum_sample_policy,
        sample_sufficient,
    )
    report.validate()
    return report


def validate_provider_qualification(
    *args: object, **kwargs: object
) -> ProviderQualificationReport:
    """Named validation alias for callers integrating the qualification gate."""

    return qualify_provider_observations(*args, **kwargs)  # type: ignore[arg-type]


def run_offline_qualification(
    *args: object, **kwargs: object
) -> ProviderQualificationReport:
    """Run only over serialized evidence; no network-capable runner exists."""

    return qualify_provider_observations(*args, **kwargs)  # type: ignore[arg-type]


def bridge_real_observation_to_builder1_shadow_evidence(
    observation: RealProviderObservation,
    session: ProviderQualificationSession,
    expected_fixture: ExpectedCascadeFixture,
    timing_policy: QualificationTimingPolicy,
    provider_readiness: Mapping[str, ProviderReadinessState],
    authorization: CEOAuthorization | None = None,
    *,
    minimum_sample_policy: MinimumSamplePolicy | None = None,
):
    """Bridge only a validated real observation to v1 NO-BET shadow evidence."""

    report = qualify_provider_observations(
        (observation,),
        session,
        expected_fixture,
        timing_policy,
        provider_readiness,
        authorization,
        minimum_sample_policy=minimum_sample_policy,
    )
    if (
        report.qualification_status
        is not ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED
        or not report.accepted_observation_ids
    ):
        raise QualificationContractError(
            "only validated REAL_OBSERVED evidence may cross the Builder 1 bridge"
        )
    return cascade_to_shadow_observation_evidence(
        observation.cascade_evidence,
        CascadeValidationPolicy(
            maximum_odds_age_seconds=timing_policy.maximum_odds_age_seconds,
            kickoff_tolerance_seconds=timing_policy.kickoff_tolerance_seconds,
            configured_provider_order=tuple(
                observation.cascade_evidence.configured_provider_order
            )
            if isinstance(observation.cascade_evidence, CascadeEvidence)
            else tuple(
                CascadeEvidence.from_payload(
                    observation.cascade_evidence
                ).configured_provider_order
            ),
            provider_readiness=provider_readiness,
            expected_fixture=expected_fixture,
        ),
    )


def offline_fixture_catalog() -> tuple[dict[str, object], ...]:
    """Declare deterministic non-real fixtures without manufacturing observations."""

    return tuple(
        {
            "provider_identity": provider,
            "evidence_kind": ObservationEvidenceKind.TEST_FIXTURE.value,
            "counts_as_real": False,
            "network_calls": 0,
        }
        for provider in CASCADE_PROVIDER_ORDER
    )


__all__ = [
    "CASCADE_PROVIDER_ORDER",
    "NO_PRODUCTION_SIGNAL_TIME_VALUES",
    "QUALIFICATION_ARCHIVE_ROOT",
    "QUALIFICATION_CONTRACT_VERSION",
    "TOP5_LEAGUES",
    "CEOAuthorization",
    "FreshnessSummary",
    "MinimumSamplePolicy",
    "ObservationEvidenceKind",
    "ObservationValidationResult",
    "ProviderLeagueMetrics",
    "ProviderQualificationReport",
    "ProviderQualificationSession",
    "ProviderQualificationStatus",
    "ProviderReadinessState",
    "ProviderTimestampProvenance",
    "QualificationArchive",
    "QualificationCode",
    "QualificationContractError",
    "QualificationTimingPolicy",
    "RealProviderObservation",
    "bridge_real_observation_to_builder1_shadow_evidence",
    "offline_fixture_catalog",
    "qualify_provider_observations",
    "run_offline_qualification",
    "validate_provider_qualification",
]
