"""Fail-closed activation state, controlled-run, and rollback contracts.

Everything here is an in-memory readiness harness.  It creates plans and
validates evidence, but contains no activation writer, scheduler, provider,
publisher, Cloudflare, or ledger dependency.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum

from src.football.production_contracts import (
    ActivationMode,
    ProductionContractError,
    RolloutEvidence,
    RolloutStage,
    SignalTimeContract,
    _utc,
)
from src.football.top5_provider_validation import ProviderAuthority


class ActivationStage(str, Enum):
    RESEARCH_APPROVED = "research_approved"
    ADAPTER_READY = "adapter_ready"
    OFFLINE_COMPATIBLE = "offline_compatible"
    SHADOW_INFERENCE = "shadow_inference"
    SIGNAL_TIME_VALIDATED = "signal_time_validated"
    PROVIDER_VALIDATED = "provider_validated"
    SHADOW_PERFORMANCE_VALIDATED = "shadow_performance_validated"
    CEO_APPROVED = "ceo_approved"
    CONTROLLED_ACTIVATION = "controlled_activation"
    PRODUCTION_VERIFIED = "production_verified"


ACTIVATION_STAGE_ORDER: tuple[ActivationStage, ...] = (
    ActivationStage.RESEARCH_APPROVED,
    ActivationStage.ADAPTER_READY,
    ActivationStage.OFFLINE_COMPATIBLE,
    ActivationStage.SHADOW_INFERENCE,
    ActivationStage.SIGNAL_TIME_VALIDATED,
    ActivationStage.PROVIDER_VALIDATED,
    ActivationStage.SHADOW_PERFORMANCE_VALIDATED,
    ActivationStage.CEO_APPROVED,
    ActivationStage.CONTROLLED_ACTIVATION,
    ActivationStage.PRODUCTION_VERIFIED,
)

CEO_DECISION_KEYS: tuple[str, ...] = (
    "production_candidate_model",
    "signal_time_numeric_configuration",
    "provider_authority",
    "result_authority",
    "quota_cost_budget",
    "minimum_shadow_observation_requirement",
    "activation_league_order",
    "activation_scope",
    "publication_policy",
)


@dataclass(frozen=True)
class CEODecisionRegister:
    """Unresolved decisions are explicit and cannot be inferred by tooling."""

    decisions: Mapping[str, str] = field(
        default_factory=lambda: {key: "unresolved" for key in CEO_DECISION_KEYS}
    )

    def validate(self) -> None:
        if set(self.decisions) != set(CEO_DECISION_KEYS):
            raise ProductionContractError("CEO decision register must contain every required decision")
        if any(not value.strip() for value in self.decisions.values()):
            raise ProductionContractError("CEO decision register contains a blank decision")

    @property
    def unresolved(self) -> tuple[str, ...]:
        self.validate()
        return tuple(key for key, value in self.decisions.items() if value == "unresolved")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {"decisions": dict(self.decisions), "unresolved": list(self.unresolved)}


@dataclass(frozen=True)
class ActivationState:
    """Explicit state snapshot; it has no external side effect."""

    evidence: RolloutEvidence = field(default_factory=RolloutEvidence)
    controlled_activation: bool = False
    production_verified: bool = False

    def completed(self, stage: ActivationStage) -> bool:
        stage = ActivationStage(stage)
        if stage is ActivationStage.CONTROLLED_ACTIVATION:
            return self.controlled_activation
        if stage is ActivationStage.PRODUCTION_VERIFIED:
            return self.production_verified
        field_name = {
            ActivationStage.RESEARCH_APPROVED: "research_approved",
            ActivationStage.ADAPTER_READY: "adapter_ready",
            ActivationStage.OFFLINE_COMPATIBLE: "offline_compatible",
            ActivationStage.SHADOW_INFERENCE: "shadow_inference",
            ActivationStage.SIGNAL_TIME_VALIDATED: "signal_time_validated",
            ActivationStage.PROVIDER_VALIDATED: "provider_validated",
            ActivationStage.SHADOW_PERFORMANCE_VALIDATED: "shadow_performance",
            ActivationStage.CEO_APPROVED: "ceo_approved",
        }[stage]
        return bool(getattr(self.evidence, field_name))


@dataclass(frozen=True)
class ActivationGateResult:
    stage: ActivationStage
    passed: bool
    missing_predecessors: tuple[ActivationStage, ...]

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(stage.value for stage in self.missing_predecessors)

    def as_payload(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "passed": self.passed,
            "missing_predecessors": list(self.failures),
        }


class ActivationStateMachine:
    """Cumulative state machine for future controlled activation."""

    def __init__(self, state: ActivationState | None = None) -> None:
        self._state = state or ActivationState()

    @property
    def state(self) -> ActivationState:
        return self._state

    def check(self, stage: ActivationStage) -> ActivationGateResult:
        target = ActivationStage(stage)
        index = ACTIVATION_STAGE_ORDER.index(target)
        # Evidence stages are checks of completed evidence.  Controlled
        # activation and production verification are transitions, so their
        # own flags become true only after the requested transition succeeds.
        required = ACTIVATION_STAGE_ORDER[:index + 1]
        if target is ActivationStage.CONTROLLED_ACTIVATION or target is ActivationStage.PRODUCTION_VERIFIED:
            required = ACTIVATION_STAGE_ORDER[:index]
        missing = tuple(candidate for candidate in required if not self._state.completed(candidate))
        return ActivationGateResult(target, not missing, missing)

    def require(self, stage: ActivationStage) -> None:
        result = self.check(stage)
        if not result.passed:
            raise ProductionContractError(
                f"activation stage {result.stage.value} missing: {', '.join(result.failures)}"
            )

    def enter(self, stage: ActivationStage) -> ActivationState:
        """Advance an in-memory state snapshot after explicit validation."""

        target = ActivationStage(stage)
        self.require(target)
        if target is ActivationStage.CONTROLLED_ACTIVATION:
            self._state = replace(
                self._state,
                controlled_activation=True,
                evidence=replace(self._state.evidence, controlled_activation=True),
            )
        elif target is ActivationStage.PRODUCTION_VERIFIED:
            self._state = replace(
                self._state,
                production_verified=True,
                evidence=replace(self._state.evidence, production_verified=True),
            )
        return self._state


@dataclass(frozen=True)
class ActivationPreflight:
    """All checks required before a future controlled run can be prepared."""

    source_sha_matches: bool = False
    research_sha_matches: bool = False
    model_hash_matches: bool = False
    league_matches: bool = False
    signal_time_configured: bool = False
    provider_authority_configured: bool = False
    rollback_ready: bool = False
    no_closing_leakage: bool = False
    publication_disabled: bool = True
    no_bet: bool = True

    @property
    def passed(self) -> bool:
        return all((
            self.source_sha_matches,
            self.research_sha_matches,
            self.model_hash_matches,
            self.league_matches,
            self.signal_time_configured,
            self.provider_authority_configured,
            self.rollback_ready,
            self.no_closing_leakage,
            self.publication_disabled,
            self.no_bet,
        ))

    def validate(self) -> None:
        if self.passed and not self.publication_disabled:
            raise ProductionContractError("activation preflight cannot enable publication")

    def failures(self) -> tuple[str, ...]:
        return tuple(
            name for name, passed in (
                ("source_sha_matches", self.source_sha_matches),
                ("research_sha_matches", self.research_sha_matches),
                ("model_hash_matches", self.model_hash_matches),
                ("league_matches", self.league_matches),
                ("signal_time_configured", self.signal_time_configured),
                ("provider_authority_configured", self.provider_authority_configured),
                ("rollback_ready", self.rollback_ready),
                ("no_closing_leakage", self.no_closing_leakage),
                ("publication_disabled", self.publication_disabled),
                ("no_bet", self.no_bet),
            ) if not passed
        )

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {"passed": self.passed, "failures": list(self.failures())}


@dataclass(frozen=True)
class ControlledActivationRequest:
    """A future authorization envelope; it is not executable in this harness."""

    league_code: str
    candidate_id: str
    model_identity: str
    source_sha: str
    research_sha: str
    model_artifact_hash: str
    provider_authority: ProviderAuthority
    signal_time_contract: SignalTimeContract
    rollback_pointer: str
    config_snapshot: Mapping[str, object]
    ceo_authorization_token: str | None = None
    ceo_authorized: bool = False
    activation_mode: ActivationMode = ActivationMode.CONTROLLED
    publication_enabled: bool = False
    no_bet: bool = True

    def validate(self) -> None:
        required = {
            "league_code": self.league_code,
            "candidate_id": self.candidate_id,
            "model_identity": self.model_identity,
            "source_sha": self.source_sha,
            "research_sha": self.research_sha,
            "model_artifact_hash": self.model_artifact_hash,
            "rollback_pointer": self.rollback_pointer,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ProductionContractError(f"activation request lacks identity: {', '.join(missing)}")
        for name, value in (
            ("source_sha", self.source_sha),
            ("research_sha", self.research_sha),
            ("model_artifact_hash", self.model_artifact_hash),
        ):
            if not _is_hash(value):
                raise ProductionContractError(f"{name} must be an exact hexadecimal artifact hash")
        self.provider_authority.validate()
        if not self.provider_authority.is_complete:
            raise ProductionContractError("activation request requires all provider authorities")
        self.signal_time_contract.validate()
        if not self.config_snapshot:
            raise ProductionContractError("activation request requires a deterministic config snapshot")
        if not self.ceo_authorized or not self.ceo_authorization_token or not self.ceo_authorization_token.strip():
            raise ProductionContractError("activation request requires explicit CEO authorization")
        if ActivationMode(self.activation_mode) is not ActivationMode.CONTROLLED:
            raise ProductionContractError("future Top-5 runs must use controlled activation mode")
        if self.publication_enabled or not self.no_bet:
            raise ProductionContractError("controlled readiness request must remain unpublished and no-bet")


@dataclass(frozen=True)
class PreparedActivation:
    request: ControlledActivationRequest
    preflight: ActivationPreflight
    prepared_at: datetime
    executed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "prepared_at", _utc(self.prepared_at, "prepared_at"))

    def validate(self) -> None:
        self.request.validate()
        self.preflight.validate()
        if not self.preflight.passed:
            raise ProductionContractError(
                f"activation preflight failed: {', '.join(self.preflight.failures())}"
            )
        if self.executed:
            raise ProductionContractError("readiness harness cannot execute activation")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "league": self.request.league_code,
            "candidate": self.request.candidate_id,
            "model": self.request.model_identity,
            "source_sha": self.request.source_sha,
            "research_sha": self.request.research_sha,
            "model_artifact_hash": self.request.model_artifact_hash,
            "rollback_pointer": self.request.rollback_pointer,
            "prepared_at": self.prepared_at.isoformat(),
            "executed": False,
            "publication_enabled": False,
            "no_bet": True,
        }


@dataclass(frozen=True)
class ProductionVerification:
    """Post-run evidence checklist; it does not change production state."""

    source_sha_matches: bool = False
    research_sha_matches: bool = False
    model_hash_matches: bool = False
    league_matches: bool = False
    signal_time_matches: bool = False
    provider_authority_matches: bool = False
    no_closing_leakage: bool = False
    pwa_available: bool = False
    publisher_healthy: bool = False
    ledger_safe: bool = False
    settlement_compatible: bool = False
    rollback_ready: bool = False

    @property
    def passed(self) -> bool:
        return all((
            self.source_sha_matches,
            self.research_sha_matches,
            self.model_hash_matches,
            self.league_matches,
            self.signal_time_matches,
            self.provider_authority_matches,
            self.no_closing_leakage,
            self.pwa_available,
            self.publisher_healthy,
            self.ledger_safe,
            self.settlement_compatible,
            self.rollback_ready,
        ))

    def failures(self) -> tuple[str, ...]:
        return tuple(
            name for name, passed in (
                ("source_sha_matches", self.source_sha_matches),
                ("research_sha_matches", self.research_sha_matches),
                ("model_hash_matches", self.model_hash_matches),
                ("league_matches", self.league_matches),
                ("signal_time_matches", self.signal_time_matches),
                ("provider_authority_matches", self.provider_authority_matches),
                ("no_closing_leakage", self.no_closing_leakage),
                ("pwa_available", self.pwa_available),
                ("publisher_healthy", self.publisher_healthy),
                ("ledger_safe", self.ledger_safe),
                ("settlement_compatible", self.settlement_compatible),
                ("rollback_ready", self.rollback_ready),
            ) if not passed
        )

    def validate(self) -> None:
        if self.passed and not self.ledger_safe:
            raise ProductionContractError("production verification cannot pass with ledger risk")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {"passed": self.passed, "failures": list(self.failures())}


class ControlledActivationHarness:
    """Prepare a controlled run while making execution impossible."""

    def prepare(
        self,
        request: ControlledActivationRequest,
        evidence: RolloutEvidence,
        preflight: ActivationPreflight,
        *,
        prepared_at: datetime,
    ) -> PreparedActivation:
        evidence.require(RolloutStage.CEO_APPROVED)
        request.validate()
        preflight.validate()
        if not preflight.passed:
            raise ProductionContractError(
                f"activation preflight failed: {', '.join(preflight.failures())}"
            )
        plan = PreparedActivation(request, preflight, prepared_at)
        plan.validate()
        return plan

    def execute(self, _plan: PreparedActivation) -> None:
        raise ProductionContractError("controlled activation execution is disabled in readiness harness")

    def verify(
        self,
        _plan: PreparedActivation,
        verification: ProductionVerification,
    ) -> ProductionVerification:
        """Validate future post-activation evidence without writing state."""

        verification.validate()
        if not verification.passed:
            raise ProductionContractError(
                f"production verification failed: {', '.join(verification.failures())}"
            )
        return verification


class RollbackTrigger(str, Enum):
    PROVIDER_FAILURE = "provider_failure"
    STALE_MARKET_DATA = "stale_market_data"
    INFERENCE_FAILURE = "inference_failure"
    MALFORMED_PREDICTION = "malformed_prediction"
    PUBLICATION_ERROR = "publication_error"
    SCHEDULER_ERROR = "scheduler_error"
    HEALTH_DEGRADATION = "health_degradation"
    DUPLICATE_SIGNAL = "duplicate_signal"
    WRONG_LEAGUE_MAPPING = "wrong_league_mapping"
    MODEL_PROVENANCE_MISMATCH = "model_provenance_mismatch"


@dataclass(frozen=True)
class RollbackResult:
    trigger: RollbackTrigger
    restored_disabled: bool = True
    activation_mode: ActivationMode = ActivationMode.DISABLED
    no_bet: bool = True
    publication_enabled: bool = False
    scheduler_enabled: bool = False
    ledger_mutated: bool = False

    def validate(self) -> None:
        if not self.restored_disabled or self.activation_mode is not ActivationMode.DISABLED:
            raise ProductionContractError("rollback must restore disabled mode")
        if not self.no_bet or self.publication_enabled or self.scheduler_enabled or self.ledger_mutated:
            raise ProductionContractError("rollback safety flags are invalid")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "trigger": self.trigger.value,
            "restored_disabled": True,
            "activation_mode": ActivationMode.DISABLED.value,
            "no_bet": True,
            "publication_enabled": False,
            "scheduler_enabled": False,
            "ledger_mutated": False,
        }


class RollbackController:
    """Return an in-memory safe-state result for every injected trigger."""

    def restore_safe_disabled_state(self, trigger: RollbackTrigger) -> RollbackResult:
        result = RollbackResult(RollbackTrigger(trigger))
        result.validate()
        return result

    rollback = restore_safe_disabled_state


def _is_hash(value: str) -> bool:
    return bool(re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{40,64}", value.strip()))


def activation_stage_matrix() -> tuple[ActivationStage, ...]:
    return ACTIVATION_STAGE_ORDER
