"""Versioned two-stage Top-5 signal lifecycle; no provider or activation side effects.

The lifecycle owns event-relative timing validation and immutable history only.
It is deliberately not registered with a scheduler, provider, publisher, or
production activation writer.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
from pathlib import Path
from types import MappingProxyType

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    SignalTimeContract,
    _utc,
)
from src.runtime.paths import runtime_state_path
from src.utils.atomic_io import atomic_write_json

LIFECYCLE_SCHEMA_VERSION = "top5-signal-lifecycle-v1"
LIFECYCLE_STORE_SCHEMA_VERSION = "top5-signal-lifecycle-store-v1"
LIFECYCLE_STATE_RELATIVE_DIR = "football/top5/signal_lifecycle"
EXPECTED_PROVIDER_IDENTITY = "the_odds_api"


class SignalLifecycleError(ProductionContractError):
    """Raised when a lifecycle transition or durable replay is invalid."""


class SignalLifecycleStage(str, Enum):
    INITIAL = "INITIAL"
    REFINED = "REFINED"
    WITHDRAWN = "WITHDRAWN"


class RefinementClassification(str, Enum):
    STRENGTHENED = "STRENGTHENED"
    WEAKENED = "WEAKENED"
    UNCHANGED = "UNCHANGED"
    WITHDRAWN = "WITHDRAWN"


class LifecyclePlanStatus(str, Enum):
    INITIAL_NOT_DUE = "INITIAL_NOT_DUE"
    INITIAL_DUE = "INITIAL_DUE"
    INITIAL_MISSED = "INITIAL_MISSED"
    WAITING_FOR_REFINEMENT = "WAITING_FOR_REFINEMENT"
    REFINEMENT_DUE = "REFINEMENT_DUE"
    REFINEMENT_MISSED = "REFINEMENT_MISSED"
    COMPLETE = "COMPLETE"
    WITHDRAWN = "WITHDRAWN"


def _canonical(value: object) -> object:
    if isinstance(value, datetime):
        return _utc(value, "timestamp").isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(
            _canonical(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SignalLifecycleError("lifecycle payload is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SignalLifecycleError(f"{name} must be non-empty text")
    return value.strip()


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise SignalLifecycleError(f"{name} must be boolean")
    return value


def _freeze_json(value: object, *, name: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise SignalLifecycleError(f"{name} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) or not key for key in value):
            raise SignalLifecycleError(f"{name} contains an invalid object key")
        return MappingProxyType(
            {key: _freeze_json(item, name=name) for key, item in sorted(value.items())}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item, name=name) for item in value)
    raise SignalLifecycleError(f"{name} contains a non-JSON value")


def _number_map(
    value: Mapping[str, float] | None,
    name: str,
    *,
    probability: bool = False,
    odds: bool = False,
) -> Mapping[str, float]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise SignalLifecycleError(f"{name} must be a mapping")
    normalized: dict[str, float] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key.strip():
            raise SignalLifecycleError(f"{name} contains an invalid identity")
        if isinstance(raw, bool):
            raise SignalLifecycleError(f"{name}.{key} must be numeric")
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SignalLifecycleError(f"{name}.{key} must be numeric") from exc
        if not isfinite(number):
            raise SignalLifecycleError(f"{name}.{key} must be finite")
        if probability and not 0 <= number <= 1:
            raise SignalLifecycleError(f"{name}.{key} must be in [0, 1]")
        if odds and number <= 1:
            raise SignalLifecycleError(f"{name}.{key} must be greater than 1")
        normalized[key.strip()] = number
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True)
class SignalLifecycleStagePolicy:
    minimum_minutes_before_kickoff: int
    maximum_minutes_before_kickoff: int
    maximum_odds_age_seconds: int
    retry_budget: int = 0

    def validate(self, name: str) -> None:
        if (
            isinstance(self.minimum_minutes_before_kickoff, bool)
            or not isinstance(self.minimum_minutes_before_kickoff, int)
            or self.minimum_minutes_before_kickoff < 0
        ):
            raise SignalLifecycleError(
                f"{name} minimum lead must be non-negative minutes"
            )
        if (
            isinstance(self.maximum_minutes_before_kickoff, bool)
            or not isinstance(self.maximum_minutes_before_kickoff, int)
            or self.maximum_minutes_before_kickoff < self.minimum_minutes_before_kickoff
        ):
            raise SignalLifecycleError(f"{name} maximum lead is invalid")
        if (
            isinstance(self.maximum_odds_age_seconds, bool)
            or not isinstance(self.maximum_odds_age_seconds, int)
            or self.maximum_odds_age_seconds <= 0
        ):
            raise SignalLifecycleError(f"{name} maximum odds age must be positive")
        if (
            isinstance(self.retry_budget, bool)
            or not isinstance(self.retry_budget, int)
            or self.retry_budget != 0
        ):
            raise SignalLifecycleError(f"{name} retry budget must remain zero")

    def as_payload(self) -> dict[str, int]:
        return {
            "minimum_minutes_before_kickoff": self.minimum_minutes_before_kickoff,
            "maximum_minutes_before_kickoff": self.maximum_minutes_before_kickoff,
            "maximum_odds_age_seconds": self.maximum_odds_age_seconds,
            "retry_budget": self.retry_budget,
        }


@dataclass(frozen=True)
class Top5SignalLifecycleContract:
    """Explicit two-stage product timing contract; existence grants no authority."""

    initial: SignalLifecycleStagePolicy = field(
        default_factory=lambda: SignalLifecycleStagePolicy(22 * 60, 26 * 60, 900, 0)
    )
    refinement: SignalLifecycleStagePolicy = field(
        default_factory=lambda: SignalLifecycleStagePolicy(60, 120, 900, 0)
    )
    schema_version: str = LIFECYCLE_SCHEMA_VERSION
    snapshot_kind: MarketSnapshotKind = MarketSnapshotKind.SIGNAL_TIME
    provider_authority_expectation: str = EXPECTED_PROVIDER_IDENTITY
    stage_order: tuple[str, str] = ("INITIAL", "REFINEMENT")
    model_identity_binding_rule: str = (
        "candidate-model-and-provenance-identical-across-lifecycle-versions"
    )
    no_closing_odds: bool = True
    signal_time_approved_for_production: bool = False

    def validate(self) -> None:
        if self.schema_version != LIFECYCLE_SCHEMA_VERSION:
            raise SignalLifecycleError("unsupported signal lifecycle schema")
        self.initial.validate("INITIAL")
        self.refinement.validate("REFINEMENT")
        if self.initial != SignalLifecycleStagePolicy(22 * 60, 26 * 60, 900, 0):
            raise SignalLifecycleError("v1 INITIAL timing policy is immutable")
        if self.refinement != SignalLifecycleStagePolicy(60, 120, 900, 0):
            raise SignalLifecycleError("v1 REFINEMENT timing policy is immutable")
        if self.snapshot_kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise SignalLifecycleError("lifecycle snapshots must be SIGNAL_TIME")
        if self.provider_authority_expectation != EXPECTED_PROVIDER_IDENTITY:
            raise SignalLifecycleError(
                "lifecycle provider expectation must remain the_odds_api"
            )
        if self.stage_order != ("INITIAL", "REFINEMENT"):
            raise SignalLifecycleError("lifecycle stage order is immutable")
        if (
            self.model_identity_binding_rule
            != "candidate-model-and-provenance-identical-across-lifecycle-versions"
        ):
            raise SignalLifecycleError("unsupported model identity binding rule")
        if self.no_closing_odds is not True:
            raise SignalLifecycleError("closing odds must remain excluded")
        if self.signal_time_approved_for_production is not False:
            raise SignalLifecycleError(
                "a lifecycle contract cannot approve production Signal-Time"
            )

    @property
    def contract_id(self) -> str:
        self.validate()
        return "top5-signal-lifecycle-v1-" + _digest(self.as_payload())

    def policy_for(self, stage: SignalLifecycleStage) -> SignalLifecycleStagePolicy:
        resolved = SignalLifecycleStage(stage)
        if resolved is SignalLifecycleStage.INITIAL:
            return self.initial
        if resolved in (SignalLifecycleStage.REFINED, SignalLifecycleStage.WITHDRAWN):
            return self.refinement
        raise SignalLifecycleError("unsupported lifecycle stage")

    def stage_contract_id(self, stage: SignalLifecycleStage) -> str:
        resolved = SignalLifecycleStage(stage)
        policy = self.policy_for(resolved)
        return "top5-signal-stage-v1-" + _digest(
            {
                "lifecycle_contract_id": self.contract_id,
                "stage": "INITIAL"
                if resolved is SignalLifecycleStage.INITIAL
                else "REFINEMENT",
                "policy": policy.as_payload(),
                "snapshot_kind": self.snapshot_kind.value,
                "provider_authority_expectation": self.provider_authority_expectation,
            }
        )

    def as_signal_time_contract(
        self, stage: SignalLifecycleStage, *, approval_ref: str | None = None
    ) -> SignalTimeContract:
        """Adapt one stage to the legacy single-window contract without approving it."""

        policy = self.policy_for(stage)
        contract = SignalTimeContract(
            minimum_minutes_before_kickoff=policy.minimum_minutes_before_kickoff,
            maximum_minutes_before_kickoff=policy.maximum_minutes_before_kickoff,
            maximum_odds_age_seconds=policy.maximum_odds_age_seconds,
            approval_ref=approval_ref,
        )
        contract.validate()
        return contract

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "initial": self.initial.as_payload(),
            "refinement": self.refinement.as_payload(),
            "snapshot_kind": MarketSnapshotKind(self.snapshot_kind).value,
            "provider_authority_expectation": self.provider_authority_expectation,
            "stage_order": list(self.stage_order),
            "model_identity_binding_rule": self.model_identity_binding_rule,
            "no_closing_odds": self.no_closing_odds,
            "signal_time_approved_for_production": False,
        }

    @classmethod
    def from_payload(cls, raw: object) -> Top5SignalLifecycleContract:
        if not isinstance(raw, Mapping):
            raise SignalLifecycleError("lifecycle contract must be an object")
        expected = {
            "schema_version",
            "initial",
            "refinement",
            "snapshot_kind",
            "provider_authority_expectation",
            "stage_order",
            "model_identity_binding_rule",
            "no_closing_odds",
            "signal_time_approved_for_production",
        }
        if set(raw) != expected:
            raise SignalLifecycleError("lifecycle contract fields are malformed")

        def parse_policy(name: str) -> SignalLifecycleStagePolicy:
            value = raw[name]
            if not isinstance(value, Mapping) or set(value) != {
                "minimum_minutes_before_kickoff",
                "maximum_minutes_before_kickoff",
                "maximum_odds_age_seconds",
                "retry_budget",
            }:
                raise SignalLifecycleError(f"{name} stage policy is malformed")
            return SignalLifecycleStagePolicy(**value)

        order = raw["stage_order"]
        if not isinstance(order, (list, tuple)):
            raise SignalLifecycleError("stage_order must be a sequence")
        contract = cls(
            initial=parse_policy("initial"),
            refinement=parse_policy("refinement"),
            schema_version=str(raw["schema_version"]),
            snapshot_kind=MarketSnapshotKind(raw["snapshot_kind"]),
            provider_authority_expectation=str(raw["provider_authority_expectation"]),
            stage_order=tuple(str(item) for item in order),  # type: ignore[arg-type]
            model_identity_binding_rule=str(raw["model_identity_binding_rule"]),
            no_closing_odds=raw["no_closing_odds"],
            signal_time_approved_for_production=raw[
                "signal_time_approved_for_production"
            ],
        )
        contract.validate()
        return contract


DEFAULT_SIGNAL_LIFECYCLE_CONTRACT = Top5SignalLifecycleContract()


def _logical_lifecycle_id(
    *,
    contract_id: str,
    league_code: str,
    fixture_key: str,
    market_id: str,
    outcome_id: str,
    candidate_id: str,
    model_identity: str,
) -> str:
    return "top5-signal-lifecycle-v1-" + _digest(
        {
            "schema_version": LIFECYCLE_SCHEMA_VERSION,
            "lifecycle_contract_id": contract_id,
            "league_code": league_code,
            "fixture_key": fixture_key,
            "market_id": market_id,
            "outcome_id": outcome_id,
            "candidate_id": candidate_id,
            "model_identity": model_identity,
        }
    )


@dataclass(frozen=True)
class Top5SignalLifecycleVersion:
    lifecycle_id: str
    version_number: int
    stage: SignalLifecycleStage
    league_code: str
    fixture_key: str
    kickoff: datetime
    market_id: str
    outcome_id: str
    candidate_id: str
    model_identity: str
    lifecycle_contract_id: str
    stage_contract_id: str
    provider_identity: str
    snapshot_id: str
    snapshot_kind: MarketSnapshotKind
    snapshot_source: str
    odds_captured_at: datetime
    prediction_generated_at: datetime
    probabilities: Mapping[str, float]
    market_odds: Mapping[str, float] = field(default_factory=dict)
    implied_probabilities: Mapping[str, float] = field(default_factory=dict)
    edges: Mapping[str, float] = field(default_factory=dict)
    confidence_metadata: Mapping[str, object] = field(default_factory=dict)
    source_sha: str = ""
    research_sha: str = ""
    model_artifact_hash: str = ""
    eligibility_decision: bool = True
    withdrawal_authorized: bool = False
    decision_id: str = ""
    decision_reason: str = ""
    classification: RefinementClassification | None = None
    predecessor_version_digest: str | None = None
    no_bet: bool = True
    publication_enabled: bool = False
    activation_enabled: bool = False
    version_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kickoff", _utc(self.kickoff, "kickoff"))
        object.__setattr__(
            self, "odds_captured_at", _utc(self.odds_captured_at, "odds_captured_at")
        )
        object.__setattr__(
            self,
            "prediction_generated_at",
            _utc(self.prediction_generated_at, "prediction_generated_at"),
        )
        object.__setattr__(self, "stage", SignalLifecycleStage(self.stage))
        object.__setattr__(
            self, "snapshot_kind", MarketSnapshotKind(self.snapshot_kind)
        )
        if self.classification is not None:
            object.__setattr__(
                self,
                "classification",
                RefinementClassification(self.classification),
            )
        object.__setattr__(
            self,
            "probabilities",
            _number_map(self.probabilities, "probabilities", probability=True),
        )
        object.__setattr__(
            self, "market_odds", _number_map(self.market_odds, "market_odds", odds=True)
        )
        object.__setattr__(
            self,
            "implied_probabilities",
            _number_map(
                self.implied_probabilities, "implied_probabilities", probability=True
            ),
        )
        object.__setattr__(self, "edges", _number_map(self.edges, "edges"))
        object.__setattr__(
            self,
            "confidence_metadata",
            _freeze_json(self.confidence_metadata, name="confidence_metadata"),
        )
        if not self.version_digest:
            object.__setattr__(
                self, "version_digest", _digest(self._payload(include_digest=False))
            )

    @property
    def idempotency_key(self) -> str:
        return _digest(
            {
                "lifecycle_id": self.lifecycle_id,
                "stage": "INITIAL"
                if self.stage is SignalLifecycleStage.INITIAL
                else "REFINEMENT",
                "snapshot_id": self.snapshot_id,
                "candidate_id": self.candidate_id,
                "model_identity": self.model_identity,
                "lifecycle_contract_id": self.lifecycle_contract_id,
            }
        )

    def _payload(self, *, include_digest: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "lifecycle_id": self.lifecycle_id,
            "version_number": self.version_number,
            "stage": self.stage.value,
            "league_code": self.league_code,
            "fixture_key": self.fixture_key,
            "kickoff": self.kickoff,
            "market_id": self.market_id,
            "outcome_id": self.outcome_id,
            "candidate_id": self.candidate_id,
            "model_identity": self.model_identity,
            "lifecycle_contract_id": self.lifecycle_contract_id,
            "stage_contract_id": self.stage_contract_id,
            "provider_identity": self.provider_identity,
            "snapshot_id": self.snapshot_id,
            "snapshot_kind": self.snapshot_kind.value,
            "snapshot_source": self.snapshot_source,
            "odds_captured_at": self.odds_captured_at,
            "prediction_generated_at": self.prediction_generated_at,
            "probabilities": self.probabilities,
            "market_odds": self.market_odds,
            "implied_probabilities": self.implied_probabilities,
            "edges": self.edges,
            "confidence_metadata": self.confidence_metadata,
            "source_sha": self.source_sha,
            "research_sha": self.research_sha,
            "model_artifact_hash": self.model_artifact_hash,
            "eligibility_decision": self.eligibility_decision,
            "withdrawal_authorized": self.withdrawal_authorized,
            "decision_id": self.decision_id,
            "decision_reason": self.decision_reason,
            "classification": self.classification.value
            if self.classification
            else None,
            "predecessor_version_digest": self.predecessor_version_digest,
            "no_bet": self.no_bet,
            "publication_enabled": self.publication_enabled,
            "activation_enabled": self.activation_enabled,
        }
        if include_digest:
            payload["version_digest"] = self.version_digest
        return payload

    def validate(self) -> None:
        for name in (
            "lifecycle_id",
            "league_code",
            "fixture_key",
            "market_id",
            "outcome_id",
            "candidate_id",
            "model_identity",
            "lifecycle_contract_id",
            "stage_contract_id",
            "provider_identity",
            "snapshot_id",
            "snapshot_source",
            "source_sha",
            "research_sha",
            "model_artifact_hash",
            "decision_id",
            "decision_reason",
        ):
            _text(getattr(self, name), name)
        for name in ("source_sha", "research_sha", "model_artifact_hash"):
            value = getattr(self, name)
            if len(value) not in (40, 64) or any(
                c not in "0123456789abcdefABCDEF" for c in value
            ):
                raise SignalLifecycleError(
                    f"{name} must be a 40- or 64-character hex digest"
                )
        if isinstance(self.version_number, bool) or not isinstance(
            self.version_number, int
        ):
            raise SignalLifecycleError("version_number must be an integer")
        if self.snapshot_kind is not MarketSnapshotKind.SIGNAL_TIME:
            raise SignalLifecycleError("closing odds cannot enter a lifecycle version")
        if self.provider_identity != EXPECTED_PROVIDER_IDENTITY:
            raise SignalLifecycleError(
                "signal lifecycle provider identity is not the_odds_api"
            )
        if self.prediction_generated_at < self.odds_captured_at:
            raise SignalLifecycleError(
                "prediction generation precedes its odds capture"
            )
        if self.outcome_id not in self.probabilities:
            raise SignalLifecycleError("selected outcome is missing from probabilities")
        for name, value in (
            ("eligibility_decision", self.eligibility_decision),
            ("withdrawal_authorized", self.withdrawal_authorized),
            ("no_bet", self.no_bet),
            ("publication_enabled", self.publication_enabled),
            ("activation_enabled", self.activation_enabled),
        ):
            _boolean(value, name)
        if not self.no_bet or self.publication_enabled or self.activation_enabled:
            raise SignalLifecycleError(
                "lifecycle records must remain no-bet and non-authorizing"
            )
        if self.stage is SignalLifecycleStage.INITIAL:
            if (
                self.version_number != 1
                or self.predecessor_version_digest is not None
                or self.classification is not None
                or not self.eligibility_decision
                or self.withdrawal_authorized
            ):
                raise SignalLifecycleError("INITIAL version state is malformed")
        else:
            if self.version_number != 2 or not self.predecessor_version_digest:
                raise SignalLifecycleError(
                    "refinement version must supersede version 1"
                )
            if self.stage is SignalLifecycleStage.WITHDRAWN:
                if (
                    self.classification is not RefinementClassification.WITHDRAWN
                    or not self.withdrawal_authorized
                    or self.eligibility_decision
                ):
                    raise SignalLifecycleError(
                        "withdrawal requires an explicit withdrawal decision"
                    )
            elif (
                self.stage is not SignalLifecycleStage.REFINED
                or self.classification
                not in {
                    RefinementClassification.STRENGTHENED,
                    RefinementClassification.WEAKENED,
                    RefinementClassification.UNCHANGED,
                }
                or not self.eligibility_decision
                or self.withdrawal_authorized
            ):
                raise SignalLifecycleError("REFINED version decision is malformed")
        if self.version_digest != _digest(self._payload(include_digest=False)):
            raise SignalLifecycleError("lifecycle version digest mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return _canonical(self._payload(include_digest=True))  # type: ignore[return-value]

    @classmethod
    def from_payload(cls, raw: object) -> Top5SignalLifecycleVersion:
        if not isinstance(raw, Mapping):
            raise SignalLifecycleError("lifecycle version must be an object")
        expected = {
            "lifecycle_id",
            "version_number",
            "stage",
            "league_code",
            "fixture_key",
            "kickoff",
            "market_id",
            "outcome_id",
            "candidate_id",
            "model_identity",
            "lifecycle_contract_id",
            "stage_contract_id",
            "provider_identity",
            "snapshot_id",
            "snapshot_kind",
            "snapshot_source",
            "odds_captured_at",
            "prediction_generated_at",
            "probabilities",
            "market_odds",
            "implied_probabilities",
            "edges",
            "confidence_metadata",
            "source_sha",
            "research_sha",
            "model_artifact_hash",
            "eligibility_decision",
            "withdrawal_authorized",
            "decision_id",
            "decision_reason",
            "classification",
            "predecessor_version_digest",
            "no_bet",
            "publication_enabled",
            "activation_enabled",
            "version_digest",
        }
        if set(raw) != expected:
            raise SignalLifecycleError("lifecycle version fields are malformed")

        def timestamp(name: str) -> datetime:
            value = raw[name]
            if not isinstance(value, str):
                raise SignalLifecycleError(f"{name} must be an ISO timestamp")
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise SignalLifecycleError(f"{name} must be an ISO timestamp") from exc

        maps = (
            "probabilities",
            "market_odds",
            "implied_probabilities",
            "edges",
            "confidence_metadata",
        )
        if any(not isinstance(raw[name], Mapping) for name in maps):
            raise SignalLifecycleError("lifecycle version maps are malformed")
        version = cls(
            lifecycle_id=str(raw["lifecycle_id"]),
            version_number=raw["version_number"],
            stage=SignalLifecycleStage(raw["stage"]),
            league_code=str(raw["league_code"]),
            fixture_key=str(raw["fixture_key"]),
            kickoff=timestamp("kickoff"),
            market_id=str(raw["market_id"]),
            outcome_id=str(raw["outcome_id"]),
            candidate_id=str(raw["candidate_id"]),
            model_identity=str(raw["model_identity"]),
            lifecycle_contract_id=str(raw["lifecycle_contract_id"]),
            stage_contract_id=str(raw["stage_contract_id"]),
            provider_identity=str(raw["provider_identity"]),
            snapshot_id=str(raw["snapshot_id"]),
            snapshot_kind=MarketSnapshotKind(raw["snapshot_kind"]),
            snapshot_source=str(raw["snapshot_source"]),
            odds_captured_at=timestamp("odds_captured_at"),
            prediction_generated_at=timestamp("prediction_generated_at"),
            probabilities=raw["probabilities"],  # type: ignore[arg-type]
            market_odds=raw["market_odds"],  # type: ignore[arg-type]
            implied_probabilities=raw["implied_probabilities"],  # type: ignore[arg-type]
            edges=raw["edges"],  # type: ignore[arg-type]
            confidence_metadata=raw["confidence_metadata"],  # type: ignore[arg-type]
            source_sha=str(raw["source_sha"]),
            research_sha=str(raw["research_sha"]),
            model_artifact_hash=str(raw["model_artifact_hash"]),
            eligibility_decision=raw["eligibility_decision"],  # type: ignore[arg-type]
            withdrawal_authorized=raw["withdrawal_authorized"],  # type: ignore[arg-type]
            decision_id=str(raw["decision_id"]),
            decision_reason=str(raw["decision_reason"]),
            classification=(
                RefinementClassification(raw["classification"])
                if raw["classification"] is not None
                else None
            ),
            predecessor_version_digest=(
                str(raw["predecessor_version_digest"])
                if raw["predecessor_version_digest"] is not None
                else None
            ),
            no_bet=raw["no_bet"],  # type: ignore[arg-type]
            publication_enabled=raw["publication_enabled"],  # type: ignore[arg-type]
            activation_enabled=raw["activation_enabled"],  # type: ignore[arg-type]
            version_digest=str(raw["version_digest"]),
        )
        version.validate()
        return version


@dataclass(frozen=True)
class Top5SignalLifecycle:
    lifecycle_id: str
    contract: Top5SignalLifecycleContract
    versions: tuple[Top5SignalLifecycleVersion, ...]

    def validate(self) -> None:
        self.contract.validate()
        if not self.versions or len(self.versions) > 2:
            raise SignalLifecycleError("lifecycle must contain one or two versions")
        if self.lifecycle_id != self.versions[0].lifecycle_id:
            raise SignalLifecycleError(
                "lifecycle identity differs from initial version"
            )
        initial = self.versions[0]
        initial.validate()
        if initial.stage is not SignalLifecycleStage.INITIAL:
            raise SignalLifecycleError("lifecycle history must start with INITIAL")
        expected_id = _logical_lifecycle_id(
            contract_id=self.contract.contract_id,
            league_code=initial.league_code,
            fixture_key=initial.fixture_key,
            market_id=initial.market_id,
            outcome_id=initial.outcome_id,
            candidate_id=initial.candidate_id,
            model_identity=initial.model_identity,
        )
        if expected_id != self.lifecycle_id:
            raise SignalLifecycleError("logical lifecycle identity digest mismatch")
        for item in self.versions:
            item.validate()
            if (
                item.lifecycle_contract_id != self.contract.contract_id
                or item.lifecycle_id != self.lifecycle_id
                or item.provider_identity
                != self.contract.provider_authority_expectation
                or item.snapshot_kind is not self.contract.snapshot_kind
            ):
                raise SignalLifecycleError("version does not match lifecycle contract")
        if len(self.versions) == 2:
            refined = self.versions[1]
            if refined.stage not in (
                SignalLifecycleStage.REFINED,
                SignalLifecycleStage.WITHDRAWN,
            ):
                raise SignalLifecycleError(
                    "second lifecycle version is not a refinement"
                )
            if (
                refined.version_number != 2
                or refined.predecessor_version_digest != initial.version_digest
            ):
                raise SignalLifecycleError("lifecycle version chain is not monotonic")
            immutable_fields = (
                "league_code",
                "fixture_key",
                "kickoff",
                "market_id",
                "outcome_id",
                "candidate_id",
                "model_identity",
                "lifecycle_contract_id",
                "provider_identity",
                "source_sha",
                "research_sha",
                "model_artifact_hash",
            )
            if any(
                getattr(initial, name) != getattr(refined, name)
                for name in immutable_fields
            ):
                raise SignalLifecycleError(
                    "refinement changed immutable lifecycle identity"
                )
            if initial.snapshot_id == refined.snapshot_id:
                raise SignalLifecycleError(
                    "refinement requires a new odds snapshot identity"
                )
        if self.versions[0].stage_contract_id != self.contract.stage_contract_id(
            SignalLifecycleStage.INITIAL
        ):
            raise SignalLifecycleError("initial stage contract digest mismatch")
        if len(self.versions) == 2 and self.versions[
            1
        ].stage_contract_id != self.contract.stage_contract_id(self.versions[1].stage):
            raise SignalLifecycleError("refinement stage contract digest mismatch")

    @property
    def initial_version(self) -> Top5SignalLifecycleVersion:
        self.validate()
        return self.versions[0]

    @property
    def current_version(self) -> Top5SignalLifecycleVersion:
        self.validate()
        return self.versions[-1]

    @property
    def current_lifecycle_stage(self) -> SignalLifecycleStage:
        return self.current_version.stage

    @property
    def last_successfully_updated_at(self) -> datetime:
        return self.current_version.prediction_generated_at

    @property
    def refinement_completed(self) -> bool:
        return len(self.versions) == 2

    @property
    def withdrawn(self) -> bool:
        return self.current_lifecycle_stage is SignalLifecycleStage.WITHDRAWN

    @property
    def lifecycle_digest(self) -> str:
        return _digest(self._payload(include_digest=False))

    def _payload(self, *, include_digest: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": LIFECYCLE_SCHEMA_VERSION,
            "lifecycle_id": self.lifecycle_id,
            "contract": self.contract.as_payload(),
            "contract_id": self.contract.contract_id,
            "versions": [version.as_payload() for version in self.versions],
        }
        if include_digest:
            payload["lifecycle_digest"] = self.lifecycle_digest
        return payload

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return _canonical(self._payload(include_digest=True))  # type: ignore[return-value]

    @classmethod
    def from_payload(cls, raw: object) -> Top5SignalLifecycle:
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "lifecycle_id",
            "contract",
            "contract_id",
            "versions",
            "lifecycle_digest",
        }:
            raise SignalLifecycleError("lifecycle state fields are malformed")
        if raw["schema_version"] != LIFECYCLE_SCHEMA_VERSION:
            raise SignalLifecycleError("unsupported lifecycle state schema")
        contract = Top5SignalLifecycleContract.from_payload(raw["contract"])
        if raw["contract_id"] != contract.contract_id:
            raise SignalLifecycleError("lifecycle contract digest mismatch")
        versions_raw = raw["versions"]
        if not isinstance(versions_raw, (list, tuple)):
            raise SignalLifecycleError("lifecycle versions must be a list")
        lifecycle = cls(
            lifecycle_id=str(raw["lifecycle_id"]),
            contract=contract,
            versions=tuple(
                Top5SignalLifecycleVersion.from_payload(item) for item in versions_raw
            ),
        )
        lifecycle.validate()
        if raw["lifecycle_digest"] != lifecycle.lifecycle_digest:
            raise SignalLifecycleError("lifecycle state digest mismatch")
        return lifecycle


def _build_version(
    *,
    fixture: Fixture,
    snapshot: MarketSnapshot,
    now: datetime,
    stage: SignalLifecycleStage,
    contract: Top5SignalLifecycleContract,
    market_id: str,
    outcome_id: str,
    candidate_id: str,
    model_identity: str,
    provider_identity: str,
    probabilities: Mapping[str, float],
    source_sha: str,
    research_sha: str,
    model_artifact_hash: str,
    decision_id: str,
    decision_reason: str,
    eligibility_decision: bool,
    withdrawal_authorized: bool,
    classification: RefinementClassification | None,
    predecessor_version_digest: str | None,
    implied_probabilities: Mapping[str, float] | None,
    edges: Mapping[str, float] | None,
    confidence_metadata: Mapping[str, object] | None,
) -> Top5SignalLifecycleVersion:
    contract.validate()
    fixture.validate()
    snapshot.validate()
    now_utc = _utc(now, "now")
    resolved_stage = SignalLifecycleStage(stage)
    if snapshot.kind is not contract.snapshot_kind:
        raise SignalLifecycleError(
            "closing odds cannot be used for this lifecycle stage"
        )
    if snapshot.fixture_key != fixture.fixture_key:
        raise SignalLifecycleError("snapshot belongs to a different fixture")
    if not snapshot.snapshot_id.strip():
        raise SignalLifecycleError(
            "snapshot identity is required; timestamp fallback is forbidden"
        )
    if provider_identity != contract.provider_authority_expectation:
        raise SignalLifecycleError(
            "provider identity differs from lifecycle expectation"
        )
    stage_contract = contract.as_signal_time_contract(resolved_stage)
    if not stage_contract.accepts(fixture.kickoff, snapshot.captured_at, now_utc):
        raise SignalLifecycleError("snapshot is outside the stage window or stale")
    if now_utc < snapshot.captured_at:
        raise SignalLifecycleError("snapshot timestamp is in the future")
    lifecycle_id = _logical_lifecycle_id(
        contract_id=contract.contract_id,
        league_code=fixture.league_code,
        fixture_key=fixture.fixture_key,
        market_id=_text(market_id, "market_id"),
        outcome_id=_text(outcome_id, "outcome_id"),
        candidate_id=_text(candidate_id, "candidate_id"),
        model_identity=_text(model_identity, "model_identity"),
    )
    version = Top5SignalLifecycleVersion(
        lifecycle_id=lifecycle_id,
        version_number=1 if resolved_stage is SignalLifecycleStage.INITIAL else 2,
        stage=resolved_stage,
        league_code=fixture.league_code,
        fixture_key=fixture.fixture_key,
        kickoff=fixture.kickoff,
        market_id=market_id,
        outcome_id=outcome_id,
        candidate_id=candidate_id,
        model_identity=model_identity,
        lifecycle_contract_id=contract.contract_id,
        stage_contract_id=contract.stage_contract_id(resolved_stage),
        provider_identity=provider_identity,
        snapshot_id=snapshot.snapshot_id,
        snapshot_kind=snapshot.kind,
        snapshot_source=snapshot.source,
        odds_captured_at=snapshot.captured_at,
        prediction_generated_at=now_utc,
        probabilities=probabilities,
        market_odds=snapshot.odds,
        implied_probabilities=implied_probabilities or {},
        edges=edges or {},
        confidence_metadata=confidence_metadata or {},
        source_sha=source_sha,
        research_sha=research_sha,
        model_artifact_hash=model_artifact_hash,
        eligibility_decision=eligibility_decision,
        withdrawal_authorized=withdrawal_authorized,
        decision_id=decision_id,
        decision_reason=decision_reason,
        classification=classification,
        predecessor_version_digest=predecessor_version_digest,
    )
    version.validate()
    return version


def create_initial_signal(
    *,
    fixture: Fixture,
    snapshot: MarketSnapshot,
    now: datetime,
    market_id: str,
    outcome_id: str,
    candidate_id: str,
    model_identity: str,
    provider_identity: str = EXPECTED_PROVIDER_IDENTITY,
    probabilities: Mapping[str, float],
    source_sha: str,
    research_sha: str,
    model_artifact_hash: str,
    eligibility_decision: bool,
    decision_id: str,
    decision_reason: str,
    contract: Top5SignalLifecycleContract = DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    implied_probabilities: Mapping[str, float] | None = None,
    edges: Mapping[str, float] | None = None,
    confidence_metadata: Mapping[str, object] | None = None,
) -> Top5SignalLifecycle:
    """Create immutable version 1 only from an eligible fresh INITIAL input."""

    if not _boolean(eligibility_decision, "eligibility_decision"):
        raise SignalLifecycleError("caller marked the INITIAL signal ineligible")
    version = _build_version(
        fixture=fixture,
        snapshot=snapshot,
        now=now,
        stage=SignalLifecycleStage.INITIAL,
        contract=contract,
        market_id=market_id,
        outcome_id=outcome_id,
        candidate_id=candidate_id,
        model_identity=model_identity,
        provider_identity=provider_identity,
        probabilities=probabilities,
        source_sha=source_sha,
        research_sha=research_sha,
        model_artifact_hash=model_artifact_hash,
        decision_id=decision_id,
        decision_reason=decision_reason,
        eligibility_decision=True,
        withdrawal_authorized=False,
        classification=None,
        predecessor_version_digest=None,
        implied_probabilities=implied_probabilities,
        edges=edges,
        confidence_metadata=confidence_metadata,
    )
    lifecycle = Top5SignalLifecycle(version.lifecycle_id, contract, (version,))
    lifecycle.validate()
    return lifecycle


def refine_signal(
    lifecycle: Top5SignalLifecycle,
    *,
    fixture: Fixture,
    snapshot: MarketSnapshot,
    now: datetime,
    probabilities: Mapping[str, float],
    eligibility_decision: bool,
    withdrawal_authorized: bool,
    decision_id: str,
    decision_reason: str,
    classification: RefinementClassification | None = None,
    provider_identity: str = EXPECTED_PROVIDER_IDENTITY,
    implied_probabilities: Mapping[str, float] | None = None,
    edges: Mapping[str, float] | None = None,
    confidence_metadata: Mapping[str, object] | None = None,
) -> Top5SignalLifecycle:
    """Append exactly one caller-classified refinement or explicit withdrawal."""

    lifecycle.validate()
    initial = lifecycle.initial_version
    eligible = _boolean(eligibility_decision, "eligibility_decision")
    withdraw = _boolean(withdrawal_authorized, "withdrawal_authorized")
    if withdraw:
        if eligible or classification not in (None, RefinementClassification.WITHDRAWN):
            raise SignalLifecycleError("withdrawal decision is inconsistent")
        resolved_classification = RefinementClassification.WITHDRAWN
        stage = SignalLifecycleStage.WITHDRAWN
    else:
        if not eligible:
            raise SignalLifecycleError(
                "ineligible refinement requires an explicit withdrawal decision"
            )
        if classification is None:
            raise SignalLifecycleError(
                "refinement requires a caller-supplied classification"
            )
        resolved_classification = RefinementClassification(classification)
        if resolved_classification is RefinementClassification.WITHDRAWN:
            raise SignalLifecycleError("WITHDRAWN requires withdrawal_authorized=true")
        stage = SignalLifecycleStage.REFINED
    version = _build_version(
        fixture=fixture,
        snapshot=snapshot,
        now=now,
        stage=stage,
        contract=lifecycle.contract,
        market_id=initial.market_id,
        outcome_id=initial.outcome_id,
        candidate_id=initial.candidate_id,
        model_identity=initial.model_identity,
        provider_identity=provider_identity,
        probabilities=probabilities,
        source_sha=initial.source_sha,
        research_sha=initial.research_sha,
        model_artifact_hash=initial.model_artifact_hash,
        decision_id=decision_id,
        decision_reason=decision_reason,
        eligibility_decision=eligible,
        withdrawal_authorized=withdraw,
        classification=resolved_classification,
        predecessor_version_digest=initial.version_digest,
        implied_probabilities=implied_probabilities,
        edges=edges,
        confidence_metadata=confidence_metadata,
    )
    if version.lifecycle_id != lifecycle.lifecycle_id:
        raise SignalLifecycleError("refinement changed logical signal identity")
    if lifecycle.refinement_completed:
        if lifecycle.current_version == version:
            return lifecycle
        raise SignalLifecycleError("conflicting replay for completed lifecycle stage")
    refined = Top5SignalLifecycle(
        lifecycle.lifecycle_id, lifecycle.contract, (*lifecycle.versions, version)
    )
    refined.validate()
    return refined


@dataclass(frozen=True)
class LifecyclePlan:
    status: LifecyclePlanStatus
    lead_minutes: float
    due_stage: SignalLifecycleStage | None


def plan_signal_lifecycle(
    fixture: Fixture,
    now: datetime,
    lifecycle: Top5SignalLifecycle | None = None,
    *,
    contract: Top5SignalLifecycleContract = DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
) -> LifecyclePlan:
    """Pure event-relative planner; it never sleeps, calls providers, or schedules work."""

    fixture.validate()
    contract.validate()
    now_utc = _utc(now, "now")
    lead_minutes = (fixture.kickoff - now_utc).total_seconds() / 60
    if lifecycle is not None:
        lifecycle.validate()
        if lifecycle.contract.contract_id != contract.contract_id:
            raise SignalLifecycleError(
                "planner contract differs from existing lifecycle"
            )
        initial = lifecycle.initial_version
        if (initial.fixture_key, initial.league_code, initial.kickoff) != (
            fixture.fixture_key,
            fixture.league_code,
            fixture.kickoff,
        ):
            raise SignalLifecycleError(
                "planner fixture differs from existing lifecycle"
            )
        if lifecycle.withdrawn:
            return LifecyclePlan(LifecyclePlanStatus.WITHDRAWN, lead_minutes, None)
        if lifecycle.refinement_completed:
            return LifecyclePlan(LifecyclePlanStatus.COMPLETE, lead_minutes, None)
        policy = contract.refinement
        if lead_minutes > policy.maximum_minutes_before_kickoff:
            status = LifecyclePlanStatus.WAITING_FOR_REFINEMENT
        elif lead_minutes >= policy.minimum_minutes_before_kickoff:
            status = LifecyclePlanStatus.REFINEMENT_DUE
        else:
            status = LifecyclePlanStatus.REFINEMENT_MISSED
        return LifecyclePlan(
            status,
            lead_minutes,
            SignalLifecycleStage.REFINED
            if status is LifecyclePlanStatus.REFINEMENT_DUE
            else None,
        )

    policy = contract.initial
    if lead_minutes > policy.maximum_minutes_before_kickoff:
        status = LifecyclePlanStatus.INITIAL_NOT_DUE
    elif lead_minutes >= policy.minimum_minutes_before_kickoff:
        status = LifecyclePlanStatus.INITIAL_DUE
    else:
        status = LifecyclePlanStatus.INITIAL_MISSED
    return LifecyclePlan(
        status,
        lead_minutes,
        SignalLifecycleStage.INITIAL
        if status is LifecyclePlanStatus.INITIAL_DUE
        else None,
    )


def lifecycle_state_path(lifecycle_id: str) -> Path:
    """Resolve the dedicated external lifecycle state path for one logical signal."""

    _text(lifecycle_id, "lifecycle_id")
    if not lifecycle_id.startswith("top5-signal-lifecycle-v1-"):
        raise SignalLifecycleError("lifecycle_id is not a canonical lifecycle identity")
    digest = lifecycle_id.removeprefix("top5-signal-lifecycle-v1-")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise SignalLifecycleError("lifecycle_id digest is malformed")
    path = runtime_state_path(
        f"{LIFECYCLE_STATE_RELATIVE_DIR}/{lifecycle_id}.json",
        require_external=True,
    )
    active_root = Path(__file__).resolve().parents[2]
    resolved = path.expanduser().resolve()
    if resolved == active_root or active_root in resolved.parents:
        raise SignalLifecycleError(
            "lifecycle store must remain outside the active checkout"
        )
    return path


class Top5SignalLifecycleStore:
    """External atomic append-only lifecycle store; not registered with runtime."""

    @contextmanager
    def _locked(self, path: Path) -> Iterator[None]:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        with lock_path.open("a+") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _wrapper(lifecycle: Top5SignalLifecycle) -> dict[str, object]:
        payload = lifecycle.as_payload()
        wrapper = {
            "schema_version": LIFECYCLE_STORE_SCHEMA_VERSION,
            "lifecycle_id": lifecycle.lifecycle_id,
            "lifecycle_digest": lifecycle.lifecycle_digest,
            "lifecycle": payload,
        }
        wrapper["store_digest"] = _digest(wrapper)
        return wrapper

    @staticmethod
    def _read(path: Path, expected_id: str) -> Top5SignalLifecycle:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SignalLifecycleError("lifecycle store is unreadable") from exc
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "lifecycle_id",
            "lifecycle_digest",
            "lifecycle",
            "store_digest",
        }:
            raise SignalLifecycleError("lifecycle store wrapper is malformed")
        if raw["schema_version"] != LIFECYCLE_STORE_SCHEMA_VERSION:
            raise SignalLifecycleError("unsupported lifecycle store schema")
        if raw["lifecycle_id"] != expected_id:
            raise SignalLifecycleError("lifecycle store path identity mismatch")
        unsigned = dict(raw)
        actual_store_digest = unsigned.pop("store_digest")
        if actual_store_digest != _digest(unsigned):
            raise SignalLifecycleError("lifecycle store digest mismatch")
        lifecycle = Top5SignalLifecycle.from_payload(raw["lifecycle"])
        if (
            lifecycle.lifecycle_id != expected_id
            or raw["lifecycle_digest"] != lifecycle.lifecycle_digest
        ):
            raise SignalLifecycleError("lifecycle wrapper binding mismatch")
        return lifecycle

    def load(self, lifecycle_id: str) -> Top5SignalLifecycle | None:
        path = lifecycle_state_path(lifecycle_id)
        with self._locked(path):
            if not path.is_file():
                return None
            return self._read(path, lifecycle_id)

    def save(self, lifecycle: Top5SignalLifecycle) -> Top5SignalLifecycle:
        lifecycle.validate()
        path = lifecycle_state_path(lifecycle.lifecycle_id)
        with self._locked(path):
            if path.is_file():
                current = self._read(path, lifecycle.lifecycle_id)
                if current.lifecycle_digest == lifecycle.lifecycle_digest:
                    return current
                if (
                    len(lifecycle.versions) == len(current.versions) + 1
                    and lifecycle.versions[:-1] == current.versions
                ):
                    atomic_write_json(path, self._wrapper(lifecycle), sort_keys=True)
                    os.chmod(path, 0o600)
                    return lifecycle
                raise SignalLifecycleError(
                    "conflicting lifecycle replay or non-append update"
                )
            if len(lifecycle.versions) != 1:
                raise SignalLifecycleError(
                    "a durable lifecycle must start with INITIAL"
                )
            atomic_write_json(path, self._wrapper(lifecycle), sort_keys=True)
            os.chmod(path, 0o600)
            return lifecycle


__all__ = [
    "DEFAULT_SIGNAL_LIFECYCLE_CONTRACT",
    "EXPECTED_PROVIDER_IDENTITY",
    "LIFECYCLE_SCHEMA_VERSION",
    "LIFECYCLE_STATE_RELATIVE_DIR",
    "LifecyclePlan",
    "LifecyclePlanStatus",
    "RefinementClassification",
    "SignalLifecycleError",
    "SignalLifecycleStage",
    "SignalLifecycleStagePolicy",
    "Top5SignalLifecycle",
    "Top5SignalLifecycleContract",
    "Top5SignalLifecycleStore",
    "Top5SignalLifecycleVersion",
    "create_initial_signal",
    "lifecycle_state_path",
    "plan_signal_lifecycle",
    "refine_signal",
]
