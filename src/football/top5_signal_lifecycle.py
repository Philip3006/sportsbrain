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
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
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
LIFECYCLE_SET_SCHEMA_VERSION = "top5-signal-lifecycle-set-v1"
LIFECYCLE_SET_STORE_SCHEMA_VERSION = "top5-signal-lifecycle-set-store-v1"
LIFECYCLE_SET_STATE_RELATIVE_DIR = "football/top5/signal_lifecycle_sets"
TOP5_H2H_OUTCOMES = ("home", "draw", "away")
LIFECYCLE_STATE_RELATIVE_DIR = "football/top5/signal_lifecycle"
EXPECTED_PROVIDER_IDENTITY = "the_odds_api"
_SUPPORTED_SIGNAL_EVIDENCE_SOURCES = frozenset(
    {EXPECTED_PROVIDER_IDENTITY, "isports_api"}
)


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


def _canonical_snapshot_provider(source: object) -> str | None:
    """Parse the provider token from a canonical colon-delimited source label.

    Bare provider identities and qualified internal routes are supported. Route
    components are restricted to ASCII identifiers so URLs or substring
    lookalikes cannot masquerade as provider provenance.
    """

    if not isinstance(source, str) or not source or source != source.strip():
        return None
    provider, separator, route = source.partition(":")
    if provider not in _SUPPORTED_SIGNAL_EVIDENCE_SOURCES:
        return None
    if not separator:
        return provider
    components = route.split(":")
    if any(
        not component
        or not component.isascii()
        or any(
            not (character.isalnum() or character in "_.-") for character in component
        )
        for component in components
    ):
        return None
    return provider


def _validate_snapshot_source_binding(
    provider_identity: str, snapshot_source: object
) -> None:
    # ``provider_identity`` is the production authority binding and stays
    # The Odds API.  The snapshot source records where accepted market
    # evidence actually came from; a typed iSports candidate observation may
    # be consumed without promoting it to production authority.
    if provider_identity != EXPECTED_PROVIDER_IDENTITY:
        raise SignalLifecycleError(
            "signal lifecycle provider identity is not the_odds_api"
        )
    if _canonical_snapshot_provider(snapshot_source) not in _SUPPORTED_SIGNAL_EVIDENCE_SOURCES:
        raise SignalLifecycleError(
            "snapshot source does not canonically bind to an accepted evidence provider"
        )


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
        _validate_snapshot_source_binding(self.provider_identity, self.snapshot_source)
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


