"""Deterministic, candidate-only model-family retrain adapters.

This module is the model-training half of the universal learning loop.  It is
deliberately a plan/execution boundary, not a scheduler and not a promotion
mechanism:

* plans are driven by :class:`CausalTrainingRowV1` identities and their
  result-safe timestamps, never by signal or bet outcomes;
* execution is opt-in and receives an injected trainer and validator;
* trainers write only to a caller-owned staging directory;
* an incomplete candidate can never be described as active.

The family specifications below are an audit of the current repository.  They
do not import or call provider, credential, ledger, publication, or production
workflow code.  In particular, the active Nations League v1.1 release remains
Elo-based; its tree model is represented as a candidate only.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .outcome_contracts import (
    CausalTrainingRowV1,
    LifecycleError,
    RetrainDecision,
    append_training_rows,
    canonical_digest,
)

RETRAIN_PLAN_SCHEMA = "sportsbrain-model-retrain-plan-v1"
RETRAIN_RECEIPT_SCHEMA = "sportsbrain-model-retrain-receipt-v1"
VALIDATION_RECEIPT_SCHEMA = "sportsbrain-model-validation-receipt-v1"

# These names are result/settlement fields, not point-in-time predictors.  A
# row containing one as a feature is rejected rather than silently treating a
# post-match value as a causal input.
_FORBIDDEN_FEATURE_NAMES = frozenset(
    {
        "actual_result",
        "away_score",
        "home_score",
        "label",
        "outcome",
        "result",
        "settlement_state",
        "winner",
    }
)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError(f"{field} is required")
    return value.strip()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_digest(path: Path) -> str:
    """Hash one staged file or directory deterministically."""

    path = Path(path)
    if not path.exists():
        raise LifecycleError("candidate artifact path does not exist")
    if path.is_symlink():
        raise LifecycleError("candidate artifact path must not be a symlink")
    if path.is_file():
        return _sha_file(path)
    entries: list[dict[str, str]] = []
    for child in sorted(path.rglob("*")):
        if child.is_symlink() or not child.is_file():
            continue
        entries.append(
            {
                "path": child.relative_to(path).as_posix(),
                "sha256": _sha_file(child),
            }
        )
    if not entries:
        raise LifecycleError("candidate artifact is empty")
    return canonical_digest(
        {"schema": "sportsbrain-artifact-tree-v1", "files": entries}
    )


def _feature_schema(rows: Sequence[CausalTrainingRowV1]) -> tuple[str, ...]:
    if not rows:
        return ()
    expected = tuple(sorted(str(key) for key in rows[0].features))
    for row in rows:
        actual = tuple(sorted(str(key) for key in row.features))
        if actual != expected:
            raise LifecycleError("causal rows have inconsistent feature schemas")
        for name in actual:
            if name.casefold() in _FORBIDDEN_FEATURE_NAMES:
                raise LifecycleError(f"feature {name!r} is a result-leakage field")
    return expected


def feature_schema_digest(rows: Iterable[CausalTrainingRowV1]) -> str:
    """Return the stable digest of the validated feature-name schema."""

    normalized = _validated_rows(rows)
    return canonical_digest(
        {
            "schema": "sportsbrain-causal-feature-schema-v1",
            "features": list(_feature_schema(normalized)),
        }
    )


def training_data_digest(rows: Iterable[CausalTrainingRowV1]) -> str:
    """Digest the complete sorted causal rows, including true labels."""

    normalized = _validated_rows(rows)
    return canonical_digest([row.to_payload() for row in normalized])


def authoritative_result_digest(rows: Iterable[CausalTrainingRowV1]) -> str:
    """Digest the result identities bound to a causal dataset."""

    normalized = _validated_rows(rows)
    return canonical_digest(sorted(row.result_id for row in normalized))


def _validated_rows(
    rows: Iterable[CausalTrainingRowV1],
) -> tuple[CausalTrainingRowV1, ...]:
    materialized = tuple(rows)
    for row in materialized:
        if not isinstance(row, CausalTrainingRowV1):
            raise LifecycleError("retrain adapters accept only CausalTrainingRowV1")
        if not row.label:
            raise LifecycleError("causal training row has no true result label")
    normalized = append_training_rows((), materialized)
    _feature_schema(normalized)
    return normalized


@dataclass(frozen=True)
class ModelFamilySpec:
    """Governed audit metadata for one model family."""

    sport: str
    competition: str
    model_family: str
    components: tuple[str, ...]
    lgbm_included: bool
    lgbm_active: bool
    lgbm_reason: str | None
    current_artifact: str
    trainer_entrypoint: str
    governance_blocked: bool = False
    governance_reason: str | None = None
    stacker_cadence: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "sport",
            "competition",
            "model_family",
            "current_artifact",
            "trainer_entrypoint",
        ):
            _text(getattr(self, name), name)
        if not self.components:
            raise LifecycleError("model family must have a component")
        if self.lgbm_included and self.lgbm_reason is not None:
            raise LifecycleError("included LGBM must not have an exclusion reason")
        if not self.lgbm_included and not self.lgbm_reason:
            raise LifecycleError("excluded LGBM requires an explicit reason")
        if self.governance_blocked and not self.governance_reason:
            raise LifecycleError("governance-blocked family requires a reason")

    def to_payload(self) -> dict[str, Any]:
        return {
            "sport": self.sport,
            "competition": self.competition,
            "model_family": self.model_family,
            "components": list(self.components),
            "lgbm_included": self.lgbm_included,
            "lgbm_active": self.lgbm_active,
            "lgbm_reason": self.lgbm_reason,
            "current_artifact": self.current_artifact,
            "trainer_entrypoint": self.trainer_entrypoint,
            "governance_blocked": self.governance_blocked,
            "governance_reason": self.governance_reason,
            "stacker_cadence": self.stacker_cadence,
        }


@dataclass(frozen=True)
class RetrainPlan:
    """Complete deterministic decision for one model-family extension."""

    sport: str
    competition: str
    model_family: str
    previous_training_digest: str
    new_training_digest: str
    previous_result_digest: str
    new_result_digest: str
    causal_training_row_count: int
    new_result_count: int
    newly_result_safe_count: int
    affected_components: tuple[str, ...]
    component_status: Mapping[str, str]
    lgbm_included: bool
    lgbm_active: bool
    lgbm_reason: str | None
    feature_schema_digest: str
    current_release_id: str
    candidate_release_identities: tuple[str, ...]
    candidate_artifact_identities: tuple[str, ...]
    decision: RetrainDecision
    reason: str
    plan_digest: str

    def to_payload_without_digest(self) -> dict[str, Any]:
        return {
            "schema": RETRAIN_PLAN_SCHEMA,
            "sport": self.sport,
            "competition": self.competition,
            "model_family": self.model_family,
            "previous_training_digest": self.previous_training_digest,
            "new_training_digest": self.new_training_digest,
            "previous_result_digest": self.previous_result_digest,
            "new_result_digest": self.new_result_digest,
            "causal_training_row_count": self.causal_training_row_count,
            "new_result_count": self.new_result_count,
            "newly_result_safe_count": self.newly_result_safe_count,
            "affected_components": list(self.affected_components),
            "component_status": dict(sorted(self.component_status.items())),
            "lgbm_included": self.lgbm_included,
            "lgbm_active": self.lgbm_active,
            "lgbm_reason": self.lgbm_reason,
            "feature_schema_digest": self.feature_schema_digest,
            "current_release_id": self.current_release_id,
            "candidate_release_identities": list(self.candidate_release_identities),
            "candidate_artifact_identities": list(self.candidate_artifact_identities),
            "decision": self.decision.value,
            "reason": self.reason,
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self.to_payload_without_digest(), "plan_digest": self.plan_digest}


@dataclass(frozen=True)
class ValidationReceipt:
    """Validation evidence returned by an injected candidate validator."""

    training_rows: int
    validation_rows: int
    class_distribution: Mapping[str, int]
    brier: float | None
    log_loss: float | None
    calibration_ece: float | None
    active_baseline_comparison: Mapping[str, Any]
    feature_schema_digest: str
    artifact_digest: str
    passed: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": VALIDATION_RECEIPT_SCHEMA,
            "training_rows": self.training_rows,
            "validation_rows": self.validation_rows,
            "class_distribution": dict(sorted(self.class_distribution.items())),
            "brier": self.brier,
            "log_loss": self.log_loss,
            "calibration_ece": self.calibration_ece,
            "active_baseline_comparison": dict(self.active_baseline_comparison),
            "feature_schema_digest": self.feature_schema_digest,
            "artifact_digest": self.artifact_digest,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class RetrainReceipt:
    """Candidate execution receipt; no field represents activation."""

    model_family: str
    decision: RetrainDecision
    status: str
    candidate_release_id: str | None
    candidate_artifact_digest: str | None
    validation: Mapping[str, Any] | None
    failure_reason: str | None
    active_untouched: bool
    receipt_digest: str

    def to_payload_without_digest(self) -> dict[str, Any]:
        return {
            "schema": RETRAIN_RECEIPT_SCHEMA,
            "model_family": self.model_family,
            "decision": self.decision.value,
            "status": self.status,
            "candidate_release_id": self.candidate_release_id,
            "candidate_artifact_digest": self.candidate_artifact_digest,
            "validation": dict(self.validation)
            if self.validation is not None
            else None,
            "failure_reason": self.failure_reason,
            "active_untouched": self.active_untouched,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.to_payload_without_digest(),
            "receipt_digest": self.receipt_digest,
        }


class CandidateTrainer(Protocol):
    def __call__(
        self, rows: Sequence[CausalTrainingRowV1], output_dir: Path
    ) -> None: ...


class CandidateValidator(Protocol):
    def __call__(
        self, rows: Sequence[CausalTrainingRowV1], artifact_dir: Path
    ) -> Mapping[str, Any]: ...


def _receipt(
    *,
    model_family: str,
    decision: RetrainDecision,
    status: str,
    candidate_release_id: str | None = None,
    candidate_artifact_digest: str | None = None,
    validation: Mapping[str, Any] | None = None,
    failure_reason: str | None = None,
) -> RetrainReceipt:
    base = {
        "schema": RETRAIN_RECEIPT_SCHEMA,
        "model_family": model_family,
        "decision": decision.value,
        "status": status,
        "candidate_release_id": candidate_release_id,
        "candidate_artifact_digest": candidate_artifact_digest,
        "validation": dict(validation) if validation is not None else None,
        "failure_reason": failure_reason,
        "active_untouched": True,
    }
    return RetrainReceipt(
        model_family=model_family,
        decision=decision,
        status=status,
        candidate_release_id=candidate_release_id,
        candidate_artifact_digest=candidate_artifact_digest,
        validation=validation,
        failure_reason=failure_reason,
        active_untouched=True,
        receipt_digest=canonical_digest(base),
    )


def _default_class_distribution(rows: Sequence[CausalTrainingRowV1]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        label = row.label.get("outcome", row.label.get("winner", "unclassified"))
        key = str(label)
        counts[key] = counts.get(key, 0) + 1
    return counts


class ModelFamilyRetrainAdapter:
    """Plan and explicitly execute a candidate for one audited family."""

    spec: ModelFamilySpec

    def __init__(self, spec: ModelFamilySpec) -> None:
        self.spec = spec

    def plan(
        self,
        existing_rows: Iterable[CausalTrainingRowV1],
        candidate_rows: Iterable[CausalTrainingRowV1],
        *,
        current_release_id: str,
    ) -> RetrainPlan:
        _text(current_release_id, "current_release_id")
        existing = _validated_rows(existing_rows)
        incoming = _validated_rows(candidate_rows)
        merged = append_training_rows(existing, incoming)
        existing_ids = {row.row_id for row in existing}
        new_rows = tuple(row for row in merged if row.row_id not in existing_ids)
        previous_digest = training_data_digest(existing)
        new_digest = training_data_digest(merged)
        previous_result = authoritative_result_digest(existing)
        new_result = authoritative_result_digest(merged)
        existing_result_ids = {row.result_id for row in existing}
        new_result_ids = {
            row.result_id for row in merged if row.result_id not in existing_result_ids
        }
        feature_digest = feature_schema_digest(merged)
        candidate_identity = canonical_digest(
            {
                "schema": "sportsbrain-candidate-release-identity-v1",
                "model_family": self.spec.model_family,
                "training_data_digest": new_digest,
                "feature_schema_digest": feature_digest,
                "current_release_id": current_release_id,
            }
        )

        statuses: dict[str, str] = {}
        if not new_rows:
            decision = RetrainDecision.NO_OP
            reason = "authoritative causal training digest unchanged"
            affected: tuple[str, ...] = ()
            statuses = {component: "UNCHANGED" for component in self.spec.components}
            candidate_ids: tuple[str, ...] = ()
        elif self.spec.governance_blocked:
            decision = RetrainDecision.RETRAIN_BLOCKED_BY_GOVERNANCE
            reason = self.spec.governance_reason or "family retraining is not approved"
            affected = self.spec.components
            statuses = {
                component: "BLOCKED_BY_GOVERNANCE" for component in self.spec.components
            }
            candidate_ids = (candidate_identity,)
        else:
            decision = RetrainDecision.RETRAIN_REQUIRED
            reason = "authoritative causal training digest changed"
            affected_list = list(self.spec.components)
            statuses = {component: "RETRAIN_REQUIRED" for component in affected_list}
            if self.spec.stacker_cadence is not None:
                if len(new_rows) < self.spec.stacker_cadence:
                    statuses["stacker"] = "DEFERRED_EVIDENCE_CADENCE"
                    affected_list = [
                        item for item in affected_list if item != "stacker"
                    ]
                else:
                    statuses["stacker"] = "RETRAIN_REQUIRED"
            affected = tuple(affected_list)
            candidate_ids = (candidate_identity,)

        payload = {
            "schema": RETRAIN_PLAN_SCHEMA,
            "sport": self.spec.sport,
            "competition": self.spec.competition,
            "model_family": self.spec.model_family,
            "previous_training_digest": previous_digest,
            "new_training_digest": new_digest,
            "previous_result_digest": previous_result,
            "new_result_digest": new_result,
            "causal_training_row_count": len(merged),
            "new_result_count": len(new_result_ids),
            "newly_result_safe_count": len(new_rows),
            "affected_components": list(affected),
            "component_status": dict(sorted(statuses.items())),
            "lgbm_included": self.spec.lgbm_included,
            "lgbm_active": self.spec.lgbm_active,
            "lgbm_reason": self.spec.lgbm_reason,
            "feature_schema_digest": feature_digest,
            "current_release_id": current_release_id,
            "candidate_release_identities": list(candidate_ids),
            "candidate_artifact_identities": [],
            "decision": decision.value,
            "reason": reason,
        }
        return RetrainPlan(
            sport=self.spec.sport,
            competition=self.spec.competition,
            model_family=self.spec.model_family,
            previous_training_digest=previous_digest,
            new_training_digest=new_digest,
            previous_result_digest=previous_result,
            new_result_digest=new_result,
            causal_training_row_count=len(merged),
            new_result_count=len(new_result_ids),
            newly_result_safe_count=len(new_rows),
            affected_components=affected,
            component_status=statuses,
            lgbm_included=self.spec.lgbm_included,
            lgbm_active=self.spec.lgbm_active,
            lgbm_reason=self.spec.lgbm_reason,
            feature_schema_digest=feature_digest,
            current_release_id=current_release_id,
            candidate_release_identities=candidate_ids,
            candidate_artifact_identities=(),
            decision=decision,
            reason=reason,
            plan_digest=canonical_digest(payload),
        )

    def execute(
        self,
        plan: RetrainPlan,
        rows: Iterable[CausalTrainingRowV1],
        *,
        trainer: CandidateTrainer | None = None,
        validator: CandidateValidator | None = None,
        staging_root: Path | None = None,
        execute: bool = False,
    ) -> RetrainReceipt:
        """Materialize a candidate only when explicit execution is requested.

        The caller supplies the trainer and validator.  No repository model
        path, active pointer, provider, or ledger path is accepted here.
        """

        if not execute:
            return _receipt(
                model_family=self.spec.model_family,
                decision=plan.decision,
                status="PLAN_ONLY",
                failure_reason="explicit execute=True is required",
            )
        if plan.decision is not RetrainDecision.RETRAIN_REQUIRED:
            return _receipt(
                model_family=self.spec.model_family,
                decision=plan.decision,
                status="NOT_EXECUTED",
                failure_reason=plan.reason,
            )
        if trainer is None or validator is None:
            return _receipt(
                model_family=self.spec.model_family,
                decision=plan.decision,
                status="INCOMPLETE",
                failure_reason="injected trainer and validator are required",
            )

        normalized = _validated_rows(rows)
        if training_data_digest(normalized) != plan.new_training_digest:
            return _receipt(
                model_family=self.spec.model_family,
                decision=plan.decision,
                status="INCOMPLETE",
                failure_reason="execution rows do not match the planned causal digest",
            )

        owned_root = False
        if staging_root is None:
            staging_root = Path(tempfile.mkdtemp(prefix="sportsbrain-retrain-"))
            owned_root = True
        else:
            staging_root = Path(staging_root)
            staging_root.mkdir(parents=True, exist_ok=True)
        candidate_dir = (
            staging_root / f"{self.spec.model_family}-{plan.plan_digest[:16]}"
        )
        try:
            candidate_dir.mkdir()
            trainer(normalized, candidate_dir)
            digest = artifact_digest(candidate_dir)
            raw_validation = validator(normalized, candidate_dir)
            if not isinstance(raw_validation, Mapping):
                raise LifecycleError("candidate validator must return an object")
            validation = {
                "schema": VALIDATION_RECEIPT_SCHEMA,
                "training_rows": len(normalized),
                "validation_rows": raw_validation.get("validation_rows"),
                "class_distribution": dict(
                    raw_validation.get(
                        "class_distribution", _default_class_distribution(normalized)
                    )
                ),
                "brier": raw_validation.get("brier"),
                "log_loss": raw_validation.get("log_loss"),
                "calibration_ece": raw_validation.get("calibration_ece"),
                "active_baseline_comparison": dict(
                    raw_validation.get("active_baseline_comparison", {})
                ),
                "feature_schema_digest": plan.feature_schema_digest,
                "artifact_digest": digest,
                "passed": bool(raw_validation.get("passed", True)),
            }
            candidate_release_id = canonical_digest(
                {
                    "candidate_release_identity": plan.candidate_release_identities[0],
                    "artifact_digest": digest,
                }
            )
            if not validation["passed"]:
                raise LifecycleError("candidate validation gate failed")
            return _receipt(
                model_family=self.spec.model_family,
                decision=plan.decision,
                status="COMPLETE",
                candidate_release_id=candidate_release_id,
                candidate_artifact_digest=digest,
                validation=validation,
            )
        except Exception as exc:  # noqa: BLE001 - candidate failures become receipts
            return _receipt(
                model_family=self.spec.model_family,
                decision=plan.decision,
                status="INCOMPLETE",
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
        finally:
            # Keep the staged evidence for caller inspection.  The temporary
            # root is intentionally not removed here; no active path is ever
            # touched, and cleanup remains the caller's responsibility.
            if owned_root:
                os.chmod(staging_root, 0o700)


class TennisLGBMRetrainAdapter(ModelFamilyRetrainAdapter):
    def __init__(self) -> None:
        super().__init__(
            ModelFamilySpec(
                sport="tennis",
                competition="tennis_tour",
                model_family="tennis_lgbm",
                components=(
                    "tennis_lgbm",
                    "tennis_lgbm_calibrator",
                    "signal_meta_calibration",
                ),
                lgbm_included=True,
                lgbm_active=True,
                lgbm_reason=None,
                current_artifact="models/tennis_lgbm/{model.pkl,calibrator.pkl,metadata.json}",
                trainer_entrypoint="scripts/tennis_train.py",
            )
        )


class Bundesliga2RetrainAdapter(ModelFamilyRetrainAdapter):
    def __init__(self) -> None:
        super().__init__(
            ModelFamilySpec(
                sport="football",
                competition="2. Bundesliga",
                model_family="bundesliga2_ensemble",
                components=("dixon_coles", "elo", "bundesliga2_lgbm", "calibrator"),
                lgbm_included=True,
                lgbm_active=False,
                lgbm_reason=None,
                current_artifact="models/lgbm_bundesliga2/{model.pkl,calibrators.pkl,gate.json}",
                trainer_entrypoint="scripts/train_lgbm_bundesliga2.py",
            )
        )


class GenericFootballRetrainAdapter(ModelFamilyRetrainAdapter):
    def __init__(self) -> None:
        super().__init__(
            ModelFamilySpec(
                sport="football",
                competition="international_football",
                model_family="international_dc_elo_lgbm_stacker",
                components=("dixon_coles", "elo", "lgbm", "stacker"),
                lgbm_included=True,
                lgbm_active=True,
                lgbm_reason=None,
                current_artifact="models/{dixon_coles,lgbm}",
                trainer_entrypoint="scripts/train_lgbm.py",
                stacker_cadence=3,
            )
        )


class NationsLeagueRetrainAdapter(ModelFamilyRetrainAdapter):
    def __init__(self) -> None:
        super().__init__(
            ModelFamilySpec(
                sport="football",
                competition="UEFA Nations League",
                model_family="nations_league_v1_1_tree_candidate",
                components=("nations_league_v1_1_elo", "nations_league_tree_candidate"),
                lgbm_included=True,
                lgbm_active=False,
                lgbm_reason=None,
                current_artifact="candidate-only:nations_league_tree_model",
                trainer_entrypoint="research-only:causal-gbt-adapter",
            )
        )


class Top5RetrainAdapter(ModelFamilyRetrainAdapter):
    def __init__(self) -> None:
        super().__init__(
            ModelFamilySpec(
                sport="football",
                competition="Top-5",
                model_family="top5_m5_candidate",
                components=("top5_candidate",),
                lgbm_included=False,
                lgbm_active=False,
                lgbm_reason="approved M5 is a market formula; M3/M4 LGBM candidates remain disabled",
                current_artifact="candidate-only:M5_market_preclose",
                trainer_entrypoint="NOT_APPROVED",
                governance_blocked=True,
                governance_reason="Top-5 production retraining is not approved by governance",
            )
        )


def build_default_adapters() -> tuple[ModelFamilyRetrainAdapter, ...]:
    """Return the audited families in deterministic order."""

    return (
        TennisLGBMRetrainAdapter(),
        Bundesliga2RetrainAdapter(),
        GenericFootballRetrainAdapter(),
        NationsLeagueRetrainAdapter(),
        Top5RetrainAdapter(),
    )


# Short names keep the seam convenient for family-specific callers while the
# explicit names above remain the canonical audit/reporting API.
TennisRetrainAdapter = TennisLGBMRetrainAdapter
BL2RetrainAdapter = Bundesliga2RetrainAdapter
InternationalFootballRetrainAdapter = GenericFootballRetrainAdapter
NLTreeCandidateRetrainAdapter = NationsLeagueRetrainAdapter


__all__ = [
    "RETRAIN_PLAN_SCHEMA",
    "RETRAIN_RECEIPT_SCHEMA",
    "BL2RetrainAdapter",
    "Bundesliga2RetrainAdapter",
    "CandidateTrainer",
    "CandidateValidator",
    "GenericFootballRetrainAdapter",
    "InternationalFootballRetrainAdapter",
    "ModelFamilyRetrainAdapter",
    "ModelFamilySpec",
    "NLTreeCandidateRetrainAdapter",
    "NationsLeagueRetrainAdapter",
    "RetrainPlan",
    "RetrainReceipt",
    "TennisLGBMRetrainAdapter",
    "TennisRetrainAdapter",
    "Top5RetrainAdapter",
    "ValidationReceipt",
    "artifact_digest",
    "authoritative_result_digest",
    "build_default_adapters",
    "feature_schema_digest",
    "training_data_digest",
]
