"""Independent validation and CEO-gating for Top-5 NO-BET shadow evidence.

This module consumes an external evidence contract.  It deliberately does not
import Builder 1's execution runner and contains no provider, scheduler,
publisher, model-binding, ledger, or activation side effect.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from math import isfinite, log

from src.football.production_contracts import ProductionContractError
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA

TOP5_VALIDATION_CONTRACT_VERSION = "top5-shadow-evidence-v1"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_OUTCOMES = ("away", "draw", "home")


class ShadowValidationError(ProductionContractError):
    """Invalid or incomplete external evidence."""


class ShadowSafetyRejection(ShadowValidationError):
    """Evidence violates a hard safety boundary."""


class GateState(str, Enum):
    NO_EVIDENCE = "NO_EVIDENCE"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    OBSERVING = "OBSERVING"
    PROVIDER_VALIDATION_PENDING = "PROVIDER_VALIDATION_PENDING"
    SIGNAL_TIME_VALIDATION_PENDING = "SIGNAL_TIME_VALIDATION_PENDING"
    SHADOW_PERFORMANCE_PENDING = "SHADOW_PERFORMANCE_PENDING"
    READY_FOR_CEO_GATE = "READY_FOR_CEO_GATE"
    CEO_DECISION_REQUIRED = "CEO_DECISION_REQUIRED"
    REJECTED_SAFETY = "REJECTED_SAFETY"
    APPROVED_FOR_CONTROLLED_ACTIVATION = "APPROVED_FOR_CONTROLLED_ACTIVATION"


class ProviderOutcome(str, Enum):
    SUCCESS = "success"
    HTTP_403 = "http_403"
    HTTP_429 = "http_429"
    TIMEOUT = "timeout"
    EMPTY_RESPONSE = "empty_response"
    MALFORMED_RESPONSE = "malformed_response"
    PARTIAL_RESPONSE = "partial_response"
    STALE = "stale"
    WRONG_MARKET = "wrong_market"
    WRONG_FIXTURE = "wrong_fixture"


class FailureCode(str, Enum):
    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    STALE_EVIDENCE = "stale_evidence"
    PROVIDER_403 = "provider_403"
    PROVIDER_429 = "provider_429"
    PROVIDER_TIMEOUT = "provider_timeout"
    MALFORMED_RESPONSE = "malformed_response"
    PARTIAL_RESPONSE = "partial_response"
    WRONG_MARKET = "wrong_market"
    WRONG_FIXTURE = "wrong_fixture"
    RESULT_MISMATCH = "result_mismatch"
    DUPLICATE_EVIDENCE = "duplicate_evidence"
    CROSS_LEAGUE_CONTAMINATION = "cross_league_contamination"
    CLOSING_LEAKAGE = "closing_leakage"
    MISSING_PROVENANCE = "missing_provenance"
    AMBIGUOUS_IDENTITY = "ambiguous_identity"
    NO_BET_VIOLATION = "no_bet_violation"
    PUBLICATION_ENABLED = "publication_enabled"
    REAL_BET = "real_bet_created"
    LEDGER_MUTATION = "ledger_mutation"
    SEALED_DATA_ACCESS = "sealed_data_access"
    RESEARCH_MUTATION = "research_mutation"
    ACTIVATION_WITHOUT_CEO = "activation_without_ceo"


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShadowValidationError(f"{field_name} is required")
    return value.strip()


def _require_sha(value: object, field_name: str) -> str:
    text = _require_text(value, field_name)
    if _SHA_RE.fullmatch(text) is None:
        raise ShadowValidationError(
            f"{field_name} must be an unambiguous 40-64 character SHA"
        )
    return text.lower()


def _utc(value: datetime | None, field_name: str) -> datetime:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise ShadowValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _bounded(value: object, field_name: str, *, upper: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ShadowValidationError(f"{field_name} must be numeric") from exc
    if not isfinite(number) or not 0 <= number <= upper:
        raise ShadowValidationError(f"{field_name} must be finite and in [0, {upper}]")
    return number


def _non_negative(value: object, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ShadowValidationError(f"{field_name} must be numeric") from exc
    if not isfinite(number) or number < 0:
        raise ShadowValidationError(f"{field_name} must be finite and non-negative")
    return number


def _iso(value: datetime | None) -> str | None:
    return _utc(value, "timestamp").isoformat() if value is not None else None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _tuple_strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence):
        raise ShadowValidationError("expected a sequence of strings")
    return tuple(str(item) for item in value)


def _enum_value(enum_type: type[Enum], value: object, field_name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ShadowValidationError(f"{field_name} is invalid") from exc


@dataclass(frozen=True)
class EvidenceProvenance:
    """Stable identity shared by every externally supplied evidence record."""

    evidence_id: str
    artifact_id: str
    artifact_sha: str
    source_sha: str
    research_sha: str
    league_code: str
    candidate_id: str
    model_identity: str
    generated_at: datetime | None
    fixture_key: str | None = None
    feature_schema_hash: str | None = None
    contract_version: str = TOP5_VALIDATION_CONTRACT_VERSION

    def validate(self, *, require_fixture: bool = False) -> None:
        for name, value in (
            ("evidence_id", self.evidence_id),
            ("artifact_id", self.artifact_id),
            ("league_code", self.league_code),
            ("candidate_id", self.candidate_id),
            ("model_identity", self.model_identity),
        ):
            _require_text(value, name)
        _require_sha(self.artifact_sha, "artifact_sha")
        _require_sha(self.source_sha, "source_sha")
        research_sha = _require_sha(self.research_sha, "research_sha")
        if research_sha != FROZEN_RESEARCH_SHA.lower():
            raise ShadowSafetyRejection("evidence references an unfrozen Research SHA")
        _utc(self.generated_at, "generated_at")
        if self.contract_version != TOP5_VALIDATION_CONTRACT_VERSION:
            raise ShadowValidationError("unsupported shadow evidence contract version")
        if self.league_code not in TOP5_LEAGUE_ADAPTERS:
            raise ShadowSafetyRejection("evidence references an unknown league")
        if require_fixture:
            _require_text(self.fixture_key, "fixture_key")
        elif self.fixture_key is not None and not self.fixture_key.strip():
            raise ShadowValidationError("fixture_key cannot be blank")
        if self.feature_schema_hash is not None:
            _require_sha(self.feature_schema_hash, "feature_schema_hash")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "evidence_id": self.evidence_id,
            "artifact_id": self.artifact_id,
            "artifact_sha": self.artifact_sha.lower(),
            "source_sha": self.source_sha.lower(),
            "research_sha": self.research_sha.lower(),
            "league_code": self.league_code,
            "fixture_key": self.fixture_key,
            "candidate_id": self.candidate_id,
            "model_identity": self.model_identity,
            "feature_schema_hash": self.feature_schema_hash,
            "generated_at": _iso(self.generated_at),
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True)
class SafetyAssertions:
    """Explicit safety assertions required by an evidence bundle."""

    no_bet: bool | None
    publication_enabled: bool | None
    real_bet_created: bool | None
    ledger_mutated: bool | None
    sealed_data_accessed: bool | None
    research_mutated: bool | None
    production_activation: bool | None

    def validate(self) -> None:
        if self.no_bet is not True:
            raise ShadowSafetyRejection("no_bet=true is required")
        checks = (
            ("publication enabled", self.publication_enabled),
            ("real bet creation", self.real_bet_created),
            ("ledger mutation", self.ledger_mutated),
            ("sealed-data access", self.sealed_data_accessed),
            ("Research mutation", self.research_mutated),
            ("production activation", self.production_activation),
        )
        for label, value in checks:
            if value is not False:
                raise ShadowSafetyRejection(f"safety assertion failed: {label}")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "no_bet": self.no_bet,
            "publication_enabled": self.publication_enabled,
            "real_bet_created": self.real_bet_created,
            "ledger_mutated": self.ledger_mutated,
            "sealed_data_accessed": self.sealed_data_accessed,
            "research_mutated": self.research_mutated,
            "production_activation": self.production_activation,
        }


@dataclass(frozen=True)
class ShadowObservationEvidence:
    provenance: EvidenceProvenance
    discovered: bool
    eligible: bool
    valid_odds: bool
    prediction_id: str | None = None
    rejected: bool = False
    rejected_reason: str | None = None
    provider_covered: bool = False
    stale: bool = False
    fallback_used: bool = False
    error: bool = False
    duplicate_suppressed: bool = False
    result_resolved: bool = False
    no_bet: bool | None = True
    publication_enabled: bool | None = False

    def validate(self) -> None:
        self.provenance.validate(require_fixture=True)
        for name, value in (
            ("discovered", self.discovered),
            ("eligible", self.eligible),
            ("valid_odds", self.valid_odds),
            ("rejected", self.rejected),
            ("provider_covered", self.provider_covered),
            ("stale", self.stale),
            ("fallback_used", self.fallback_used),
            ("error", self.error),
            ("duplicate_suppressed", self.duplicate_suppressed),
            ("result_resolved", self.result_resolved),
        ):
            if not isinstance(value, bool):
                raise ShadowValidationError(f"{name} must be boolean")
        if self.no_bet is not True:
            raise ShadowSafetyRejection("shadow observation violates no_bet=true")
        if self.publication_enabled is not False:
            raise ShadowSafetyRejection("shadow observation enables publication")
        if self.eligible and not self.discovered:
            raise ShadowValidationError("an undiscovered fixture cannot be eligible")
        if self.valid_odds and not self.discovered:
            raise ShadowValidationError(
                "an undiscovered fixture cannot have valid odds"
            )
        if self.prediction_id is not None:
            _require_text(self.prediction_id, "prediction_id")
            if not self.eligible:
                raise ShadowValidationError(
                    "an ineligible fixture cannot have a prediction"
                )
        if self.rejected and not self.rejected_reason:
            raise ShadowValidationError("rejected fixtures require a rejection reason")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return _observation_payload(self)


@dataclass(frozen=True)
class PredictionEvidence:
    provenance: EvidenceProvenance
    prediction_id: str
    signal_snapshot_id: str
    input_snapshot_kinds: tuple[str, ...]
    probabilities: Mapping[str, float]
    market_probabilities: Mapping[str, float] = field(default_factory=dict)
    closing_snapshot_ids: tuple[str, ...] = ()
    no_bet: bool | None = True
    publication_enabled: bool | None = False

    def validate(self) -> None:
        self.provenance.validate(require_fixture=True)
        _require_text(self.prediction_id, "prediction_id")
        if self.provenance.artifact_id != self.prediction_id:
            raise ShadowSafetyRejection("prediction artifact identity is ambiguous")
        _require_text(self.signal_snapshot_id, "signal_snapshot_id")
        if not self.input_snapshot_kinds:
            raise ShadowSafetyRejection("prediction input snapshot identity is missing")
        if any(kind != "signal_time" for kind in self.input_snapshot_kinds):
            raise ShadowSafetyRejection("closing leakage entered prediction inputs")
        if self.closing_snapshot_ids:
            raise ShadowSafetyRejection("closing snapshot entered prediction inputs")
        if self.no_bet is not True:
            raise ShadowSafetyRejection("prediction violates no_bet=true")
        if self.publication_enabled is not False:
            raise ShadowSafetyRejection("prediction enables publication")
        if set(self.probabilities) != set(_OUTCOMES):
            raise ShadowValidationError(
                "prediction probabilities require away, draw, and home"
            )
        total = 0.0
        for name in _OUTCOMES:
            total += _bounded(self.probabilities[name], f"probabilities[{name}]")
        if abs(total - 1.0) > 1e-9:
            raise ShadowValidationError("prediction probabilities must sum to one")
        if self.market_probabilities:
            if set(self.market_probabilities) != set(_OUTCOMES):
                raise ShadowValidationError(
                    "market probabilities require away, draw, and home"
                )
            market_total = sum(
                _bounded(
                    self.market_probabilities[name], f"market_probabilities[{name}]"
                )
                for name in _OUTCOMES
            )
            if abs(market_total - 1.0) > 1e-9:
                raise ShadowValidationError("market probabilities must sum to one")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provenance": self.provenance.as_payload(),
            "prediction_id": self.prediction_id,
            "signal_snapshot_id": self.signal_snapshot_id,
            "input_snapshot_kinds": list(self.input_snapshot_kinds),
            "closing_snapshot_ids": list(self.closing_snapshot_ids),
            "probabilities": dict(self.probabilities),
            "market_probabilities": dict(self.market_probabilities),
            "no_bet": self.no_bet,
            "publication_enabled": self.publication_enabled,
        }


@dataclass(frozen=True)
class ProviderEvidence:
    provenance: EvidenceProvenance
    provider_name: str
    outcome: ProviderOutcome
    requested_fixture_count: int
    covered_fixture_count: int
    availability: bool
    latency_ms: int
    odds_age_seconds: float | None
    maximum_odds_age_seconds: float | None
    bulk_requests: int
    fallback_requests: int
    retry_count: int
    bulk_reused: bool
    stale_rejections: int = 0
    wrong_market_rejections: int = 0
    wrong_fixture_rejections: int = 0

    def validate(self) -> None:
        self.provenance.validate(require_fixture=True)
        _require_text(self.provider_name, "provider_name")
        outcome = _enum_value(ProviderOutcome, self.outcome, "provider outcome")
        counts = (
            ("requested_fixture_count", self.requested_fixture_count),
            ("covered_fixture_count", self.covered_fixture_count),
            ("latency_ms", self.latency_ms),
            ("bulk_requests", self.bulk_requests),
            ("fallback_requests", self.fallback_requests),
            ("retry_count", self.retry_count),
            ("stale_rejections", self.stale_rejections),
            ("wrong_market_rejections", self.wrong_market_rejections),
            ("wrong_fixture_rejections", self.wrong_fixture_rejections),
        )
        for name, value in counts:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ShadowValidationError(f"{name} must be a non-negative integer")
        if self.requested_fixture_count <= 0:
            raise ShadowValidationError(
                "provider evidence requires a positive fixture denominator"
            )
        if self.covered_fixture_count > self.requested_fixture_count:
            raise ShadowValidationError(
                "provider coverage cannot exceed its denominator"
            )
        if not isinstance(self.availability, bool) or not isinstance(
            self.bulk_reused, bool
        ):
            raise ShadowValidationError(
                "provider availability and reuse must be boolean"
            )
        if self.odds_age_seconds is not None:
            _non_negative(self.odds_age_seconds, "odds_age_seconds")
        if self.maximum_odds_age_seconds is not None:
            _non_negative(self.maximum_odds_age_seconds, "maximum_odds_age_seconds")
        if outcome is ProviderOutcome.SUCCESS and not self.availability:
            raise ShadowValidationError(
                "successful provider evidence must be available"
            )

    @property
    def freshness_ok(self) -> bool:
        return (
            self.odds_age_seconds is not None
            and self.maximum_odds_age_seconds is not None
            and self.odds_age_seconds <= self.maximum_odds_age_seconds
        )

    @property
    def completeness(self) -> float:
        return self.covered_fixture_count / self.requested_fixture_count


@dataclass(frozen=True)
class SignalTimeEvidence:
    provenance: EvidenceProvenance
    candidate_name: str
    eligible: bool
    odds_age_seconds: float
    maximum_odds_age_seconds: float
    request_load: float
    fallback_used: bool
    stale_rejected: bool
    latency_ms: float
    quota_cost_units: float
    operational_complexity: float

    def validate(self) -> None:
        self.provenance.validate(require_fixture=True)
        _require_text(self.candidate_name, "candidate_name")
        if not isinstance(self.eligible, bool):
            raise ShadowValidationError("signal-time eligibility must be boolean")
        for name, value in (
            ("odds_age_seconds", self.odds_age_seconds),
            ("maximum_odds_age_seconds", self.maximum_odds_age_seconds),
            ("request_load", self.request_load),
            ("latency_ms", self.latency_ms),
            ("quota_cost_units", self.quota_cost_units),
            ("operational_complexity", self.operational_complexity),
        ):
            _non_negative(value, name)
        if self.maximum_odds_age_seconds <= 0:
            raise ShadowValidationError("maximum_odds_age_seconds must be positive")
        if not isinstance(self.fallback_used, bool) or not isinstance(
            self.stale_rejected, bool
        ):
            raise ShadowValidationError("signal-time failure flags must be boolean")


@dataclass(frozen=True)
class QuotaCostEvidence:
    provenance: EvidenceProvenance
    horizon: str
    total_fixture_count: int
    logical_evaluations: int
    bulk_odds_requests: int
    fallback_event_requests: int
    result_requests: int
    revalidation_requests: int
    closing_capture_requests: int
    raw_http_requests: int
    provider_cost_units: float

    def validate(self) -> None:
        self.provenance.validate()
        _require_text(self.horizon, "horizon")
        counts = (
            ("total_fixture_count", self.total_fixture_count),
            ("logical_evaluations", self.logical_evaluations),
            ("bulk_odds_requests", self.bulk_odds_requests),
            ("fallback_event_requests", self.fallback_event_requests),
            ("result_requests", self.result_requests),
            ("revalidation_requests", self.revalidation_requests),
            ("closing_capture_requests", self.closing_capture_requests),
            ("raw_http_requests", self.raw_http_requests),
        )
        for name, value in counts:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ShadowValidationError(f"{name} must be a non-negative integer")
        _non_negative(self.provider_cost_units, "provider_cost_units")
        category_total = sum(
            (
                self.bulk_odds_requests,
                self.fallback_event_requests,
                self.result_requests,
                self.revalidation_requests,
                self.closing_capture_requests,
            )
        )
        if self.raw_http_requests < category_total:
            raise ShadowValidationError(
                "raw HTTP requests cannot understate request categories"
            )


@dataclass(frozen=True)
class HealthEvidence:
    provenance: EvidenceProvenance
    provider_health: str
    inference_health: str
    publisher_health: str
    result_source_health: str
    activation_state: str
    registered: bool
    no_bet: bool
    publication_enabled: bool

    def validate(self) -> None:
        self.provenance.validate()
        for name, value in (
            ("provider_health", self.provider_health),
            ("inference_health", self.inference_health),
            ("publisher_health", self.publisher_health),
            ("result_source_health", self.result_source_health),
            ("activation_state", self.activation_state),
        ):
            _require_text(value, name)
        if self.activation_state not in {"disabled", "shadow"}:
            raise ShadowSafetyRejection(
                "health evidence reports an active production state"
            )
        if self.registered:
            raise ShadowSafetyRejection("Top-5 shadow evidence must not be registered")
        if not self.no_bet:
            raise ShadowSafetyRejection("health evidence violates no_bet=true")
        if self.publication_enabled:
            raise ShadowSafetyRejection("health evidence enables publication")


@dataclass(frozen=True)
class ResultAttachment:
    provenance: EvidenceProvenance
    prediction_id: str
    actual_outcome: str | None
    resolved: bool
    result_source: str
    result_delay_seconds: float = 0.0

    def validate(self) -> None:
        self.provenance.validate(require_fixture=True)
        _require_text(self.prediction_id, "prediction_id")
        if not isinstance(self.resolved, bool):
            raise ShadowValidationError("result resolved must be boolean")
        _require_text(self.result_source, "result_source")
        _non_negative(self.result_delay_seconds, "result_delay_seconds")
        if self.resolved and self.actual_outcome not in _OUTCOMES:
            raise ShadowValidationError("resolved result has an invalid outcome")


@dataclass(frozen=True)
class ClosingBenchmarkEvidence:
    provenance: EvidenceProvenance
    prediction_id: str
    signal_snapshot_id: str
    closing_snapshot_id: str
    signal_probabilities: Mapping[str, float]
    closing_probabilities: Mapping[str, float]
    used_for_prediction: bool = False
    clv_style_value: float | None = None

    def validate(self) -> None:
        self.provenance.validate(require_fixture=True)
        _require_text(self.prediction_id, "prediction_id")
        _require_text(self.signal_snapshot_id, "signal_snapshot_id")
        _require_text(self.closing_snapshot_id, "closing_snapshot_id")
        if self.used_for_prediction:
            raise ShadowSafetyRejection(
                "closing benchmark data entered prediction inputs"
            )
        for name, values in (
            ("signal_probabilities", self.signal_probabilities),
            ("closing_probabilities", self.closing_probabilities),
        ):
            if set(values) != set(_OUTCOMES):
                raise ShadowValidationError(f"{name} require away, draw, and home")
            total = sum(_bounded(values[key], f"{name}[{key}]") for key in _OUTCOMES)
            if abs(total - 1.0) > 1e-9:
                raise ShadowValidationError(f"{name} must sum to one")
        if self.clv_style_value is not None:
            _non_negative(abs(self.clv_style_value), "clv_style_value")


@dataclass(frozen=True)
class FailureEvidence:
    provenance: EvidenceProvenance
    code: FailureCode
    message: str
    blocking: bool = True

    def validate(self) -> None:
        self.provenance.validate()
        _enum_value(FailureCode, self.code, "failure code")
        _require_text(self.message, "failure message")
        if not isinstance(self.blocking, bool):
            raise ShadowValidationError("failure blocking flag must be boolean")


@dataclass(frozen=True)
class ShadowEvidenceBundle:
    """The only input boundary exposed to Builder 1."""

    evidence_window_start: datetime | None
    evidence_window_end: datetime | None
    safety: SafetyAssertions | None = None
    observations: tuple[ShadowObservationEvidence, ...] = ()
    predictions: tuple[PredictionEvidence, ...] = ()
    provider_evidence: tuple[ProviderEvidence, ...] = ()
    signal_time_evidence: tuple[SignalTimeEvidence, ...] = ()
    quota_cost_evidence: tuple[QuotaCostEvidence, ...] = ()
    health_evidence: tuple[HealthEvidence, ...] = ()
    result_attachments: tuple[ResultAttachment, ...] = ()
    closing_benchmark_evidence: tuple[ClosingBenchmarkEvidence, ...] = ()
    failure_evidence: tuple[FailureEvidence, ...] = ()

    @property
    def record_count(self) -> int:
        return sum(
            len(records)
            for records in (
                self.observations,
                self.predictions,
                self.provider_evidence,
                self.signal_time_evidence,
                self.quota_cost_evidence,
                self.health_evidence,
                self.result_attachments,
                self.closing_benchmark_evidence,
                self.failure_evidence,
            )
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ShadowEvidenceBundle:
        if not isinstance(payload, Mapping):
            raise ShadowValidationError("shadow evidence payload must be a mapping")
        window = payload.get("evidence_window", {})
        if not isinstance(window, Mapping):
            window = {}
        safety_raw = payload.get("safety")
        safety = None
        if isinstance(safety_raw, Mapping):
            safety = SafetyAssertions(
                no_bet=safety_raw.get("no_bet"),
                publication_enabled=safety_raw.get("publication_enabled"),
                real_bet_created=safety_raw.get("real_bet_created"),
                ledger_mutated=safety_raw.get("ledger_mutated"),
                sealed_data_accessed=safety_raw.get("sealed_data_accessed"),
                research_mutated=safety_raw.get("research_mutated"),
                production_activation=safety_raw.get("production_activation"),
            )
        return cls(
            evidence_window_start=_parse_datetime(
                window.get("start", payload.get("window_start"))
            ),
            evidence_window_end=_parse_datetime(
                window.get("end", payload.get("window_end"))
            ),
            safety=safety,
            observations=tuple(
                _observation_from_payload(item)
                for item in _records(payload, "observations")
            ),
            predictions=tuple(
                _prediction_from_payload(item)
                for item in _records(payload, "predictions")
            ),
            provider_evidence=tuple(
                _provider_from_payload(item)
                for item in _records(payload, "provider_evidence")
            ),
            signal_time_evidence=tuple(
                _signal_time_from_payload(item)
                for item in _records(payload, "signal_time_evidence")
            ),
            quota_cost_evidence=tuple(
                _quota_from_payload(item)
                for item in _records(payload, "quota_cost_evidence")
            ),
            health_evidence=tuple(
                _health_from_payload(item)
                for item in _records(payload, "health_evidence")
            ),
            result_attachments=tuple(
                _result_from_payload(item)
                for item in _records(payload, "result_attachments")
            ),
            closing_benchmark_evidence=tuple(
                _closing_from_payload(item)
                for item in _records(payload, "closing_benchmark_evidence")
            ),
            failure_evidence=tuple(
                _failure_from_payload(item)
                for item in _records(payload, "failure_evidence")
            ),
        )

    def validate(self) -> None:
        if self.record_count == 0:
            return
        _utc(self.evidence_window_start, "evidence_window_start")
        _utc(self.evidence_window_end, "evidence_window_end")
        if self.evidence_window_start >= self.evidence_window_end:
            raise ShadowValidationError("evidence window must be ordered")
        if self.safety is None:
            raise ShadowSafetyRejection(
                "evidence bundle requires explicit safety assertions"
            )
        self.safety.validate()
        groups = (
            ("observation", self.observations),
            ("prediction", self.predictions),
            ("provider", self.provider_evidence),
            ("signal_time", self.signal_time_evidence),
            ("quota_cost", self.quota_cost_evidence),
            ("health", self.health_evidence),
            ("result", self.result_attachments),
            ("closing", self.closing_benchmark_evidence),
            ("failure", self.failure_evidence),
        )
        seen_records: set[tuple[str, str]] = set()
        artifacts: dict[str, tuple[str, str, str, str]] = {}
        fixture_leagues: dict[str, str] = {}
        observation_keys: set[tuple[str, str, str]] = set()
        bundle_identity: tuple[str, str] | None = None
        validators = {
            "observation": lambda item: item.validate(),
            "prediction": lambda item: item.validate(),
            "provider": lambda item: item.validate(),
            "signal_time": lambda item: item.validate(),
            "quota_cost": lambda item: item.validate(),
            "health": lambda item: item.validate(),
            "result": lambda item: item.validate(),
            "closing": lambda item: item.validate(),
            "failure": lambda item: item.validate(),
        }
        for kind, records in groups:
            for item in records:
                validators[kind](item)
                provenance = item.provenance
                identity = (
                    provenance.source_sha.lower(),
                    provenance.research_sha.lower(),
                )
                if bundle_identity is None:
                    bundle_identity = identity
                elif identity != bundle_identity:
                    raise ShadowSafetyRejection(
                        "evidence bundle mixes source or Research identities"
                    )
                if provenance.fixture_key is not None:
                    old_league = fixture_leagues.get(provenance.fixture_key)
                    if old_league is not None and old_league != provenance.league_code:
                        raise ShadowSafetyRejection(
                            "cross-league contamination detected"
                        )
                    fixture_leagues[provenance.fixture_key] = provenance.league_code
                key = (kind, provenance.evidence_id)
                if key in seen_records:
                    raise ShadowValidationError("duplicate evidence record identity")
                seen_records.add(key)
                fingerprint = (
                    provenance.artifact_sha.lower(),
                    provenance.source_sha.lower(),
                    provenance.research_sha.lower(),
                    provenance.league_code,
                )
                old = artifacts.get(provenance.artifact_id)
                if old is not None and old != fingerprint:
                    raise ShadowSafetyRejection("ambiguous SHA/artifact identity")
                artifacts[provenance.artifact_id] = fingerprint
                if kind == "observation":
                    observation_key = (
                        provenance.league_code,
                        provenance.fixture_key or "",
                        provenance.candidate_id,
                    )
                    if observation_key in observation_keys:
                        raise ShadowValidationError(
                            "duplicate fixture observation evidence"
                        )
                    observation_keys.add(observation_key)
        self._validate_links()

    def _validate_links(self) -> None:
        predictions = {item.prediction_id: item for item in self.predictions}
        for observation in self.observations:
            if observation.prediction_id is None:
                continue
            prediction = predictions.get(observation.prediction_id)
            if prediction is None:
                raise ShadowValidationError(
                    "observation references a missing prediction"
                )
            if prediction.provenance.fixture_key != observation.provenance.fixture_key:
                raise ShadowSafetyRejection("prediction belongs to another fixture")
            if prediction.provenance.league_code != observation.provenance.league_code:
                raise ShadowSafetyRejection("prediction belongs to another league")
        seen_results: set[str] = set()
        for result in self.result_attachments:
            prediction = predictions.get(result.prediction_id)
            if prediction is None:
                raise ShadowValidationError(
                    "result attachment references a missing prediction"
                )
            if result.prediction_id in seen_results:
                raise ShadowValidationError("duplicate result attachment")
            seen_results.add(result.prediction_id)
            if result.provenance.fixture_key != prediction.provenance.fixture_key:
                raise ShadowValidationError("result attachment has a fixture mismatch")
            if (
                result.resolved
                and result.actual_outcome not in prediction.probabilities
            ):
                raise ShadowValidationError(
                    "result mismatch: outcome absent from prediction"
                )
        for benchmark in self.closing_benchmark_evidence:
            prediction = predictions.get(benchmark.prediction_id)
            if prediction is None:
                raise ShadowValidationError(
                    "closing benchmark references a missing prediction"
                )
            if benchmark.provenance.fixture_key != prediction.provenance.fixture_key:
                raise ShadowValidationError("closing benchmark has a fixture mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "contract_version": TOP5_VALIDATION_CONTRACT_VERSION,
            "evidence_window": {
                "start": _iso(self.evidence_window_start),
                "end": _iso(self.evidence_window_end),
            },
            "safety": self.safety.as_payload() if self.safety else None,
            "observations": [_observation_payload(item) for item in self.observations],
            "predictions": [item.as_payload() for item in self.predictions],
            "provider_evidence": [
                _provider_payload(item) for item in self.provider_evidence
            ],
            "signal_time_evidence": [
                _signal_time_payload(item) for item in self.signal_time_evidence
            ],
            "quota_cost_evidence": [
                _quota_payload(item) for item in self.quota_cost_evidence
            ],
            "health_evidence": [_health_payload(item) for item in self.health_evidence],
            "result_attachments": [
                _result_payload(item) for item in self.result_attachments
            ],
            "closing_benchmark_evidence": [
                _closing_payload(item) for item in self.closing_benchmark_evidence
            ],
            "failure_evidence": [
                _failure_payload(item) for item in self.failure_evidence
            ],
        }


@dataclass(frozen=True)
class LeagueCoverageMetrics:
    league_code: str
    discovered_fixtures: int
    eligible_fixtures: int
    valid_odds: int
    predictions: int
    rejected_fixtures: int
    provider_covered: int
    stale_fixtures: int
    fallback_fixtures: int
    errors: int
    duplicate_suppressed: int
    resolved_results: int

    @property
    def denominator(self) -> int:
        return self.discovered_fixtures

    def _rate(self, value: int) -> float:
        return value / self.denominator if self.denominator else 0.0

    @property
    def provider_coverage(self) -> float:
        return self._rate(self.provider_covered)

    @property
    def stale_rate(self) -> float:
        return self._rate(self.stale_fixtures)

    @property
    def fallback_rate(self) -> float:
        return self._rate(self.fallback_fixtures)

    @property
    def error_rate(self) -> float:
        return self._rate(self.errors)

    @property
    def duplicate_rate(self) -> float:
        return self._rate(self.duplicate_suppressed)

    @property
    def result_resolution_rate(self) -> float:
        return self.resolved_results / self.predictions if self.predictions else 0.0

    def validate(self) -> None:
        _require_text(self.league_code, "league_code")
        values = (
            self.discovered_fixtures,
            self.eligible_fixtures,
            self.valid_odds,
            self.predictions,
            self.rejected_fixtures,
            self.provider_covered,
            self.stale_fixtures,
            self.fallback_fixtures,
            self.errors,
            self.duplicate_suppressed,
            self.resolved_results,
        )
        if any(not isinstance(value, int) or value < 0 for value in values):
            raise ShadowValidationError("coverage counts must be non-negative integers")
        if any(value > self.discovered_fixtures for value in values[1:-1]):
            raise ShadowValidationError("coverage numerator exceeds denominator")
        if self.predictions > self.eligible_fixtures:
            raise ShadowValidationError("predictions exceed eligible fixtures")
        if self.resolved_results > self.predictions:
            raise ShadowValidationError("resolved results exceed predictions")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "league": self.league_code,
            "discovered_fixtures": self.discovered_fixtures,
            "eligible_fixtures": self.eligible_fixtures,
            "valid_odds": self.valid_odds,
            "predictions": self.predictions,
            "rejected_fixtures": self.rejected_fixtures,
            "provider_covered": self.provider_covered,
            "provider_coverage": self.provider_coverage,
            "stale_fixtures": self.stale_fixtures,
            "stale_rate": self.stale_rate,
            "fallback_fixtures": self.fallback_fixtures,
            "fallback_rate": self.fallback_rate,
            "errors": self.errors,
            "error_rate": self.error_rate,
            "duplicate_suppressed": self.duplicate_suppressed,
            "duplicate_rate": self.duplicate_rate,
            "resolved_results": self.resolved_results,
            "result_resolution_rate": self.result_resolution_rate,
        }


@dataclass(frozen=True)
class CoverageReport:
    by_league: tuple[LeagueCoverageMetrics, ...]

    def validate(self) -> None:
        codes = [item.league_code for item in self.by_league]
        if len(codes) != len(set(codes)):
            raise ShadowValidationError("coverage report contains duplicate leagues")
        for item in self.by_league:
            item.validate()

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {"leagues": [item.as_payload() for item in self.by_league]}


def validate_coverage(bundle: ShadowEvidenceBundle) -> CoverageReport:
    bundle.validate()
    by_league: dict[str, list[ShadowObservationEvidence]] = defaultdict(list)
    for item in bundle.observations:
        by_league[item.provenance.league_code].append(item)
    reports: list[LeagueCoverageMetrics] = []
    for league_code in sorted(by_league):
        observations = by_league[league_code]
        predictions = sum(item.prediction_id is not None for item in observations)
        resolved_fixtures = {
            item.provenance.fixture_key
            for item in bundle.result_attachments
            if item.resolved and item.provenance.league_code == league_code
        }
        reports.append(
            LeagueCoverageMetrics(
                league_code=league_code,
                discovered_fixtures=sum(item.discovered for item in observations),
                eligible_fixtures=sum(item.eligible for item in observations),
                valid_odds=sum(item.valid_odds for item in observations),
                predictions=predictions,
                rejected_fixtures=sum(item.rejected for item in observations),
                provider_covered=sum(item.provider_covered for item in observations),
                stale_fixtures=sum(item.stale for item in observations),
                fallback_fixtures=sum(item.fallback_used for item in observations),
                errors=sum(item.error for item in observations),
                duplicate_suppressed=sum(
                    item.duplicate_suppressed for item in observations
                ),
                resolved_results=sum(
                    item.provenance.fixture_key in resolved_fixtures
                    for item in observations
                    if item.prediction_id is not None
                ),
            )
        )
    report = CoverageReport(tuple(reports))
    report.validate()
    return report


@dataclass(frozen=True)
class ProviderAssessment:
    league_code: str
    provider_name: str
    request_count: int
    requested_fixtures: int
    covered_fixtures: int
    availability_rate: float
    mean_latency_ms: float
    freshness_rate: float
    completeness_rate: float
    bulk_reuse_rate: float
    fallback_frequency: float
    retry_rate: float
    failure_taxonomy: Mapping[str, int]

    def validate(self) -> None:
        _require_text(self.league_code, "league_code")
        _require_text(self.provider_name, "provider_name")
        if self.request_count <= 0 or self.requested_fixtures <= 0:
            raise ShadowValidationError("provider assessment requires denominators")
        if self.covered_fixtures > self.requested_fixtures:
            raise ShadowValidationError(
                "provider assessment coverage exceeds denominator"
            )
        for name, value in (
            ("availability_rate", self.availability_rate),
            ("freshness_rate", self.freshness_rate),
            ("completeness_rate", self.completeness_rate),
            ("bulk_reuse_rate", self.bulk_reuse_rate),
            ("fallback_frequency", self.fallback_frequency),
            ("retry_rate", self.retry_rate),
        ):
            _bounded(value, name)
        _non_negative(self.mean_latency_ms, "mean_latency_ms")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "league": self.league_code,
            "provider": self.provider_name,
            "requests": self.request_count,
            "requested_fixtures": self.requested_fixtures,
            "covered_fixtures": self.covered_fixtures,
            "availability_rate": self.availability_rate,
            "mean_latency_ms": self.mean_latency_ms,
            "freshness_rate": self.freshness_rate,
            "completeness_rate": self.completeness_rate,
            "bulk_reuse_rate": self.bulk_reuse_rate,
            "fallback_frequency": self.fallback_frequency,
            "retry_rate": self.retry_rate,
            "failure_taxonomy": dict(sorted(self.failure_taxonomy.items())),
            "selected": False,
        }


def evaluate_provider_evidence(
    bundle: ShadowEvidenceBundle,
) -> tuple[ProviderAssessment, ...]:
    bundle.validate()
    groups: dict[tuple[str, str], list[ProviderEvidence]] = defaultdict(list)
    for item in bundle.provider_evidence:
        groups[(item.provenance.league_code, item.provider_name)].append(item)
    assessments: list[ProviderAssessment] = []
    for (league_code, provider_name), items in sorted(groups.items()):
        failures = Counter(
            _enum_value(ProviderOutcome, item.outcome, "provider outcome").value
            for item in items
            if _enum_value(ProviderOutcome, item.outcome, "provider outcome")
            is not ProviderOutcome.SUCCESS
        )
        assessments.append(
            ProviderAssessment(
                league_code=league_code,
                provider_name=provider_name,
                request_count=len(items),
                requested_fixtures=sum(item.requested_fixture_count for item in items),
                covered_fixtures=sum(item.covered_fixture_count for item in items),
                availability_rate=sum(item.availability for item in items) / len(items),
                mean_latency_ms=sum(item.latency_ms for item in items) / len(items),
                freshness_rate=sum(item.freshness_ok for item in items) / len(items),
                completeness_rate=sum(item.completeness for item in items) / len(items),
                bulk_reuse_rate=sum(item.bulk_reused for item in items) / len(items),
                fallback_frequency=sum(item.fallback_requests for item in items)
                / max(sum(item.requested_fixture_count for item in items), 1),
                retry_rate=sum(item.retry_count > 0 for item in items) / len(items),
                failure_taxonomy=failures,
            )
        )
    return tuple(assessments)


@dataclass(frozen=True)
class SignalTimeCandidateMetrics:
    candidate_name: str
    sample_size: int
    coverage: float
    freshness: float
    mean_odds_age_seconds: float
    mean_request_load: float
    fallback_frequency: float
    stale_risk: float
    mean_latency_ms: float
    quota_cost_units: float
    operational_complexity: float

    def validate(self) -> None:
        _require_text(self.candidate_name, "candidate_name")
        if self.sample_size <= 0:
            raise ShadowValidationError(
                "signal-time comparison requires a sample denominator"
            )
        for name, value in (
            ("coverage", self.coverage),
            ("freshness", self.freshness),
            ("fallback_frequency", self.fallback_frequency),
            ("stale_risk", self.stale_risk),
        ):
            _bounded(value, name)
        for name, value in (
            ("mean_odds_age_seconds", self.mean_odds_age_seconds),
            ("mean_request_load", self.mean_request_load),
            ("mean_latency_ms", self.mean_latency_ms),
            ("quota_cost_units", self.quota_cost_units),
            ("operational_complexity", self.operational_complexity),
        ):
            _non_negative(value, name)

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "candidate": self.candidate_name,
            "sample_size": self.sample_size,
            "coverage": self.coverage,
            "freshness": self.freshness,
            "mean_odds_age_seconds": self.mean_odds_age_seconds,
            "mean_request_load": self.mean_request_load,
            "fallback_frequency": self.fallback_frequency,
            "stale_risk": self.stale_risk,
            "mean_latency_ms": self.mean_latency_ms,
            "quota_cost_units": self.quota_cost_units,
            "operational_complexity": self.operational_complexity,
            "selected": False,
        }


@dataclass(frozen=True)
class SignalTimeComparison:
    candidates: tuple[SignalTimeCandidateMetrics, ...]
    selected_candidate: None = None
    recommendation: None = None

    def validate(self) -> None:
        names = [item.candidate_name for item in self.candidates]
        if not names or len(names) != len(set(names)):
            raise ShadowValidationError(
                "signal-time comparison requires unique candidates"
            )
        if self.selected_candidate is not None or self.recommendation is not None:
            raise ShadowSafetyRejection(
                "signal-time validation cannot select a production timing"
            )
        for item in self.candidates:
            item.validate()

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "selected_candidate": None,
            "recommendation": None,
            "candidates": [item.as_payload() for item in self.candidates],
        }


def compare_signal_time_evidence(
    bundle: ShadowEvidenceBundle,
) -> SignalTimeComparison | None:
    bundle.validate()
    groups: dict[str, list[SignalTimeEvidence]] = defaultdict(list)
    for item in bundle.signal_time_evidence:
        groups[item.candidate_name].append(item)
    if not groups:
        return None
    candidates: list[SignalTimeCandidateMetrics] = []
    for name, items in sorted(groups.items()):
        candidates.append(
            SignalTimeCandidateMetrics(
                candidate_name=name,
                sample_size=len(items),
                coverage=sum(item.eligible for item in items) / len(items),
                freshness=sum(not item.stale_rejected for item in items) / len(items),
                mean_odds_age_seconds=sum(item.odds_age_seconds for item in items)
                / len(items),
                mean_request_load=sum(item.request_load for item in items) / len(items),
                fallback_frequency=sum(item.fallback_used for item in items)
                / len(items),
                stale_risk=sum(item.stale_rejected for item in items) / len(items),
                mean_latency_ms=sum(item.latency_ms for item in items) / len(items),
                quota_cost_units=sum(item.quota_cost_units for item in items)
                / len(items),
                operational_complexity=sum(
                    item.operational_complexity for item in items
                )
                / len(items),
            )
        )
    result = SignalTimeComparison(tuple(candidates))
    result.validate()
    return result


@dataclass(frozen=True)
class PerformanceMetrics:
    sample_size: int
    resolved_result_count: int
    brier_score: float | None
    log_loss: float | None
    calibration_error: float | None
    market_relative_mean_abs_difference: float | None
    market_comparison_count: int
    closing_comparison_count: int
    clv_style_diagnostic: float | None

    def validate(self) -> None:
        if self.sample_size < 0 or self.resolved_result_count < 0:
            raise ShadowValidationError("performance sample sizes must be non-negative")
        if self.resolved_result_count > self.sample_size:
            raise ShadowValidationError("resolved results exceed performance sample")
        if self.market_comparison_count < 0 or self.closing_comparison_count < 0:
            raise ShadowValidationError(
                "performance comparison counts must be non-negative"
            )
        for name, value in (
            ("brier_score", self.brier_score),
            ("log_loss", self.log_loss),
            ("calibration_error", self.calibration_error),
            (
                "market_relative_mean_abs_difference",
                self.market_relative_mean_abs_difference,
            ),
            ("clv_style_diagnostic", self.clv_style_diagnostic),
        ):
            if value is not None and (not isfinite(float(value)) or float(value) < 0):
                raise ShadowValidationError(f"{name} must be finite and non-negative")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "sample_size": self.sample_size,
            "resolved_result_count": self.resolved_result_count,
            "brier_score": self.brier_score,
            "log_loss": self.log_loss,
            "calibration_error": self.calibration_error,
            "market_relative_mean_abs_difference": self.market_relative_mean_abs_difference,
            "market_comparison_count": self.market_comparison_count,
            "closing_comparison_count": self.closing_comparison_count,
            "clv_style_diagnostic": self.clv_style_diagnostic,
            "profitability_claim": False,
            "significance_claim": False,
        }


def calculate_performance_metrics(bundle: ShadowEvidenceBundle) -> PerformanceMetrics:
    bundle.validate()
    predictions = {item.prediction_id: item for item in bundle.predictions}
    results = {item.prediction_id: item for item in bundle.result_attachments}
    resolved = [
        (predictions[prediction_id], result)
        for prediction_id, result in results.items()
        if result.resolved and prediction_id in predictions
    ]
    brier_values: list[float] = []
    log_loss_values: list[float] = []
    calibration_values: list[float] = []
    market_values: list[float] = []
    for prediction, result in resolved:
        actual = result.actual_outcome
        if actual is None:
            continue
        brier_values.append(
            sum(
                (
                    float(prediction.probabilities[name])
                    - (1.0 if name == actual else 0.0)
                )
                ** 2
                for name in _OUTCOMES
            )
            / len(_OUTCOMES)
        )
        log_loss_values.append(
            -log(max(float(prediction.probabilities[actual]), 1e-15))
        )
        calibration_values.extend(
            abs(
                float(prediction.probabilities[name]) - (1.0 if name == actual else 0.0)
            )
            for name in _OUTCOMES
        )
        if prediction.market_probabilities:
            market_values.append(
                sum(
                    abs(
                        float(prediction.probabilities[name])
                        - float(prediction.market_probabilities[name])
                    )
                    for name in _OUTCOMES
                )
                / len(_OUTCOMES)
            )
    closing_values: list[float] = []
    prediction_by_id = {item.prediction_id: item for item in bundle.predictions}
    for benchmark in bundle.closing_benchmark_evidence:
        prediction = prediction_by_id[benchmark.prediction_id]
        closing_values.append(
            sum(
                abs(
                    float(benchmark.closing_probabilities[name])
                    - float(benchmark.signal_probabilities[name])
                )
                for name in _OUTCOMES
            )
            / len(_OUTCOMES)
        )
        if prediction.signal_snapshot_id != benchmark.signal_snapshot_id:
            raise ShadowValidationError("closing benchmark signal snapshot mismatch")
    metrics = PerformanceMetrics(
        sample_size=len(bundle.predictions),
        resolved_result_count=len(resolved),
        brier_score=sum(brier_values) / len(brier_values) if brier_values else None,
        log_loss=sum(log_loss_values) / len(log_loss_values)
        if log_loss_values
        else None,
        calibration_error=sum(calibration_values) / len(calibration_values)
        if calibration_values
        else None,
        market_relative_mean_abs_difference=sum(market_values) / len(market_values)
        if market_values
        else None,
        market_comparison_count=len(market_values),
        closing_comparison_count=len(closing_values),
        clv_style_diagnostic=sum(closing_values) / len(closing_values)
        if closing_values
        else None,
    )
    metrics.validate()
    return metrics


def failure_taxonomy(bundle: ShadowEvidenceBundle) -> Mapping[str, int]:
    bundle.validate()
    return dict(
        sorted(
            Counter(
                FailureCode(item.code).value for item in bundle.failure_evidence
            ).items()
        )
    )


CEO_UNRESOLVED_DECISIONS = (
    "production_model_and_artifact",
    "signal_time_numeric_configuration",
    "provider_authority",
    "result_authority",
    "quota_and_cost_budget",
    "minimum_shadow_observation_requirement",
    "activation_league_order_and_scope",
    "publication_policy",
)


@dataclass(frozen=True)
class ShadowValidationAssessment:
    state: GateState
    evidence_digest: str | None
    coverage: CoverageReport
    provider_assessment: tuple[ProviderAssessment, ...]
    signal_time_comparison: SignalTimeComparison | None
    performance: PerformanceMetrics
    failure_taxonomy: Mapping[str, int]
    blockers: tuple[str, ...]
    unresolved_decisions: tuple[str, ...] = CEO_UNRESOLVED_DECISIONS
    recommendation: str = "NO_ACTIVATION"

    def validate(self) -> None:
        self.coverage.validate()
        for item in self.provider_assessment:
            item.validate()
        if self.signal_time_comparison is not None:
            self.signal_time_comparison.validate()
        self.performance.validate()
        if (
            self.state is GateState.APPROVED_FOR_CONTROLLED_ACTIVATION
            and self.recommendation == "NO_ACTIVATION"
        ):
            raise ShadowSafetyRejection(
                "approval state requires an explicit CEO decision record"
            )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "state": self.state.value,
            "evidence_digest": self.evidence_digest,
            "coverage": self.coverage.as_payload(),
            "provider_assessment": [
                item.as_payload() for item in self.provider_assessment
            ],
            "signal_time_comparison": self.signal_time_comparison.as_payload()
            if self.signal_time_comparison
            else None,
            "performance": self.performance.as_payload(),
            "failure_taxonomy": dict(self.failure_taxonomy),
            "blockers": list(self.blockers),
            "unresolved_decisions": list(self.unresolved_decisions),
            "recommendation": self.recommendation,
        }


def _empty_assessment(state: GateState, *blockers: str) -> ShadowValidationAssessment:
    result = ShadowValidationAssessment(
        state=state,
        evidence_digest=None,
        coverage=CoverageReport(()),
        provider_assessment=(),
        signal_time_comparison=None,
        performance=PerformanceMetrics(0, 0, None, None, None, None, 0, 0, None),
        failure_taxonomy={},
        blockers=tuple(blockers),
    )
    result.validate()
    return result


def evidence_digest(bundle: ShadowEvidenceBundle) -> str:
    payload = json.dumps(
        bundle.as_payload(), sort_keys=True, separators=(",", ":"), default=str
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def assess_shadow_evidence(
    bundle: ShadowEvidenceBundle | Mapping[str, object],
    *,
    minimum_performance_sample: int = 1,
) -> ShadowValidationAssessment:
    if isinstance(bundle, Mapping):
        if not bundle:
            return _empty_assessment(GateState.NO_EVIDENCE, "no evidence supplied")
        try:
            bundle = ShadowEvidenceBundle.from_payload(bundle)
        except ShadowSafetyRejection as exc:
            return _empty_assessment(GateState.REJECTED_SAFETY, str(exc))
        except ShadowValidationError as exc:
            return _empty_assessment(GateState.EVIDENCE_INCOMPLETE, str(exc))
    if bundle.record_count == 0:
        return _empty_assessment(GateState.NO_EVIDENCE, "no evidence supplied")
    try:
        bundle.validate()
    except ShadowSafetyRejection as exc:
        return _empty_assessment(GateState.REJECTED_SAFETY, str(exc))
    except ShadowValidationError as exc:
        return _empty_assessment(GateState.EVIDENCE_INCOMPLETE, str(exc))
    coverage = validate_coverage(bundle)
    provider = evaluate_provider_evidence(bundle)
    signal_time = compare_signal_time_evidence(bundle)
    performance = calculate_performance_metrics(bundle)
    blockers: list[str] = []
    if not bundle.observations:
        blockers.append("shadow observations are missing")
    if bundle.observations and not bundle.predictions:
        return ShadowValidationAssessment(
            GateState.OBSERVING,
            evidence_digest(bundle),
            coverage,
            provider,
            signal_time,
            performance,
            failure_taxonomy(bundle),
            ("prediction artifacts are not yet present",),
        )
    if not bundle.provider_evidence:
        blockers.append("provider evidence is missing")
        state = GateState.PROVIDER_VALIDATION_PENDING
    elif not bundle.signal_time_evidence:
        blockers.append("signal-time evidence is missing")
        state = GateState.SIGNAL_TIME_VALIDATION_PENDING
    elif not bundle.quota_cost_evidence or not bundle.health_evidence:
        if not bundle.quota_cost_evidence:
            blockers.append("quota/cost evidence is missing")
        if not bundle.health_evidence:
            blockers.append("health evidence is missing")
        state = GateState.EVIDENCE_INCOMPLETE
    elif performance.resolved_result_count < minimum_performance_sample:
        blockers.append(
            f"performance sample is {performance.resolved_result_count}; "
            f"minimum is {minimum_performance_sample}"
        )
        state = GateState.SHADOW_PERFORMANCE_PENDING
    else:
        state = GateState.READY_FOR_CEO_GATE
    blocking_failures = tuple(
        item.message for item in bundle.failure_evidence if item.blocking
    )
    blockers.extend(blocking_failures)
    if blocking_failures and state is GateState.READY_FOR_CEO_GATE:
        state = GateState.SHADOW_PERFORMANCE_PENDING
    result = ShadowValidationAssessment(
        state=state,
        evidence_digest=evidence_digest(bundle),
        coverage=coverage,
        provider_assessment=provider,
        signal_time_comparison=signal_time,
        performance=performance,
        failure_taxonomy=failure_taxonomy(bundle),
        blockers=tuple(blockers),
    )
    result.validate()
    return result


@dataclass(frozen=True)
class CEOAuthorization:
    decision_id: str
    approved: bool
    evidence_digest: str
    scope: str
    decided_at: datetime | None
    reason: str

    def validate(self, *, expected_digest: str | None = None) -> None:
        _require_text(self.decision_id, "decision_id")
        _require_text(self.evidence_digest, "evidence_digest")
        _require_text(self.scope, "scope")
        _require_text(self.reason, "reason")
        _utc(self.decided_at, "decided_at")
        if expected_digest is not None and self.evidence_digest != expected_digest:
            raise ShadowSafetyRejection(
                "CEO authorization does not match the evidence digest"
            )
        if not isinstance(self.approved, bool):
            raise ShadowValidationError("CEO approval must be boolean")


class ShadowValidationGate:
    """State transitions for validation only; no activation operation exists."""

    @staticmethod
    def assess(
        bundle: ShadowEvidenceBundle | Mapping[str, object],
        *,
        minimum_performance_sample: int = 1,
    ) -> ShadowValidationAssessment:
        return assess_shadow_evidence(
            bundle, minimum_performance_sample=minimum_performance_sample
        )

    @staticmethod
    def require_ceo_decision(
        assessment: ShadowValidationAssessment,
    ) -> ShadowValidationAssessment:
        assessment.validate()
        if assessment.state is not GateState.READY_FOR_CEO_GATE:
            raise ShadowValidationError("evidence is not ready for the CEO gate")
        return replace(assessment, state=GateState.CEO_DECISION_REQUIRED)

    @staticmethod
    def apply_ceo_authorization(
        assessment: ShadowValidationAssessment,
        authorization: CEOAuthorization,
    ) -> ShadowValidationAssessment:
        assessment.validate()
        if assessment.state is not GateState.CEO_DECISION_REQUIRED:
            raise ShadowValidationError(
                "CEO authorization can only be applied at the CEO gate"
            )
        authorization.validate(expected_digest=assessment.evidence_digest)
        if not authorization.approved:
            return replace(
                assessment,
                blockers=assessment.blockers
                + ("CEO did not authorize controlled activation",),
            )
        result = replace(
            assessment,
            state=GateState.APPROVED_FOR_CONTROLLED_ACTIVATION,
            recommendation="CEO_AUTHORIZED_VALIDATION_ONLY",
        )
        result.validate()
        return result


@dataclass(frozen=True)
class CEOReviewPacket:
    evidence_window: Mapping[str, str | None]
    evidence_digest: str
    leagues_covered: tuple[str, ...]
    fixture_counts: Mapping[str, Mapping[str, int]]
    provider_assessment: tuple[ProviderAssessment, ...]
    timing_comparison: SignalTimeComparison | None
    quota_cost_assessment: tuple[QuotaCostEvidence, ...]
    coverage: CoverageReport
    freshness: Mapping[str, float]
    failure_taxonomy: Mapping[str, int]
    result_completeness: Mapping[str, float | int]
    performance_metrics: PerformanceMetrics
    closing_clv_diagnostics: Mapping[str, object]
    unresolved_decisions: tuple[str, ...]
    blockers: tuple[str, ...]
    recommendation: str

    def validate(self) -> None:
        _require_text(self.evidence_digest, "evidence_digest")
        if self.recommendation not in {
            "CEO_DECISION_REQUIRED",
            "NO_ACTIVATION",
            "CEO_AUTHORIZED_VALIDATION_ONLY",
        }:
            raise ShadowSafetyRejection(
                "CEO packet contains an invalid activation recommendation"
            )
        self.coverage.validate()
        self.performance_metrics.validate()
        if self.timing_comparison is not None:
            self.timing_comparison.validate()

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "evidence_window": dict(self.evidence_window),
            "evidence_digest": self.evidence_digest,
            "leagues_covered": list(self.leagues_covered),
            "fixture_counts": {
                key: dict(value) for key, value in sorted(self.fixture_counts.items())
            },
            "provider_assessment": [
                item.as_payload() for item in self.provider_assessment
            ],
            "timing_comparison": self.timing_comparison.as_payload()
            if self.timing_comparison
            else None,
            "quota_cost_assessment": [
                _quota_payload(item) for item in self.quota_cost_assessment
            ],
            "coverage": self.coverage.as_payload(),
            "freshness": dict(sorted(self.freshness.items())),
            "failure_taxonomy": dict(sorted(self.failure_taxonomy.items())),
            "result_completeness": dict(self.result_completeness),
            "performance_metrics": self.performance_metrics.as_payload(),
            "closing_clv_diagnostics": dict(self.closing_clv_diagnostics),
            "unresolved_decisions": list(self.unresolved_decisions),
            "blockers": list(self.blockers),
            "recommendation": self.recommendation,
        }


def build_ceo_review_packet(
    bundle: ShadowEvidenceBundle,
    assessment: ShadowValidationAssessment | None = None,
) -> CEOReviewPacket:
    bundle.validate()
    if assessment is None:
        assessment = assess_shadow_evidence(bundle)
    coverage = assessment.coverage
    fixture_counts = {
        item.league_code: {
            "discovered": item.discovered_fixtures,
            "eligible": item.eligible_fixtures,
            "valid_odds": item.valid_odds,
            "predictions": item.predictions,
            "rejected": item.rejected_fixtures,
        }
        for item in coverage.by_league
    }
    freshness = {
        item.league_code: item.freshness_rate for item in assessment.provider_assessment
    }
    resolved = assessment.performance.resolved_result_count
    packet = CEOReviewPacket(
        evidence_window={
            "start": _iso(bundle.evidence_window_start),
            "end": _iso(bundle.evidence_window_end),
        },
        evidence_digest=assessment.evidence_digest or evidence_digest(bundle),
        leagues_covered=tuple(sorted(fixture_counts)),
        fixture_counts=fixture_counts,
        provider_assessment=assessment.provider_assessment,
        timing_comparison=assessment.signal_time_comparison,
        quota_cost_assessment=bundle.quota_cost_evidence,
        coverage=coverage,
        freshness=freshness,
        failure_taxonomy=assessment.failure_taxonomy,
        result_completeness={
            "prediction_count": assessment.performance.sample_size,
            "resolved_result_count": resolved,
            "resolution_rate": resolved / assessment.performance.sample_size
            if assessment.performance.sample_size
            else 0.0,
        },
        performance_metrics=assessment.performance,
        closing_clv_diagnostics={
            "benchmark_count": assessment.performance.closing_comparison_count,
            "clv_style_diagnostic": assessment.performance.clv_style_diagnostic,
            "benchmark_only": True,
        },
        unresolved_decisions=assessment.unresolved_decisions,
        blockers=assessment.blockers,
        recommendation=(
            "CEO_DECISION_REQUIRED"
            if assessment.state
            in {GateState.READY_FOR_CEO_GATE, GateState.CEO_DECISION_REQUIRED}
            else "NO_ACTIVATION"
        ),
    )
    packet.validate()
    return packet


def ingest_shadow_evidence(
    payload: ShadowEvidenceBundle | Mapping[str, object],
    *,
    minimum_performance_sample: int = 1,
) -> ShadowValidationAssessment:
    """Deterministically ingest external evidence without executing anything."""

    return assess_shadow_evidence(
        payload, minimum_performance_sample=minimum_performance_sample
    )


def _records(
    payload: Mapping[str, object], key: str
) -> tuple[Mapping[str, object], ...]:
    value = payload.get(key, ())
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ShadowValidationError(f"{key} must be a list of evidence records")
    if any(not isinstance(item, Mapping) for item in value):
        raise ShadowValidationError(f"{key} contains a non-mapping record")
    return tuple(value)  # type: ignore[arg-type]


def _provenance(record: Mapping[str, object]) -> EvidenceProvenance:
    raw = record.get("provenance", {})
    if not isinstance(raw, Mapping):
        raw = {}
    return EvidenceProvenance(
        evidence_id=raw.get("evidence_id", ""),  # type: ignore[arg-type]
        artifact_id=raw.get("artifact_id", ""),  # type: ignore[arg-type]
        artifact_sha=raw.get("artifact_sha", ""),  # type: ignore[arg-type]
        source_sha=raw.get("source_sha", ""),  # type: ignore[arg-type]
        research_sha=raw.get("research_sha", ""),  # type: ignore[arg-type]
        league_code=raw.get("league_code", ""),  # type: ignore[arg-type]
        fixture_key=raw.get("fixture_key"),  # type: ignore[arg-type]
        candidate_id=raw.get("candidate_id", ""),  # type: ignore[arg-type]
        model_identity=raw.get("model_identity", ""),  # type: ignore[arg-type]
        feature_schema_hash=raw.get("feature_schema_hash"),  # type: ignore[arg-type]
        generated_at=_parse_datetime(raw.get("generated_at")),
        contract_version=raw.get("contract_version", TOP5_VALIDATION_CONTRACT_VERSION),  # type: ignore[arg-type]
    )


def _observation_from_payload(
    record: Mapping[str, object],
) -> ShadowObservationEvidence:
    return ShadowObservationEvidence(
        provenance=_provenance(record),
        discovered=record.get("discovered", False),
        eligible=record.get("eligible", False),
        valid_odds=record.get("valid_odds", False),
        prediction_id=record.get("prediction_id"),  # type: ignore[arg-type]
        rejected=record.get("rejected", False),
        rejected_reason=record.get("rejected_reason"),  # type: ignore[arg-type]
        provider_covered=record.get("provider_covered", False),
        stale=record.get("stale", False),
        fallback_used=record.get("fallback_used", False),
        error=record.get("error", False),
        duplicate_suppressed=record.get("duplicate_suppressed", False),
        result_resolved=record.get("result_resolved", False),
        no_bet=record.get("no_bet"),
        publication_enabled=record.get("publication_enabled"),
    )


def _prediction_from_payload(record: Mapping[str, object]) -> PredictionEvidence:
    return PredictionEvidence(
        provenance=_provenance(record),
        prediction_id=record.get("prediction_id", ""),  # type: ignore[arg-type]
        signal_snapshot_id=record.get("signal_snapshot_id", ""),  # type: ignore[arg-type]
        input_snapshot_kinds=_tuple_strings(record.get("input_snapshot_kinds")),
        probabilities=record.get("probabilities", {}),  # type: ignore[arg-type]
        market_probabilities=record.get("market_probabilities", {}),  # type: ignore[arg-type]
        closing_snapshot_ids=_tuple_strings(record.get("closing_snapshot_ids")),
        no_bet=record.get("no_bet"),
        publication_enabled=record.get("publication_enabled"),
    )


def _provider_from_payload(record: Mapping[str, object]) -> ProviderEvidence:
    return ProviderEvidence(
        provenance=_provenance(record),
        provider_name=record.get("provider_name", ""),  # type: ignore[arg-type]
        outcome=record.get("outcome", ""),  # type: ignore[arg-type]
        requested_fixture_count=record.get("requested_fixture_count", 0),  # type: ignore[arg-type]
        covered_fixture_count=record.get("covered_fixture_count", 0),  # type: ignore[arg-type]
        availability=record.get("availability", False),
        latency_ms=record.get("latency_ms", 0),  # type: ignore[arg-type]
        odds_age_seconds=record.get("odds_age_seconds"),  # type: ignore[arg-type]
        maximum_odds_age_seconds=record.get("maximum_odds_age_seconds"),  # type: ignore[arg-type]
        bulk_requests=record.get("bulk_requests", 0),  # type: ignore[arg-type]
        fallback_requests=record.get("fallback_requests", 0),  # type: ignore[arg-type]
        retry_count=record.get("retry_count", 0),  # type: ignore[arg-type]
        bulk_reused=record.get("bulk_reused", False),
        stale_rejections=record.get("stale_rejections", 0),  # type: ignore[arg-type]
        wrong_market_rejections=record.get("wrong_market_rejections", 0),  # type: ignore[arg-type]
        wrong_fixture_rejections=record.get("wrong_fixture_rejections", 0),  # type: ignore[arg-type]
    )


def _signal_time_from_payload(record: Mapping[str, object]) -> SignalTimeEvidence:
    return SignalTimeEvidence(
        provenance=_provenance(record),
        candidate_name=record.get("candidate_name", ""),  # type: ignore[arg-type]
        eligible=record.get("eligible", False),
        odds_age_seconds=record.get("odds_age_seconds", 0),  # type: ignore[arg-type]
        maximum_odds_age_seconds=record.get("maximum_odds_age_seconds", 1),  # type: ignore[arg-type]
        request_load=record.get("request_load", 0),  # type: ignore[arg-type]
        fallback_used=record.get("fallback_used", False),
        stale_rejected=record.get("stale_rejected", False),
        latency_ms=record.get("latency_ms", 0),  # type: ignore[arg-type]
        quota_cost_units=record.get("quota_cost_units", 0),  # type: ignore[arg-type]
        operational_complexity=record.get("operational_complexity", 0),  # type: ignore[arg-type]
    )


def _quota_from_payload(record: Mapping[str, object]) -> QuotaCostEvidence:
    return QuotaCostEvidence(
        provenance=_provenance(record),
        horizon=record.get("horizon", ""),  # type: ignore[arg-type]
        total_fixture_count=record.get("total_fixture_count", 0),  # type: ignore[arg-type]
        logical_evaluations=record.get("logical_evaluations", 0),  # type: ignore[arg-type]
        bulk_odds_requests=record.get("bulk_odds_requests", 0),  # type: ignore[arg-type]
        fallback_event_requests=record.get("fallback_event_requests", 0),  # type: ignore[arg-type]
        result_requests=record.get("result_requests", 0),  # type: ignore[arg-type]
        revalidation_requests=record.get("revalidation_requests", 0),  # type: ignore[arg-type]
        closing_capture_requests=record.get("closing_capture_requests", 0),  # type: ignore[arg-type]
        raw_http_requests=record.get("raw_http_requests", 0),  # type: ignore[arg-type]
        provider_cost_units=record.get("provider_cost_units", 0),  # type: ignore[arg-type]
    )


def _health_from_payload(record: Mapping[str, object]) -> HealthEvidence:
    return HealthEvidence(
        provenance=_provenance(record),
        provider_health=record.get("provider_health", ""),  # type: ignore[arg-type]
        inference_health=record.get("inference_health", ""),  # type: ignore[arg-type]
        publisher_health=record.get("publisher_health", ""),  # type: ignore[arg-type]
        result_source_health=record.get("result_source_health", ""),  # type: ignore[arg-type]
        activation_state=record.get("activation_state", ""),  # type: ignore[arg-type]
        registered=record.get("registered", False),
        no_bet=record.get("no_bet", False),
        publication_enabled=record.get("publication_enabled", True),
    )


def _result_from_payload(record: Mapping[str, object]) -> ResultAttachment:
    return ResultAttachment(
        provenance=_provenance(record),
        prediction_id=record.get("prediction_id", ""),  # type: ignore[arg-type]
        actual_outcome=record.get("actual_outcome"),  # type: ignore[arg-type]
        resolved=record.get("resolved", False),
        result_source=record.get("result_source", ""),  # type: ignore[arg-type]
        result_delay_seconds=record.get("result_delay_seconds", 0),  # type: ignore[arg-type]
    )


def _closing_from_payload(record: Mapping[str, object]) -> ClosingBenchmarkEvidence:
    return ClosingBenchmarkEvidence(
        provenance=_provenance(record),
        prediction_id=record.get("prediction_id", ""),  # type: ignore[arg-type]
        signal_snapshot_id=record.get("signal_snapshot_id", ""),  # type: ignore[arg-type]
        closing_snapshot_id=record.get("closing_snapshot_id", ""),  # type: ignore[arg-type]
        signal_probabilities=record.get("signal_probabilities", {}),  # type: ignore[arg-type]
        closing_probabilities=record.get("closing_probabilities", {}),  # type: ignore[arg-type]
        used_for_prediction=record.get("used_for_prediction", False),
        clv_style_value=record.get("clv_style_value"),  # type: ignore[arg-type]
    )


def _failure_from_payload(record: Mapping[str, object]) -> FailureEvidence:
    return FailureEvidence(
        provenance=_provenance(record),
        code=record.get("code", ""),  # type: ignore[arg-type]
        message=record.get("message", ""),  # type: ignore[arg-type]
        blocking=record.get("blocking", True),
    )


def _observation_payload(item: ShadowObservationEvidence) -> dict[str, object]:
    item.validate()
    return {
        "provenance": item.provenance.as_payload(),
        "discovered": item.discovered,
        "eligible": item.eligible,
        "valid_odds": item.valid_odds,
        "prediction_id": item.prediction_id,
        "rejected": item.rejected,
        "rejected_reason": item.rejected_reason,
        "provider_covered": item.provider_covered,
        "stale": item.stale,
        "fallback_used": item.fallback_used,
        "error": item.error,
        "duplicate_suppressed": item.duplicate_suppressed,
        "result_resolved": item.result_resolved,
        "no_bet": item.no_bet,
        "publication_enabled": item.publication_enabled,
    }


def _provider_payload(item: ProviderEvidence) -> dict[str, object]:
    item.validate()
    return {
        "provenance": item.provenance.as_payload(),
        "provider_name": item.provider_name,
        "outcome": ProviderOutcome(item.outcome).value,
        "requested_fixture_count": item.requested_fixture_count,
        "covered_fixture_count": item.covered_fixture_count,
        "availability": item.availability,
        "latency_ms": item.latency_ms,
        "odds_age_seconds": item.odds_age_seconds,
        "maximum_odds_age_seconds": item.maximum_odds_age_seconds,
        "bulk_requests": item.bulk_requests,
        "fallback_requests": item.fallback_requests,
        "retry_count": item.retry_count,
        "bulk_reused": item.bulk_reused,
        "stale_rejections": item.stale_rejections,
        "wrong_market_rejections": item.wrong_market_rejections,
        "wrong_fixture_rejections": item.wrong_fixture_rejections,
    }


def _signal_time_payload(item: SignalTimeEvidence) -> dict[str, object]:
    item.validate()
    return {
        "provenance": item.provenance.as_payload(),
        "candidate_name": item.candidate_name,
        "eligible": item.eligible,
        "odds_age_seconds": item.odds_age_seconds,
        "maximum_odds_age_seconds": item.maximum_odds_age_seconds,
        "request_load": item.request_load,
        "fallback_used": item.fallback_used,
        "stale_rejected": item.stale_rejected,
        "latency_ms": item.latency_ms,
        "quota_cost_units": item.quota_cost_units,
        "operational_complexity": item.operational_complexity,
    }


def _quota_payload(item: QuotaCostEvidence) -> dict[str, object]:
    item.validate()
    return {
        "provenance": item.provenance.as_payload(),
        "horizon": item.horizon,
        "total_fixture_count": item.total_fixture_count,
        "logical_evaluations": item.logical_evaluations,
        "bulk_odds_requests": item.bulk_odds_requests,
        "fallback_event_requests": item.fallback_event_requests,
        "result_requests": item.result_requests,
        "revalidation_requests": item.revalidation_requests,
        "closing_capture_requests": item.closing_capture_requests,
        "raw_http_requests": item.raw_http_requests,
        "provider_cost_units": item.provider_cost_units,
    }


def _health_payload(item: HealthEvidence) -> dict[str, object]:
    item.validate()
    return {
        "provenance": item.provenance.as_payload(),
        "provider_health": item.provider_health,
        "inference_health": item.inference_health,
        "publisher_health": item.publisher_health,
        "result_source_health": item.result_source_health,
        "activation_state": item.activation_state,
        "registered": item.registered,
        "no_bet": item.no_bet,
        "publication_enabled": item.publication_enabled,
    }


def _result_payload(item: ResultAttachment) -> dict[str, object]:
    item.validate()
    return {
        "provenance": item.provenance.as_payload(),
        "prediction_id": item.prediction_id,
        "actual_outcome": item.actual_outcome,
        "resolved": item.resolved,
        "result_source": item.result_source,
        "result_delay_seconds": item.result_delay_seconds,
    }


def _closing_payload(item: ClosingBenchmarkEvidence) -> dict[str, object]:
    item.validate()
    return {
        "provenance": item.provenance.as_payload(),
        "prediction_id": item.prediction_id,
        "signal_snapshot_id": item.signal_snapshot_id,
        "closing_snapshot_id": item.closing_snapshot_id,
        "signal_probabilities": dict(item.signal_probabilities),
        "closing_probabilities": dict(item.closing_probabilities),
        "used_for_prediction": item.used_for_prediction,
        "clv_style_value": item.clv_style_value,
    }


def _failure_payload(item: FailureEvidence) -> dict[str, object]:
    item.validate()
    return {
        "provenance": item.provenance.as_payload(),
        "code": FailureCode(item.code).value,
        "message": item.message,
        "blocking": item.blocking,
    }