def canonical_top5_h2h_lifecycle_set(
    lifecycles: object,
) -> dict[str, Top5SignalLifecycle]:
    """Validate and order the complete canonical home/draw/away lifecycle set."""
    if not isinstance(lifecycles, Sequence) or isinstance(
        lifecycles, (str, bytes, bytearray)
    ):
        raise SignalLifecycleError("Top-5 lifecycle set must be a sequence")
    if len(lifecycles) != len(TOP5_H2H_OUTCOMES):
        raise SignalLifecycleError("Top-5 lifecycle set must contain exactly three")
    by_outcome: dict[str, Top5SignalLifecycle] = {}
    for lifecycle in lifecycles:
        if not isinstance(lifecycle, Top5SignalLifecycle):
            raise SignalLifecycleError(
                "Top-5 lifecycle set contains a non-canonical item"
            )
        lifecycle.validate()
        initial = lifecycle.initial_version
        outcome = initial.outcome_id
        if outcome not in TOP5_H2H_OUTCOMES or outcome in by_outcome:
            raise SignalLifecycleError(
                "Top-5 lifecycle outcomes are duplicate or invalid"
            )
        if initial.market_id != "h2h":
            raise SignalLifecycleError("Top-5 lifecycle market must be h2h")
        by_outcome[outcome] = lifecycle
    if set(by_outcome) != set(TOP5_H2H_OUTCOMES):
        raise SignalLifecycleError("Top-5 lifecycle outcomes must be home/draw/away")

    reference = by_outcome[TOP5_H2H_OUTCOMES[0]]
    reference_initial = reference.initial_version
    reference_current = reference.current_version
    for outcome in TOP5_H2H_OUTCOMES[1:]:
        lifecycle = by_outcome[outcome]
        initial = lifecycle.initial_version
        current = lifecycle.current_version
        for field_name in (
            "league_code",
            "fixture_key",
            "kickoff",
            "candidate_id",
            "model_identity",
            "lifecycle_contract_id",
            "provider_identity",
            "source_sha",
            "research_sha",
            "model_artifact_hash",
            "snapshot_id",
            "snapshot_kind",
            "snapshot_source",
            "odds_captured_at",
            "prediction_generated_at",
            "probabilities",
            "market_odds",
            "implied_probabilities",
            "edges",
            "decision_id",
            "decision_reason",
            "eligibility_decision",
            "withdrawal_authorized",
            "confidence_metadata",
        ):
            if getattr(initial, field_name) != getattr(reference_initial, field_name):
                raise SignalLifecycleError(
                    f"Top-5 lifecycle initial {field_name} differs across outcomes"
                )
        for field_name in (
            "league_code",
            "fixture_key",
            "kickoff",
            "candidate_id",
            "model_identity",
            "lifecycle_contract_id",
            "provider_identity",
            "source_sha",
            "research_sha",
            "model_artifact_hash",
            "snapshot_id",
            "snapshot_kind",
            "snapshot_source",
            "odds_captured_at",
            "prediction_generated_at",
            "probabilities",
            "market_odds",
            "implied_probabilities",
            "edges",
            "decision_id",
            "decision_reason",
            "eligibility_decision",
            "withdrawal_authorized",
            "confidence_metadata",
        ):
            if getattr(current, field_name) != getattr(reference_current, field_name):
                raise SignalLifecycleError(
                    f"Top-5 lifecycle current {field_name} differs across outcomes"
                )
        if (
            len(lifecycle.versions) != len(reference.versions)
            or current.stage is not reference_current.stage
            or current.version_number != reference_current.version_number
        ):
            raise SignalLifecycleError(
                "Top-5 lifecycle versions/stages differ across outcomes"
            )
    return {outcome: by_outcome[outcome] for outcome in TOP5_H2H_OUTCOMES}


def parse_top5_h2h_lifecycle_set(raw: object) -> tuple[Top5SignalLifecycle, ...]:
    """Parse the sole supported CLI lifecycle-state envelope."""
    if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "lifecycles"}:
        raise SignalLifecycleError("Top-5 lifecycle-set envelope fields are malformed")
    if raw["schema_version"] != LIFECYCLE_SET_SCHEMA_VERSION:
        raise SignalLifecycleError("unsupported Top-5 lifecycle-set schema")
    payloads = raw["lifecycles"]
    if not isinstance(payloads, (list, tuple)) or len(payloads) != 3:
        raise SignalLifecycleError(
            "Top-5 lifecycle-set envelope requires exactly three"
        )
    parsed = tuple(Top5SignalLifecycle.from_payload(item) for item in payloads)
    ordered = canonical_top5_h2h_lifecycle_set(parsed)
    return tuple(ordered[outcome] for outcome in TOP5_H2H_OUTCOMES)


def top5_h2h_lifecycle_set_payload(lifecycles: object) -> dict[str, object]:
    ordered = canonical_top5_h2h_lifecycle_set(lifecycles)
    return {
        "schema_version": LIFECYCLE_SET_SCHEMA_VERSION,
        "lifecycles": [ordered[outcome].as_payload() for outcome in TOP5_H2H_OUTCOMES],
    }


def _lifecycle_set_shared_identity(
    lifecycles: Mapping[str, Top5SignalLifecycle],
) -> dict[str, str]:
    home = lifecycles["home"].initial_version
    return {
        "lifecycle_contract_id": home.lifecycle_contract_id,
        "league_code": home.league_code,
        "fixture_key": home.fixture_key,
        "market_id": home.market_id,
        "candidate_id": home.candidate_id,
        "model_identity": home.model_identity,
    }


