"""Provider-neutral contracts for the durable Top-5 real-shadow lifecycle."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from types import MappingProxyType

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    SignalTimeContract,
    _utc,
)
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS
from src.football.top5_builder2_qualification_receipt import (
    Builder2QualificationReceiptError,
    validate_builder1_qualification_receipt,
)
from src.football.top5_dispatch import signal_time_contract_id
from src.football.top5_research_binding import FROZEN_RESEARCH_SHA, M5_CANDIDATE_ID

REAL_OBSERVED_MARKER = "REAL_OBSERVED"
OFFLINE_REPLAY_MARKER = "OFFLINE_REPLAY"
TEST_FIXTURE_MARKER = "TEST_FIXTURE"
REAL_SHADOW_SESSION_SCHEMA = "top5-real-shadow-session-v1"
REAL_SHADOW_SESSION_NAMESPACE = "football/top5/shadow_sessions/"
TOP5_REAL_SHADOW_LEAGUES = tuple(sorted(TOP5_LEAGUE_ADAPTERS))
_OUTCOMES = ("away", "draw", "home")
_SECRET_KEY_PARTS = (
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "response_body",
    "headers",
)
_CANONICAL_RECEIPT_FORBIDDEN_KEY_PARTS = (
    "api_key",
    "authorization_header",
    "cookie",
    "password",
    "secret",
    "token",
    "response_body",
    "headers",
    "credential",
)


class RealShadowContractError(ProductionContractError):
    """Raised when an observed shadow record crosses a hard boundary."""


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RealShadowContractError(f"{field_name} is required")
    return value.strip()


def _sha(value: object, field_name: str, *, minimum: int = 40) -> str:
    text = _required_text(value, field_name)
    if len(text) < minimum or len(text) > 64 or any(char not in "0123456789abcdef" for char in text.lower()):
        raise RealShadowContractError(f"{field_name} must be a hexadecimal digest")
    return text.lower()


def _parse_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        try:
            return _utc(value, field_name)
        except ProductionContractError as exc:
            raise RealShadowContractError(str(exc)) from exc
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RealShadowContractError(f"{field_name} is malformed") from exc
        return _parse_datetime(parsed, field_name)
    raise RealShadowContractError(f"{field_name} must be timezone-aware")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _metadata(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RealShadowContractError(f"{field_name} must be a mapping")
    def reject_secret_keys(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                lowered = str(key).lower()
                if any(part in lowered for part in _SECRET_KEY_PARTS):
                    raise RealShadowContractError(f"{field_name} contains a secret-bearing key")
                reject_secret_keys(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                reject_secret_keys(nested)

    reject_secret_keys(value)
    try:
        json.dumps(_thaw(value), allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise RealShadowContractError(f"{field_name} must be JSON-serializable") from exc
    return MappingProxyType(dict(_thaw(value)))


def _canonical_evidence(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RealShadowContractError(f"{field_name} must be a mapping")

    def reject_secret_keys(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                lowered = str(key).lower()
                if any(
                    lowered == part or lowered.endswith(f"_{part}")
                    for part in _CANONICAL_RECEIPT_FORBIDDEN_KEY_PARTS
                ):
                    raise RealShadowContractError(
                        f"{field_name} contains a secret-bearing key"
                    )
                reject_secret_keys(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                reject_secret_keys(nested)

    reject_secret_keys(value)
    try:
        thawed = _thaw(value)
        json.dumps(thawed, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise RealShadowContractError(
            f"{field_name} must be JSON-serializable"
        ) from exc
    return MappingProxyType(dict(thawed))


def _digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(_thaw(payload), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _market_probabilities(odds: Mapping[str, float]) -> Mapping[str, float]:
    inverse = {name: 1.0 / float(odds[name]) for name in _OUTCOMES}
    total = sum(inverse.values())
    return MappingProxyType({name: inverse[name] / total for name in _OUTCOMES})


@dataclass(frozen=True)
class RealShadowExperiment:
    """Caller-supplied timing values; no production timing defaults exist."""

    experiment_id: str
    minimum_lead_minutes: int
    maximum_lead_minutes: int
    maximum_odds_age_seconds: int
    kickoff_tolerance_seconds: int

    def validate(self) -> None:
        identity = _required_text(self.experiment_id, "experiment_id")
        if not identity.startswith("shadow-experiment:"):
            raise RealShadowContractError("experiment_id must use shadow-experiment namespace")
        values = (
            ("minimum_lead_minutes", self.minimum_lead_minutes),
            ("maximum_lead_minutes", self.maximum_lead_minutes),
            ("maximum_odds_age_seconds", self.maximum_odds_age_seconds),
            ("kickoff_tolerance_seconds", self.kickoff_tolerance_seconds),
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for _, value in values):
            raise RealShadowContractError("experiment timing values must be non-negative integers")
        if self.maximum_lead_minutes < self.minimum_lead_minutes:
            raise RealShadowContractError("maximum lead must be >= minimum lead")
        if self.maximum_odds_age_seconds <= 0:
            raise RealShadowContractError("maximum odds age must be positive")

    @property
    def signal_time(self) -> SignalTimeContract:
        self.validate()
        return SignalTimeContract(
            self.minimum_lead_minutes,
            self.maximum_lead_minutes,
            self.maximum_odds_age_seconds,
        )

    @property
    def contract_id(self) -> str:
        return signal_time_contract_id(self.signal_time)

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "experiment_id": self.experiment_id,
            "minimum_lead_minutes": self.minimum_lead_minutes,
            "maximum_lead_minutes": self.maximum_lead_minutes,
            "maximum_odds_age_seconds": self.maximum_odds_age_seconds,
            "kickoff_tolerance_seconds": self.kickoff_tolerance_seconds,
            "contract_id": self.contract_id,
        }


@dataclass(frozen=True)
class NormalizedProviderObservation:
    """Serialized observation seam with canonical Builder-2 admission evidence."""

    league_code: str
    fixture_key: str
    provider_fixture_id: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    market_type: str
    home_odds: float
    draw_odds: float
    away_odds: float
    provider_identity: str
    bookmaker_identity: str
    source_timestamp: datetime
    captured_at: datetime
    request_identity: str
    raw_record_digest: str
    adapter_version: str
    provider_priority: int
    fallback_depth: int
    cascade_trace: Mapping[str, object]
    quality_metadata: Mapping[str, object]
    eligibility_state: str
    signal_snapshot_id: str
    independent_validation: Mapping[str, object] | None
    observation_mode: str
    latency_ms: int = 0
    network_request_count: int = 1
    request_cost_units: float = 0.0
    observation_id: str | None = None
    qualification_session_id: str | None = None
    adapter_source_sha: str | None = None
    normalized_record_digest: str | None = None
    canonical_observation: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kickoff_utc", _parse_datetime(self.kickoff_utc, "kickoff_utc"))
        object.__setattr__(self, "source_timestamp", _parse_datetime(self.source_timestamp, "source_timestamp"))
        object.__setattr__(self, "captured_at", _parse_datetime(self.captured_at, "captured_at"))
        object.__setattr__(self, "cascade_trace", _metadata(self.cascade_trace, "cascade_trace"))
        object.__setattr__(self, "quality_metadata", _metadata(self.quality_metadata, "quality_metadata"))
        if self.independent_validation is not None:
            object.__setattr__(
                self,
                "independent_validation",
                _canonical_evidence(self.independent_validation, "independent_validation"),
            )
        if self.canonical_observation is not None:
            object.__setattr__(
                self,
                "canonical_observation",
                _canonical_evidence(self.canonical_observation, "canonical_observation"),
            )

    def validate(self, experiment: RealShadowExperiment) -> None:
        experiment.validate()
        texts = (
            ("league_code", self.league_code),
            ("fixture_key", self.fixture_key),
            ("provider_fixture_id", self.provider_fixture_id),
            ("home_team", self.home_team),
            ("away_team", self.away_team),
            ("market_type", self.market_type),
            ("provider_identity", self.provider_identity),
            ("bookmaker_identity", self.bookmaker_identity),
            ("request_identity", self.request_identity),
            ("adapter_version", self.adapter_version),
            ("signal_snapshot_id", self.signal_snapshot_id),
        )
        for name, value in texts:
            _required_text(value, name)
        if self.league_code not in TOP5_REAL_SHADOW_LEAGUES:
            raise RealShadowContractError("observation league is outside Top-5 scope")
        if self.home_team.strip().casefold() == self.away_team.strip().casefold():
            raise RealShadowContractError("fixture teams must be distinct")
        if self.market_type != "h2h_1x2":
            raise RealShadowContractError("only canonical h2h_1x2 observations are accepted")
        if self.source_timestamp > self.captured_at:
            raise RealShadowContractError("source timestamp is after capture timestamp")
        if self.captured_at > self.kickoff_utc + timedelta(seconds=experiment.kickoff_tolerance_seconds):
            raise RealShadowContractError("observation capture is after kickoff tolerance")
        odds = (self.home_odds, self.draw_odds, self.away_odds)
        if any(not isfinite(float(value)) or float(value) <= 1.0 for value in odds):
            raise RealShadowContractError("observation odds must be finite and greater than one")
        for name, value in (("provider_priority", self.provider_priority), ("fallback_depth", self.fallback_depth), ("latency_ms", self.latency_ms), ("network_request_count", self.network_request_count)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RealShadowContractError(f"{name} must be a non-negative integer")
        if not isfinite(float(self.request_cost_units)) or self.request_cost_units < 0:
            raise RealShadowContractError("request_cost_units must be finite and non-negative")
        if self.eligibility_state not in {"eligible", "rejected"}:
            raise RealShadowContractError("eligibility_state must be explicit")
        if self.observation_mode not in {REAL_OBSERVED_MARKER, TEST_FIXTURE_MARKER}:
            raise RealShadowContractError("observation mode must be explicit")
        _sha(self.raw_record_digest, "raw_record_digest")
        if self.eligibility_state == "eligible":
            if self.observation_mode == REAL_OBSERVED_MARKER:
                self._validate_builder2_acceptance()
            elif self.independent_validation is not None or self.canonical_observation is not None:
                raise RealShadowContractError(
                    "TEST_FIXTURE cannot carry real qualification evidence"
                )

    def _validate_builder2_acceptance(self) -> None:
        receipt = self.independent_validation
        canonical_payload = self.canonical_observation
        if not isinstance(receipt, Mapping) or not isinstance(canonical_payload, Mapping):
            raise RealShadowContractError(
                "REAL_OBSERVED requires the canonical Builder2QualificationReceiptV1 "
                "and its exact observation envelope"
            )
        try:
            if canonical_payload.get("evidence_kind") != REAL_OBSERVED_MARKER:
                raise RealShadowContractError(
                    "only REAL_OBSERVED Builder-2 evidence may admit a real session"
                )
            validate_builder1_qualification_receipt(
                receipt,
                expected_observation=canonical_payload,
            )
        except (Builder2QualificationReceiptError, ProductionContractError, TypeError, ValueError) as exc:
            raise RealShadowContractError(
                "REAL_OBSERVED requires an exact canonical Builder2QualificationReceiptV1"
            ) from exc

        bindings = (
            ("observation_id", self.observation_id, canonical_payload.get("observation_id")),
            (
                "qualification_session_id",
                self.qualification_session_id,
                canonical_payload.get("qualification_session_id"),
            ),
            ("fixture_key", self.fixture_key, canonical_payload.get("fixture_key")),
            ("provider_identity", self.provider_identity, canonical_payload.get("provider_identity")),
            ("provider_event_id", self.provider_fixture_id, canonical_payload.get("provider_event_id")),
            ("provider_request_id", self.request_identity, canonical_payload.get("provider_request_id")),
            ("league", self.league_code, canonical_payload.get("league")),
            ("home_team", self.home_team, canonical_payload.get("home_team")),
            ("away_team", self.away_team, canonical_payload.get("away_team")),
            ("market_type", self.market_type, canonical_payload.get("market_type")),
            ("bookmaker_identity", self.bookmaker_identity, canonical_payload.get("bookmaker_identity")),
            ("adapter_version", self.adapter_version, canonical_payload.get("adapter_version")),
            ("adapter_source_sha", self.adapter_source_sha, canonical_payload.get("adapter_source_sha")),
            ("raw_record_digest", self.raw_record_digest.lower(), str(canonical_payload.get("raw_response_digest", "")).lower()),
            (
                "normalized_record_digest",
                self.normalized_record_digest,
                canonical_payload.get("normalized_record_digest"),
            ),
            ("latency_ms", self.latency_ms, canonical_payload.get("latency_ms")),
            ("network_request_count", self.network_request_count, canonical_payload.get("network_request_count")),
        )
        if any(local != expected for _, local, expected in bindings):
            raise RealShadowContractError(
                "local observation is not exactly bound to canonical Builder-2 evidence"
            )
        for name, local, expected in (
            (
                "kickoff",
                self.kickoff_utc,
                _parse_datetime(canonical_payload.get("kickoff"), "canonical kickoff"),
            ),
            (
                "source_timestamp",
                self.source_timestamp,
                _parse_datetime(
                    canonical_payload.get("source_timestamp"),
                    "canonical source_timestamp",
                ),
            ),
            (
                "captured_at",
                self.captured_at,
                _parse_datetime(canonical_payload.get("captured_at"), "canonical captured_at"),
            ),
        ):
            if _utc(local, name) != _utc(expected, name):
                raise RealShadowContractError(
                    "local observation is not exactly bound to canonical Builder-2 evidence"
                )
        for name, local, expected in (
            ("home_odds", self.home_odds, canonical_payload.get("home_odds")),
            ("draw_odds", self.draw_odds, canonical_payload.get("draw_odds")),
            ("away_odds", self.away_odds, canonical_payload.get("away_odds")),
        ):
            if float(local) != float(expected):
                raise RealShadowContractError(
                    "local observation is not exactly bound to canonical Builder-2 evidence"
                )

    def fixture(self) -> Fixture:
        return Fixture(self.fixture_key, self.league_code, self.home_team, self.away_team, self.kickoff_utc)

    def snapshot(self) -> MarketSnapshot:
        return MarketSnapshot(
            fixture_key=self.fixture_key,
            captured_at=self.source_timestamp,
            kind=MarketSnapshotKind.SIGNAL_TIME,
            source=f"real_observed:{self.provider_identity}:{self.bookmaker_identity}",
            odds={"home": self.home_odds, "draw": self.draw_odds, "away": self.away_odds},
            snapshot_id=self.signal_snapshot_id,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "league_code": self.league_code,
            "fixture_key": self.fixture_key,
            "provider_fixture_id": self.provider_fixture_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff_utc": self.kickoff_utc.isoformat(),
            "market_type": self.market_type,
            "home_odds": self.home_odds,
            "draw_odds": self.draw_odds,
            "away_odds": self.away_odds,
            "provider_identity": self.provider_identity,
            "bookmaker_identity": self.bookmaker_identity,
            "source_timestamp": self.source_timestamp.isoformat(),
            "captured_at": self.captured_at.isoformat(),
            "request_identity": self.request_identity,
            "raw_record_digest": self.raw_record_digest.lower(),
            "adapter_version": self.adapter_version,
            "provider_priority": self.provider_priority,
            "fallback_depth": self.fallback_depth,
            "cascade_trace": _thaw(self.cascade_trace),
            "quality_metadata": _thaw(self.quality_metadata),
            "eligibility_state": self.eligibility_state,
            "signal_snapshot_id": self.signal_snapshot_id,
            "independent_validation": _thaw(self.independent_validation),
            "observation_mode": self.observation_mode,
            "latency_ms": self.latency_ms,
            "network_request_count": self.network_request_count,
            "request_cost_units": self.request_cost_units,
            "observation_id": self.observation_id,
            "qualification_session_id": self.qualification_session_id,
            "adapter_source_sha": self.adapter_source_sha,
            "normalized_record_digest": self.normalized_record_digest,
            "canonical_observation": _thaw(self.canonical_observation),
        }

    @classmethod
    def from_payload(cls, payload: object) -> NormalizedProviderObservation:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(
            league_code=raw.get("league_code", ""), fixture_key=raw.get("fixture_key", ""),
            provider_fixture_id=raw.get("provider_fixture_id", ""), home_team=raw.get("home_team", ""),
            away_team=raw.get("away_team", ""), kickoff_utc=_parse_datetime(raw.get("kickoff_utc"), "kickoff_utc"),
            market_type=raw.get("market_type", ""), home_odds=raw.get("home_odds"), draw_odds=raw.get("draw_odds"),
            away_odds=raw.get("away_odds"), provider_identity=raw.get("provider_identity", ""),
            bookmaker_identity=raw.get("bookmaker_identity", ""), source_timestamp=_parse_datetime(raw.get("source_timestamp"), "source_timestamp"),
            captured_at=_parse_datetime(raw.get("captured_at"), "captured_at"), request_identity=raw.get("request_identity", ""),
            raw_record_digest=raw.get("raw_record_digest", ""), adapter_version=raw.get("adapter_version", ""),
            provider_priority=raw.get("provider_priority", -1), fallback_depth=raw.get("fallback_depth", -1),
            cascade_trace=raw.get("cascade_trace", {}), quality_metadata=raw.get("quality_metadata", {}),
            eligibility_state=raw.get("eligibility_state", ""), signal_snapshot_id=raw.get("signal_snapshot_id", ""),
            independent_validation=raw.get("independent_validation"), latency_ms=raw.get("latency_ms", 0),
            observation_mode=raw.get("observation_mode", ""),
            network_request_count=raw.get("network_request_count", 0), request_cost_units=raw.get("request_cost_units", 0.0),
            observation_id=raw.get("observation_id"),
            qualification_session_id=raw.get("qualification_session_id"),
            adapter_source_sha=raw.get("adapter_source_sha"),
            normalized_record_digest=raw.get("normalized_record_digest"),
            canonical_observation=raw.get("canonical_observation"),
        )

    @classmethod
    def from_builder2_package(
        cls,
        observation_payload: Mapping[str, object],
        receipt_payload: Mapping[str, object],
    ) -> NormalizedProviderObservation:
        """Project an accepted Builder-2 package into the B1 session seam."""

        try:
            from src.football.top5_controlled_shadow_provider_qualification import (
                RealProviderObservation,
            )
            from src.football.top5_provider_cascade_validation import CascadeEvidence

            canonical = RealProviderObservation.from_payload(observation_payload)
            canonical.validate_structural()
            if canonical.evidence_kind != REAL_OBSERVED_MARKER:
                raise RealShadowContractError(
                    "Builder-2 package is not REAL_OBSERVED"
                )
            receipt = validate_builder1_qualification_receipt(
                receipt_payload,
                expected_observation=canonical,
            )
            cascade = CascadeEvidence.from_payload(canonical.cascade_evidence)
            cascade.validate_structural()
            selected = [
                attempt
                for attempt in cascade.attempts
                if attempt.provider_identity == canonical.provider_identity
                and attempt.provider_record_id == canonical.provider_event_id
                and attempt.request_identity == canonical.provider_request_id
            ]
            if len(selected) != 1:
                raise RealShadowContractError(
                    "Builder-2 package must contain one exact selected attempt"
                )
            selected_attempt = selected[0]
            if (
                getattr(selected_attempt.outcome, "value", selected_attempt.outcome)
                != "SUCCESS"
                or selected_attempt.network_called is not True
                or canonical.source_timestamp is None
            ):
                raise RealShadowContractError(
                    "Builder-2 package selected attempt is not an observed success"
                )
            return cls(
                league_code=canonical.league,
                fixture_key=canonical.fixture_key,
                provider_fixture_id=canonical.provider_event_id,
                home_team=canonical.home_team,
                away_team=canonical.away_team,
                kickoff_utc=canonical.kickoff,
                market_type=canonical.market_type,
                home_odds=canonical.home_odds,
                draw_odds=canonical.draw_odds,
                away_odds=canonical.away_odds,
                provider_identity=canonical.provider_identity,
                bookmaker_identity=canonical.bookmaker_identity,
                source_timestamp=canonical.source_timestamp,
                captured_at=canonical.captured_at,
                request_identity=canonical.provider_request_id,
                raw_record_digest=canonical.raw_response_digest,
                adapter_version=canonical.adapter_version,
                provider_priority=selected_attempt.provider_attempt_index + 1,
                fallback_depth=selected_attempt.fallback_depth,
                cascade_trace={
                    "configured_provider_order": list(
                        cascade.configured_provider_order
                    ),
                    "attempts": [
                        attempt.provider_identity for attempt in cascade.attempts
                    ],
                    "selected": cascade.selected_provider,
                },
                quality_metadata={
                    "market": canonical.market_type,
                    "market_phase": canonical.market_phase,
                },
                eligibility_state="eligible",
                signal_snapshot_id=canonical.observation_id,
                independent_validation=receipt.as_payload(),
                observation_mode=REAL_OBSERVED_MARKER,
                latency_ms=canonical.latency_ms,
                network_request_count=canonical.network_request_count,
                request_cost_units=canonical.quota_cost_units,
                observation_id=canonical.observation_id,
                qualification_session_id=canonical.qualification_session_id,
                adapter_source_sha=canonical.adapter_source_sha,
                normalized_record_digest=canonical.normalized_record_digest,
                canonical_observation=canonical.as_payload(),
            )
        except (
            Builder2QualificationReceiptError,
            ProductionContractError,
            TypeError,
            ValueError,
        ) as exc:
            raise RealShadowContractError(
                "Builder-2 package cannot enter the real-shadow session"
            ) from exc

    def observation_digest(self) -> str:
        payload = self.as_payload()
        payload["independent_validation"] = None
        return _digest(payload)

    def cascade_trace_digest(self) -> str:
        return _digest(self.cascade_trace)


@dataclass(frozen=True)
class RealShadowPredictionArtifact:
    prediction_id: str
    session_id: str
    fixture_key: str
    league_code: str
    home_team: str
    away_team: str
    kickoff: datetime
    provider_identity: str
    bookmaker_identity: str
    provider_fixture_id: str
    observation_digest: str
    source_timestamp: datetime
    captured_at: datetime
    cascade_trace: Mapping[str, object]
    quality_metadata: Mapping[str, object]
    signal_snapshot_id: str
    signal_time_contract_id: str
    minimum_lead_minutes: int
    maximum_lead_minutes: int
    maximum_odds_age_seconds: int
    kickoff_tolerance_seconds: int
    model_identity: str
    research_sha: str
    integration_sha: str
    probabilities: Mapping[str, float]
    market_probabilities: Mapping[str, float]
    artifact_sha: str
    marker: str = REAL_OBSERVED_MARKER
    no_bet: bool = True
    publication: bool = False
    activation: bool = False

    def _payload(self) -> dict[str, object]:
        return {key: value for key, value in {
            "prediction_id": self.prediction_id, "session_id": self.session_id, "fixture_key": self.fixture_key,
            "league_code": self.league_code, "home_team": self.home_team, "away_team": self.away_team,
            "kickoff": self.kickoff.isoformat(), "provider_identity": self.provider_identity,
            "bookmaker_identity": self.bookmaker_identity, "provider_fixture_id": self.provider_fixture_id,
            "observation_digest": self.observation_digest, "source_timestamp": self.source_timestamp.isoformat(),
            "captured_at": self.captured_at.isoformat(), "cascade_trace": _thaw(self.cascade_trace),
            "quality_metadata": _thaw(self.quality_metadata), "signal_snapshot_id": self.signal_snapshot_id,
            "signal_time_contract_id": self.signal_time_contract_id, "minimum_lead_minutes": self.minimum_lead_minutes,
            "maximum_lead_minutes": self.maximum_lead_minutes, "maximum_odds_age_seconds": self.maximum_odds_age_seconds,
            "kickoff_tolerance_seconds": self.kickoff_tolerance_seconds, "model_identity": self.model_identity,
            "research_sha": self.research_sha, "integration_sha": self.integration_sha,
            "probabilities": dict(self.probabilities), "market_probabilities": dict(self.market_probabilities),
            "marker": self.marker, "no_bet": self.no_bet, "publication": self.publication, "activation": self.activation,
        }.items()}

    def validate(self) -> None:
        for name, value in (("prediction_id", self.prediction_id), ("session_id", self.session_id), ("fixture_key", self.fixture_key), ("league_code", self.league_code), ("home_team", self.home_team), ("away_team", self.away_team), ("provider_identity", self.provider_identity), ("bookmaker_identity", self.bookmaker_identity), ("provider_fixture_id", self.provider_fixture_id), ("observation_digest", self.observation_digest), ("signal_snapshot_id", self.signal_snapshot_id)):
            _required_text(value, name)
        if self.league_code not in TOP5_REAL_SHADOW_LEAGUES or self.home_team.casefold() == self.away_team.casefold():
            raise RealShadowContractError("prediction fixture identity is invalid")
        for field_name, value in (("kickoff", self.kickoff), ("source_timestamp", self.source_timestamp), ("captured_at", self.captured_at)):
            _utc(value, field_name)
        if self.source_timestamp > self.captured_at:
            raise RealShadowContractError("prediction timestamps are not ordered")
        if self.model_identity != M5_CANDIDATE_ID or self.research_sha != FROZEN_RESEARCH_SHA:
            raise RealShadowContractError("prediction is not bound to frozen M5 research")
        _sha(self.integration_sha, "integration_sha")
        if self.marker not in {REAL_OBSERVED_MARKER, TEST_FIXTURE_MARKER} or not self.no_bet or self.publication or self.activation:
            raise RealShadowContractError("real-shadow prediction violates safety markers")
        if self.signal_time_contract_id != signal_time_contract_id(SignalTimeContract(self.minimum_lead_minutes, self.maximum_lead_minutes, self.maximum_odds_age_seconds)):
            raise RealShadowContractError("prediction signal-time contract is inconsistent")
        for name, value in (("minimum_lead_minutes", self.minimum_lead_minutes), ("maximum_lead_minutes", self.maximum_lead_minutes), ("maximum_odds_age_seconds", self.maximum_odds_age_seconds), ("kickoff_tolerance_seconds", self.kickoff_tolerance_seconds)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RealShadowContractError(f"{name} must be non-negative")
        if self.maximum_lead_minutes < self.minimum_lead_minutes or self.maximum_odds_age_seconds <= 0:
            raise RealShadowContractError("prediction signal-time values are invalid")
        for name, values in (("probabilities", self.probabilities), ("market_probabilities", self.market_probabilities)):
            if set(values) != set(_OUTCOMES) or any(not isfinite(float(values[key])) or not 0 <= float(values[key]) <= 1 for key in _OUTCOMES) or abs(sum(float(values[key]) for key in _OUTCOMES) - 1.0) > 1e-9:
                raise RealShadowContractError(f"{name} must contain normalized outcomes")
        _metadata(self.cascade_trace, "cascade_trace")
        _metadata(self.quality_metadata, "quality_metadata")
        if _digest(self._payload()) != self.artifact_sha:
            raise RealShadowContractError("real-shadow prediction digest changed")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {**self._payload(), "artifact_sha": self.artifact_sha}

    @classmethod
    def from_payload(cls, payload: object) -> RealShadowPredictionArtifact:
        raw = payload if isinstance(payload, Mapping) else {}
        return cls(
            prediction_id=raw.get("prediction_id", ""), session_id=raw.get("session_id", ""), fixture_key=raw.get("fixture_key", ""),
            league_code=raw.get("league_code", ""), home_team=raw.get("home_team", ""), away_team=raw.get("away_team", ""),
            kickoff=_parse_datetime(raw.get("kickoff"), "kickoff"), provider_identity=raw.get("provider_identity", ""),
            bookmaker_identity=raw.get("bookmaker_identity", ""), provider_fixture_id=raw.get("provider_fixture_id", ""),
            observation_digest=raw.get("observation_digest", ""), source_timestamp=_parse_datetime(raw.get("source_timestamp"), "source_timestamp"),
            captured_at=_parse_datetime(raw.get("captured_at"), "captured_at"), cascade_trace=raw.get("cascade_trace", {}),
            quality_metadata=raw.get("quality_metadata", {}), signal_snapshot_id=raw.get("signal_snapshot_id", ""),
            signal_time_contract_id=raw.get("signal_time_contract_id", ""), minimum_lead_minutes=raw.get("minimum_lead_minutes", -1),
            maximum_lead_minutes=raw.get("maximum_lead_minutes", -1), maximum_odds_age_seconds=raw.get("maximum_odds_age_seconds", -1),
            kickoff_tolerance_seconds=raw.get("kickoff_tolerance_seconds", -1), model_identity=raw.get("model_identity", ""),
            research_sha=raw.get("research_sha", ""), integration_sha=raw.get("integration_sha", ""),
            probabilities=raw.get("probabilities", {}), market_probabilities=raw.get("market_probabilities", {}),
            artifact_sha=raw.get("artifact_sha", ""), marker=raw.get("marker", ""), no_bet=raw.get("no_bet", False),
            publication=raw.get("publication", True), activation=raw.get("activation", True),
        )


__all__ = [
    "FROZEN_RESEARCH_SHA",
    "M5_CANDIDATE_ID",
    "OFFLINE_REPLAY_MARKER",
    "REAL_OBSERVED_MARKER",
    "REAL_SHADOW_SESSION_NAMESPACE",
    "REAL_SHADOW_SESSION_SCHEMA",
    "TEST_FIXTURE_MARKER",
    "TOP5_REAL_SHADOW_LEAGUES",
    "NormalizedProviderObservation",
    "RealShadowContractError",
    "RealShadowExperiment",
    "RealShadowPredictionArtifact",
]
