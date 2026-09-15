"""Independent validation boundary for the Top-5 provider cascade.

This module consumes serialized cascade evidence.  It deliberately does not
import Builder 4's router, provider clients, budget manager, or health state.
It validates routing causality, source quality, quota safety, readiness, and
the NO-BET boundary before an accepted observation can cross into the existing
``top5-shadow-evidence-v1`` contract.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from itertools import pairwise
from math import isfinite

from src.football.production_contracts import ProductionContractError
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA
from src.football.top5_shadow_provider_redundancy import (
    ProviderReadinessState,
    make_fixture_key,
    normalize_league,
    normalize_team_name,
)
from src.football.top5_shadow_validation import (
    EvidenceProvenance,
    SafetyAssertions,
    ShadowEvidenceBundle,
    ShadowObservationEvidence,
)

TOP5_CASCADE_VALIDATION_CONTRACT_VERSION = "top5-provider-cascade-validation-v1"
CASCADE_PROVIDER_ORDER = (
    "the_odds_api",
    "odds_api_io",
    "api_football",
    "betfair_delayed",
)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_KNOWN_PROVIDERS = frozenset(CASCADE_PROVIDER_ORDER)
_READY_STATES = frozenset(
    {
        ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION,
        ProviderReadinessState.REAL_OBSERVATION_VALIDATED,
    }
)
_READINESS_RANK = {
    ProviderReadinessState.CONTRACT_SUPPORTED: 0,
    ProviderReadinessState.LIVE_PATH_PREREQUISITES_MISSING: 0,
    ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION: 1,
    ProviderReadinessState.REAL_OBSERVATION_VALIDATED: 2,
}


class CascadeValidationError(ProductionContractError):
    """Invalid serialized cascade evidence or caller policy."""


class CascadeSafetyRejection(CascadeValidationError):
    """Cascade evidence violates a hard safety boundary."""

    def __init__(self, message: str, codes: Sequence[str] = ()):
        self.codes = tuple(codes)
        super().__init__(message)


class CascadeOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILED = "AUTH_FAILED"
    TIMEOUT = "TIMEOUT"
    HTTP_401 = "HTTP_401"
    HTTP_403 = "HTTP_403"
    HTTP_404 = "HTTP_404"
    HTTP_422 = "HTTP_422"
    HTTP_429 = "HTTP_429"
    HTTP_500 = "HTTP_500"
    HTTP_502 = "HTTP_502"
    HTTP_503 = "HTTP_503"
    HTTP_5XX = "HTTP_5XX"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    UNSUPPORTED_FIXTURE = "UNSUPPORTED_FIXTURE"
    UNSUPPORTED_LEAGUE = "UNSUPPORTED_LEAGUE"
    UNSUPPORTED_MARKET = "UNSUPPORTED_MARKET"
    STALE = "STALE"
    MALFORMED = "MALFORMED"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    PARTIAL = "PARTIAL"
    QUALITY_REJECTED = "QUALITY_REJECTED"
    CONFIG_DISABLED = "CONFIG_DISABLED"
    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    BUDGET_REJECTED = "BUDGET_REJECTED"


class ValidationCode(str, Enum):
    INVALID_SOURCE_CONTRACT = "INVALID_SOURCE_CONTRACT"
    CONFIGURATION_MISMATCH = "CONFIGURATION_MISMATCH"
    ORDER_MISMATCH = "ORDER_MISMATCH"
    SKIPPED_PROVIDER = "SKIPPED_PROVIDER"
    DUPLICATE_ATTEMPT = "DUPLICATE_ATTEMPT"
    ILLEGAL_EXTRA_CALL = "ILLEGAL_EXTRA_CALL"
    FANOUT_DETECTED = "FANOUT_DETECTED"
    FALLBACK_WITHOUT_FAILURE = "FALLBACK_WITHOUT_FAILURE"
    SELECTED_REJECTED_PROVIDER = "SELECTED_REJECTED_PROVIDER"
    SELECTED_PROVIDER_MISMATCH = "SELECTED_PROVIDER_MISMATCH"
    NETWORK_AFTER_PREFLIGHT_DENIED = "NETWORK_AFTER_PREFLIGHT_DENIED"
    NETWORK_AFTER_QUOTA_EXHAUSTED = "NETWORK_AFTER_QUOTA_EXHAUSTED"
    QUOTA_REQUEST_NOT_AUTHORIZED = "QUOTA_REQUEST_NOT_AUTHORIZED"
    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    BUDGET_REJECTED = "BUDGET_REJECTED"
    UNKNOWN_REQUEST_COUNT = "UNKNOWN_REQUEST_COUNT"
    REQUEST_COUNT_MISMATCH = "REQUEST_COUNT_MISMATCH"
    INVALID_REQUEST_COST = "INVALID_REQUEST_COST"
    INVALID_SUCCESS = "INVALID_SUCCESS"
    READINESS_ESCALATION = "READINESS_ESCALATION"
    PROVIDER_NOT_READY = "PROVIDER_NOT_READY"
    WRONG_LEAGUE = "WRONG_LEAGUE"
    WRONG_FIXTURE = "WRONG_FIXTURE"
    TEAM_ALIAS_MISMATCH = "TEAM_ALIAS_MISMATCH"
    INVERTED_HOME_AWAY = "INVERTED_HOME_AWAY"
    KICKOFF_MISMATCH = "KICKOFF_MISMATCH"
    MARKET_REJECTED = "MARKET_REJECTED"
    IN_PLAY_ODDS = "IN_PLAY_ODDS"
    PARTIAL_MARKET = "PARTIAL_MARKET"
    MISSING_DRAW = "MISSING_DRAW"
    MALFORMED_ODDS = "MALFORMED_ODDS"
    ODDS_SANITY = "ODDS_SANITY"
    STALE_INPUT = "STALE_INPUT"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    PREDICTION_WITHOUT_VALID_SOURCE = "PREDICTION_WITHOUT_VALID_SOURCE"
    NO_BET_VIOLATION = "NO_BET_VIOLATION"
    PUBLICATION_ENABLED = "PUBLICATION_ENABLED"
    LEDGER_MUTATION = "LEDGER_MUTATION"
    PRODUCTION_ACTIVATION = "PRODUCTION_ACTIVATION"
    SEALED_DATA_ACCESS = "SEALED_DATA_ACCESS"
    RESEARCH_MUTATION = "RESEARCH_MUTATION"
    MONETARY_SPEND_AUTHORIZED = "MONETARY_SPEND_AUTHORIZED"


class RequestCostClassification(str, Enum):
    QUOTA_CONSUMING_REQUEST = "QUOTA_CONSUMING_REQUEST"
    ZERO_COST_AUTHENTICATION = "ZERO_COST_AUTHENTICATION"
    FREE_CACHE = "FREE_CACHE"
    UNKNOWN = "UNKNOWN"


class BudgetDecision(str, Enum):
    ALLOWED = "ALLOWED"
    REJECTED = "REJECTED"


class MarketPhase(str, Enum):
    PRE_MATCH = "PRE_MATCH"
    IN_PLAY = "IN_PLAY"
    CLOSING = "CLOSING"


class ExecutionMode(str, Enum):
    SEQUENTIAL = "SEQUENTIAL"
    PARALLEL = "PARALLEL"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CascadeValidationError(f"{name} is required")
    return value.strip()


def _required_sha(value: object, name: str) -> str:
    text = _required_text(value, name)
    if _SHA_RE.fullmatch(text) is None:
        raise CascadeValidationError(f"{name} must be a 40-64 character SHA")
    return text.lower()


def _utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise CascadeValidationError(f"{name} must be timezone-aware")
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


def _number(value: object, name: str, *, minimum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CascadeValidationError(f"{name} must be numeric") from exc
    if not isfinite(number) or (minimum is not None and number < minimum):
        raise CascadeValidationError(f"{name} is outside the allowed numeric range")
    return number


def _tuple_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence):
        raise CascadeValidationError("expected a sequence")
    return tuple(item if isinstance(item, str) else str(item) for item in value)


def _enum(enum_type: type[Enum], value: object, name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise CascadeValidationError(f"{name} is unknown") from exc


@dataclass(frozen=True)
class CascadeQuotaSnapshot:
    """Quota state captured before or after one routed request."""

    authenticated: bool | None
    quota_used: int | None
    quota_remaining: int | None

    def validate(self) -> None:
        if self.authenticated is not None and not isinstance(self.authenticated, bool):
            raise CascadeValidationError("quota authenticated must be boolean or null")
        for name, value in (
            ("quota_used", self.quota_used),
            ("quota_remaining", self.quota_remaining),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise CascadeValidationError(
                    f"{name} must be a non-negative integer or null"
                )

    @classmethod
    def from_payload(cls, payload: object) -> CascadeQuotaSnapshot | None:
        if payload is None:
            return None
        if not isinstance(payload, Mapping):
            return cls(None, None, None)
        return cls(
            authenticated=payload.get("authenticated"),
            quota_used=payload.get("quota_used"),
            quota_remaining=payload.get("quota_remaining"),
        )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "authenticated": self.authenticated,
            "quota_used": self.quota_used,
            "quota_remaining": self.quota_remaining,
        }


@dataclass(frozen=True)
class CascadeProvenance:
    """Evidence identity supplied by the external cascade producer."""

    evidence_id: str
    artifact_id: str
    artifact_sha: str
    source_sha: str
    research_sha: str
    candidate_id: str
    model_identity: str
    generated_at: datetime | None
    contract_version: str = TOP5_CASCADE_VALIDATION_CONTRACT_VERSION

    def validate(self) -> None:
        for name, value in (
            ("evidence_id", self.evidence_id),
            ("artifact_id", self.artifact_id),
            ("candidate_id", self.candidate_id),
            ("model_identity", self.model_identity),
        ):
            _required_text(value, name)
        _required_sha(self.artifact_sha, "artifact_sha")
        _required_sha(self.source_sha, "source_sha")
        if (
            _required_sha(self.research_sha, "research_sha")
            != FROZEN_RESEARCH_SHA.lower()
        ):
            raise CascadeSafetyRejection(
                "cascade evidence references unfrozen Research",
                ("RESEARCH_MUTATION",),
            )
        _utc(self.generated_at, "generated_at")
        if self.contract_version != TOP5_CASCADE_VALIDATION_CONTRACT_VERSION:
            raise CascadeValidationError(
                "unsupported cascade evidence contract version"
            )

    @classmethod
    def from_payload(cls, payload: object) -> CascadeProvenance:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(
            evidence_id=raw.get("evidence_id", ""),
            artifact_id=raw.get("artifact_id", ""),
            artifact_sha=raw.get("artifact_sha", ""),
            source_sha=raw.get("source_sha", ""),
            research_sha=raw.get("research_sha", ""),
            candidate_id=raw.get("candidate_id", ""),
            model_identity=raw.get("model_identity", ""),
            generated_at=_parse_datetime(raw.get("generated_at")),
            contract_version=raw.get(
                "contract_version", TOP5_CASCADE_VALIDATION_CONTRACT_VERSION
            ),
        )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "evidence_id": self.evidence_id,
            "artifact_id": self.artifact_id,
            "artifact_sha": self.artifact_sha.lower(),
            "source_sha": self.source_sha.lower(),
            "research_sha": self.research_sha.lower(),
            "candidate_id": self.candidate_id,
            "model_identity": self.model_identity,
            "generated_at": _utc(self.generated_at, "generated_at").isoformat(),
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True)
class CascadeSafety:
    no_bet: bool | None
    publication_enabled: bool | None
    ledger_mutated: bool | None
    production_activation: bool | None
    sealed_data_accessed: bool | None
    research_mutated: bool | None
    monetary_spend_authorized: bool | None

    def validate(self) -> None:
        violations: list[str] = []
        if self.no_bet is not True:
            violations.append("NO_BET_VIOLATION")
        for code, value in (
            ("PUBLICATION_ENABLED", self.publication_enabled),
            ("LEDGER_MUTATION", self.ledger_mutated),
            ("PRODUCTION_ACTIVATION", self.production_activation),
            ("SEALED_DATA_ACCESS", self.sealed_data_accessed),
            ("RESEARCH_MUTATION", self.research_mutated),
            ("MONETARY_SPEND_AUTHORIZED", self.monetary_spend_authorized),
        ):
            if value is not False:
                violations.append(code)
        if violations:
            raise CascadeSafetyRejection(
                "safety assertions failed: " + ", ".join(violations),
                violations,
            )

    @classmethod
    def from_payload(cls, payload: object) -> CascadeSafety:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(
            no_bet=raw.get("no_bet"),
            publication_enabled=raw.get("publication_enabled"),
            ledger_mutated=raw.get("ledger_mutated"),
            production_activation=raw.get("production_activation"),
            sealed_data_accessed=raw.get("sealed_data_accessed"),
            research_mutated=raw.get("research_mutated"),
            monetary_spend_authorized=raw.get("monetary_spend_authorized"),
        )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "no_bet": self.no_bet,
            "publication_enabled": self.publication_enabled,
            "ledger_mutated": self.ledger_mutated,
            "production_activation": self.production_activation,
            "sealed_data_accessed": self.sealed_data_accessed,
            "research_mutated": self.research_mutated,
            "monetary_spend_authorized": self.monetary_spend_authorized,
        }


@dataclass(frozen=True)
class SkippedProvider:
    provider_identity: str
    provider_order_index: int
    reason: str

    def validate(self) -> None:
        _required_text(self.provider_identity, "skipped provider identity")
        if (
            not isinstance(self.provider_order_index, int)
            or isinstance(self.provider_order_index, bool)
            or self.provider_order_index < 0
        ):
            raise CascadeValidationError("skipped provider index must be non-negative")
        _required_text(self.reason, "skipped provider reason")

    @classmethod
    def from_payload(cls, payload: object) -> SkippedProvider:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(
            provider_identity=raw.get("provider_identity", ""),
            provider_order_index=raw.get("provider_order_index", -1),
            reason=raw.get("reason", ""),
        )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider_identity": self.provider_identity,
            "provider_order_index": self.provider_order_index,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CascadeAttempt:
    """One sequential routed request, including failed attempts."""

    league: str
    fixture_key: str
    home_team: str
    away_team: str
    kickoff: datetime | None
    configured_provider_order: tuple[str, ...]
    provider_attempt_index: int
    fallback_depth: int
    provider_identity: str
    network_called: bool
    start_timestamp: datetime | None
    end_timestamp: datetime | None
    capture_timestamp: datetime | None
    outcome: CascadeOutcome | str
    failure_classification: CascadeOutcome | str | None
    market_type: str
    home_odds: float | None
    draw_odds: float | None
    away_odds: float | None
    bookmaker_identity: str | None
    source_identity: str | None
    market_phase: MarketPhase | str
    source_timestamp: datetime | None
    request_latency_ms: int
    quota_before: CascadeQuotaSnapshot | None
    quota_after: CascadeQuotaSnapshot | None
    preflight_allowed: bool
    budget_decision: BudgetDecision | str
    request_cost_classification: RequestCostClassification | str
    network_request_count: int | None
    quota_cost_units: float | None
    credentials_available: bool | None
    provider_record_id: str
    adapter_version: str
    raw_record_digest: str
    request_identity: str
    provider_readiness_state: ProviderReadinessState | str

    def validate_structural(self) -> None:
        for name, value in (
            ("league", self.league),
            ("fixture_key", self.fixture_key),
            ("home_team", self.home_team),
            ("away_team", self.away_team),
            ("provider_identity", self.provider_identity),
            ("market_type", self.market_type),
            ("provider_record_id", self.provider_record_id),
            ("adapter_version", self.adapter_version),
            ("raw_record_digest", self.raw_record_digest),
            ("request_identity", self.request_identity),
        ):
            _required_text(value, name)
        order = self.configured_provider_order
        if not order or len(order) != len(set(order)):
            raise CascadeValidationError("configured provider order must be unique")
        if self.provider_identity not in _KNOWN_PROVIDERS:
            raise CascadeValidationError(
                "provider identity is not a Top-5 cascade candidate"
            )
        for index_name, value in (
            ("provider_attempt_index", self.provider_attempt_index),
            ("fallback_depth", self.fallback_depth),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CascadeValidationError(f"{index_name} must be non-negative")
        if not isinstance(self.network_called, bool):
            raise CascadeValidationError("network_called must be boolean")
        for name, value in (
            ("kickoff", self.kickoff),
            ("start_timestamp", self.start_timestamp),
            ("end_timestamp", self.end_timestamp),
            ("capture_timestamp", self.capture_timestamp),
        ):
            _utc(value, name)
        start = _utc(self.start_timestamp, "start_timestamp")
        end = _utc(self.end_timestamp, "end_timestamp")
        capture = _utc(self.capture_timestamp, "capture_timestamp")
        if start > end or not start <= capture <= end:
            raise CascadeValidationError("request timestamps are not ordered")
        outcome = _enum(CascadeOutcome, self.outcome, "outcome")
        if outcome is CascadeOutcome.SUCCESS:
            if self.failure_classification is not None:
                raise CascadeValidationError(
                    "success cannot carry failure classification"
                )
        elif (
            _enum(CascadeOutcome, self.failure_classification, "failure classification")
            is not outcome
        ):
            raise CascadeValidationError("failure classification must match outcome")
        _enum(MarketPhase, self.market_phase, "market phase")
        if (
            not isinstance(self.request_latency_ms, int)
            or isinstance(self.request_latency_ms, bool)
            or self.request_latency_ms < 0
        ):
            raise CascadeValidationError("request latency must be non-negative")
        if not isinstance(self.preflight_allowed, bool):
            raise CascadeValidationError("preflight_allowed must be boolean")
        budget_decision = _enum(BudgetDecision, self.budget_decision, "budget decision")
        cost_classification = _enum(
            RequestCostClassification,
            self.request_cost_classification,
            "request cost classification",
        )
        if self.network_request_count is not None and (
            not isinstance(self.network_request_count, int)
            or isinstance(self.network_request_count, bool)
            or self.network_request_count < 0
        ):
            raise CascadeValidationError(
                "network request count must be a non-negative integer or null"
            )
        if self.quota_cost_units is not None:
            _number(self.quota_cost_units, "quota_cost_units", minimum=0.0)
        if self.credentials_available is not None and not isinstance(
            self.credentials_available, bool
        ):
            raise CascadeValidationError(
                "credentials_available must be boolean or null"
            )
        readiness = _enum(
            ProviderReadinessState,
            self.provider_readiness_state,
            "provider readiness state",
        )
        if (
            readiness is ProviderReadinessState.REAL_OBSERVATION_VALIDATED
            and outcome is not CascadeOutcome.SUCCESS
        ):
            raise CascadeValidationError(
                "real observation state cannot describe a failed attempt"
            )
        for quota in (self.quota_before, self.quota_after):
            if quota is not None:
                quota.validate()
        if (
            not self.preflight_allowed or budget_decision is BudgetDecision.REJECTED
        ) and self.network_called:
            raise CascadeValidationError("denied request cannot call the network")
        if cost_classification is RequestCostClassification.UNKNOWN:
            raise CascadeValidationError("request cost classification is unknown")
        for name, value in (
            ("home_odds", self.home_odds),
            ("draw_odds", self.draw_odds),
            ("away_odds", self.away_odds),
        ):
            if value is not None:
                _number(value, name)
        if self.source_timestamp is not None:
            _utc(self.source_timestamp, "source_timestamp")

    @classmethod
    def from_payload(cls, payload: object) -> CascadeAttempt:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(
            league=raw.get("league", ""),
            fixture_key=raw.get("fixture_key", ""),
            home_team=raw.get("home_team", ""),
            away_team=raw.get("away_team", ""),
            kickoff=_parse_datetime(raw.get("kickoff")),
            configured_provider_order=_tuple_strings(
                raw.get("configured_provider_order", ())
            ),
            provider_attempt_index=raw.get("provider_attempt_index", -1),
            fallback_depth=raw.get("fallback_depth", -1),
            provider_identity=raw.get("provider_identity", ""),
            network_called=raw.get("network_called"),
            start_timestamp=_parse_datetime(raw.get("start_timestamp")),
            end_timestamp=_parse_datetime(raw.get("end_timestamp")),
            capture_timestamp=_parse_datetime(raw.get("capture_timestamp")),
            outcome=raw.get("outcome", ""),
            failure_classification=raw.get("failure_classification"),
            market_type=raw.get("market_type", ""),
            home_odds=raw.get("home_odds"),
            draw_odds=raw.get("draw_odds"),
            away_odds=raw.get("away_odds"),
            bookmaker_identity=raw.get("bookmaker_identity"),
            source_identity=raw.get("source_identity"),
            market_phase=raw.get("market_phase", ""),
            source_timestamp=_parse_datetime(raw.get("source_timestamp")),
            request_latency_ms=raw.get("request_latency_ms", -1),
            quota_before=CascadeQuotaSnapshot.from_payload(raw.get("quota_before")),
            quota_after=CascadeQuotaSnapshot.from_payload(raw.get("quota_after")),
            preflight_allowed=raw.get("preflight_allowed"),
            budget_decision=raw.get("budget_decision", ""),
            request_cost_classification=raw.get("request_cost_classification", ""),
            network_request_count=raw.get("network_request_count"),
            quota_cost_units=raw.get("quota_cost_units"),
            credentials_available=raw.get("credentials_available"),
            provider_record_id=raw.get("provider_record_id", ""),
            adapter_version=raw.get("adapter_version", ""),
            raw_record_digest=raw.get("raw_record_digest", ""),
            request_identity=raw.get("request_identity", ""),
            provider_readiness_state=raw.get("provider_readiness_state", ""),
        )

    def as_payload(self) -> dict[str, object]:
        self.validate_structural()
        return {
            "league": self.league,
            "fixture_key": self.fixture_key,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff": _utc(self.kickoff, "kickoff").isoformat(),
            "configured_provider_order": list(self.configured_provider_order),
            "provider_attempt_index": self.provider_attempt_index,
            "fallback_depth": self.fallback_depth,
            "provider_identity": self.provider_identity,
            "network_called": self.network_called,
            "start_timestamp": _utc(
                self.start_timestamp, "start_timestamp"
            ).isoformat(),
            "end_timestamp": _utc(self.end_timestamp, "end_timestamp").isoformat(),
            "capture_timestamp": _utc(
                self.capture_timestamp, "capture_timestamp"
            ).isoformat(),
            "outcome": _enum(CascadeOutcome, self.outcome, "outcome").value,
            "failure_classification": (
                _enum(
                    CascadeOutcome,
                    self.failure_classification,
                    "failure classification",
                ).value
                if self.failure_classification is not None
                else None
            ),
            "market_type": self.market_type,
            "home_odds": self.home_odds,
            "draw_odds": self.draw_odds,
            "away_odds": self.away_odds,
            "bookmaker_identity": self.bookmaker_identity,
            "source_identity": self.source_identity,
            "market_phase": _enum(MarketPhase, self.market_phase, "market phase").value,
            "source_timestamp": (
                _utc(self.source_timestamp, "source_timestamp").isoformat()
                if self.source_timestamp is not None
                else None
            ),
            "request_latency_ms": self.request_latency_ms,
            "quota_before": self.quota_before.as_payload()
            if self.quota_before
            else None,
            "quota_after": self.quota_after.as_payload() if self.quota_after else None,
            "preflight_allowed": self.preflight_allowed,
            "budget_decision": _enum(
                BudgetDecision, self.budget_decision, "budget decision"
            ).value,
            "request_cost_classification": _enum(
                RequestCostClassification,
                self.request_cost_classification,
                "request cost classification",
            ).value,
            "network_request_count": self.network_request_count,
            "quota_cost_units": self.quota_cost_units,
            "credentials_available": self.credentials_available,
            "provider_record_id": self.provider_record_id,
            "adapter_version": self.adapter_version,
            "raw_record_digest": self.raw_record_digest,
            "request_identity": self.request_identity,
            "provider_readiness_state": _enum(
                ProviderReadinessState,
                self.provider_readiness_state,
                "provider readiness state",
            ).value,
        }


@dataclass(frozen=True)
class CascadeEvidence:
    provenance: CascadeProvenance
    configured_provider_order: tuple[str, ...]
    execution_mode: ExecutionMode | str
    attempts: tuple[CascadeAttempt, ...]
    skipped_providers: tuple[SkippedProvider, ...]
    selected_provider: str | None
    prediction_input_allowed: bool
    safety: CascadeSafety

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> CascadeEvidence:
        if not isinstance(payload, Mapping):
            raise CascadeValidationError("cascade evidence must be a mapping")
        attempts_raw = payload.get("attempts", ())
        skipped_raw = payload.get("skipped_providers", ())
        if not isinstance(attempts_raw, Sequence) or isinstance(attempts_raw, str):
            attempts_raw = ()
        if not isinstance(skipped_raw, Sequence) or isinstance(skipped_raw, str):
            skipped_raw = ()
        return cls(
            provenance=CascadeProvenance.from_payload(payload.get("provenance")),
            configured_provider_order=_tuple_strings(
                payload.get("configured_provider_order", ())
            ),
            execution_mode=payload.get("execution_mode", ""),
            attempts=tuple(CascadeAttempt.from_payload(item) for item in attempts_raw),
            skipped_providers=tuple(
                SkippedProvider.from_payload(item) for item in skipped_raw
            ),
            selected_provider=payload.get("selected_provider"),
            prediction_input_allowed=payload.get("prediction_input_allowed"),
            safety=CascadeSafety.from_payload(payload.get("safety")),
        )

    def validate_structural(self) -> None:
        self.provenance.validate()
        if not self.configured_provider_order or len(
            self.configured_provider_order
        ) != len(set(self.configured_provider_order)):
            raise CascadeValidationError(
                "cascade provider order must be unique and non-empty"
            )
        if any(
            provider not in _KNOWN_PROVIDERS
            for provider in self.configured_provider_order
        ):
            raise CascadeValidationError(
                "cascade provider order contains an unknown provider"
            )
        _enum(ExecutionMode, self.execution_mode, "execution mode")
        if not self.attempts:
            raise CascadeValidationError(
                "cascade evidence requires at least one attempt"
            )
        if not isinstance(self.prediction_input_allowed, bool):
            raise CascadeValidationError("prediction_input_allowed must be boolean")
        if self.selected_provider is not None:
            _required_text(self.selected_provider, "selected provider")
        self.safety.validate()
        for attempt in self.attempts:
            attempt.validate_structural()
        for skipped in self.skipped_providers:
            skipped.validate()

    @classmethod
    def from_attempts(
        cls,
        *,
        provenance: CascadeProvenance,
        attempts: Sequence[CascadeAttempt],
        selected_provider: str | None,
        prediction_input_allowed: bool,
        safety: CascadeSafety,
        skipped_providers: Sequence[SkippedProvider] = (),
    ) -> CascadeEvidence:
        return cls(
            provenance=provenance,
            configured_provider_order=tuple(attempts[0].configured_provider_order),
            execution_mode=ExecutionMode.SEQUENTIAL,
            attempts=tuple(attempts),
            skipped_providers=tuple(skipped_providers),
            selected_provider=selected_provider,
            prediction_input_allowed=prediction_input_allowed,
            safety=safety,
        )

    def as_payload(self) -> dict[str, object]:
        self.validate_structural()
        return {
            "contract_version": TOP5_CASCADE_VALIDATION_CONTRACT_VERSION,
            "provenance": self.provenance.as_payload(),
            "configured_provider_order": list(self.configured_provider_order),
            "execution_mode": _enum(
                ExecutionMode, self.execution_mode, "execution mode"
            ).value,
            "attempts": [attempt.as_payload() for attempt in self.attempts],
            "skipped_providers": [item.as_payload() for item in self.skipped_providers],
            "selected_provider": self.selected_provider,
            "prediction_input_allowed": self.prediction_input_allowed,
            "safety": self.safety.as_payload(),
        }


@dataclass(frozen=True)
class ExpectedCascadeFixture:
    """Exact caller-supplied fixture identity used by every quality gate."""

    league: str
    fixture_key: str
    home_team: str
    away_team: str
    kickoff: datetime

    def validate(self) -> None:
        _required_text(self.league, "expected league")
        _required_text(self.fixture_key, "expected fixture_key")
        _required_text(self.home_team, "expected home_team")
        _required_text(self.away_team, "expected away_team")
        expected_key = make_fixture_key(
            self.league, self.home_team, self.away_team, self.kickoff
        )
        if self.fixture_key != expected_key:
            raise CascadeValidationError("expected fixture identity is not canonical")


@dataclass(frozen=True)
class CascadeValidationPolicy:
    """Explicit experiment policy; no production timing defaults exist."""

    maximum_odds_age_seconds: int
    kickoff_tolerance_seconds: int
    configured_provider_order: tuple[str, ...] = CASCADE_PROVIDER_ORDER
    provider_readiness: Mapping[str, ProviderReadinessState] = field(
        default_factory=dict
    )
    expected_fixture: ExpectedCascadeFixture | None = None

    def validate(self) -> None:
        if (
            not isinstance(self.maximum_odds_age_seconds, int)
            or isinstance(self.maximum_odds_age_seconds, bool)
            or self.maximum_odds_age_seconds <= 0
            or not isinstance(self.kickoff_tolerance_seconds, int)
            or isinstance(self.kickoff_tolerance_seconds, bool)
            or self.kickoff_tolerance_seconds < 0
        ):
            raise CascadeValidationError("caller-supplied timing policy is invalid")
        configured_order = tuple(self.configured_provider_order)
        if not configured_order:
            raise CascadeValidationError("cascade order must be non-empty")
        if len(configured_order) != len(set(configured_order)):
            raise CascadeValidationError("cascade order must not contain duplicates")
        if any(provider not in _KNOWN_PROVIDERS for provider in configured_order):
            raise CascadeValidationError("cascade order contains an unknown provider")
        if self.expected_fixture is None:
            raise CascadeValidationError(
                "caller-supplied expected fixture is required; fuzzy fixture matching is disabled"
            )
        self.expected_fixture.validate()
        for provider, state in self.provider_readiness.items():
            if provider not in _KNOWN_PROVIDERS:
                raise CascadeValidationError("readiness contains an unknown provider")
            _enum(ProviderReadinessState, state, "provider readiness state")

    def readiness_for(self, provider: str) -> ProviderReadinessState:
        return ProviderReadinessState(
            self.provider_readiness.get(
                provider, ProviderReadinessState.CONTRACT_SUPPORTED
            )
        )


@dataclass(frozen=True)
class CascadeMetrics:
    provider_attempts: int
    provider_success_count: int
    provider_rejection_count: int
    fallback_depth: int
    successful_fallback_count: int
    fail_closed_count: int
    fixture_match_rate: float
    complete_1x2_rate: float
    freshness_acceptance_rate: float
    latency_observations_ms: tuple[float, ...]
    quota_rejected_before_network_count: int

    def as_payload(self) -> dict[str, object]:
        return {
            "provider_attempts": self.provider_attempts,
            "provider_success_count": self.provider_success_count,
            "provider_rejection_count": self.provider_rejection_count,
            "fallback_depth": self.fallback_depth,
            "successful_fallback_count": self.successful_fallback_count,
            "fail_closed_count": self.fail_closed_count,
            "fixture_match_rate": self.fixture_match_rate,
            "complete_1x2_rate": self.complete_1x2_rate,
            "freshness_acceptance_rate": self.freshness_acceptance_rate,
            "latency_observations_ms": list(self.latency_observations_ms),
            "quota_rejected_before_network_count": self.quota_rejected_before_network_count,
        }


@dataclass(frozen=True)
class CascadeValidationReport:
    accepted: bool
    prediction_input_allowed: bool
    selected_provider: str | None
    errors: tuple[ValidationCode, ...]
    metrics: CascadeMetrics
    readiness: Mapping[str, ProviderReadinessState]

    @property
    def fail_closed(self) -> bool:
        return not self.prediction_input_allowed

    def validate(self) -> None:
        if self.accepted and self.errors:
            raise CascadeValidationError(
                "accepted cascade report cannot contain errors"
            )
        if self.prediction_input_allowed and not self.accepted:
            raise CascadeValidationError(
                "rejected cascade cannot enter prediction input"
            )
        if self.selected_provider is None and self.prediction_input_allowed:
            raise CascadeValidationError("prediction input requires selected provider")


def _append(errors: list[ValidationCode], code: ValidationCode) -> None:
    if code not in errors:
        errors.append(code)


def _quality_complete(attempt: CascadeAttempt) -> bool:
    return all(
        value is not None
        for value in (attempt.home_odds, attempt.draw_odds, attempt.away_odds)
    )


def _odds_valid(attempt: CascadeAttempt) -> bool:
    if not _quality_complete(attempt):
        return False
    try:
        values = tuple(
            _number(value, name, minimum=1.01)
            for name, value in zip(
                ("home_odds", "draw_odds", "away_odds"),
                (attempt.home_odds, attempt.draw_odds, attempt.away_odds),
                strict=True,
            )
        )
    except CascadeValidationError:
        return False
    implied = sum(1.0 / value for value in values)
    return 1.0 < implied <= 1.20


def _metrics(
    attempts: Sequence[CascadeAttempt],
    policy: CascadeValidationPolicy,
    *,
    selected_provider: str | None,
    fixture_matches: int,
) -> CascadeMetrics:
    total = len(attempts)
    success_count = sum(
        _enum(CascadeOutcome, item.outcome, "outcome") is CascadeOutcome.SUCCESS
        for item in attempts
    )
    complete_count = sum(_quality_complete(item) for item in attempts)
    timed = [
        item
        for item in attempts
        if item.source_timestamp is not None and item.capture_timestamp is not None
    ]
    fresh = sum(
        0
        <= (
            _utc(item.capture_timestamp, "capture_timestamp")
            - _utc(item.source_timestamp, "source_timestamp")
        ).total_seconds()
        <= policy.maximum_odds_age_seconds
        for item in timed
    )
    quota_rejected = sum(
        item.quota_before is not None
        and item.quota_before.quota_remaining == 0
        and _enum(
            RequestCostClassification,
            item.request_cost_classification,
            "request cost classification",
        )
        is RequestCostClassification.QUOTA_CONSUMING_REQUEST
        and not item.network_called
        for item in attempts
    )
    return CascadeMetrics(
        provider_attempts=total,
        provider_success_count=success_count,
        provider_rejection_count=total - success_count,
        fallback_depth=max((item.fallback_depth for item in attempts), default=0),
        successful_fallback_count=sum(
            _enum(CascadeOutcome, item.outcome, "outcome") is CascadeOutcome.SUCCESS
            and item.provider_attempt_index > 0
            for item in attempts
        ),
        fail_closed_count=int(selected_provider is None),
        fixture_match_rate=fixture_matches / total if total else 0.0,
        complete_1x2_rate=complete_count / total if total else 0.0,
        freshness_acceptance_rate=fresh / len(timed) if timed else 0.0,
        latency_observations_ms=tuple(
            float(item.request_latency_ms) for item in attempts
        ),
        quota_rejected_before_network_count=quota_rejected,
    )


def _invalid_report(errors: Sequence[ValidationCode]) -> CascadeValidationReport:
    report = CascadeValidationReport(
        accepted=False,
        prediction_input_allowed=False,
        selected_provider=None,
        errors=tuple(dict.fromkeys(errors)),
        metrics=CascadeMetrics(0, 0, 0, 0, 0, 1, 0.0, 0.0, 0.0, (), 0),
        readiness={},
    )
    report.validate()
    return report


def validate_cascade_evidence(
    evidence: CascadeEvidence | Mapping[str, object],
    policy: CascadeValidationPolicy,
) -> CascadeValidationReport:
    """Validate external cascade evidence without executing any provider."""

    if policy is None:
        raise CascadeValidationError(
            "explicit CascadeValidationPolicy is required; production timing is unresolved"
        )
    policy.validate()
    try:
        record = (
            evidence
            if isinstance(evidence, CascadeEvidence)
            else CascadeEvidence.from_payload(evidence)
        )
        record.validate_structural()
    except CascadeSafetyRejection as exc:
        if exc.codes:
            return _invalid_report(tuple(ValidationCode(code) for code in exc.codes))
        return _invalid_report((ValidationCode.INVALID_SOURCE_CONTRACT,))
    except (AttributeError, TypeError, ValueError, CascadeValidationError):
        return _invalid_report((ValidationCode.INVALID_SOURCE_CONTRACT,))

    errors: list[ValidationCode] = []
    policy_order = tuple(policy.configured_provider_order)
    if record.configured_provider_order != policy_order:
        _append(errors, ValidationCode.CONFIGURATION_MISMATCH)
    if (
        _enum(ExecutionMode, record.execution_mode, "execution mode")
        is not ExecutionMode.SEQUENTIAL
    ):
        _append(errors, ValidationCode.FANOUT_DETECTED)

    attempts = record.attempts
    expected_fixture = policy.expected_fixture
    assert expected_fixture is not None
    expected_fixture.validate()
    readiness = {
        provider: policy.readiness_for(provider)
        for provider in record.configured_provider_order
    }
    attempted_order_indices: list[int] = []
    seen_providers: set[str] = set()
    fixture_matches = 0
    baseline: CascadeAttempt | None = None
    successful_attempts: list[CascadeAttempt] = []
    for position, attempt in enumerate(attempts):
        if attempt.configured_provider_order != record.configured_provider_order:
            _append(errors, ValidationCode.CONFIGURATION_MISMATCH)
        if (
            attempt.provider_attempt_index != position
            or attempt.fallback_depth != position
        ):
            _append(errors, ValidationCode.ORDER_MISMATCH)
        if attempt.provider_identity in seen_providers:
            _append(errors, ValidationCode.DUPLICATE_ATTEMPT)
        seen_providers.add(attempt.provider_identity)
        provider_index = (
            record.configured_provider_order.index(attempt.provider_identity)
            if attempt.provider_identity in record.configured_provider_order
            else -1
        )
        if provider_index < 0 or (
            attempted_order_indices and provider_index <= attempted_order_indices[-1]
        ):
            _append(errors, ValidationCode.ORDER_MISMATCH)
        attempted_order_indices.append(provider_index)
        if (
            position
            and _enum(CascadeOutcome, attempts[position - 1].outcome, "outcome")
            is CascadeOutcome.SUCCESS
        ):
            _append(errors, ValidationCode.ILLEGAL_EXTRA_CALL)
            _append(errors, ValidationCode.FALLBACK_WITHOUT_FAILURE)
        if position:
            previous_end = _utc(attempts[position - 1].end_timestamp, "end_timestamp")
            current_start = _utc(attempt.start_timestamp, "start_timestamp")
            if current_start < previous_end:
                _append(errors, ValidationCode.FANOUT_DETECTED)
        if not attempt.preflight_allowed and attempt.network_called:
            _append(errors, ValidationCode.NETWORK_AFTER_PREFLIGHT_DENIED)
        budget_decision = _enum(
            BudgetDecision, attempt.budget_decision, "budget decision"
        )
        cost_classification = _enum(
            RequestCostClassification,
            attempt.request_cost_classification,
            "request cost classification",
        )
        if budget_decision is BudgetDecision.REJECTED:
            if attempt.network_called:
                _append(errors, ValidationCode.NETWORK_AFTER_PREFLIGHT_DENIED)
            if (
                _enum(CascadeOutcome, attempt.outcome, "outcome")
                is not CascadeOutcome.BUDGET_REJECTED
            ):
                _append(errors, ValidationCode.BUDGET_REJECTED)
        if attempt.network_request_count is None or attempt.network_request_count > 1:
            _append(errors, ValidationCode.UNKNOWN_REQUEST_COUNT)
        elif attempt.network_request_count != int(attempt.network_called):
            _append(errors, ValidationCode.REQUEST_COUNT_MISMATCH)
        if attempt.quota_cost_units is None or not isfinite(
            float(attempt.quota_cost_units)
        ):
            _append(errors, ValidationCode.INVALID_REQUEST_COST)
        if cost_classification is RequestCostClassification.QUOTA_CONSUMING_REQUEST:
            if attempt.network_called and (
                attempt.quota_before is None or attempt.quota_after is None
            ):
                _append(errors, ValidationCode.INVALID_REQUEST_COST)
            if attempt.quota_cost_units is not None and (
                (attempt.network_called and attempt.quota_cost_units <= 0)
                or (not attempt.network_called and attempt.quota_cost_units != 0)
            ):
                _append(errors, ValidationCode.INVALID_REQUEST_COST)
        if attempt.credentials_available is not True and attempt.network_called:
            _append(errors, ValidationCode.CREDENTIAL_MISSING)
        if (
            attempt.quota_before is not None
            and attempt.quota_before.quota_remaining == 0
            and cost_classification is RequestCostClassification.QUOTA_CONSUMING_REQUEST
        ):
            if attempt.network_called:
                _append(errors, ValidationCode.NETWORK_AFTER_QUOTA_EXHAUSTED)
            if (
                _enum(CascadeOutcome, attempt.outcome, "outcome")
                is not CascadeOutcome.QUOTA_EXHAUSTED
            ):
                _append(errors, ValidationCode.QUOTA_REQUEST_NOT_AUTHORIZED)
        if cost_classification is RequestCostClassification.ZERO_COST_AUTHENTICATION:
            if attempt.quota_cost_units not in (0, 0.0):
                _append(errors, ValidationCode.INVALID_REQUEST_COST)
            if (
                _enum(CascadeOutcome, attempt.outcome, "outcome")
                is CascadeOutcome.SUCCESS
            ):
                _append(errors, ValidationCode.INVALID_SUCCESS)
        if cost_classification is RequestCostClassification.FREE_CACHE and (
            attempt.network_called or attempt.quota_cost_units not in (0, 0.0)
        ):
            _append(errors, ValidationCode.INVALID_REQUEST_COST)
        provider_state = readiness.get(
            attempt.provider_identity, ProviderReadinessState.CONTRACT_SUPPORTED
        )
        attempt_state = ProviderReadinessState(attempt.provider_readiness_state)
        if _READINESS_RANK[attempt_state] > _READINESS_RANK[provider_state]:
            _append(errors, ValidationCode.READINESS_ESCALATION)

        try:
            observed_league = normalize_league(attempt.league)
            observed_home = normalize_team_name(attempt.home_team)
            observed_away = normalize_team_name(attempt.away_team)
            kickoff = _utc(attempt.kickoff, "kickoff")
            canonical = make_fixture_key(
                observed_league, observed_home, observed_away, kickoff
            )
            if attempt.fixture_key != canonical:
                _append(errors, ValidationCode.WRONG_FIXTURE)
            expected_league = normalize_league(expected_fixture.league)
            expected_home = normalize_team_name(expected_fixture.home_team)
            expected_away = normalize_team_name(expected_fixture.away_team)
            if observed_league != expected_league:
                _append(errors, ValidationCode.WRONG_LEAGUE)
            if (observed_home, observed_away) == (expected_away, expected_home):
                _append(errors, ValidationCode.INVERTED_HOME_AWAY)
            elif (observed_home, observed_away) != (expected_home, expected_away):
                _append(errors, ValidationCode.TEAM_ALIAS_MISMATCH)
            kickoff_delta = abs(
                (
                    kickoff - _utc(expected_fixture.kickoff, "expected kickoff")
                ).total_seconds()
            )
            if kickoff_delta > policy.kickoff_tolerance_seconds:
                _append(errors, ValidationCode.KICKOFF_MISMATCH)
            if attempt.fixture_key != expected_fixture.fixture_key:
                _append(errors, ValidationCode.WRONG_FIXTURE)
            if attempt.fixture_key == expected_fixture.fixture_key:
                fixture_matches += 1
            if baseline is None:
                baseline = attempt
            else:
                if observed_league != normalize_league(baseline.league):
                    _append(errors, ValidationCode.WRONG_LEAGUE)
                baseline_home = normalize_team_name(baseline.home_team)
                baseline_away = normalize_team_name(baseline.away_team)
                if (observed_home, observed_away) == (baseline_away, baseline_home):
                    _append(errors, ValidationCode.INVERTED_HOME_AWAY)
                elif (observed_home, observed_away) != (baseline_home, baseline_away):
                    _append(errors, ValidationCode.TEAM_ALIAS_MISMATCH)
        except (AttributeError, TypeError, ValueError, CascadeValidationError):
            _append(errors, ValidationCode.WRONG_FIXTURE)

        outcome = _enum(CascadeOutcome, attempt.outcome, "outcome")
        if outcome is CascadeOutcome.SUCCESS:
            successful_attempts.append(attempt)
            if not attempt.network_called:
                _append(errors, ValidationCode.INVALID_SUCCESS)
            if (
                attempt_state not in _READY_STATES
                or provider_state not in _READY_STATES
            ):
                _append(errors, ValidationCode.PROVIDER_NOT_READY)
            if attempt.market_type != "h2h_1x2":
                _append(errors, ValidationCode.MARKET_REJECTED)
            if (
                _enum(MarketPhase, attempt.market_phase, "market phase")
                is not MarketPhase.PRE_MATCH
            ):
                _append(errors, ValidationCode.IN_PLAY_ODDS)
            if not _quality_complete(attempt):
                _append(
                    errors,
                    ValidationCode.MISSING_DRAW
                    if attempt.draw_odds is None
                    else ValidationCode.PARTIAL_MARKET,
                )
            elif not _odds_valid(attempt):
                _append(errors, ValidationCode.MALFORMED_ODDS)
            if not attempt.bookmaker_identity or not attempt.source_identity:
                _append(errors, ValidationCode.MISSING_PROVENANCE)
            if attempt.source_timestamp is None:
                _append(errors, ValidationCode.MISSING_PROVENANCE)
            else:
                source = _utc(attempt.source_timestamp, "source_timestamp")
                capture = _utc(attempt.capture_timestamp, "capture_timestamp")
                age = (capture - source).total_seconds()
                if age < 0:
                    _append(errors, ValidationCode.FUTURE_TIMESTAMP)
                elif age > policy.maximum_odds_age_seconds:
                    _append(errors, ValidationCode.STALE_INPUT)
        if (
            attempt.quota_before is not None
            and attempt.quota_before.quota_remaining == 0
            and not attempt.network_called
            and cost_classification is RequestCostClassification.QUOTA_CONSUMING_REQUEST
            and outcome is not CascadeOutcome.QUOTA_EXHAUSTED
        ):
            _append(errors, ValidationCode.QUOTA_REQUEST_NOT_AUTHORIZED)

    expected_gaps: set[int] = set()
    for previous_index, current_index in pairwise(attempted_order_indices):
        expected_gaps.update(range(previous_index + 1, current_index))
    if attempted_order_indices and attempted_order_indices[0] > 0:
        expected_gaps.update(range(attempted_order_indices[0]))
    if (
        not successful_attempts
        and attempted_order_indices
        and attempted_order_indices[-1] < len(record.configured_provider_order) - 1
    ):
        expected_gaps.update(
            range(
                attempted_order_indices[-1] + 1,
                len(record.configured_provider_order),
            )
        )
    for skipped in record.skipped_providers:
        if (
            skipped.provider_order_index not in expected_gaps
            or skipped.provider_order_index >= len(record.configured_provider_order)
            or skipped.provider_identity
            != record.configured_provider_order[skipped.provider_order_index]
        ):
            _append(errors, ValidationCode.SKIPPED_PROVIDER)
    skipped_indices = {item.provider_order_index for item in record.skipped_providers}
    if skipped_indices != expected_gaps:
        _append(errors, ValidationCode.SKIPPED_PROVIDER)
    if len(successful_attempts) > 1:
        _append(errors, ValidationCode.ILLEGAL_EXTRA_CALL)
    if successful_attempts:
        selected = successful_attempts[0].provider_identity
        if record.selected_provider != selected:
            _append(errors, ValidationCode.SELECTED_PROVIDER_MISMATCH)
    elif record.selected_provider is not None:
        _append(errors, ValidationCode.SELECTED_REJECTED_PROVIDER)
    if record.prediction_input_allowed and (
        not successful_attempts or errors or record.selected_provider is None
    ):
        _append(errors, ValidationCode.PREDICTION_WITHOUT_VALID_SOURCE)

    report = CascadeValidationReport(
        accepted=not errors,
        prediction_input_allowed=not errors
        and bool(successful_attempts)
        and record.prediction_input_allowed,
        selected_provider=record.selected_provider if not errors else None,
        errors=tuple(errors),
        metrics=_metrics(
            attempts,
            policy,
            selected_provider=record.selected_provider if not errors else None,
            fixture_matches=fixture_matches,
        ),
        readiness=readiness,
    )
    report.validate()
    return report


def _v1_provenance(
    record: CascadeEvidence, attempt: CascadeAttempt
) -> EvidenceProvenance:
    return EvidenceProvenance(
        evidence_id=record.provenance.evidence_id,
        artifact_id=record.provenance.artifact_id,
        artifact_sha=record.provenance.artifact_sha,
        source_sha=record.provenance.source_sha,
        research_sha=record.provenance.research_sha,
        league_code=normalize_league(attempt.league),
        candidate_id=record.provenance.candidate_id,
        model_identity=record.provenance.model_identity,
        generated_at=record.provenance.generated_at,
        fixture_key=attempt.fixture_key,
    )


def cascade_to_shadow_observation_evidence(
    evidence: CascadeEvidence | Mapping[str, object],
    policy: CascadeValidationPolicy,
) -> ShadowObservationEvidence:
    """Bridge independently accepted cascade evidence to v1 observation evidence."""

    record = (
        evidence
        if isinstance(evidence, CascadeEvidence)
        else CascadeEvidence.from_payload(evidence)
    )
    report = validate_cascade_evidence(record, policy)
    if not report.accepted:
        raise CascadeValidationError(
            "rejected cascade evidence cannot be bridged as accepted input"
        )
    attempt = record.attempts[0]
    if record.selected_provider is not None:
        attempt = next(
            item
            for item in record.attempts
            if item.provider_identity == record.selected_provider
        )
    observation = ShadowObservationEvidence(
        provenance=_v1_provenance(record, attempt),
        discovered=True,
        eligible=report.prediction_input_allowed,
        valid_odds=report.prediction_input_allowed,
        prediction_id=None,
        rejected=not report.prediction_input_allowed,
        rejected_reason="all providers rejected"
        if not report.prediction_input_allowed
        else None,
        provider_covered=record.selected_provider is not None,
        stale=ValidationCode.STALE_INPUT in report.errors,
        fallback_used=attempt.provider_attempt_index > 0,
        error=any(
            _enum(CascadeOutcome, item.outcome, "outcome") is not CascadeOutcome.SUCCESS
            for item in record.attempts
        ),
        duplicate_suppressed=ValidationCode.DUPLICATE_ATTEMPT in report.errors,
        no_bet=True,
        publication_enabled=False,
    )
    observation.validate()
    return observation


def cascade_to_shadow_evidence_bundle(
    evidence: CascadeEvidence | Mapping[str, object],
    policy: CascadeValidationPolicy,
) -> ShadowEvidenceBundle:
    """Return a NO-BET v1 bundle; no prediction or authority is created."""

    record = (
        evidence
        if isinstance(evidence, CascadeEvidence)
        else CascadeEvidence.from_payload(evidence)
    )
    observation = cascade_to_shadow_observation_evidence(record, policy)
    start = min(
        _utc(item.start_timestamp, "start_timestamp") for item in record.attempts
    )
    end = max(_utc(item.end_timestamp, "end_timestamp") for item in record.attempts)
    bundle = ShadowEvidenceBundle(
        evidence_window_start=start,
        evidence_window_end=end,
        safety=SafetyAssertions(
            no_bet=True,
            publication_enabled=False,
            real_bet_created=False,
            ledger_mutated=False,
            sealed_data_accessed=False,
            research_mutated=False,
            production_activation=False,
        ),
        observations=(observation,),
    )
    bundle.validate()
    return bundle


def evidence_digest(evidence: CascadeEvidence | Mapping[str, object]) -> str:
    """Stable digest for external evidence, not provider selection."""

    record = (
        evidence
        if isinstance(evidence, CascadeEvidence)
        else CascadeEvidence.from_payload(evidence)
    )
    payload = json.dumps(record.as_payload(), sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "CASCADE_PROVIDER_ORDER",
    "TOP5_CASCADE_VALIDATION_CONTRACT_VERSION",
    "BudgetDecision",
    "CascadeAttempt",
    "CascadeEvidence",
    "CascadeMetrics",
    "CascadeOutcome",
    "CascadeProvenance",
    "CascadeQuotaSnapshot",
    "CascadeSafety",
    "CascadeSafetyRejection",
    "CascadeValidationError",
    "CascadeValidationPolicy",
    "CascadeValidationReport",
    "ExecutionMode",
    "ExpectedCascadeFixture",
    "MarketPhase",
    "RequestCostClassification",
    "SkippedProvider",
    "ValidationCode",
    "cascade_to_shadow_evidence_bundle",
    "cascade_to_shadow_observation_evidence",
    "evidence_digest",
    "validate_cascade_evidence",
]