def top5_h2h_lifecycle_set_identity_for_scope(
    *,
    lifecycle_contract_id: str,
    league_code: str,
    fixture_key: str,
    market_id: str,
    candidate_id: str,
    model_identity: str,
) -> str:
    """Return the stable transaction identity for one immutable 1X2 lifecycle set."""
    shared = {
        "lifecycle_contract_id": _text(lifecycle_contract_id, "lifecycle_contract_id"),
        "league_code": _text(league_code, "league_code"),
        "fixture_key": _text(fixture_key, "fixture_key"),
        "market_id": _text(market_id, "market_id"),
        "candidate_id": _text(candidate_id, "candidate_id"),
        "model_identity": _text(model_identity, "model_identity"),
    }
    if shared["market_id"] != "h2h":
        raise SignalLifecycleError("lifecycle-set market must be h2h")
    digest = _digest(
        {
            "schema_version": LIFECYCLE_SET_STORE_SCHEMA_VERSION,
            **shared,
        }
    )
    return f"top5-signal-lifecycle-set-v1-{digest}"


def top5_h2h_lifecycle_set_id(lifecycles: object) -> str:
    ordered = canonical_top5_h2h_lifecycle_set(lifecycles)
    return top5_h2h_lifecycle_set_identity_for_scope(
        **_lifecycle_set_shared_identity(ordered)
    )


def top5_h2h_lifecycle_set_digest(lifecycles: object) -> str:
    """Digest the exact outcome-keyed canonical member digests of a complete set."""
    ordered = canonical_top5_h2h_lifecycle_set(lifecycles)
    return _digest(
        {outcome: ordered[outcome].lifecycle_digest for outcome in TOP5_H2H_OUTCOMES}
    )


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
    _validate_snapshot_source_binding(provider_identity, snapshot.source)
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


