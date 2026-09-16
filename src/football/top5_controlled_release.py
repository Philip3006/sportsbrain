"""Fail-closed controlled activation and publication preflight.

This module is the narrow seam between completed real-shadow evidence and a
future production adapter.  It validates caller-supplied decisions and
artifacts, performs only in-memory state transitions, and delegates public
serialization to the existing football serializer.  It never calls a
provider, selects a model, writes runtime state, schedules work, or mutates a
ledger.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from src.football.production_contracts import (
    ActivationMode,
    ProductionContractError,
    RolloutEvidence,
    RolloutStage,
    SignalTimeContract,
    _utc,
)
from src.football.top5_activation_readiness import (
    ControlledActivationRequest,
    RollbackController,
    RollbackResult,
    RollbackTrigger,
)
from src.football.top5_builder2_qualification_receipt import (
    Builder2QualificationReceiptError,
    Builder2QualificationReceiptV1,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    MinimumSamplePolicy,
)
from src.football.top5_provider_validation import ProviderAuthority
from src.football.top5_publisher import (
    ControlledTop5PublicationPayload,
    InMemoryTop5PublicationStore,
    PublicationRollback,
    PublishedTop5Artifact,
    Top5PublicationAuthorization,
)
from src.football.top5_qualification_sample_aggregator import (
    Builder2QualificationSampleAggregatorError,
    Builder2QualificationSampleReportV1,
    aggregate_builder2_qualification_samples,
)

REAL_OBSERVED = "REAL_OBSERVED"
READY_FOR_CONTROLLED_ACTIVATION = "READY_FOR_CONTROLLED_ACTIVATION"
BLOCKED = "BLOCKED"


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProductionContractError(f"{name} must be non-empty text")
    return value


def _hash(value: object, name: str) -> str:
    value = _text(value, name)
    if len(value) not in (40, 64) or any(
        char not in "0123456789abcdefABCDEF" for char in value
    ):
        raise ProductionContractError(f"{name} must be a hexadecimal digest")
    return value


def _records(value: object, name: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProductionContractError(f"{name} must be a sequence")
    records: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ProductionContractError(f"{name} contains a non-object record")
        records.append(item)
    return tuple(records)


@dataclass(frozen=True)
class ControlledActivationAuthorization:
    """Activation-specific CEO approval; never created by this module."""

    authorization_id: str
    activation_id: str
    league_code: str
    candidate_id: str
    model_identity: str
    source_sha: str
    research_sha: str
    model_artifact_hash: str
    signal_time_experiment_id: str
    minimum_sample_policy: MinimumSamplePolicy
    provider_authority: ProviderAuthority
    controlled_shadow_run_id: str
    qualification_session_id: str
    fixture_scope: tuple[str, ...]
    rollback_pointer: str
    authorization_token: str
    issued_at: datetime
    expires_at: datetime
    activation_authorized: bool = True
    no_bet: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "issued_at", _utc(self.issued_at, "issued_at"))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))

    def validate(self, *, now: datetime | None = None) -> None:
        for name, value in (
            ("authorization_id", self.authorization_id),
            ("activation_id", self.activation_id),
            ("league_code", self.league_code),
            ("candidate_id", self.candidate_id),
            ("model_identity", self.model_identity),
            ("rollback_pointer", self.rollback_pointer),
            ("authorization_token", self.authorization_token),
            ("signal_time_experiment_id", self.signal_time_experiment_id),
            ("controlled_shadow_run_id", self.controlled_shadow_run_id),
            ("qualification_session_id", self.qualification_session_id),
        ):
            _text(value, name)
        for name, value in (
            ("source_sha", self.source_sha),
            ("research_sha", self.research_sha),
            ("model_artifact_hash", self.model_artifact_hash),
        ):
            _hash(value, name)
        if not isinstance(self.minimum_sample_policy, MinimumSamplePolicy):
            raise ProductionContractError(
                "activation authorization requires the canonical caller-supplied MinimumSamplePolicy"
            )
        self.minimum_sample_policy.validate()
        if not isinstance(self.provider_authority, ProviderAuthority):
            raise ProductionContractError(
                "activation authorization requires provider and result authority"
            )
        self.provider_authority.validate()
        if not self.provider_authority.is_complete:
            raise ProductionContractError(
                "activation authorization requires complete provider/result authority"
            )
        if not self.fixture_scope or len(set(self.fixture_scope)) != len(
            self.fixture_scope
        ):
            raise ProductionContractError(
                "activation authorization requires unique fixture scope"
            )
        if any(
            not isinstance(value, str) or not value.strip()
            for value in self.fixture_scope
        ):
            raise ProductionContractError(
                "activation authorization fixture scope is invalid"
            )
        if not self.activation_authorized:
            raise ProductionContractError("activation authorization is not approved")
        if not self.no_bet:
            raise ProductionContractError("controlled activation must remain no-bet")
        if self.expires_at <= self.issued_at:
            raise ProductionContractError("activation authorization expiry is invalid")
        if (
            now is not None
            and not self.issued_at <= _utc(now, "now") <= self.expires_at
        ):
            raise ProductionContractError(
                "activation authorization is expired or not yet valid"
            )

    def binds_request(self, request: ControlledActivationRequest) -> None:
        self.validate()
        request.validate()
        for name in (
            "league_code",
            "candidate_id",
            "model_identity",
            "source_sha",
            "research_sha",
            "model_artifact_hash",
            "rollback_pointer",
        ):
            if getattr(self, name) != getattr(request, name):
                raise ProductionContractError(f"activation binding mismatch: {name}")
        if self.provider_authority != request.provider_authority:
            raise ProductionContractError(
                "activation binding mismatch: provider_authority"
            )
        if not isinstance(request.signal_time_contract, SignalTimeContract):
            raise ProductionContractError(
                "activation request signal-time contract is invalid"
            )
        if request.signal_time_contract.approval_ref != self.authorization_id:
            raise ProductionContractError(
                "activation binding mismatch: signal-time approval"
            )


@dataclass(frozen=True)
class Top5ControlledReleaseEvidence:
    """The complete, already-produced B4 → B2 → B1 evidence chain."""

    receipts: tuple[Builder2QualificationReceiptV1, ...]
    sample_report: Builder2QualificationSampleReportV1
    audit_report: Mapping[str, object]
    measurement_report: Mapping[str, object]

    def evidence_digest(self) -> str:
        return _digest(
            {
                "receipts": [receipt.as_payload() for receipt in self.receipts],
                "sample_report": self.sample_report.as_payload(),
                "audit_report": dict(self.audit_report),
                "measurement_report": dict(self.measurement_report),
            }
        )

    def validate(
        self,
        request: ControlledActivationRequest,
        authorization: ControlledActivationAuthorization,
    ) -> None:
        if not self.receipts:
            raise ProductionContractError(
                "controlled activation requires at least one canonical receipt"
            )
        authorization.validate()
        self.sample_report.validate()
        if (
            self.sample_report.minimum_sample_policy
            != authorization.minimum_sample_policy
        ):
            raise ProductionContractError(
                "sample policy is not the exact authorized policy"
            )
        if self.sample_report.sample_sufficient is not True:
            raise ProductionContractError(
                "sample evidence is not sufficient under the caller-supplied policy"
            )
        if not self.sample_report.no_bet or self.sample_report.publication:
            raise ProductionContractError(
                "sample report violates no-bet/publication safety"
            )
        if self.sample_report.production_activation_authorized:
            raise ProductionContractError("sample report cannot authorize production")
        if any(
            (
                self.sample_report.duplicate_receipt_ids,
                self.sample_report.divergent_receipt_ids,
                self.sample_report.duplicate_observation_ids,
                self.sample_report.observation_identity_conflict_ids,
                self.sample_report.observation_identity_conflict_digests,
                self.sample_report.fixture_provider_conflicts,
                self.sample_report.unattributed_fixture_keys,
            )
        ):
            raise ProductionContractError(
                "sample evidence contains unresolved identity conflicts"
            )
        try:
            for receipt in self.receipts:
                receipt.validate()
            recomputed = aggregate_builder2_qualification_samples(
                self.receipts,
                minimum_sample_policy=authorization.minimum_sample_policy,
            )
        except (
            Builder2QualificationReceiptError,
            Builder2QualificationSampleAggregatorError,
        ) as exc:
            raise ProductionContractError(
                "canonical receipt evidence is invalid"
            ) from exc
        if recomputed.report_digest != self.sample_report.report_digest:
            raise ProductionContractError(
                "sample report does not match canonical receipts"
            )
        receipt_ids = {receipt.qualification_receipt_id for receipt in self.receipts}
        receipt_digests = {receipt.receipt_digest for receipt in self.receipts}
        if any(
            receipt.controlled_shadow_run_id != authorization.controlled_shadow_run_id
            or receipt.qualification_session_id
            != authorization.qualification_session_id
            or receipt.fixture_key not in authorization.fixture_scope
            for receipt in self.receipts
        ):
            raise ProductionContractError(
                "receipt run/session/fixture binding mismatch"
            )
        self._validate_audit(
            request,
            authorization,
            receipt_ids=receipt_ids,
            receipt_digests=receipt_digests,
        )
        self._validate_measurement(
            request,
            authorization,
            receipt_ids=receipt_ids,
            receipt_digests=receipt_digests,
        )

    def _validate_audit(
        self,
        request: ControlledActivationRequest,
        authorization: ControlledActivationAuthorization,
        *,
        receipt_ids: set[str],
        receipt_digests: set[str],
    ) -> None:
        audit = self.audit_report
        if audit.get("overall_state") != "COMPLETE":
            raise ProductionContractError("shadow audit is not complete")
        if audit.get("integration_sha") != request.source_sha:
            raise ProductionContractError("shadow audit source SHA mismatch")
        if audit.get("research_sha") != request.research_sha:
            raise ProductionContractError("shadow audit Research SHA mismatch")
        if audit.get("model_identity") != request.model_identity:
            raise ProductionContractError("shadow audit model identity mismatch")
        predictions = _records(audit.get("predictions"), "audit.predictions")
        if not predictions:
            raise ProductionContractError("shadow audit has no completed predictions")
        for item in predictions:
            if (
                item.get("overall_state") != "COMPLETE"
                or item.get("evidence_mode") != REAL_OBSERVED
            ):
                raise ProductionContractError(
                    "shadow audit contains non-real or incomplete evidence"
                )
            if item.get("league") != request.league_code:
                raise ProductionContractError("shadow audit league binding mismatch")
            if (
                item.get("signal_time_experiment_id")
                != authorization.signal_time_experiment_id
            ):
                raise ProductionContractError(
                    "shadow audit Signal-Time binding mismatch"
                )
            if item.get("qualification_receipt_id") not in receipt_ids:
                raise ProductionContractError(
                    "shadow audit references an unknown receipt"
                )
            if item.get("qualification_receipt_digest") not in receipt_digests:
                raise ProductionContractError(
                    "shadow audit references an unknown receipt digest"
                )
            if (
                item.get("controlled_shadow_run_id")
                != authorization.controlled_shadow_run_id
            ):
                raise ProductionContractError(
                    "shadow audit controlled-run binding mismatch"
                )
            if (
                item.get("qualification_session_id")
                != authorization.qualification_session_id
            ):
                raise ProductionContractError(
                    "shadow audit qualification-session binding mismatch"
                )

    def _validate_measurement(
        self,
        request: ControlledActivationRequest,
        authorization: ControlledActivationAuthorization,
        *,
        receipt_ids: set[str],
        receipt_digests: set[str],
    ) -> None:
        measurement = self.measurement_report
        if measurement.get("overall_state") != "COMPLETE":
            raise ProductionContractError("shadow measurement is not complete")
        if measurement.get("research_sha") != request.research_sha:
            raise ProductionContractError("shadow measurement Research SHA mismatch")
        if measurement.get("model_identity") != request.model_identity:
            raise ProductionContractError("shadow measurement model identity mismatch")
        if (
            not isinstance(measurement.get("eligible_count"), int)
            or measurement["eligible_count"] <= 0
        ):
            raise ProductionContractError(
                "shadow measurement has no eligible prediction"
            )
        safety = measurement.get("safety_invariants")
        if not isinstance(safety, Mapping):
            raise ProductionContractError(
                "shadow measurement safety invariants are missing"
            )
        for name in (
            "production_activation_authorized",
            "publication_authorized",
            "betting_authorized",
            "model_approved_for_production",
            "signal_time_approved_for_production",
            "sealed_data_accessed",
            "closing_used_for_prediction",
        ):
            if safety.get(name) is not False:
                raise ProductionContractError(
                    f"shadow measurement safety invariant is unsafe: {name}"
                )
        candidates = _records(
            measurement.get("eligible_predictions"), "measurement.eligible_predictions"
        )
        if len(candidates) != measurement["eligible_count"]:
            raise ProductionContractError(
                "shadow measurement eligible count is inconsistent"
            )
        for item in candidates:
            if item.get("evidence_mode") != REAL_OBSERVED:
                raise ProductionContractError("measurement contains non-real evidence")
            if item.get("league") != request.league_code:
                raise ProductionContractError("measurement league binding mismatch")
            if item.get("model_identity") != request.model_identity:
                raise ProductionContractError("measurement model identity mismatch")
            if item.get("research_sha") != request.research_sha:
                raise ProductionContractError("measurement Research SHA mismatch")
            if (
                item.get("signal_time_experiment_id")
                != authorization.signal_time_experiment_id
            ):
                raise ProductionContractError(
                    "measurement Signal-Time binding mismatch"
                )
            if item.get("qualification_receipt_id") not in receipt_ids:
                raise ProductionContractError(
                    "measurement references an unknown receipt"
                )
            if item.get("qualification_receipt_digest") not in receipt_digests:
                raise ProductionContractError(
                    "measurement references an unknown receipt digest"
                )
            if (
                item.get("controlled_shadow_run_id")
                != authorization.controlled_shadow_run_id
            ):
                raise ProductionContractError(
                    "measurement controlled-run binding mismatch"
                )
            if (
                item.get("qualification_session_id")
                != authorization.qualification_session_id
            ):
                raise ProductionContractError(
                    "measurement qualification-session binding mismatch"
                )


@dataclass(frozen=True)
class ControlledActivationPlan:
    request: ControlledActivationRequest
    authorization: ControlledActivationAuthorization
    evidence: Top5ControlledReleaseEvidence
    rollout_evidence: RolloutEvidence
    prepared_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "prepared_at", _utc(self.prepared_at, "prepared_at"))

    @property
    def evidence_digest(self) -> str:
        return self.evidence.evidence_digest()

    def validate(self, *, now: datetime | None = None) -> None:
        self.rollout_evidence.require(RolloutStage.CONTROLLED_ACTIVATION)
        self.authorization.validate(now=now)
        self.authorization.binds_request(self.request)
        self.evidence.validate(self.request, self.authorization)


@dataclass(frozen=True)
class ControlledActivationState:
    activation_id: str
    league_code: str
    candidate_id: str
    model_identity: str
    source_sha: str
    research_sha: str
    model_artifact_hash: str
    signal_time_experiment_id: str
    provider_authority: str
    result_authority: str
    evidence_digest: str
    activated_at: datetime
    activation_mode: ActivationMode = ActivationMode.CONTROLLED
    no_bet: bool = True
    publication_enabled: bool = False
    scheduler_enabled: bool = False
    provider_authority_created: bool = False
    ledger_mutated: bool = False

    def validate(self) -> None:
        _text(self.activation_id, "activation_id")
        _text(self.signal_time_experiment_id, "signal_time_experiment_id")
        _text(self.provider_authority, "provider_authority")
        _text(self.result_authority, "result_authority")
        _hash(self.source_sha, "source_sha")
        _hash(self.research_sha, "research_sha")
        _hash(self.model_artifact_hash, "model_artifact_hash")
        _hash(self.evidence_digest, "evidence_digest")
        _utc(self.activated_at, "activated_at")
        if ActivationMode(self.activation_mode) is not ActivationMode.CONTROLLED:
            raise ProductionContractError("activation state is not controlled")
        if (
            not self.no_bet
            or self.publication_enabled
            or self.scheduler_enabled
            or self.provider_authority_created
            or self.ledger_mutated
        ):
            raise ProductionContractError(
                "controlled activation state violates safety boundaries"
            )

    def as_bindings(self) -> dict[str, object]:
        self.validate()
        return {
            "active": True,
            "activation_id": self.activation_id,
            "league_code": self.league_code,
            "candidate_id": self.candidate_id,
            "model_identity": self.model_identity,
            "source_sha": self.source_sha,
            "research_sha": self.research_sha,
            "model_artifact_hash": self.model_artifact_hash,
            "signal_time_experiment_id": self.signal_time_experiment_id,
            "provider_authority": self.provider_authority,
            "result_authority": self.result_authority,
            "evidence_digest": self.evidence_digest,
        }


class InMemoryControlledActivationRuntime:
    """Execution-capable seam with no external production side effects."""

    def __init__(self) -> None:
        self._state: ControlledActivationState | None = None

    @property
    def state(self) -> ControlledActivationState | None:
        return self._state

    def prepare(
        self,
        request: ControlledActivationRequest,
        authorization: ControlledActivationAuthorization,
        evidence: Top5ControlledReleaseEvidence,
        rollout_evidence: RolloutEvidence,
        *,
        prepared_at: datetime,
        now: datetime | None = None,
    ) -> ControlledActivationPlan:
        plan = ControlledActivationPlan(
            request=request,
            authorization=authorization,
            evidence=evidence,
            rollout_evidence=rollout_evidence,
            prepared_at=prepared_at,
        )
        plan.validate(now=now)
        return plan

    def activate(
        self, plan: ControlledActivationPlan, *, now: datetime
    ) -> ControlledActivationState:
        if self._state is not None:
            raise ProductionContractError("a controlled activation is already active")
        plan.validate(now=now)
        state = ControlledActivationState(
            activation_id=plan.authorization.activation_id,
            league_code=plan.request.league_code,
            candidate_id=plan.request.candidate_id,
            model_identity=plan.request.model_identity,
            source_sha=plan.request.source_sha,
            research_sha=plan.request.research_sha,
            model_artifact_hash=plan.request.model_artifact_hash,
            signal_time_experiment_id=plan.authorization.signal_time_experiment_id,
            provider_authority=plan.request.provider_authority.odds_authority or "",
            result_authority=plan.request.provider_authority.result_authority or "",
            evidence_digest=plan.evidence_digest,
            activated_at=_utc(now, "activated_at"),
        )
        state.validate()
        self._state = state
        return state

    def rollback(self, trigger: RollbackTrigger) -> RollbackResult:
        result = RollbackController().rollback(trigger)
        self._state = None
        return result


@dataclass(frozen=True)
class ControlledReleaseDryRun:
    final_state: str
    reasons: tuple[str, ...]
    evidence_artifacts_present: bool
    exact_bindings_match: bool
    activation_authorization_valid: bool
    publication_authorized: bool
    rollback_target_valid: bool
    public_artifact_valid: bool
    health_preconditions_valid: bool
    no_production_mutation: bool = True

    def validate(self) -> None:
        if self.final_state not in {READY_FOR_CONTROLLED_ACTIVATION, BLOCKED}:
            raise ProductionContractError("dry-run state is invalid")
        if self.final_state == READY_FOR_CONTROLLED_ACTIVATION and self.reasons:
            raise ProductionContractError(
                "ready dry-run cannot contain blocking reasons"
            )
        if not self.no_production_mutation:
            raise ProductionContractError("dry-run must not mutate production")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "final_state": self.final_state,
            "reasons": list(self.reasons),
            "evidence_artifacts_present": self.evidence_artifacts_present,
            "exact_bindings_match": self.exact_bindings_match,
            "activation_authorization_valid": self.activation_authorization_valid,
            "publication_authorized": self.publication_authorized,
            "rollback_target_valid": self.rollback_target_valid,
            "public_artifact_valid": self.public_artifact_valid,
            "health_preconditions_valid": self.health_preconditions_valid,
            "no_production_mutation": True,
        }


class Top5ControlledRelease:
    """One operator-facing preflight/activation/publication facade."""

    def __init__(
        self,
        *,
        activation_runtime: InMemoryControlledActivationRuntime | None = None,
        publication_store: InMemoryTop5PublicationStore | None = None,
    ) -> None:
        self.activation_runtime = (
            activation_runtime or InMemoryControlledActivationRuntime()
        )
        self.publication_store = publication_store or InMemoryTop5PublicationStore()

    def dry_run(
        self,
        request: ControlledActivationRequest,
        authorization: ControlledActivationAuthorization,
        evidence: Top5ControlledReleaseEvidence,
        rollout_evidence: RolloutEvidence,
        publication_artifact: ControlledTop5PublicationPayload,
        *,
        publication_authorization: Top5PublicationAuthorization | None = None,
        health_preconditions: Mapping[str, object] | None = None,
        now: datetime,
    ) -> ControlledReleaseDryRun:
        reasons: list[str] = []
        evidence_present = bool(evidence.receipts)
        authorization_valid = False
        exact_bindings = False
        rollback_valid = False
        publication_authorized = False
        try:
            plan = self.activation_runtime.prepare(
                request,
                authorization,
                evidence,
                rollout_evidence,
                prepared_at=now,
                now=now,
            )
            authorization_valid = True
            exact_bindings = True
            rollback_valid = bool(plan.request.rollback_pointer.strip())
        except (ProductionContractError, ValueError) as exc:
            reasons.append(str(exc))
        artifact_valid = False
        try:
            publication_artifact.validate()
            if publication_artifact.activation_id != authorization.activation_id:
                raise ProductionContractError(
                    "publication artifact activation binding mismatch"
                )
            if (
                publication_artifact.provider_authority
                != authorization.provider_authority.odds_authority
            ):
                raise ProductionContractError(
                    "publication artifact provider authority mismatch"
                )
            if (
                publication_artifact.result_authority
                != authorization.provider_authority.result_authority
            ):
                raise ProductionContractError(
                    "publication artifact result authority mismatch"
                )
            if publication_artifact.evidence_digest != evidence.evidence_digest():
                raise ProductionContractError(
                    "publication artifact evidence digest mismatch"
                )
            artifact_valid = True
        except (ProductionContractError, ValueError) as exc:
            reasons.append(str(exc))
        health_valid = self._health_preconditions_valid(health_preconditions)
        if not health_valid:
            reasons.append("health/monitoring preconditions are incomplete")
        if publication_authorization is not None:
            try:
                publication_authorization.validate(now=now)
                publication_authorization.binds(publication_artifact)
                publication_authorized = True
            except (ProductionContractError, ValueError) as exc:
                reasons.append(str(exc))
        result = ControlledReleaseDryRun(
            final_state=(READY_FOR_CONTROLLED_ACTIVATION if not reasons else BLOCKED),
            reasons=tuple(dict.fromkeys(reasons)),
            evidence_artifacts_present=evidence_present,
            exact_bindings_match=exact_bindings,
            activation_authorization_valid=authorization_valid,
            publication_authorized=publication_authorized,
            rollback_target_valid=rollback_valid,
            public_artifact_valid=artifact_valid,
            health_preconditions_valid=health_valid,
        )
        result.validate()
        return result

    def activate(
        self,
        request: ControlledActivationRequest,
        authorization: ControlledActivationAuthorization,
        evidence: Top5ControlledReleaseEvidence,
        rollout_evidence: RolloutEvidence,
        *,
        now: datetime,
    ) -> ControlledActivationState:
        plan = self.activation_runtime.prepare(
            request,
            authorization,
            evidence,
            rollout_evidence,
            prepared_at=now,
            now=now,
        )
        return self.activation_runtime.activate(plan, now=now)

    def publish(
        self,
        artifact: ControlledTop5PublicationPayload,
        authorization: Top5PublicationAuthorization,
        *,
        now: datetime,
    ) -> PublishedTop5Artifact:
        state = self.activation_runtime.state
        return self.publication_store.publish(
            artifact,
            authorization,
            activation_bindings=state.as_bindings() if state else {},
            now=now,
        )

    def rollback(
        self,
        trigger: RollbackTrigger,
    ) -> tuple[RollbackResult, PublicationRollback]:
        activation_result = self.activation_runtime.rollback(trigger)
        publication_result = self.publication_store.rollback()
        return activation_result, publication_result

    @staticmethod
    def _health_preconditions_valid(health: Mapping[str, object] | None) -> bool:
        if not isinstance(health, Mapping):
            return False
        required = (
            "provider_healthy",
            "fixture_coverage_valid",
            "odds_freshness_valid",
            "signal_time_coverage_valid",
            "inference_healthy",
            "publisher_healthy",
            "result_source_healthy",
            "rollback_ready",
        )
        return all(health.get(name) is True for name in required)


__all__ = [
    "BLOCKED",
    "READY_FOR_CONTROLLED_ACTIVATION",
    "REAL_OBSERVED",
    "ControlledActivationAuthorization",
    "ControlledActivationPlan",
    "ControlledActivationState",
    "ControlledReleaseDryRun",
    "InMemoryControlledActivationRuntime",
    "Top5ControlledRelease",
    "Top5ControlledReleaseEvidence",
]
