"""Provider-neutral, non-authorizing Top-5 B4 evidence contracts.

The legacy TheRundown B4 bridge remains available in
``top5_provider_native_evidence_bridge``.  This module defines the common
evidence vocabulary for provider operations, fixtures, markets, readiness,
controlled-shadow captures, reconciliation, qualification, and the B1 logical
handoff.  It performs no provider I/O and grants no authority.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite

from src.football.provider_cascade.contracts import TOP5_LEAGUE_CODES
from src.football.top5_shadow_provider_redundancy import make_fixture_key

PROVIDER_NEUTRAL_B4_SCHEMA_VERSION = "top5-b4-provider-neutral-evidence-v1"
PROVIDER_OPERATION_SCHEMA_VERSION = "top5-b4-provider-operation-evidence-v1"
FIXTURE_DISCOVERY_SCHEMA_VERSION = "top5-b4-fixture-discovery-evidence-v1"
MARKET_EVIDENCE_SCHEMA_VERSION = "top5-b4-market-evidence-v1"
READINESS_SCHEMA_VERSION = "top5-b4-provider-readiness-v1"
CONTROLLED_SHADOW_SCHEMA_VERSION = "top5-b4-provider-neutral-shadow-v1"
RECONCILIATION_SCHEMA_VERSION = "top5-b4-provider-neutral-reconciliation-v1"
QUALIFICATION_SCHEMA_VERSION = "top5-b4-provider-neutral-qualification-v1"
DOSSIER_SCHEMA_VERSION = "top5-b4-provider-neutral-dossier-v1"
AUTHORIZATION_PROVENANCE_SCHEMA_VERSION = "top5-b4-authorization-provenance-v1"
OPERATION_PROVENANCE_SCHEMA_VERSION = "top5-b4-provider-operation-provenance-v1"

# This is an evidence-contract allowlist, not a routing or active-provider list.
SUPPORTED_EVIDENCE_PROVIDERS = frozenset({"isports_api", "the_odds_api"})
TOP5_LEAGUE_ORDER = ("BL1", "EPL", "LL", "SA", "L1")
MAX_DISCOVERY_AGE_SECONDS = 900
MAX_ODDS_AGE_SECONDS = 300
ZERO_RETRIES = 0
_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SOURCE_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_OPERATION_KINDS = frozenset(
    {
        "competition_catalog",
        "competition_schedule",
        "fixture_discovery",
        "bulk_odds",
    }
)
_USAGE_STATES = frozenset({"available", "not_exposed", "unavailable"})
_B1_LOGICAL_INPUT_KEYS = (
    "source_main_sha",
    "b4_quota_proof_package",
    "b4_quota_headroom",
    "discovery_evidence",
    "provider_native_discovery_provenance",
    "controlled_shadow",
    "b4_reconciliation",
    "b4_qualification",
    "b4_native_authorization",
    "b4_dossier_digest",
)


class ProviderNeutralB4EvidenceError(ValueError):
    """Malformed, stale, mismatched, or unsafe provider-neutral B4 evidence."""


def _canonical(value: object) -> object:
    if isinstance(value, datetime):
        return _timestamp(value, "timestamp").isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ProviderNeutralB4EvidenceError(
                "canonical object keys must be strings"
            )
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and not isfinite(value):
        raise ProviderNeutralB4EvidenceError("non-finite values are not canonical")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ProviderNeutralB4EvidenceError("value is not canonical JSON")


def canonical_evidence_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            _canonical(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProviderNeutralB4EvidenceError("evidence is not canonical JSON") from exc
    return sha256(encoded).hexdigest()


def _timestamp(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProviderNeutralB4EvidenceError(f"{name} is not ISO-8601") from exc
    else:
        raise ProviderNeutralB4EvidenceError(f"{name} must be a timestamp")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ProviderNeutralB4EvidenceError(f"{name} must be timezone-aware")
    return result.astimezone(timezone.utc)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderNeutralB4EvidenceError(f"{name} must be non-empty text")
    return value.strip()


def _digest(value: object, name: str) -> str:
    result = _text(value, name).lower()
    if _SHA_RE.fullmatch(result) is None:
        raise ProviderNeutralB4EvidenceError(f"{name} must be a SHA-256 digest")
    return result


def _source_sha(value: object, name: str) -> str:
    result = _text(value, name).lower()
    if _SOURCE_SHA_RE.fullmatch(result) is None:
        raise ProviderNeutralB4EvidenceError(f"{name} must be a Git SHA")
    return result


def _provider(value: object) -> str:
    result = _text(value, "provider_identity")
    if result not in SUPPORTED_EVIDENCE_PROVIDERS:
        raise ProviderNeutralB4EvidenceError("provider identity is not supported")
    return result


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ProviderNeutralB4EvidenceError(f"{name} must be an integer >= {minimum}")
    return value


def _exact_mapping(
    value: object, expected: set[str], name: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ProviderNeutralB4EvidenceError(f"{name} payload shape is invalid")
    return value


def _fresh(now: datetime, observed: datetime, limit: int, name: str) -> None:
    now_utc = _timestamp(now, "now")
    observed_utc = _timestamp(observed, name)
    if observed_utc > now_utc:
        raise ProviderNeutralB4EvidenceError(f"{name} is in the future")
    if (now_utc - observed_utc).total_seconds() > limit:
        raise ProviderNeutralB4EvidenceError(f"{name} is stale")


@dataclass(frozen=True)
class B4ProviderUsageEvidenceV1:
    """Safe, provider-neutral quota and rate-limit facts; never datapoints."""

    quota_status: str = "not_exposed"
    quota_remaining_requests: int | None = None
    quota_limit_requests: int | None = None
    quota_reset_at: datetime | None = None
    rate_status: str = "not_exposed"
    rate_remaining_requests: int | None = None
    rate_limit_requests: int | None = None
    rate_reset_at: datetime | None = None

    def validate(self) -> None:
        for prefix in ("quota", "rate"):
            status = getattr(self, f"{prefix}_status")
            remaining = getattr(self, f"{prefix}_remaining_requests")
            limit = getattr(self, f"{prefix}_limit_requests")
            reset = getattr(self, f"{prefix}_reset_at")
            if status not in _USAGE_STATES:
                raise ProviderNeutralB4EvidenceError(
                    f"{prefix} metadata status is invalid"
                )
            if remaining is not None:
                _integer(remaining, f"{prefix}_remaining_requests")
            if limit is not None:
                _integer(limit, f"{prefix}_limit_requests", minimum=1)
            if reset is not None:
                _timestamp(reset, f"{prefix}_reset_at")
            if status == "available":
                if remaining is None and limit is None and reset is None:
                    raise ProviderNeutralB4EvidenceError(
                        f"available {prefix} metadata has no observed values"
                    )
                if remaining is not None and limit is not None and remaining > limit:
                    raise ProviderNeutralB4EvidenceError(
                        f"{prefix} remaining count exceeds its limit"
                    )
            elif remaining is not None or limit is not None or reset is not None:
                raise ProviderNeutralB4EvidenceError(
                    f"{prefix} values contradict {status} status"
                )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": "top5-b4-provider-usage-evidence-v1",
            "quota_status": self.quota_status,
            "quota_remaining_requests": self.quota_remaining_requests,
            "quota_limit_requests": self.quota_limit_requests,
            "quota_reset_at": (
                _timestamp(self.quota_reset_at, "quota_reset_at").isoformat()
                if self.quota_reset_at is not None
                else None
            ),
            "rate_status": self.rate_status,
            "rate_remaining_requests": self.rate_remaining_requests,
            "rate_limit_requests": self.rate_limit_requests,
            "rate_reset_at": (
                _timestamp(self.rate_reset_at, "rate_reset_at").isoformat()
                if self.rate_reset_at is not None
                else None
            ),
        }

    @classmethod
    def from_payload(cls, payload: object) -> B4ProviderUsageEvidenceV1:
        expected = {
            "schema_version",
            "quota_status",
            "quota_remaining_requests",
            "quota_limit_requests",
            "quota_reset_at",
            "rate_status",
            "rate_remaining_requests",
            "rate_limit_requests",
            "rate_reset_at",
        }
        raw = _exact_mapping(payload, expected, "provider usage")
        if raw["schema_version"] != "top5-b4-provider-usage-evidence-v1":
            raise ProviderNeutralB4EvidenceError("provider usage schema is unsupported")

        def parse_time(name: str) -> datetime | None:
            return _timestamp(raw[name], name) if raw[name] is not None else None

        result = cls(
            quota_status=_text(raw["quota_status"], "quota_status"),
            quota_remaining_requests=raw["quota_remaining_requests"],
            quota_limit_requests=raw["quota_limit_requests"],
            quota_reset_at=parse_time("quota_reset_at"),
            rate_status=_text(raw["rate_status"], "rate_status"),
            rate_remaining_requests=raw["rate_remaining_requests"],
            rate_limit_requests=raw["rate_limit_requests"],
            rate_reset_at=parse_time("rate_reset_at"),
        )
        result.validate()
        return result


@dataclass(frozen=True)
class B4ProviderOperationEvidenceV1:
    """One bounded HTTP operation response, without secrets or provider billing fiction."""

    provider_identity: str
    operation_id: str
    request_identity: str
    operation_kind: str
    endpoint_path: str
    request_ordinal: int
    request_count: int
    retry_count: int
    http_status: int
    requested_at: datetime
    completed_at: datetime
    response_digest: str
    usage: B4ProviderUsageEvidenceV1
    schema_version: str = PROVIDER_OPERATION_SCHEMA_VERSION

    def validate(self) -> None:
        _provider(self.provider_identity)
        _text(self.operation_id, "operation_id")
        _text(self.request_identity, "request_identity")
        if self.operation_kind not in _OPERATION_KINDS:
            raise ProviderNeutralB4EvidenceError(
                "provider operation kind is unsupported"
            )
        endpoint = _text(self.endpoint_path, "endpoint_path")
        if (
            not endpoint.startswith("/")
            or endpoint.startswith("//")
            or any(char in endpoint for char in ("?", "#", "\\"))
            or "://" in endpoint
            or ".." in endpoint.split("/")
            or any(
                secret in endpoint.casefold()
                for secret in ("token", "api_key", "apikey")
            )
        ):
            raise ProviderNeutralB4EvidenceError(
                "endpoint_path must be a secret-free path without query data"
            )
        _integer(self.request_ordinal, "request_ordinal", minimum=1)
        if self.request_count != 1:
            raise ProviderNeutralB4EvidenceError(
                "each operation record must represent exactly one HTTP request"
            )
        if self.retry_count != ZERO_RETRIES:
            raise ProviderNeutralB4EvidenceError(
                "provider operation retries are forbidden"
            )
        if (
            not isinstance(self.http_status, int)
            or isinstance(self.http_status, bool)
            or not 100 <= self.http_status <= 599
        ):
            raise ProviderNeutralB4EvidenceError("HTTP status is invalid")
        requested = _timestamp(self.requested_at, "requested_at")
        completed = _timestamp(self.completed_at, "completed_at")
        if completed < requested:
            raise ProviderNeutralB4EvidenceError("operation timestamps are reversed")
        _digest(self.response_digest, "response_digest")
        if self.schema_version != PROVIDER_OPERATION_SCHEMA_VERSION:
            raise ProviderNeutralB4EvidenceError(
                "provider operation schema is unsupported"
            )
        self.usage.validate()

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_identity": self.provider_identity,
            "operation_id": self.operation_id,
            "request_identity": self.request_identity,
            "operation_kind": self.operation_kind,
            "endpoint_path": self.endpoint_path,
            "request_ordinal": self.request_ordinal,
            "request_count": self.request_count,
            "retry_count": self.retry_count,
            "http_status": self.http_status,
            "requested_at": _timestamp(self.requested_at, "requested_at").isoformat(),
            "completed_at": _timestamp(self.completed_at, "completed_at").isoformat(),
            "response_digest": self.response_digest.lower(),
            "usage": self.usage.as_payload(),
        }

    @property
    def evidence_digest(self) -> str:
        return canonical_evidence_digest(self._payload())

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload(), "evidence_digest": self.evidence_digest}

    @classmethod
    def from_payload(cls, payload: object) -> B4ProviderOperationEvidenceV1:
        expected = {
            "schema_version",
            "provider_identity",
            "operation_id",
            "request_identity",
            "operation_kind",
            "endpoint_path",
            "request_ordinal",
            "request_count",
            "retry_count",
            "http_status",
            "requested_at",
            "completed_at",
            "response_digest",
            "usage",
            "evidence_digest",
        }
        raw = _exact_mapping(payload, expected, "provider operation")
        result = cls(
            provider_identity=raw["provider_identity"],
            operation_id=raw["operation_id"],
            request_identity=raw["request_identity"],
            operation_kind=raw["operation_kind"],
            endpoint_path=raw["endpoint_path"],
            request_ordinal=raw["request_ordinal"],
            request_count=raw["request_count"],
            retry_count=raw["retry_count"],
            http_status=raw["http_status"],
            requested_at=_timestamp(raw["requested_at"], "requested_at"),
            completed_at=_timestamp(raw["completed_at"], "completed_at"),
            response_digest=raw["response_digest"],
            usage=B4ProviderUsageEvidenceV1.from_payload(raw["usage"]),
            schema_version=raw["schema_version"],
        )
        result.validate()
        if (
            _digest(raw["evidence_digest"], "operation evidence digest")
            != result.evidence_digest
        ):
            raise ProviderNeutralB4EvidenceError("provider operation digest mismatch")
        return result


@dataclass(frozen=True)
class B4FixtureDiscoveryEvidenceV1:
    provider_identity: str
    league: str
    provider_competition_id: str
    provider_fixture_id: str
    fixture_key: str
    home_team: str
    away_team: str
    kickoff: datetime
    discovered_at: datetime
    operation_id: str
    complete: bool
    schema_version: str = FIXTURE_DISCOVERY_SCHEMA_VERSION

    def validate(self, *, now: datetime) -> None:
        _provider(self.provider_identity)
        if self.league not in TOP5_LEAGUE_CODES:
            raise ProviderNeutralB4EvidenceError("fixture league is outside Top-5")
        for name, value in (
            ("provider_competition_id", self.provider_competition_id),
            ("provider_fixture_id", self.provider_fixture_id),
            ("fixture_key", self.fixture_key),
            ("home_team", self.home_team),
            ("away_team", self.away_team),
            ("operation_id", self.operation_id),
        ):
            _text(value, name)
        kickoff = _timestamp(self.kickoff, "kickoff")
        discovered = _timestamp(self.discovered_at, "discovered_at")
        if self.complete is not True:
            raise ProviderNeutralB4EvidenceError("fixture discovery is incomplete")
        if self.schema_version != FIXTURE_DISCOVERY_SCHEMA_VERSION:
            raise ProviderNeutralB4EvidenceError(
                "fixture discovery schema is unsupported"
            )
        if self.fixture_key != make_fixture_key(
            self.league, self.home_team, self.away_team, kickoff
        ):
            raise ProviderNeutralB4EvidenceError("canonical fixture identity mismatch")
        _fresh(now, discovered, MAX_DISCOVERY_AGE_SECONDS, "fixture discovery")

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_identity": self.provider_identity,
            "league": self.league,
            "provider_competition_id": self.provider_competition_id,
            "provider_fixture_id": self.provider_fixture_id,
            "fixture_key": self.fixture_key,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": _timestamp(self.kickoff, "kickoff").isoformat(),
            "discovered_at": _timestamp(
                self.discovered_at, "discovered_at"
            ).isoformat(),
            "operation_id": self.operation_id,
            "complete": self.complete,
        }

    @property
    def evidence_digest(self) -> str:
        return canonical_evidence_digest(self._payload())

    def as_payload(self, *, now: datetime) -> dict[str, object]:
        self.validate(now=now)
        return {**self._payload(), "evidence_digest": self.evidence_digest}

    @classmethod
    def from_payload(
        cls, payload: object, *, now: datetime
    ) -> B4FixtureDiscoveryEvidenceV1:
        expected = {
            "schema_version",
            "provider_identity",
            "league",
            "provider_competition_id",
            "provider_fixture_id",
            "fixture_key",
            "home_team",
            "away_team",
            "kickoff",
            "discovered_at",
            "operation_id",
            "complete",
            "evidence_digest",
        }
        raw = _exact_mapping(payload, expected, "fixture discovery")
        result = cls(
            provider_identity=raw["provider_identity"],
            league=raw["league"],
            provider_competition_id=raw["provider_competition_id"],
            provider_fixture_id=raw["provider_fixture_id"],
            fixture_key=raw["fixture_key"],
            home_team=raw["home_team"],
            away_team=raw["away_team"],
            kickoff=_timestamp(raw["kickoff"], "kickoff"),
            discovered_at=_timestamp(raw["discovered_at"], "discovered_at"),
            operation_id=raw["operation_id"],
            complete=raw["complete"],
            schema_version=raw["schema_version"],
        )
        result.validate(now=now)
        if (
            _digest(raw["evidence_digest"], "fixture evidence digest")
            != result.evidence_digest
        ):
            raise ProviderNeutralB4EvidenceError("fixture discovery digest mismatch")
        return result


@dataclass(frozen=True)
class B4MarketEvidenceV1:
    provider_identity: str
    league: str
    provider_fixture_id: str
    request_identity: str
    operation_id: str
    source_identity: str
    source_provenance: str
    market_type: str
    home_odds: float
    draw_odds: float
    away_odds: float
    odds_timestamp: datetime
    captured_at: datetime
    raw_response_digest: str
    provider_record_digest: str
    normalized_observation_digest: str
    schema_version: str = MARKET_EVIDENCE_SCHEMA_VERSION

    def validate(self, *, now: datetime) -> None:
        _provider(self.provider_identity)
        if self.league not in TOP5_LEAGUE_CODES:
            raise ProviderNeutralB4EvidenceError("market league is outside Top-5")
        for name, value in (
            ("provider_fixture_id", self.provider_fixture_id),
            ("request_identity", self.request_identity),
            ("operation_id", self.operation_id),
            ("source_identity", self.source_identity),
            ("source_provenance", self.source_provenance),
        ):
            _text(value, name)
        provenance = self.source_provenance.casefold()
        if any(
            marker in provenance
            for marker in (
                "?",
                "#",
                "api_key",
                "apikey",
                "token=",
                "bearer ",
                "authorization=",
            )
        ):
            raise ProviderNeutralB4EvidenceError(
                "source provenance must not contain credentials or raw query data"
            )
        if self.market_type != "PRE_MATCH_1X2_REGULATION":
            raise ProviderNeutralB4EvidenceError(
                "market type is not regulation pre-match 1X2"
            )
        for name, value in (
            ("home_odds", self.home_odds),
            ("draw_odds", self.draw_odds),
            ("away_odds", self.away_odds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or value <= 1.0
            ):
                raise ProviderNeutralB4EvidenceError(
                    f"{name} must be a valid decimal price"
                )
        odds_at = _timestamp(self.odds_timestamp, "odds_timestamp")
        captured = _timestamp(self.captured_at, "market captured_at")
        if odds_at > captured:
            raise ProviderNeutralB4EvidenceError("odds timestamp is after capture")
        if self.schema_version != MARKET_EVIDENCE_SCHEMA_VERSION:
            raise ProviderNeutralB4EvidenceError(
                "market evidence schema is unsupported"
            )
        _fresh(now, odds_at, MAX_ODDS_AGE_SECONDS, "odds timestamp")
        _fresh(now, captured, MAX_ODDS_AGE_SECONDS, "market capture")
        for name in (
            "raw_response_digest",
            "provider_record_digest",
            "normalized_observation_digest",
        ):
            _digest(getattr(self, name), name)

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_identity": self.provider_identity,
            "league": self.league,
            "provider_fixture_id": self.provider_fixture_id,
            "request_identity": self.request_identity,
            "operation_id": self.operation_id,
            "source_identity": self.source_identity,
            "source_provenance": self.source_provenance,
            "market_type": self.market_type,
            "home_odds": self.home_odds,
            "draw_odds": self.draw_odds,
            "away_odds": self.away_odds,
            "odds_timestamp": _timestamp(
                self.odds_timestamp, "odds_timestamp"
            ).isoformat(),
            "captured_at": _timestamp(self.captured_at, "captured_at").isoformat(),
            "raw_response_digest": self.raw_response_digest.lower(),
            "provider_record_digest": self.provider_record_digest.lower(),
            "normalized_observation_digest": self.normalized_observation_digest.lower(),
        }

    @property
    def evidence_digest(self) -> str:
        return canonical_evidence_digest(self._payload())

    def as_payload(self, *, now: datetime) -> dict[str, object]:
        self.validate(now=now)
        return {**self._payload(), "evidence_digest": self.evidence_digest}

    @classmethod
    def from_payload(cls, payload: object, *, now: datetime) -> B4MarketEvidenceV1:
        expected = {
            "schema_version",
            "provider_identity",
            "league",
            "provider_fixture_id",
            "request_identity",
            "operation_id",
            "source_identity",
            "source_provenance",
            "market_type",
            "home_odds",
            "draw_odds",
            "away_odds",
            "odds_timestamp",
            "captured_at",
            "raw_response_digest",
            "provider_record_digest",
            "normalized_observation_digest",
            "evidence_digest",
        }
        raw = _exact_mapping(payload, expected, "market evidence")
        result = cls(
            provider_identity=raw["provider_identity"],
            league=raw["league"],
            provider_fixture_id=raw["provider_fixture_id"],
            request_identity=raw["request_identity"],
            operation_id=raw["operation_id"],
            source_identity=raw["source_identity"],
            source_provenance=raw["source_provenance"],
            market_type=raw["market_type"],
            home_odds=raw["home_odds"],
            draw_odds=raw["draw_odds"],
            away_odds=raw["away_odds"],
            odds_timestamp=_timestamp(raw["odds_timestamp"], "odds_timestamp"),
            captured_at=_timestamp(raw["captured_at"], "captured_at"),
            raw_response_digest=raw["raw_response_digest"],
            provider_record_digest=raw["provider_record_digest"],
            normalized_observation_digest=raw["normalized_observation_digest"],
            schema_version=raw["schema_version"],
        )
        result.validate(now=now)
        if (
            _digest(raw["evidence_digest"], "market evidence digest")
            != result.evidence_digest
        ):
            raise ProviderNeutralB4EvidenceError("market evidence digest mismatch")
        return result


@dataclass(frozen=True)
class B4ProviderReadinessV1:
    """Local operation-budget headroom plus truthful optional provider limits."""

    provider_identity: str
    maximum_request_count: int
    requests_consumed_before_run: int
    authorized_run_request_count: int
    run_request_count: int
    retry_count: int
    quota_required_by_provider_policy: bool
    usage_observed_at: datetime
    usage: B4ProviderUsageEvidenceV1
    schema_version: str = READINESS_SCHEMA_VERSION

    def validate(self, *, now: datetime, operation_request_count: int) -> None:
        _provider(self.provider_identity)
        maximum = _integer(
            self.maximum_request_count, "maximum_request_count", minimum=1
        )
        before = _integer(
            self.requests_consumed_before_run, "requests_consumed_before_run"
        )
        authorized = _integer(
            self.authorized_run_request_count, "authorized_run_request_count", minimum=1
        )
        consumed = _integer(self.run_request_count, "run_request_count", minimum=1)
        retries = _integer(self.retry_count, "retry_count")
        if not isinstance(self.quota_required_by_provider_policy, bool):
            raise ProviderNeutralB4EvidenceError("quota policy must be explicit")
        if self.schema_version != READINESS_SCHEMA_VERSION:
            raise ProviderNeutralB4EvidenceError("readiness schema is unsupported")
        if retries != ZERO_RETRIES:
            raise ProviderNeutralB4EvidenceError("readiness retry count must be zero")
        if consumed != operation_request_count or consumed > authorized:
            raise ProviderNeutralB4EvidenceError(
                "observed requests contradict authorized run budget"
            )
        if before + authorized > maximum:
            raise ProviderNeutralB4EvidenceError(
                "authorized run exceeds local request headroom"
            )
        usage_at = _timestamp(self.usage_observed_at, "usage_observed_at")
        if usage_at > _timestamp(now, "now"):
            raise ProviderNeutralB4EvidenceError("usage snapshot is from the future")
        self.usage.validate()
        if self.quota_required_by_provider_policy:
            if (
                self.usage.quota_status != "available"
                or self.usage.quota_remaining_requests is None
            ):
                raise ProviderNeutralB4EvidenceError(
                    "provider quota required but unavailable"
                )
            if self.usage.quota_remaining_requests < authorized:
                raise ProviderNeutralB4EvidenceError(
                    "provider quota headroom is insufficient"
                )
        elif (
            self.usage.quota_status == "available"
            and self.usage.quota_remaining_requests is not None
            and self.usage.quota_remaining_requests < authorized
        ):
            raise ProviderNeutralB4EvidenceError(
                "observed provider quota headroom is insufficient"
            )
        if (
            self.usage.rate_status == "available"
            and self.usage.rate_remaining_requests is not None
            and self.usage.rate_remaining_requests < authorized
        ):
            raise ProviderNeutralB4EvidenceError(
                "observed provider rate headroom is insufficient"
            )

    @property
    def readiness_status(self) -> str:
        return "READY"

    @property
    def remaining_local_request_budget(self) -> int:
        return (
            self.maximum_request_count
            - self.requests_consumed_before_run
            - self.run_request_count
        )

    def as_payload(
        self, *, now: datetime, operation_request_count: int
    ) -> dict[str, object]:
        self.validate(now=now, operation_request_count=operation_request_count)
        return {
            "schema_version": self.schema_version,
            "provider_identity": self.provider_identity,
            "readiness_status": self.readiness_status,
            "maximum_request_count": self.maximum_request_count,
            "requests_consumed_before_run": self.requests_consumed_before_run,
            "authorized_run_request_count": self.authorized_run_request_count,
            "run_request_count": self.run_request_count,
            "retry_count": self.retry_count,
            "remaining_local_request_budget": self.remaining_local_request_budget,
            "quota_required_by_provider_policy": self.quota_required_by_provider_policy,
            "usage_observed_at": _timestamp(
                self.usage_observed_at, "usage_observed_at"
            ).isoformat(),
            "usage": self.usage.as_payload(),
        }

    def headroom_payload(
        self, *, now: datetime, operation_request_count: int
    ) -> dict[str, object]:
        payload = self.as_payload(
            now=now, operation_request_count=operation_request_count
        )
        return {
            "schema_version": "top5-b4-operation-headroom-v1",
            "provider_identity": self.provider_identity,
            "readiness_status": payload["readiness_status"],
            "maximum_request_count": self.maximum_request_count,
            "requests_consumed_before_run": self.requests_consumed_before_run,
            "authorized_run_request_count": self.authorized_run_request_count,
            "run_request_count": self.run_request_count,
            "retry_count": self.retry_count,
            "remaining_local_request_budget": self.remaining_local_request_budget,
            "quota_required_by_provider_policy": self.quota_required_by_provider_policy,
            "usage": self.usage.as_payload(),
        }

    def _payload_without_observation_clock(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_identity": self.provider_identity,
            "maximum_request_count": self.maximum_request_count,
            "requests_consumed_before_run": self.requests_consumed_before_run,
            "authorized_run_request_count": self.authorized_run_request_count,
            "run_request_count": self.run_request_count,
            "retry_count": self.retry_count,
            "quota_required_by_provider_policy": self.quota_required_by_provider_policy,
            "usage_observed_at": _timestamp(
                self.usage_observed_at, "usage_observed_at"
            ).isoformat(),
            "usage": self.usage.as_payload(),
        }


@dataclass(frozen=True)
class B4ControlledShadowCaptureV1:
    provider_identity: str
    league: str
    fixture_key: str
    provider_competition_id: str
    provider_fixture_id: str
    provider_request_id: str
    adapter_version: str
    adapter_source_sha: str
    discovery_evidence_digest: str
    market_evidence_digest: str
    operation_evidence_ids: tuple[str, ...]
    source_timestamp: datetime
    captured_at: datetime
    raw_response_digest: str
    provider_record_digest: str
    normalized_observation_digest: str
    cascade_evidence_digest: str
    capture_attestation_digest: str
    evidence_kind: str = "REAL_OBSERVED"
    network_execution: bool = True
    no_bet: bool = True
    publication: bool = False
    production_activation: bool = False
    betting: bool = False
    ledger_mutated: bool = False
    schema_version: str = "top5-b4-provider-neutral-capture-v1"

    def validate(self, *, now: datetime) -> None:
        _provider(self.provider_identity)
        if self.league not in TOP5_LEAGUE_CODES:
            raise ProviderNeutralB4EvidenceError("capture league is outside Top-5")
        for name, value in (
            ("fixture_key", self.fixture_key),
            ("provider_competition_id", self.provider_competition_id),
            ("provider_fixture_id", self.provider_fixture_id),
            ("provider_request_id", self.provider_request_id),
            ("adapter_version", self.adapter_version),
        ):
            _text(value, name)
        _source_sha(self.adapter_source_sha, "adapter_source_sha")
        if self.evidence_kind != "REAL_OBSERVED" or self.network_execution is not True:
            raise ProviderNeutralB4EvidenceError("capture is not network REAL_OBSERVED")
        if self.no_bet is not True or any(
            value is not False
            for value in (
                self.publication,
                self.production_activation,
                self.betting,
                self.ledger_mutated,
            )
        ):
            raise ProviderNeutralB4EvidenceError("capture safety flags are unsafe")
        if self.schema_version != "top5-b4-provider-neutral-capture-v1":
            raise ProviderNeutralB4EvidenceError("capture schema is unsupported")
        if not self.operation_evidence_ids or len(
            set(self.operation_evidence_ids)
        ) != len(self.operation_evidence_ids):
            raise ProviderNeutralB4EvidenceError(
                "capture operation references are invalid"
            )
        for operation_id in self.operation_evidence_ids:
            _text(operation_id, "operation evidence ID")
        for name in (
            "discovery_evidence_digest",
            "market_evidence_digest",
            "raw_response_digest",
            "provider_record_digest",
            "normalized_observation_digest",
            "cascade_evidence_digest",
            "capture_attestation_digest",
        ):
            _digest(getattr(self, name), name)
        source = _timestamp(self.source_timestamp, "source_timestamp")
        captured = _timestamp(self.captured_at, "capture captured_at")
        if source > captured:
            raise ProviderNeutralB4EvidenceError(
                "capture source timestamp is after capture"
            )
        _fresh(now, captured, MAX_ODDS_AGE_SECONDS, "controlled-shadow capture")

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_identity": self.provider_identity,
            "league": self.league,
            "fixture_key": self.fixture_key,
            "provider_competition_id": self.provider_competition_id,
            "provider_fixture_id": self.provider_fixture_id,
            "provider_request_id": self.provider_request_id,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha.lower(),
            "discovery_evidence_digest": self.discovery_evidence_digest,
            "market_evidence_digest": self.market_evidence_digest,
            "operation_evidence_ids": list(self.operation_evidence_ids),
            "source_timestamp": _timestamp(
                self.source_timestamp, "source_timestamp"
            ).isoformat(),
            "captured_at": _timestamp(self.captured_at, "captured_at").isoformat(),
            "raw_response_digest": self.raw_response_digest.lower(),
            "provider_record_digest": self.provider_record_digest.lower(),
            "normalized_observation_digest": self.normalized_observation_digest.lower(),
            "cascade_evidence_digest": self.cascade_evidence_digest.lower(),
            "capture_attestation_digest": self.capture_attestation_digest.lower(),
            "evidence_kind": self.evidence_kind,
            "network_execution": self.network_execution,
            "no_bet": self.no_bet,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "betting": self.betting,
            "ledger_mutated": self.ledger_mutated,
        }

    @property
    def evidence_digest(self) -> str:
        return canonical_evidence_digest(self._payload())

    def as_payload(self, *, now: datetime) -> dict[str, object]:
        self.validate(now=now)
        return {**self._payload(), "evidence_digest": self.evidence_digest}


@dataclass(frozen=True)
class B4ControlledShadowEvidenceV1:
    provider_identity: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    authorization_id: str
    authorization_digest: str
    source_main_sha: str
    configuration_digest: str
    adapter_version: str
    adapter_source_sha: str
    authorization_provenance: Mapping[str, object]
    operations: tuple[B4ProviderOperationEvidenceV1, ...]
    discovery_evidence: tuple[B4FixtureDiscoveryEvidenceV1, ...]
    market_evidence: tuple[B4MarketEvidenceV1, ...]
    readiness: B4ProviderReadinessV1
    captures: tuple[B4ControlledShadowCaptureV1, ...]
    no_bet: bool = True
    publication: bool = False
    production_activation: bool = False
    betting: bool = False
    ledger_mutated: bool = False
    schema_version: str = CONTROLLED_SHADOW_SCHEMA_VERSION

    def validate(self, *, now: datetime) -> None:
        provider = _provider(self.provider_identity)
        for name, value in (
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("qualification_session_id", self.qualification_session_id),
            ("authorization_id", self.authorization_id),
        ):
            _text(value, name)
        auth_digest = _digest(self.authorization_digest, "authorization_digest")
        source_sha = _source_sha(self.source_main_sha, "source_main_sha")
        config_digest = _digest(self.configuration_digest, "configuration_digest")
        _text(self.adapter_version, "adapter_version")
        adapter_sha = _source_sha(self.adapter_source_sha, "adapter_source_sha")
        if self.schema_version != CONTROLLED_SHADOW_SCHEMA_VERSION:
            raise ProviderNeutralB4EvidenceError(
                "controlled-shadow schema is unsupported"
            )
        if self.no_bet is not True or any(
            value is not False
            for value in (
                self.publication,
                self.production_activation,
                self.betting,
                self.ledger_mutated,
            )
        ):
            raise ProviderNeutralB4EvidenceError(
                "controlled-shadow safety flags are unsafe"
            )
        auth = self.authorization_provenance
        required_auth = {
            "schema_version",
            "provider_identity",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "authorization_id",
            "authorization_digest",
            "league_scope",
            "no_bet",
            "publication",
            "production_activation",
            "betting",
            "monetary_spend_authorized",
        }
        if not isinstance(auth, Mapping) or set(auth) != required_auth:
            raise ProviderNeutralB4EvidenceError(
                "authorization provenance shape is invalid"
            )
        if (
            auth.get("schema_version") != AUTHORIZATION_PROVENANCE_SCHEMA_VERSION
            or auth.get("provider_identity") != provider
            or auth.get("controlled_shadow_run_id") != self.controlled_shadow_run_id
            or auth.get("qualification_session_id") != self.qualification_session_id
            or auth.get("authorization_id") != self.authorization_id
            or auth.get("authorization_digest") != auth_digest
            or tuple(auth.get("league_scope", ())) != TOP5_LEAGUE_ORDER
            or auth.get("no_bet") is not True
            or any(
                auth.get(name) is not False
                for name in (
                    "publication",
                    "production_activation",
                    "betting",
                    "monetary_spend_authorized",
                )
            )
        ):
            raise ProviderNeutralB4EvidenceError(
                "authorization provenance binding is invalid"
            )
        authorization_body = {
            key: value for key, value in auth.items() if key != "authorization_digest"
        }
        if canonical_evidence_digest(authorization_body) != auth_digest:
            raise ProviderNeutralB4EvidenceError(
                "authorization provenance digest does not bind its payload"
            )
        if (
            len(self.operations) == 0
            or len(self.discovery_evidence) != 5
            or len(self.market_evidence) != 5
            or len(self.captures) != 5
        ):
            raise ProviderNeutralB4EvidenceError(
                "controlled-shadow evidence must cover five leagues"
            )
        operation_ids: dict[str, B4ProviderOperationEvidenceV1] = {}
        request_ordinals: list[int] = []
        for operation in self.operations:
            operation.validate()
            if operation.provider_identity != provider or operation.http_status != 200:
                raise ProviderNeutralB4EvidenceError(
                    "operation provider/status mismatch"
                )
            if operation.operation_id in operation_ids:
                raise ProviderNeutralB4EvidenceError("duplicate operation identity")
            operation_ids[operation.operation_id] = operation
            request_ordinals.append(operation.request_ordinal)
        if request_ordinals != list(range(1, len(self.operations) + 1)):
            raise ProviderNeutralB4EvidenceError(
                "operation request ordinals are not contiguous"
            )
        if len({item.request_identity for item in self.operations}) != len(
            self.operations
        ):
            raise ProviderNeutralB4EvidenceError("duplicate provider request identity")
        if any(
            _timestamp(left.completed_at, "operation completed_at")
            > _timestamp(right.requested_at, "operation requested_at")
            for left, right in zip(self.operations, self.operations[1:])
        ):
            raise ProviderNeutralB4EvidenceError("provider operation sequence overlaps")
        if not any(
            item.operation_kind in {"competition_schedule", "fixture_discovery"}
            for item in self.operations
        ):
            raise ProviderNeutralB4EvidenceError(
                "fixture discovery operation is missing"
            )
        if not any(item.operation_kind == "bulk_odds" for item in self.operations):
            raise ProviderNeutralB4EvidenceError("bulk odds operation is missing")
        if (
            tuple(item.league for item in self.discovery_evidence) != TOP5_LEAGUE_ORDER
            or tuple(item.league for item in self.market_evidence) != TOP5_LEAGUE_ORDER
            or tuple(item.league for item in self.captures) != TOP5_LEAGUE_ORDER
        ):
            raise ProviderNeutralB4EvidenceError(
                "evidence is not in canonical Top-5 league order"
            )
        discovery_by_league: dict[str, B4FixtureDiscoveryEvidenceV1] = {}
        market_by_league: dict[str, B4MarketEvidenceV1] = {}
        for discovery in self.discovery_evidence:
            discovery.validate(now=now)
            if (
                discovery.provider_identity != provider
                or discovery.operation_id not in operation_ids
            ):
                raise ProviderNeutralB4EvidenceError(
                    "discovery provider/operation mismatch"
                )
            op = operation_ids[discovery.operation_id]
            if (
                op.operation_kind not in {"competition_schedule", "fixture_discovery"}
                or op.completed_at > discovery.discovered_at
            ):
                raise ProviderNeutralB4EvidenceError(
                    "discovery is not bound to its operation"
                )
            discovery_by_league[discovery.league] = discovery
        for market in self.market_evidence:
            market.validate(now=now)
            if (
                market.provider_identity != provider
                or market.operation_id not in operation_ids
            ):
                raise ProviderNeutralB4EvidenceError(
                    "market provider/operation mismatch"
                )
            op = operation_ids[market.operation_id]
            if (
                op.operation_kind != "bulk_odds"
                or op.request_identity != market.request_identity
                or op.completed_at > market.captured_at
            ):
                raise ProviderNeutralB4EvidenceError(
                    "market is not bound to its operation"
                )
            if market.raw_response_digest != op.response_digest:
                raise ProviderNeutralB4EvidenceError(
                    "market raw response digest does not match operation"
                )
            discovery = next(
                item for item in self.discovery_evidence if item.league == market.league
            )
            if (
                market.odds_timestamp >= discovery.kickoff
                or market.captured_at >= discovery.kickoff
            ):
                raise ProviderNeutralB4EvidenceError(
                    "market evidence is not strictly pre-match"
                )
            market_by_league[market.league] = market
        total_requests = sum(item.request_count for item in self.operations)
        self.readiness.validate(now=now, operation_request_count=total_requests)
        if (
            self.readiness.provider_identity != provider
            or self.readiness.retry_count
            != sum(item.retry_count for item in self.operations)
        ):
            raise ProviderNeutralB4EvidenceError(
                "readiness/provider operation totals mismatch"
            )
        _fresh(
            now,
            self.readiness.usage_observed_at,
            MAX_DISCOVERY_AGE_SECONDS,
            "provider readiness snapshot",
        )
        if self.readiness.usage_observed_at > min(
            _timestamp(item.requested_at, "operation requested_at")
            for item in self.operations
        ):
            raise ProviderNeutralB4EvidenceError(
                "provider readiness snapshot was not captured before operations"
            )
        capture_fixtures: set[str] = set()
        for capture in self.captures:
            capture.validate(now=now)
            discovery = discovery_by_league[capture.league]
            market = market_by_league[capture.league]
            expected = (
                (capture.provider_identity, provider),
                (capture.adapter_version, self.adapter_version),
                (capture.adapter_source_sha, adapter_sha),
                (capture.fixture_key, discovery.fixture_key),
                (capture.provider_competition_id, discovery.provider_competition_id),
                (capture.provider_fixture_id, discovery.provider_fixture_id),
                (capture.provider_request_id, market.request_identity),
                (capture.discovery_evidence_digest, discovery.evidence_digest),
                (capture.market_evidence_digest, market.evidence_digest),
                (capture.source_timestamp, market.odds_timestamp),
                (capture.raw_response_digest, market.raw_response_digest),
                (capture.provider_record_digest, market.provider_record_digest),
                (
                    capture.normalized_observation_digest,
                    market.normalized_observation_digest,
                ),
            )
            if any(left != right for left, right in expected):
                raise ProviderNeutralB4EvidenceError(
                    "capture evidence binding mismatch"
                )
            if not set(capture.operation_evidence_ids) <= set(operation_ids):
                raise ProviderNeutralB4EvidenceError(
                    "capture references unknown operation evidence"
                )
            if (
                discovery.operation_id not in capture.operation_evidence_ids
                or market.operation_id not in capture.operation_evidence_ids
            ):
                raise ProviderNeutralB4EvidenceError(
                    "capture omits discovery or odds operation"
                )
            if capture.captured_at < market.captured_at:
                raise ProviderNeutralB4EvidenceError(
                    "capture predates market observation"
                )
            capture_fixtures.add(capture.fixture_key)
        if len(capture_fixtures) != 5:
            raise ProviderNeutralB4EvidenceError("fixture identities are duplicated")
        # A capture may also bind run-wide operations (for example the single
        # competition-catalog response used to resolve every schedule ID).
        # Keep the no-orphan invariant while allowing that operation to be
        # represented truthfully instead of disguising it as a schedule call.
        referenced_operations = (
            {item.operation_id for item in self.discovery_evidence}
            | {item.operation_id for item in self.market_evidence}
            | {
                operation_id
                for capture in self.captures
                for operation_id in capture.operation_evidence_ids
            }
        )
        if referenced_operations != set(operation_ids):
            raise ProviderNeutralB4EvidenceError("orphan provider operation evidence")
        if (
            source_sha != self.source_main_sha.lower()
            or config_digest != self.configuration_digest.lower()
        ):
            raise ProviderNeutralB4EvidenceError(
                "controlled-shadow source/config binding is invalid"
            )

    @property
    def request_count(self) -> int:
        return sum(operation.request_count for operation in self.operations)

    def operation_provenance_payload(self) -> dict[str, object]:
        operations = [item.as_payload() for item in self.operations]
        body = {
            "schema_version": OPERATION_PROVENANCE_SCHEMA_VERSION,
            "provider_identity": self.provider_identity,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "authorization_id": self.authorization_id,
            "authorization_digest": self.authorization_digest,
            "request_count": self.request_count,
            "retry_count": sum(item.retry_count for item in self.operations),
            "operations": operations,
        }
        return {**body, "provenance_digest": canonical_evidence_digest(body)}

    def as_payload(self, *, now: datetime) -> dict[str, object]:
        self.validate(now=now)
        return {
            "schema_version": self.schema_version,
            "provider_identity": self.provider_identity,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "authorization_id": self.authorization_id,
            "authorization_digest": self.authorization_digest,
            "source_main_sha": self.source_main_sha,
            "configuration_digest": self.configuration_digest,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "authorization_provenance": dict(self.authorization_provenance),
            "operations": [item.as_payload() for item in self.operations],
            "request_count": self.request_count,
            "retry_count": sum(item.retry_count for item in self.operations),
            "discovery_evidence": [
                item.as_payload(now=now) for item in self.discovery_evidence
            ],
            "market_evidence": [
                item.as_payload(now=now) for item in self.market_evidence
            ],
            "readiness": self.readiness.as_payload(
                now=now, operation_request_count=self.request_count
            ),
            "captures": [item.as_payload(now=now) for item in self.captures],
            "no_bet": self.no_bet,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "betting": self.betting,
            "ledger_mutated": self.ledger_mutated,
        }

    @classmethod
    def from_payload(
        cls, payload: object, *, now: datetime
    ) -> B4ControlledShadowEvidenceV1:
        expected = {
            "schema_version",
            "provider_identity",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "authorization_id",
            "authorization_digest",
            "source_main_sha",
            "configuration_digest",
            "adapter_version",
            "adapter_source_sha",
            "authorization_provenance",
            "operations",
            "request_count",
            "retry_count",
            "discovery_evidence",
            "market_evidence",
            "readiness",
            "captures",
            "no_bet",
            "publication",
            "production_activation",
            "betting",
            "ledger_mutated",
        }
        raw = _exact_mapping(payload, expected, "controlled-shadow evidence")

        def list_field(name: str) -> list[object]:
            value = raw[name]
            if not isinstance(value, list):
                raise ProviderNeutralB4EvidenceError(f"{name} must be a list")
            return value

        readiness_raw = _exact_mapping(
            raw["readiness"],
            {
                "schema_version",
                "provider_identity",
                "readiness_status",
                "maximum_request_count",
                "requests_consumed_before_run",
                "authorized_run_request_count",
                "run_request_count",
                "retry_count",
                "remaining_local_request_budget",
                "quota_required_by_provider_policy",
                "usage_observed_at",
                "usage",
            },
            "provider readiness",
        )
        readiness = B4ProviderReadinessV1(
            provider_identity=readiness_raw["provider_identity"],
            maximum_request_count=readiness_raw["maximum_request_count"],
            requests_consumed_before_run=readiness_raw["requests_consumed_before_run"],
            authorized_run_request_count=readiness_raw["authorized_run_request_count"],
            run_request_count=readiness_raw["run_request_count"],
            retry_count=readiness_raw["retry_count"],
            quota_required_by_provider_policy=readiness_raw[
                "quota_required_by_provider_policy"
            ],
            usage_observed_at=_timestamp(
                readiness_raw["usage_observed_at"], "usage_observed_at"
            ),
            usage=B4ProviderUsageEvidenceV1.from_payload(readiness_raw["usage"]),
            schema_version=readiness_raw["schema_version"],
        )
        expected_readiness_payload = readiness.as_payload(
            now=now, operation_request_count=readiness.run_request_count
        )
        if dict(readiness_raw) != expected_readiness_payload:
            raise ProviderNeutralB4EvidenceError(
                "provider readiness projection mismatch"
            )
        captures: list[B4ControlledShadowCaptureV1] = []
        capture_keys = {
            "schema_version",
            "provider_identity",
            "league",
            "fixture_key",
            "provider_competition_id",
            "provider_fixture_id",
            "provider_request_id",
            "adapter_version",
            "adapter_source_sha",
            "discovery_evidence_digest",
            "market_evidence_digest",
            "operation_evidence_ids",
            "source_timestamp",
            "captured_at",
            "raw_response_digest",
            "provider_record_digest",
            "normalized_observation_digest",
            "cascade_evidence_digest",
            "capture_attestation_digest",
            "evidence_kind",
            "network_execution",
            "no_bet",
            "publication",
            "production_activation",
            "betting",
            "ledger_mutated",
            "evidence_digest",
        }
        for item in list_field("captures"):
            capture_raw = _exact_mapping(
                item, capture_keys, "controlled-shadow capture"
            )
            capture = B4ControlledShadowCaptureV1(
                provider_identity=capture_raw["provider_identity"],
                league=capture_raw["league"],
                fixture_key=capture_raw["fixture_key"],
                provider_competition_id=capture_raw["provider_competition_id"],
                provider_fixture_id=capture_raw["provider_fixture_id"],
                provider_request_id=capture_raw["provider_request_id"],
                adapter_version=capture_raw["adapter_version"],
                adapter_source_sha=capture_raw["adapter_source_sha"],
                discovery_evidence_digest=capture_raw["discovery_evidence_digest"],
                market_evidence_digest=capture_raw["market_evidence_digest"],
                operation_evidence_ids=tuple(capture_raw["operation_evidence_ids"]),
                source_timestamp=_timestamp(
                    capture_raw["source_timestamp"], "source_timestamp"
                ),
                captured_at=_timestamp(capture_raw["captured_at"], "captured_at"),
                raw_response_digest=capture_raw["raw_response_digest"],
                provider_record_digest=capture_raw["provider_record_digest"],
                normalized_observation_digest=capture_raw[
                    "normalized_observation_digest"
                ],
                cascade_evidence_digest=capture_raw["cascade_evidence_digest"],
                capture_attestation_digest=capture_raw["capture_attestation_digest"],
                evidence_kind=capture_raw["evidence_kind"],
                network_execution=capture_raw["network_execution"],
                no_bet=capture_raw["no_bet"],
                publication=capture_raw["publication"],
                production_activation=capture_raw["production_activation"],
                betting=capture_raw["betting"],
                ledger_mutated=capture_raw["ledger_mutated"],
                schema_version=capture_raw["schema_version"],
            )
            capture.validate(now=now)
            if (
                _digest(capture_raw["evidence_digest"], "capture evidence digest")
                != capture.evidence_digest
            ):
                raise ProviderNeutralB4EvidenceError("capture evidence digest mismatch")
            captures.append(capture)
        result = cls(
            provider_identity=raw["provider_identity"],
            controlled_shadow_run_id=raw["controlled_shadow_run_id"],
            qualification_session_id=raw["qualification_session_id"],
            authorization_id=raw["authorization_id"],
            authorization_digest=raw["authorization_digest"],
            source_main_sha=raw["source_main_sha"],
            configuration_digest=raw["configuration_digest"],
            adapter_version=raw["adapter_version"],
            adapter_source_sha=raw["adapter_source_sha"],
            authorization_provenance=raw["authorization_provenance"],
            operations=tuple(
                B4ProviderOperationEvidenceV1.from_payload(item)
                for item in list_field("operations")
            ),
            discovery_evidence=tuple(
                B4FixtureDiscoveryEvidenceV1.from_payload(item, now=now)
                for item in list_field("discovery_evidence")
            ),
            market_evidence=tuple(
                B4MarketEvidenceV1.from_payload(item, now=now)
                for item in list_field("market_evidence")
            ),
            readiness=readiness,
            captures=tuple(captures),
            no_bet=raw["no_bet"],
            publication=raw["publication"],
            production_activation=raw["production_activation"],
            betting=raw["betting"],
            ledger_mutated=raw["ledger_mutated"],
            schema_version=raw["schema_version"],
        )
        result.validate(now=now)
        if raw["request_count"] != result.request_count or raw["retry_count"] != sum(
            item.retry_count for item in result.operations
        ):
            raise ProviderNeutralB4EvidenceError(
                "controlled-shadow request summary does not reconcile"
            )
        return result


@dataclass(frozen=True)
class B4ProviderNeutralReconciliationV1:
    provider_identity: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    authorization_id: str
    authorization_digest: str
    source_main_sha: str
    configuration_digest: str
    adapter_version: str
    adapter_source_sha: str
    league_scope: tuple[str, ...]
    fixture_keys: tuple[str, ...]
    provider_competition_ids: tuple[str, ...]
    provider_fixture_ids: tuple[str, ...]
    provider_request_ids: tuple[str, ...]
    operation_evidence_digests: tuple[str, ...]
    capture_evidence_digests: tuple[str, ...]
    request_count: int
    retry_count: int
    reconciliation_digest: str = ""
    receipt_eligible: bool = False
    authority_changed: bool = False
    publication: bool = False
    production_activation: bool = False
    monetary_spend_authorized: bool = False
    schema_version: str = RECONCILIATION_SCHEMA_VERSION

    def _body(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_identity": self.provider_identity,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "authorization_id": self.authorization_id,
            "authorization_digest": self.authorization_digest,
            "source_main_sha": self.source_main_sha,
            "configuration_digest": self.configuration_digest,
            "adapter_version": self.adapter_version,
            "adapter_source_sha": self.adapter_source_sha,
            "league_scope": list(self.league_scope),
            "fixture_keys": list(self.fixture_keys),
            "provider_competition_ids": list(self.provider_competition_ids),
            "provider_fixture_ids": list(self.provider_fixture_ids),
            "provider_request_ids": list(self.provider_request_ids),
            "operation_evidence_digests": list(self.operation_evidence_digests),
            "capture_evidence_digests": list(self.capture_evidence_digests),
            "request_count": self.request_count,
            "retry_count": self.retry_count,
            "receipt_eligible": False,
            "authority_changed": False,
            "publication": False,
            "production_activation": False,
            "monetary_spend_authorized": False,
        }

    def validate(self) -> None:
        _provider(self.provider_identity)
        if self.schema_version != RECONCILIATION_SCHEMA_VERSION:
            raise ProviderNeutralB4EvidenceError("reconciliation schema is unsupported")
        if self.league_scope != TOP5_LEAGUE_ORDER or any(
            len(values) != 5
            for values in (
                self.fixture_keys,
                self.provider_competition_ids,
                self.provider_fixture_ids,
                self.provider_request_ids,
                self.capture_evidence_digests,
            )
        ):
            raise ProviderNeutralB4EvidenceError(
                "reconciliation identity cardinality is invalid"
            )
        if len(self.operation_evidence_digests) == 0:
            raise ProviderNeutralB4EvidenceError(
                "reconciliation operation evidence is missing"
            )
        if any(
            len(set(values)) != len(values)
            for values in (
                self.fixture_keys,
                self.provider_competition_ids,
                self.provider_fixture_ids,
            )
        ):
            raise ProviderNeutralB4EvidenceError(
                "reconciliation fixture identities are duplicated"
            )
        for name in (
            "controlled_shadow_run_id",
            "qualification_session_id",
            "authorization_id",
        ):
            _text(getattr(self, name), name)
        for name in ("authorization_digest", "configuration_digest"):
            _digest(getattr(self, name), name)
        _text(self.adapter_version, "adapter_version")
        _source_sha(self.adapter_source_sha, "adapter_source_sha")
        _source_sha(self.source_main_sha, "source_main_sha")
        for values in (
            self.fixture_keys,
            self.provider_competition_ids,
            self.provider_fixture_ids,
            self.provider_request_ids,
        ):
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ProviderNeutralB4EvidenceError("reconciliation identity is empty")
        for value in (*self.operation_evidence_digests, *self.capture_evidence_digests):
            _digest(value, "reconciliation evidence digest")
        _integer(self.request_count, "request_count", minimum=1)
        if _integer(self.retry_count, "retry_count") != ZERO_RETRIES:
            raise ProviderNeutralB4EvidenceError("reconciliation retries are forbidden")
        if any(
            value is not False
            for value in (
                self.receipt_eligible,
                self.authority_changed,
                self.publication,
                self.production_activation,
                self.monetary_spend_authorized,
            )
        ):
            raise ProviderNeutralB4EvidenceError(
                "reconciliation safety flags are unsafe"
            )
        if self.reconciliation_digest != canonical_evidence_digest(self._body()):
            raise ProviderNeutralB4EvidenceError("reconciliation digest mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._body(), "reconciliation_digest": self.reconciliation_digest}


@dataclass(frozen=True)
class B4ProviderNeutralQualificationV1:
    provider_identity: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    authorization_id: str
    reconciliation_digest: str
    capture_evidence_digests: tuple[str, ...]
    structural_status: str = "REAL_OBSERVED_STRUCTURAL_VALIDATED"
    structural_provider_qualified: bool = True
    production_signal_time_approved: bool = False
    receipt_eligible: bool = False
    authority_changed: bool = False
    publication: bool = False
    production_activation: bool = False
    betting: bool = False
    schema_version: str = QUALIFICATION_SCHEMA_VERSION
    qualification_digest: str = ""

    def _body(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_identity": self.provider_identity,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "authorization_id": self.authorization_id,
            "reconciliation_digest": self.reconciliation_digest,
            "capture_evidence_digests": list(self.capture_evidence_digests),
            "structural_status": self.structural_status,
            "structural_provider_qualified": self.structural_provider_qualified,
            "production_signal_time_approved": self.production_signal_time_approved,
            "receipt_eligible": self.receipt_eligible,
            "authority_changed": self.authority_changed,
            "publication": self.publication,
            "production_activation": self.production_activation,
            "betting": self.betting,
        }

    def validate(self) -> None:
        _provider(self.provider_identity)
        if (
            self.schema_version != QUALIFICATION_SCHEMA_VERSION
            or self.structural_status != "REAL_OBSERVED_STRUCTURAL_VALIDATED"
        ):
            raise ProviderNeutralB4EvidenceError(
                "qualification schema/status is invalid"
            )
        for name in (
            "controlled_shadow_run_id",
            "qualification_session_id",
            "authorization_id",
        ):
            _text(getattr(self, name), name)
        _digest(self.reconciliation_digest, "reconciliation_digest")
        if len(self.capture_evidence_digests) != 5:
            raise ProviderNeutralB4EvidenceError("qualification requires five captures")
        for value in self.capture_evidence_digests:
            _digest(value, "capture evidence digest")
        if self.structural_provider_qualified is not True or any(
            value is not False
            for value in (
                self.production_signal_time_approved,
                self.receipt_eligible,
                self.authority_changed,
                self.publication,
                self.production_activation,
                self.betting,
            )
        ):
            raise ProviderNeutralB4EvidenceError(
                "qualification contains production authority"
            )
        if self.qualification_digest != canonical_evidence_digest(self._body()):
            raise ProviderNeutralB4EvidenceError("qualification digest mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._body(), "qualification_digest": self.qualification_digest}


@dataclass(frozen=True)
class Top5B4ProviderNeutralEvidenceDossierV1:
    """Deterministic provider-neutral B4 dossier; evidence only, never authority."""

    source_main_sha: str
    controlled_shadow: B4ControlledShadowEvidenceV1
    reconciliation: B4ProviderNeutralReconciliationV1
    qualification: B4ProviderNeutralQualificationV1
    dossier_digest: str
    schema_version: str = DOSSIER_SCHEMA_VERSION

    @classmethod
    def build(
        cls, controlled_shadow: B4ControlledShadowEvidenceV1, *, now: datetime
    ) -> Top5B4ProviderNeutralEvidenceDossierV1:
        controlled_shadow.validate(now=now)
        operations = controlled_shadow.operations
        discoveries = controlled_shadow.discovery_evidence
        captures = controlled_shadow.captures
        reconciliation = B4ProviderNeutralReconciliationV1(
            provider_identity=controlled_shadow.provider_identity,
            controlled_shadow_run_id=controlled_shadow.controlled_shadow_run_id,
            qualification_session_id=controlled_shadow.qualification_session_id,
            authorization_id=controlled_shadow.authorization_id,
            authorization_digest=controlled_shadow.authorization_digest,
            source_main_sha=controlled_shadow.source_main_sha,
            configuration_digest=controlled_shadow.configuration_digest,
            adapter_version=controlled_shadow.adapter_version,
            adapter_source_sha=controlled_shadow.adapter_source_sha,
            league_scope=TOP5_LEAGUE_ORDER,
            fixture_keys=tuple(item.fixture_key for item in discoveries),
            provider_competition_ids=tuple(
                item.provider_competition_id for item in discoveries
            ),
            provider_fixture_ids=tuple(
                item.provider_fixture_id for item in discoveries
            ),
            provider_request_ids=tuple(item.provider_request_id for item in captures),
            operation_evidence_digests=tuple(
                item.evidence_digest for item in operations
            ),
            capture_evidence_digests=tuple(item.evidence_digest for item in captures),
            request_count=controlled_shadow.request_count,
            retry_count=sum(item.retry_count for item in operations),
        )
        reconciliation = B4ProviderNeutralReconciliationV1(
            **{
                **reconciliation.__dict__,
                "reconciliation_digest": canonical_evidence_digest(
                    reconciliation._body()
                ),
            }
        )
        qualification = B4ProviderNeutralQualificationV1(
            provider_identity=controlled_shadow.provider_identity,
            controlled_shadow_run_id=controlled_shadow.controlled_shadow_run_id,
            qualification_session_id=controlled_shadow.qualification_session_id,
            authorization_id=controlled_shadow.authorization_id,
            reconciliation_digest=reconciliation.reconciliation_digest,
            capture_evidence_digests=reconciliation.capture_evidence_digests,
        )
        qualification = B4ProviderNeutralQualificationV1(
            **{
                **qualification.__dict__,
                "qualification_digest": canonical_evidence_digest(
                    qualification._body()
                ),
            }
        )
        dossier = cls(
            source_main_sha=controlled_shadow.source_main_sha,
            controlled_shadow=controlled_shadow,
            reconciliation=reconciliation,
            qualification=qualification,
            dossier_digest="",
        )
        dossier = cls(
            **{
                **dossier.__dict__,
                "dossier_digest": canonical_evidence_digest(dossier._body(now=now)),
            }
        )
        dossier.validate(now=now)
        return dossier

    def _body(self, *, now: datetime | None = None) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_main_sha": self.source_main_sha,
            "provider_identity": self.controlled_shadow.provider_identity,
            "controlled_shadow": self.controlled_shadow.as_payload(
                now=now or self.controlled_shadow.captures[-1].captured_at
            ),
            "reconciliation": self.reconciliation.as_payload(),
            "qualification": self.qualification.as_payload(),
        }

    def validate(self, *, now: datetime) -> None:
        _source_sha(self.source_main_sha, "source_main_sha")
        if self.schema_version != DOSSIER_SCHEMA_VERSION:
            raise ProviderNeutralB4EvidenceError("dossier schema is unsupported")
        self.controlled_shadow.validate(now=now)
        self.reconciliation.validate()
        self.qualification.validate()
        if self.source_main_sha != self.controlled_shadow.source_main_sha:
            raise ProviderNeutralB4EvidenceError("dossier source SHA binding mismatch")
        if (
            self.reconciliation.provider_identity
            != self.controlled_shadow.provider_identity
            or self.reconciliation.controlled_shadow_run_id
            != self.controlled_shadow.controlled_shadow_run_id
            or self.reconciliation.qualification_session_id
            != self.controlled_shadow.qualification_session_id
            or self.reconciliation.authorization_id
            != self.controlled_shadow.authorization_id
            or self.reconciliation.authorization_digest
            != self.controlled_shadow.authorization_digest
            or self.reconciliation.configuration_digest
            != self.controlled_shadow.configuration_digest
            or self.reconciliation.adapter_version
            != self.controlled_shadow.adapter_version
            or self.reconciliation.adapter_source_sha
            != self.controlled_shadow.adapter_source_sha
        ):
            raise ProviderNeutralB4EvidenceError(
                "dossier reconciliation binding mismatch"
            )
        if (
            self.qualification.provider_identity
            != self.controlled_shadow.provider_identity
            or self.qualification.controlled_shadow_run_id
            != self.controlled_shadow.controlled_shadow_run_id
            or self.qualification.qualification_session_id
            != self.controlled_shadow.qualification_session_id
            or self.qualification.authorization_id
            != self.controlled_shadow.authorization_id
            or self.qualification.reconciliation_digest
            != self.reconciliation.reconciliation_digest
            or self.qualification.capture_evidence_digests
            != self.reconciliation.capture_evidence_digests
        ):
            raise ProviderNeutralB4EvidenceError(
                "dossier qualification binding mismatch"
            )
        if self.dossier_digest != canonical_evidence_digest(self._body(now=now)):
            raise ProviderNeutralB4EvidenceError("dossier digest mismatch")

    def as_payload(self, *, now: datetime) -> dict[str, object]:
        self.validate(now=now)
        return {**self._body(now=now), "dossier_digest": self.dossier_digest}

    @classmethod
    def from_payload(
        cls, payload: object, *, now: datetime
    ) -> Top5B4ProviderNeutralEvidenceDossierV1:
        expected = {
            "schema_version",
            "source_main_sha",
            "provider_identity",
            "controlled_shadow",
            "reconciliation",
            "qualification",
            "dossier_digest",
        }
        raw = _exact_mapping(payload, expected, "provider-neutral B4 dossier")
        if raw["schema_version"] != DOSSIER_SCHEMA_VERSION:
            raise ProviderNeutralB4EvidenceError("dossier schema is unsupported")
        shadow = B4ControlledShadowEvidenceV1.from_payload(
            raw["controlled_shadow"], now=now
        )
        dossier = cls.build(shadow, now=now)
        if (
            raw["source_main_sha"] != dossier.source_main_sha
            or raw["provider_identity"] != shadow.provider_identity
            or raw["reconciliation"] != dossier.reconciliation.as_payload()
            or raw["qualification"] != dossier.qualification.as_payload()
            or raw["dossier_digest"] != dossier.dossier_digest
        ):
            raise ProviderNeutralB4EvidenceError(
                "serialized dossier projection mismatch"
            )
        return dossier

    def b1_evidence_inputs(self, *, now: datetime) -> dict[str, object]:
        """Return composer-compatible logical keys with neutral payload values."""
        self.validate(now=now)
        shadow_payload = self.controlled_shadow.as_payload(now=now)
        readiness_payload = self.controlled_shadow.readiness.as_payload(
            now=now, operation_request_count=self.controlled_shadow.request_count
        )
        result = {
            "source_main_sha": self.source_main_sha,
            # Legacy logical key retained for the stacked B1 consumer; this is
            # deliberately a readiness object, never a fabricated quota proof.
            "b4_quota_proof_package": readiness_payload,
            "b4_quota_headroom": self.controlled_shadow.readiness.headroom_payload(
                now=now, operation_request_count=self.controlled_shadow.request_count
            ),
            "discovery_evidence": [
                item.as_payload(now=now)
                for item in self.controlled_shadow.discovery_evidence
            ],
            "provider_native_discovery_provenance": self.controlled_shadow.operation_provenance_payload(),
            "controlled_shadow": shadow_payload,
            "b4_reconciliation": self.reconciliation.as_payload(),
            "b4_qualification": self.qualification.as_payload(),
            "b4_native_authorization": dict(
                self.controlled_shadow.authorization_provenance
            ),
            "b4_dossier_digest": self.dossier_digest,
        }
        if set(result) != set(_B1_LOGICAL_INPUT_KEYS):
            raise ProviderNeutralB4EvidenceError("B1 logical handoff shape changed")
        return result


def provider_neutral_dossier_digest(payload: object, *, now: datetime) -> str:
    """Compute a dossier digest from a validated typed dossier."""
    if not isinstance(payload, Top5B4ProviderNeutralEvidenceDossierV1):
        raise ProviderNeutralB4EvidenceError("expected provider-neutral B4 dossier")
    payload.validate(now=now)
    return payload.dossier_digest


__all__ = [
    "AUTHORIZATION_PROVENANCE_SCHEMA_VERSION",
    "MAX_DISCOVERY_AGE_SECONDS",
    "MAX_ODDS_AGE_SECONDS",
    "SUPPORTED_EVIDENCE_PROVIDERS",
    "TOP5_LEAGUE_ORDER",
    "B4ControlledShadowCaptureV1",
    "B4ControlledShadowEvidenceV1",
    "B4FixtureDiscoveryEvidenceV1",
    "B4MarketEvidenceV1",
    "B4ProviderNeutralQualificationV1",
    "B4ProviderNeutralReconciliationV1",
    "B4ProviderOperationEvidenceV1",
    "B4ProviderReadinessV1",
    "B4ProviderUsageEvidenceV1",
    "ProviderNeutralB4EvidenceError",
    "Top5B4ProviderNeutralEvidenceDossierV1",
    "canonical_evidence_digest",
    "provider_neutral_dossier_digest",
]