def lifecycle_set_state_path(
    lifecycle_set_id: str, *, root: Path | None = None
) -> Path:
    """Resolve one owner-only external file for an immutable lifecycle-set scope."""
    prefix = "top5-signal-lifecycle-set-v1-"
    _text(lifecycle_set_id, "lifecycle_set_id")
    digest = lifecycle_set_id.removeprefix(prefix)
    if (
        not lifecycle_set_id.startswith(prefix)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise SignalLifecycleError("lifecycle-set identity is malformed")
    path_root = (
        Path(root).expanduser()
        if root is not None
        else runtime_state_path(LIFECYCLE_SET_STATE_RELATIVE_DIR, require_external=True)
    )
    if not path_root.is_absolute():
        raise SignalLifecycleError("lifecycle-set store root must be absolute")
    absolute_root = Path(os.path.abspath(path_root))
    resolved_root = absolute_root.resolve()
    active_root = Path(__file__).resolve().parents[2]
    if (
        resolved_root != absolute_root
        or resolved_root == active_root
        or active_root in resolved_root.parents
    ):
        raise SignalLifecycleError(
            "lifecycle-set store must be external and symlink-free"
        )
    return absolute_root / f"{lifecycle_set_id}.json"


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


class Top5SignalLifecycleSetStore:
    """Atomic external authority for complete home/draw/away lifecycle sets."""

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            root = runtime_state_path(
                LIFECYCLE_SET_STATE_RELATIVE_DIR, require_external=True
            )
        self.root = Path(root).expanduser()
        if not self.root.is_absolute():
            raise SignalLifecycleError("lifecycle-set store root must be absolute")
        self.root = Path(os.path.abspath(self.root))
        active_root = Path(__file__).resolve().parents[2]
        resolved_root = self.root.resolve()
        if (
            resolved_root != self.root
            or resolved_root == active_root
            or active_root in resolved_root.parents
        ):
            raise SignalLifecycleError(
                "lifecycle-set store must be external and symlink-free"
            )

    @staticmethod
    def _scope_id(lifecycles: Mapping[str, Top5SignalLifecycle]) -> str:
        return top5_h2h_lifecycle_set_identity_for_scope(
            **_lifecycle_set_shared_identity(lifecycles)
        )

    @staticmethod
    def _member_digests(
        lifecycles: Mapping[str, Top5SignalLifecycle],
    ) -> dict[str, str]:
        return {
            outcome: lifecycles[outcome].lifecycle_digest
            for outcome in TOP5_H2H_OUTCOMES
        }

    @staticmethod
    def _require_complete_stage(
        lifecycles: Mapping[str, Top5SignalLifecycle], *, stage: str
    ) -> None:
        expected_length = 1 if stage == "INITIAL" else 2
        expected_stage = (
            SignalLifecycleStage.INITIAL
            if stage == "INITIAL"
            else SignalLifecycleStage.REFINED
        )
        for lifecycle in lifecycles.values():
            if (
                len(lifecycle.versions) != expected_length
                or lifecycle.current_version.version_number != expected_length
                or lifecycle.current_version.stage is not expected_stage
                or lifecycle.withdrawn
            ):
                raise SignalLifecycleError(
                    f"lifecycle set must contain only complete {stage} histories"
                )

    @classmethod
    def _state_kind(cls, lifecycles: Mapping[str, Top5SignalLifecycle]) -> str:
        initial = all(
            len(lifecycle.versions) == 1
            and lifecycle.current_version.version_number == 1
            and lifecycle.current_version.stage is SignalLifecycleStage.INITIAL
            and not lifecycle.withdrawn
            for lifecycle in lifecycles.values()
        )
        refined = all(
            len(lifecycle.versions) == 2
            and lifecycle.current_version.version_number == 2
            and lifecycle.current_version.stage is SignalLifecycleStage.REFINED
            and not lifecycle.withdrawn
            for lifecycle in lifecycles.values()
        )
        if initial:
            return "INITIAL"
        if refined:
            return "REFINEMENT"
        raise SignalLifecycleError(
            "lifecycle set cannot contain mixed, withdrawn, or unsupported versions"
        )

    @classmethod
    def _wrapper(
        cls, lifecycles: Mapping[str, Top5SignalLifecycle]
    ) -> dict[str, object]:
        lifecycle_set_id = cls._scope_id(lifecycles)
        shared = _lifecycle_set_shared_identity(lifecycles)
        lifecycle_digests = cls._member_digests(lifecycles)
        wrapper: dict[str, object] = {
            "schema_version": LIFECYCLE_SET_STORE_SCHEMA_VERSION,
            "lifecycle_set_id": lifecycle_set_id,
            "lifecycle_set_digest": _digest(lifecycle_digests),
            "outcome_keys": list(TOP5_H2H_OUTCOMES),
            "shared_identity": shared,
            "lifecycles": {
                outcome: lifecycles[outcome].as_payload()
                for outcome in TOP5_H2H_OUTCOMES
            },
        }
        wrapper["store_digest"] = _digest(wrapper)
        return wrapper

    @classmethod
    def _parse_wrapper(
        cls, raw: object, *, expected_set_id: str
    ) -> tuple[Top5SignalLifecycle, ...]:
        fields = {
            "schema_version",
            "lifecycle_set_id",
            "lifecycle_set_digest",
            "outcome_keys",
            "shared_identity",
            "lifecycles",
            "store_digest",
        }
        if not isinstance(raw, Mapping) or set(raw) != fields:
            raise SignalLifecycleError("lifecycle-set store envelope is malformed")
        if (
            raw["schema_version"] != LIFECYCLE_SET_STORE_SCHEMA_VERSION
            or raw["lifecycle_set_id"] != expected_set_id
            or raw["outcome_keys"] != list(TOP5_H2H_OUTCOMES)
        ):
            raise SignalLifecycleError("lifecycle-set store binding is invalid")
        lifecycle_payloads = raw["lifecycles"]
        if not isinstance(lifecycle_payloads, Mapping) or set(
            lifecycle_payloads
        ) != set(TOP5_H2H_OUTCOMES):
            raise SignalLifecycleError("lifecycle-set outcomes are malformed")
        ordered = canonical_top5_h2h_lifecycle_set(
            tuple(
                Top5SignalLifecycle.from_payload(lifecycle_payloads[outcome])
                for outcome in TOP5_H2H_OUTCOMES
            )
        )
        cls._state_kind(ordered)
        if cls._scope_id(ordered) != expected_set_id or raw[
            "shared_identity"
        ] != _lifecycle_set_shared_identity(ordered):
            raise SignalLifecycleError(
                "lifecycle-set shared immutable identity is invalid"
            )
        expected_member_digest = _digest(cls._member_digests(ordered))
        if raw["lifecycle_set_digest"] != expected_member_digest:
            raise SignalLifecycleError("lifecycle-set content digest mismatch")
        unsigned = dict(raw)
        actual_store_digest = unsigned.pop("store_digest")
        if actual_store_digest != _digest(unsigned):
            raise SignalLifecycleError("lifecycle-set store digest mismatch")
        return tuple(ordered[outcome] for outcome in TOP5_H2H_OUTCOMES)

    def _path(self, lifecycle_set_id: str) -> Path:
        return lifecycle_set_state_path(lifecycle_set_id, root=self.root)

    @contextmanager
    def _locked(self, path: Path) -> Iterator[None]:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise SignalLifecycleError("lifecycle-set directory must not be a symlink")
        if not self.root.is_dir():
            raise SignalLifecycleError("lifecycle-set directory is not a directory")
        root_metadata = self.root.stat()
        if root_metadata.st_uid != os.getuid():
            raise SignalLifecycleError("lifecycle-set directory is not owner-owned")
        if root_metadata.st_mode & 0o077:
            os.chmod(self.root, 0o700)
        lock_path = path.with_suffix(path.suffix + ".lock")
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise SignalLifecycleError(
                "lifecycle-set store requires no-follow filesystem support"
            )
        flags = os.O_CREAT | os.O_RDWR | nofollow
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            lock_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(lock_metadata.st_mode):
                raise SignalLifecycleError("lifecycle-set lock is not a regular file")
            if lock_metadata.st_uid != os.getuid():
                raise SignalLifecycleError("lifecycle-set lock is not owner-owned")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_unlocked(
        path: Path, expected_set_id: str
    ) -> tuple[Top5SignalLifecycle, ...] | None:
        if path.is_symlink():
            raise SignalLifecycleError("lifecycle-set file must not be a symlink")
        if not path.exists():
            return None
        try:
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise SignalLifecycleError(
                    "lifecycle-set store requires no-follow filesystem support"
                )
            descriptor = os.open(path, os.O_RDONLY | nofollow)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                metadata = os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise SignalLifecycleError(
                        "lifecycle-set path is not a regular file"
                    )
                if metadata.st_uid != os.getuid():
                    raise SignalLifecycleError("lifecycle-set file is not owner-owned")
                if metadata.st_mode & 0o077:
                    raise SignalLifecycleError(
                        "lifecycle-set file permissions must be owner-only"
                    )
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise SignalLifecycleError(
                "lifecycle-set store is unreadable or malformed"
            ) from exc
        return Top5SignalLifecycleSetStore._parse_wrapper(
            raw, expected_set_id=expected_set_id
        )

    @staticmethod
    def _write_staged_payload(descriptor: int, encoded: bytes) -> None:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    @classmethod
    def _stage(cls, path: Path, wrapper: Mapping[str, object]) -> Path:
        encoded = json.dumps(
            _canonical(wrapper),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temp_name)
        try:
            os.fchmod(descriptor, 0o600)
            cls._write_staged_payload(descriptor, encoded)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    @staticmethod
    def _replace_staged(temporary: Path, path: Path) -> None:
        os.replace(temporary, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @classmethod
    def _atomic_write(cls, path: Path, wrapper: Mapping[str, object]) -> None:
        temporary = cls._stage(path, wrapper)
        try:
            if path.is_symlink():
                raise SignalLifecycleError("lifecycle-set file must not be a symlink")
            cls._replace_staged(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, lifecycle_set_id: str) -> tuple[Top5SignalLifecycle, ...] | None:
        path = self._path(lifecycle_set_id)
        with self._locked(path):
            return self._read_unlocked(path, lifecycle_set_id)

    def load_for_scope(
        self,
        *,
        lifecycle_contract_id: str,
        league_code: str,
        fixture_key: str,
        market_id: str,
        candidate_id: str,
        model_identity: str,
    ) -> tuple[Top5SignalLifecycle, ...] | None:
        lifecycle_set_id = top5_h2h_lifecycle_set_identity_for_scope(
            lifecycle_contract_id=lifecycle_contract_id,
            league_code=league_code,
            fixture_key=fixture_key,
            market_id=market_id,
            candidate_id=candidate_id,
            model_identity=model_identity,
        )
        return self.load(lifecycle_set_id)

    @classmethod
    def _validate_transition(
        cls,
        existing: Sequence[Top5SignalLifecycle],
        proposed: Mapping[str, Top5SignalLifecycle],
    ) -> None:
        prior = canonical_top5_h2h_lifecycle_set(existing)
        if (
            cls._state_kind(prior) != "INITIAL"
            or cls._state_kind(proposed) != "REFINEMENT"
        ):
            raise SignalLifecycleError(
                "only a complete INITIAL-to-REFINEMENT transition is allowed"
            )
        for outcome in TOP5_H2H_OUTCOMES:
            before = prior[outcome]
            after = proposed[outcome]
            if (
                before.lifecycle_id != after.lifecycle_id
                or before.contract != after.contract
                or len(after.versions) != 2
                or after.versions[:1] != before.versions
                or after.current_version.predecessor_version_digest
                != before.current_version.version_digest
                or after.current_version.stage is not SignalLifecycleStage.REFINED
            ):
                raise SignalLifecycleError(
                    "lifecycle-set transition is not an exact append-only refinement"
                )

    def commit(self, lifecycles: object) -> tuple[Top5SignalLifecycle, ...]:
        proposed = canonical_top5_h2h_lifecycle_set(lifecycles)
        proposed_kind = self._state_kind(proposed)
        lifecycle_set_id = self._scope_id(proposed)
        path = self._path(lifecycle_set_id)
        with self._locked(path):
            existing = self._read_unlocked(path, lifecycle_set_id)
            if existing is None:
                if proposed_kind != "INITIAL":
                    raise SignalLifecycleError(
                        "a lifecycle set must begin with three INITIAL records"
                    )
            else:
                current = canonical_top5_h2h_lifecycle_set(existing)
                if self._member_digests(current) == self._member_digests(proposed):
                    return tuple(current[outcome] for outcome in TOP5_H2H_OUTCOMES)
                self._validate_transition(existing, proposed)
            wrapper = self._wrapper(proposed)
            self._atomic_write(path, wrapper)
            read_back = self._read_unlocked(path, lifecycle_set_id)
            if read_back is None or self._member_digests(
                canonical_top5_h2h_lifecycle_set(read_back)
            ) != self._member_digests(proposed):
                raise SignalLifecycleError(
                    "lifecycle-set commit read-back differs from proposed state"
                )
            return read_back


__all__ = [
    "DEFAULT_SIGNAL_LIFECYCLE_CONTRACT",
    "EXPECTED_PROVIDER_IDENTITY",
    "LIFECYCLE_SCHEMA_VERSION",
    "LIFECYCLE_SET_SCHEMA_VERSION",
    "LIFECYCLE_SET_STATE_RELATIVE_DIR",
    "LIFECYCLE_SET_STORE_SCHEMA_VERSION",
    "LIFECYCLE_STATE_RELATIVE_DIR",
    "TOP5_H2H_OUTCOMES",
    "LifecyclePlan",
    "LifecyclePlanStatus",
    "RefinementClassification",
    "SignalLifecycleError",
    "SignalLifecycleStage",
    "SignalLifecycleStagePolicy",
    "Top5SignalLifecycle",
    "Top5SignalLifecycleContract",
    "Top5SignalLifecycleSetStore",
    "Top5SignalLifecycleStore",
    "Top5SignalLifecycleVersion",
    "canonical_top5_h2h_lifecycle_set",
    "create_initial_signal",
    "lifecycle_set_state_path",
    "lifecycle_state_path",
    "parse_top5_h2h_lifecycle_set",
    "plan_signal_lifecycle",
    "refine_signal",
    "top5_h2h_lifecycle_set_digest",
    "top5_h2h_lifecycle_set_id",
    "top5_h2h_lifecycle_set_identity_for_scope",
    "top5_h2h_lifecycle_set_payload",
]
