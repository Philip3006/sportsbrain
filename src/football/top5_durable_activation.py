"""Durable, fail-closed control plane for the manual one-league Top-5 canary.

This module persists verified preparation, execution and rollback evidence
outside the checkout. Production execution is delegated to the narrow one-shot
runtime and its live route consumer; no recurring scheduler or global routing
writer is introduced.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import TracebackType

from src.football.production_contracts import (
    ProductionContractError,
    RuntimeStateBinding,
    RuntimeStateOwner,
    _utc,
)
from src.football.provider_cascade.contracts import CANDIDATE_ONLY_PROVIDER_IDENTITIES
from src.football.top5_b2_qualification_batch_orchestrator import (
    Builder2FiveLeagueReceiptPackageV1,
    Builder2QualificationBatchError,
)
from src.football.top5_controlled_release import (
    ApprovedProviderResultAuthority,
    ControlledActivationAuthorization,
)
from src.football.top5_controlled_shadow_provider_qualification import TOP5_LEAGUES
from src.football.top5_final_acceptance import (
    ACTIVE_PROVIDER,
    CANDIDATE_PROVIDER,
    STATUS_VERIFIED,
    Top5FinalAcceptanceError,
    canonical_digest,
    verify_final_acceptance,
)
from src.football.top5_production_activation import (
    TOP5_CURRENT_PROVIDER_ORDER,
    Top5ProductionRoutingSnapshot,
)
from src.football.top5_signal_lifecycle import (
    LIFECYCLE_SCHEMA_VERSION,
    TOP5_H2H_OUTCOMES,
    Top5SignalLifecycle,
)
from src.runtime.paths import ROOT, runtime_state_path

DURABLE_ACTIVATION_SCHEMA_VERSION = "top5-durable-activation-state-v1"
ACTIVATION_PLAN_SCHEMA_VERSION = "top5-controlled-activation-plan-v1"
ACTIVATION_RUNTIME_STATE_PATH = "football/top5/controlled-activation/state-v1.json"
PRODUCTION_RUNTIME_REQUIRED = "VERIFIED_ONE_SHOT_RUNTIME_REQUIRED"


class DurableActivationError(ProductionContractError):
    """Invalid durable activation input or unsafe state transition."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DurableActivationError(f"{name} must be non-empty text")
    return value.strip()


def _digest(value: object, name: str) -> str:
    result = _text(value, name)
    if len(result) != 64 or any(c not in "0123456789abcdefABCDEF" for c in result):
        raise DurableActivationError(f"{name} must be a SHA-256 digest")
    return result.lower()


def _hash(value: object, name: str) -> str:
    result = _text(value, name)
    if len(result) not in (40, 64) or any(
        c not in "0123456789abcdefABCDEF" for c in result
    ):
        raise DurableActivationError(f"{name} must be a hexadecimal source SHA")
    return result.lower()


def _timestamp(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, name)
    if not isinstance(value, str):
        raise DurableActivationError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DurableActivationError(f"{name} must be an ISO-8601 timestamp") from exc
    return _utc(parsed, name)


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DurableActivationError("activation state is not canonical JSON") from exc


def _sha(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _validate_runtime_lifecycle_evidence(
    result: Mapping[str, object], lifecycle_stage: str
) -> None:
    if lifecycle_stage not in {"INITIAL", "REFINEMENT"}:
        raise DurableActivationError("one-shot lifecycle stage is invalid")
    expected = set(TOP5_H2H_OUTCOMES)
    expected_version = 1 if lifecycle_stage == "INITIAL" else 2
    mappings = {
        name: result.get(name)
        for name in (
            "lifecycle_ids",
            "lifecycle_versions",
            "lifecycle_version_digests",
            "lifecycle_digests",
        )
    }
    if any(
        not isinstance(value, Mapping) or set(value) != expected
        for value in mappings.values()
    ):
        raise DurableActivationError(
            "one-shot result does not prove the complete lifecycle set"
        )
    ids = mappings["lifecycle_ids"]
    versions = mappings["lifecycle_versions"]
    version_digests = mappings["lifecycle_version_digests"]
    lifecycle_digests = mappings["lifecycle_digests"]
    assert isinstance(ids, Mapping)
    assert isinstance(versions, Mapping)
    assert isinstance(version_digests, Mapping)
    assert isinstance(lifecycle_digests, Mapping)
    ordered_ids = [ids[outcome] for outcome in TOP5_H2H_OUTCOMES]
    lifecycle_prefix = f"{LIFECYCLE_SCHEMA_VERSION}-"
    if any(
        not isinstance(value, str)
        or len(value) != len(lifecycle_prefix) + 64
        or not value.startswith(lifecycle_prefix)
        or any(
            char not in "0123456789abcdef" for char in value[len(lifecycle_prefix) :]
        )
        for value in ordered_ids
    ) or len(set(ordered_ids)) != len(TOP5_H2H_OUTCOMES):
        raise DurableActivationError("one-shot lifecycle IDs are invalid")
    if any(
        isinstance(versions[outcome], bool)
        or not isinstance(versions[outcome], int)
        or versions[outcome] != expected_version
        for outcome in TOP5_H2H_OUTCOMES
    ):
        raise DurableActivationError("one-shot lifecycle versions are invalid")
    for outcome in TOP5_H2H_OUTCOMES:
        for digest_value, name in (
            (version_digests[outcome], f"{outcome} lifecycle version digest"),
            (lifecycle_digests[outcome], f"{outcome} lifecycle digest"),
        ):
            if (
                not isinstance(digest_value, str)
                or len(digest_value) != 64
                or any(char not in "0123456789abcdef" for char in digest_value)
            ):
                raise DurableActivationError(f"{name} is invalid")
    set_digest = result.get("lifecycle_set_digest")
    if (
        not isinstance(set_digest, str)
        or len(set_digest) != 64
        or any(char not in "0123456789abcdef" for char in set_digest)
    ):
        raise DurableActivationError("lifecycle set digest is invalid")
    if set_digest != _sha(dict(lifecycle_digests)):
        raise DurableActivationError("one-shot lifecycle set digest mismatch")
    production_evidence = result.get("production_evidence")
    if not isinstance(production_evidence, Mapping) or any(
        production_evidence.get(name) != result.get(name)
        for name in (
            "lifecycle_ids",
            "lifecycle_versions",
            "lifecycle_version_digests",
            "lifecycle_digests",
            "lifecycle_set_digest",
        )
    ):
        raise DurableActivationError(
            "one-shot production evidence lifecycle set differs from result"
        )


def _snapshot_payload(snapshot: Top5ProductionRoutingSnapshot) -> dict[str, object]:
    return snapshot.as_payload()


def _activation_claims_digest(
    authorization: ControlledActivationAuthorization,
) -> str:
    """Bind every authorization claim without persisting its bearer token."""
    signal = authorization.signal_time_contract
    authority = authorization.provider_authority
    return _sha(
        {
            "authorization_id": authorization.authorization_id,
            "activation_id": authorization.activation_id,
            "league_code": authorization.league_code,
            "candidate_id": authorization.candidate_id,
            "model_identity": authorization.model_identity,
            "source_sha": authorization.source_sha.lower(),
            "research_sha": authorization.research_sha.lower(),
            "model_artifact_hash": authorization.model_artifact_hash.lower(),
            "signal_time_experiment_id": authorization.signal_time_experiment_id,
            "signal_time_contract": {
                "minimum_minutes_before_kickoff": signal.minimum_minutes_before_kickoff,
                "maximum_minutes_before_kickoff": signal.maximum_minutes_before_kickoff,
                "maximum_odds_age_seconds": signal.maximum_odds_age_seconds,
                "approval_ref": signal.approval_ref,
            },
            "provider_authority": {
                "authority_decision_id": authority.authority_decision_id,
                "league_code": authority.league_code,
                "approved_odds_provider": authority.approved_odds_provider,
                "approved_provider_set": list(authority.approved_provider_set),
                "approved_result_source": authority.approved_result_source,
                "issued_at": authority.issued_at.astimezone(timezone.utc).isoformat(),
                "expires_at": (
                    authority.expires_at.astimezone(timezone.utc).isoformat()
                    if authority.expires_at
                    else None
                ),
                "approved": authority.approved,
            },
            "minimum_sample_policy": authorization.minimum_sample_policy.as_payload(),
            "controlled_shadow_run_id": authorization.controlled_shadow_run_id,
            "qualification_session_id": authorization.qualification_session_id,
            "ceo_shadow_authorization_id": authorization.ceo_shadow_authorization_id,
            "fixture_scope": list(authorization.fixture_scope),
            "rollback_pointer": authorization.rollback_pointer,
            "issued_at": authorization.issued_at.astimezone(timezone.utc).isoformat(),
            "expires_at": authorization.expires_at.astimezone(timezone.utc).isoformat(),
            "activation_authorized": authorization.activation_authorized,
            "no_bet": authorization.no_bet,
            "bearer_token_present": bool(authorization.authorization_token),
        }
    )


@dataclass(frozen=True)
class Top5DurableActivationPlanV1:
    """A verified five-league evidence package bound to one activation league."""

    activation_id: str
    activation_league: str
    evidence_leagues: tuple[str, ...]
    provider_authority: str
    candidate_provider: str
    receipt_package_id: str
    receipt_package_digest: str
    b1_manifest_digest: str
    b1_source_main_sha: str
    controlled_shadow_run_id: str
    qualification_session_id: str
    ceo_shadow_authorization_id: str
    source_sha: str
    research_sha: str
    model_identity: str
    model_artifact_hash: str
    signal_time_approval_identity: str
    activation_authorization_id: str
    authorization_claims_digest: str
    pre_activation_snapshot: Mapping[str, object]
    pre_activation_snapshot_digest: str
    pre_activation_configuration_digest: str
    target_configuration_digest: str
    prepared_at: datetime
    expires_at: datetime
    publication_enabled: bool = False
    scheduler_registered: bool = False
    betting_enabled: bool = False
    ledger_mutation_enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "prepared_at", _utc(self.prepared_at, "prepared_at"))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": ACTIVATION_PLAN_SCHEMA_VERSION,
            "activation_id": self.activation_id,
            "activation_league": self.activation_league,
            "evidence_leagues": list(self.evidence_leagues),
            "provider_authority": self.provider_authority,
            "candidate_provider": self.candidate_provider,
            "receipt_package_id": self.receipt_package_id,
            "receipt_package_digest": self.receipt_package_digest,
            "b1_manifest_digest": self.b1_manifest_digest,
            "b1_source_main_sha": self.b1_source_main_sha,
            "controlled_shadow_run_id": self.controlled_shadow_run_id,
            "qualification_session_id": self.qualification_session_id,
            "ceo_shadow_authorization_id": self.ceo_shadow_authorization_id,
            "source_sha": self.source_sha,
            "research_sha": self.research_sha,
            "model_identity": self.model_identity,
            "model_artifact_hash": self.model_artifact_hash,
            "signal_time_approval_identity": self.signal_time_approval_identity,
            "activation_authorization_id": self.activation_authorization_id,
            "authorization_claims_digest": self.authorization_claims_digest,
            "pre_activation_snapshot": dict(self.pre_activation_snapshot),
            "pre_activation_snapshot_digest": self.pre_activation_snapshot_digest,
            "pre_activation_configuration_digest": self.pre_activation_configuration_digest,
            "target_configuration_digest": self.target_configuration_digest,
            "prepared_at": self.prepared_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "publication_enabled": self.publication_enabled,
            "scheduler_registered": self.scheduler_registered,
            "betting_enabled": self.betting_enabled,
            "ledger_mutation_enabled": self.ledger_mutation_enabled,
        }

    @property
    def plan_digest(self) -> str:
        return _sha(self._payload())

    def validate(self, *, now: datetime) -> None:
        for name in (
            "activation_id",
            "activation_league",
            "provider_authority",
            "candidate_provider",
            "receipt_package_id",
            "controlled_shadow_run_id",
            "qualification_session_id",
            "ceo_shadow_authorization_id",
            "model_identity",
            "signal_time_approval_identity",
            "activation_authorization_id",
        ):
            _text(getattr(self, name), name)
        if self.activation_league not in TOP5_LEAGUES:
            raise DurableActivationError("activation league is outside canonical Top-5")
        if self.evidence_leagues != TOP5_LEAGUES:
            raise DurableActivationError(
                "full evidence scope must be canonical five leagues"
            )
        if self.provider_authority != ACTIVE_PROVIDER:
            raise DurableActivationError(
                "production authority must remain the_odds_api"
            )
        if (
            self.candidate_provider != CANDIDATE_PROVIDER
            or self.candidate_provider not in CANDIDATE_ONLY_PROVIDER_IDENTITIES
        ):
            raise DurableActivationError("candidate provider identity is not canonical")
        for name in (
            "receipt_package_digest",
            "b1_manifest_digest",
            "pre_activation_snapshot_digest",
            "pre_activation_configuration_digest",
            "target_configuration_digest",
            "authorization_claims_digest",
        ):
            _digest(getattr(self, name), name)
        for name in (
            "b1_source_main_sha",
            "source_sha",
            "research_sha",
            "model_artifact_hash",
        ):
            _hash(getattr(self, name), name)
        if self.expires_at <= self.prepared_at or _utc(now, "now") > self.expires_at:
            raise DurableActivationError(
                "activation plan is expired or has invalid expiry"
            )
        if (
            self.publication_enabled
            or self.scheduler_registered
            or self.betting_enabled
            or self.ledger_mutation_enabled
        ):
            raise DurableActivationError(
                "activation plan enables a prohibited capability"
            )
        if not isinstance(self.pre_activation_snapshot, Mapping):
            raise DurableActivationError("pre-activation snapshot must be an object")
        snapshot_digest = _sha(
            {
                key: value
                for key, value in self.pre_activation_snapshot.items()
                if key != "snapshot_digest"
            }
        )
        if snapshot_digest != self.pre_activation_snapshot_digest:
            raise DurableActivationError("pre-activation snapshot digest mismatch")


def prepare_top5_durable_activation_plan(
    *,
    receipt_package: Builder2FiveLeagueReceiptPackageV1,
    b1_acceptance_bundle: Mapping[str, object],
    authority: ApprovedProviderResultAuthority,
    activation: ControlledActivationAuthorization,
    current_snapshot: Top5ProductionRoutingSnapshot,
    signal_time_approval_identity: str,
    now: datetime,
) -> Top5DurableActivationPlanV1:
    """Revalidate canonical B2/B1 evidence and bind it to one league only."""
    now = _utc(now, "now")
    try:
        receipt_package.validate()
        acceptance = verify_final_acceptance(b1_acceptance_bundle, now=now)
    except (Builder2QualificationBatchError, Top5FinalAcceptanceError) as exc:
        raise DurableActivationError(
            f"canonical evidence validation failed: {exc}"
        ) from exc
    if acceptance.get("status") != STATUS_VERIFIED:
        raise DurableActivationError("B1 final acceptance is not verified")
    manifest = acceptance.get("manifest")
    if not isinstance(manifest, Mapping):
        raise DurableActivationError("B1 final-acceptance manifest is missing")
    manifest_body = {
        key: value for key, value in manifest.items() if key != "manifest_digest"
    }
    if manifest.get("readiness") != STATUS_VERIFIED or canonical_digest(
        manifest_body
    ) != manifest.get("manifest_digest"):
        raise DurableActivationError(
            "B1 final-acceptance manifest digest/readiness mismatch"
        )
    if (
        manifest.get("provider_authority") != ACTIVE_PROVIDER
        or manifest.get("candidate_provider") != CANDIDATE_PROVIDER
    ):
        raise DurableActivationError(
            "B1 provider/candidate authority binding is invalid"
        )
    if (
        not isinstance(manifest.get("leagues"), (list, tuple))
        or set(manifest["leagues"]) != set(TOP5_LEAGUES)
        or len(manifest["leagues"]) != len(TOP5_LEAGUES)
    ):
        raise DurableActivationError(
            "B1 acceptance does not bind exactly five canonical leagues"
        )
    checks = manifest.get("checks")
    if not isinstance(checks, Mapping) or any(
        checks.get(key) is not True
        for key in (
            "five_leagues",
            "b4_quota_proof",
            "discovery",
            "controlled_shadow",
            "model_signal_time",
            "runtime_provenance",
            "candidate_not_authority",
            "no_bet",
        )
    ):
        raise DurableActivationError("B1 final-acceptance checks are incomplete")

    dossier = receipt_package.dossier
    if (
        dossier.leagues != TOP5_LEAGUES
        or tuple(binding.league for binding in dossier.bindings) != TOP5_LEAGUES
    ):
        raise DurableActivationError(
            "B2 package must retain canonical five-league evidence"
        )
    if (
        dossier.provider_identity != CANDIDATE_PROVIDER
        or dossier.candidate_provider_identity != CANDIDATE_PROVIDER
    ):
        raise DurableActivationError(
            "B2 evidence is not from the candidate-only provider"
        )
    if any(
        binding.evidence_kind != "REAL_OBSERVED"
        or binding.network_execution is not True
        for binding in dossier.bindings
    ):
        raise DurableActivationError(
            "activation requires genuine real-observed five-league evidence"
        )
    if any(
        binding.publication
        or binding.production_activation
        or binding.monetary_spend_authorized
        for binding in dossier.bindings
    ):
        raise DurableActivationError("B2 evidence contains an unsafe capability")

    activation.validate(now=now)
    authority.validate(now=now)
    current_snapshot.validate()
    if activation.provider_authority != authority:
        raise DurableActivationError(
            "activation authorization/provider authority mismatch"
        )
    if (
        activation.league_code not in TOP5_LEAGUES
        or authority.league_code != activation.league_code
    ):
        raise DurableActivationError(
            "activation authorization must select exactly one canonical league"
        )
    if (
        authority.approved_odds_provider != ACTIVE_PROVIDER
        or authority.approved_provider_set != (ACTIVE_PROVIDER,)
    ):
        raise DurableActivationError(
            "production authority must be exactly the_odds_api"
        )
    if authority.approved_odds_provider in CANDIDATE_ONLY_PROVIDER_IDENTITIES:
        raise DurableActivationError(
            "candidate-only provider cannot become production authority"
        )
    if (
        current_snapshot.provider_order != TOP5_CURRENT_PROVIDER_ORDER
        or current_snapshot.adapter_registry != TOP5_CURRENT_PROVIDER_ORDER
    ):
        raise DurableActivationError(
            "pre-activation production routing is not canonical"
        )
    if (
        current_snapshot.top5_scheduler_enabled
        or current_snapshot.publication_enabled
        or not current_snapshot.no_bet
        or current_snapshot.ledger_mutation_enabled
    ):
        raise DurableActivationError("pre-activation snapshot is not safely disabled")

    selected_bindings = tuple(
        binding
        for binding in dossier.bindings
        if binding.league == activation.league_code
    )
    if len(selected_bindings) != 1 or activation.fixture_scope != (
        selected_bindings[0].fixture_key,
    ):
        raise DurableActivationError(
            "activation fixture scope must be the one selected league fixture"
        )
    model = b1_acceptance_bundle.get("model_runtime")
    if not isinstance(model, Mapping):
        raise DurableActivationError("B1 model/runtime binding is missing")
    for key, expected in (
        ("source_sha", activation.source_sha),
        ("research_sha", activation.research_sha),
        ("model_artifact_hash", activation.model_artifact_hash),
    ):
        if str(model.get(key, "")).lower() != expected.lower():
            raise DurableActivationError(f"B1 model/runtime {key} binding mismatch")
    if (
        manifest.get("research_sha") != activation.research_sha.lower()
        or manifest.get("model_identity") != activation.model_identity
    ):
        raise DurableActivationError("B1 Research/model identity binding mismatch")
    if activation.candidate_id != activation.model_identity:
        raise DurableActivationError("activation candidate/model identity mismatch")
    signal_identity = _text(
        signal_time_approval_identity, "signal_time_approval_identity"
    )
    if activation.signal_time_contract.approval_ref != signal_identity:
        raise DurableActivationError("separate Signal-Time approval identity mismatch")
    if activation.authorization_id != activation.signal_time_contract.approval_ref:
        raise DurableActivationError(
            "activation authorization is not bound to Signal-Time approval"
        )
    if (
        activation.controlled_shadow_run_id,
        activation.qualification_session_id,
        activation.ceo_shadow_authorization_id,
    ) != (
        dossier.controlled_shadow_run_id,
        dossier.qualification_session_id,
        dossier.ceo_authorization_id,
    ):
        raise DurableActivationError(
            "activation authorization does not bind to the accepted shadow run"
        )

    b1_events = manifest.get("discovery_event_ids")
    expected_events = {
        binding.league: binding.provider_event_id for binding in dossier.bindings
    }
    if b1_events != expected_events:
        raise DurableActivationError(
            "B1 frozen event identities do not match B2 evidence"
        )
    if (
        manifest.get("controlled_shadow_run_id") != dossier.controlled_shadow_run_id
        or manifest.get("qualification_session_id") != dossier.qualification_session_id
        or manifest.get("ceo_authorization_id") != dossier.ceo_authorization_id
    ):
        raise DurableActivationError(
            "B1 and B2 run/session/authorization bindings differ"
        )
    shadow_root = b1_acceptance_bundle.get("controlled_shadow")
    if not isinstance(shadow_root, Mapping):
        raise DurableActivationError("B1 controlled-shadow artifact is missing")
    shadow_run = shadow_root.get("shadow_run", shadow_root)
    if not isinstance(shadow_run, Mapping) or not isinstance(
        shadow_run.get("captures"), (list, tuple)
    ):
        raise DurableActivationError("B1 controlled-shadow captures are missing")
    b1_captures: dict[str, Mapping[str, object]] = {}
    b1_capture_digests: list[str] = []
    b2_bindings_by_league = {binding.league: binding for binding in dossier.bindings}
    for item in shadow_run["captures"]:
        if not isinstance(item, Mapping):
            raise DurableActivationError("B1 controlled-shadow capture is malformed")
        target = item.get("target", item)
        capture = item.get("capture", item)
        request = item.get("request", item)
        if (
            not isinstance(target, Mapping)
            or not isinstance(capture, Mapping)
            or not isinstance(request, Mapping)
        ):
            raise DurableActivationError("B1 capture target/request binding is missing")
        league = target.get("league")
        if not isinstance(league, str) or league in b1_captures:
            raise DurableActivationError(
                "B1 capture league identity is duplicate or invalid"
            )
        if request.get("configuration_digest") != dossier.configuration_digest:
            raise DurableActivationError(
                "B1 request configuration digest differs from B2"
            )
        expected_binding = b2_bindings_by_league.get(league)
        if (
            expected_binding is None
            or capture.get("capture_attestation_digest")
            != expected_binding.capture_attestation_digest
        ):
            raise DurableActivationError(
                "B1 capture attestation does not match B2 evidence"
            )
        b1_captures[league] = capture
        b1_capture_digests.append(canonical_digest(capture))
    if set(b1_captures) != set(TOP5_LEAGUES):
        raise DurableActivationError("B1 controlled-shadow does not cover five leagues")
    if sorted(manifest.get("capture_digests", ())) != sorted(b1_capture_digests):
        raise DurableActivationError("B1 capture digest projection mismatch")
    if manifest.get("adapter_source_sha") != dossier.adapter_source_sha:
        raise DurableActivationError("B1 and B2 adapter provenance differs")

    snapshot_payload = _snapshot_payload(current_snapshot)
    target_configuration = {
        "schema_version": ACTIVATION_PLAN_SCHEMA_VERSION,
        "activation_id": activation.activation_id,
        "activation_league": activation.league_code,
        "provider_authority": ACTIVE_PROVIDER,
        "candidate_provider_authority": False,
        "receipt_package_digest": receipt_package.package_digest,
        "b1_manifest_digest": manifest["manifest_digest"],
        "source_sha": activation.source_sha.lower(),
        "research_sha": activation.research_sha.lower(),
        "model_identity": activation.model_identity,
        "model_artifact_hash": activation.model_artifact_hash.lower(),
        "signal_time_approval_identity": signal_identity,
        "publication_enabled": False,
        "scheduler_registered": False,
        "betting_enabled": False,
        "ledger_mutation_enabled": False,
    }
    plan = Top5DurableActivationPlanV1(
        activation_id=activation.activation_id,
        activation_league=activation.league_code,
        evidence_leagues=TOP5_LEAGUES,
        provider_authority=ACTIVE_PROVIDER,
        candidate_provider=CANDIDATE_PROVIDER,
        receipt_package_id=receipt_package.package_id,
        receipt_package_digest=receipt_package.package_digest,
        b1_manifest_digest=str(manifest["manifest_digest"]),
        b1_source_main_sha=str(manifest["source_main_sha"]),
        controlled_shadow_run_id=dossier.controlled_shadow_run_id,
        qualification_session_id=dossier.qualification_session_id,
        ceo_shadow_authorization_id=dossier.ceo_authorization_id,
        source_sha=activation.source_sha,
        research_sha=activation.research_sha,
        model_identity=activation.model_identity,
        model_artifact_hash=activation.model_artifact_hash,
        signal_time_approval_identity=signal_identity,
        activation_authorization_id=activation.authorization_id,
        authorization_claims_digest=_activation_claims_digest(activation),
        pre_activation_snapshot=snapshot_payload,
        pre_activation_snapshot_digest=current_snapshot.snapshot_digest,
        pre_activation_configuration_digest=current_snapshot.provider_config_digest,
        target_configuration_digest=_sha(target_configuration),
        prepared_at=now,
        expires_at=activation.expires_at,
    )
    plan.validate(now=now)
    return plan


@dataclass(frozen=True)
class Top5DurableActivationRecordV1:
    plan: Top5DurableActivationPlanV1
    status: str
    created_at: datetime
    updated_at: datetime
    publication_enabled: bool = False
    scheduler_registered: bool = False
    betting_enabled: bool = False
    ledger_mutation_enabled: bool = False
    rollback_evidence_digest: str | None = None
    execution_result: Mapping[str, object] | None = None
    execution_result_digest: str | None = None

    def payload(self) -> dict[str, object]:
        value = {
            "schema_version": DURABLE_ACTIVATION_SCHEMA_VERSION,
            "plan": self.plan._payload(),
            "plan_digest": self.plan.plan_digest,
            "status": self.status,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "updated_at": self.updated_at.astimezone(timezone.utc).isoformat(),
            "publication_enabled": self.publication_enabled,
            "scheduler_registered": self.scheduler_registered,
            "betting_enabled": self.betting_enabled,
            "ledger_mutation_enabled": self.ledger_mutation_enabled,
            "rollback_evidence_digest": self.rollback_evidence_digest,
            "execution_result": dict(self.execution_result)
            if self.execution_result is not None
            else None,
            "execution_result_digest": self.execution_result_digest,
        }
        value["record_digest"] = _sha(value)
        return value


class DurableTop5ActivationStore:
    """Atomic external Top-5 activation state with retained rollback evidence."""

    def __init__(self, path: Path | None = None) -> None:
        RuntimeStateBinding(
            owner=RuntimeStateOwner.TOP5_SHADOW,
            relative_path=ACTIVATION_RUNTIME_STATE_PATH,
            external_required=True,
        ).validate()
        self.path = path or runtime_state_path(
            ACTIVATION_RUNTIME_STATE_PATH, require_external=True
        )
        self.path = Path(self.path)
        resolved = self.path.resolve()
        if resolved == ROOT.resolve() or ROOT.resolve() in resolved.parents:
            raise DurableActivationError(
                "activation state must remain outside the checkout"
            )
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _locked(self):
        return _StateLock(self.lock_path)

    def _initial(self) -> dict[str, object]:
        body: dict[str, object] = {
            "schema_version": DURABLE_ACTIVATION_SCHEMA_VERSION,
            "revision": 0,
            "active_activation_id": None,
            "records": {},
        }
        body["state_digest"] = _sha(body)
        return body

    def _read_unlocked(self) -> dict[str, object]:
        if self.path.is_symlink():
            raise DurableActivationError("activation state file must not be a symlink")
        if not self.path.exists():
            return self._initial()
        if not self.path.is_file():
            raise DurableActivationError("activation state path is not a regular file")
        if self.path.stat().st_mode & 0o077:
            raise DurableActivationError(
                "activation state file permissions must be owner-only"
            )
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DurableActivationError(
                "activation state is unreadable or malformed"
            ) from exc
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "revision",
            "active_activation_id",
            "records",
            "state_digest",
        }:
            raise DurableActivationError("activation state envelope fields are invalid")
        digest = raw.pop("state_digest")
        if (
            raw.get("schema_version") != DURABLE_ACTIVATION_SCHEMA_VERSION
            or _sha(raw) != digest
        ):
            raise DurableActivationError("activation state schema/digest mismatch")
        if (
            not isinstance(raw.get("revision"), int)
            or isinstance(raw.get("revision"), bool)
            or raw["revision"] < 0
        ):
            raise DurableActivationError("activation state revision is invalid")
        records = raw.get("records")
        if not isinstance(records, dict):
            raise DurableActivationError("activation state records must be an object")
        active_id = raw.get("active_activation_id")
        if active_id is not None and (
            not isinstance(active_id, str) or active_id not in records
        ):
            raise DurableActivationError("active activation pointer is invalid")
        for activation_id, record in records.items():
            self._validate_record(activation_id, record)
        raw["state_digest"] = digest
        return raw

    @staticmethod
    def _validate_record(activation_id: object, record: object) -> None:
        if not isinstance(activation_id, str) or not isinstance(record, dict):
            raise DurableActivationError("activation record identity is invalid")
        legacy_expected = {
            "schema_version",
            "plan",
            "plan_digest",
            "status",
            "created_at",
            "updated_at",
            "publication_enabled",
            "scheduler_registered",
            "betting_enabled",
            "ledger_mutation_enabled",
            "rollback_evidence_digest",
            "record_digest",
        }
        expected = legacy_expected | {"execution_result", "execution_result_digest"}
        if (
            set(record) not in (legacy_expected, expected)
            or record.get("schema_version") != DURABLE_ACTIVATION_SCHEMA_VERSION
        ):
            raise DurableActivationError("activation record schema is invalid")
        claimed = record["record_digest"]
        body = {key: value for key, value in record.items() if key != "record_digest"}
        if _sha(body) != claimed or record.get("plan_digest") != _sha(
            record.get("plan")
        ):
            raise DurableActivationError("activation record digest mismatch")
        if record["plan"].get("activation_id") != activation_id:
            raise DurableActivationError("activation record plan identity mismatch")
        if record["status"] not in {
            "PREPARED",
            "EXECUTING",
            "PRODUCTION_VERIFIED",
            "ACTIVE",
            "ROLLED_BACK",
            "BLOCKED",
        }:
            raise DurableActivationError("activation record status is invalid")
        if any(
            record.get(field) is not False
            for field in (
                "publication_enabled",
                "scheduler_registered",
                "betting_enabled",
                "ledger_mutation_enabled",
            )
        ):
            raise DurableActivationError(
                "activation record contains an unsafe capability"
            )
        if record.get("status") == "PRODUCTION_VERIFIED":
            result = record.get("execution_result")
            result_digest = record.get("execution_result_digest")
            if (
                not isinstance(result, dict)
                or not isinstance(result_digest, str)
                or _sha(result) != result_digest
            ):
                raise DurableActivationError("verified execution result is invalid")
            _validate_runtime_lifecycle_evidence(
                result, str(result.get("lifecycle_stage", ""))
            )

    def _write_unlocked(self, state: dict[str, object]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise DurableActivationError("activation state file must not be a symlink")
        state = {key: value for key, value in state.items() if key != "state_digest"}
        state["state_digest"] = _sha(state)
        data = _canonical_bytes(state)
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def status(self, activation_id: str | None = None) -> dict[str, object]:
        with self._locked():
            state = self._read_unlocked()
            records = state["records"]
            selected = activation_id or state["active_activation_id"]
            if selected is None and records:
                selected = next(reversed(records))
            record = records.get(selected) if selected else None
            return {
                "schema_version": DURABLE_ACTIVATION_SCHEMA_VERSION,
                "activation_id": selected,
                "record": record,
                "active_activation_id": state["active_activation_id"],
                "revision": state["revision"],
                "provider_health": "HEALTHY"
                if record and record.get("status") == "PRODUCTION_VERIFIED"
                else "NOT_OBSERVED",
                "model_health": "HEALTHY"
                if record and record.get("status") == "PRODUCTION_VERIFIED"
                else "NOT_OBSERVED",
                "publication_enabled": False,
                "scheduler_registered": False,
                "betting_enabled": False,
                "ledger_mutation_enabled": False,
                "rollback_ready": bool(
                    record
                    and record.get("status")
                    in {"PREPARED", "EXECUTING", "PRODUCTION_VERIFIED", "ACTIVE"}
                ),
            }

    def prepare(
        self, plan: Top5DurableActivationPlanV1, *, now: datetime | None = None
    ) -> dict[str, object]:
        plan.validate(now=now or datetime.now(timezone.utc))
        with self._locked():
            state = self._read_unlocked()
            records = dict(state["records"])
            current = records.get(plan.activation_id)
            if current is not None:
                if current.get("plan_digest") != plan.plan_digest:
                    raise DurableActivationError(
                        "activation ID already exists with different plan"
                    )
                if current.get("status") != "PREPARED":
                    raise DurableActivationError(
                        "activation ID has already left PREPARED state"
                    )
                return current
            active_id = state["active_activation_id"]
            if active_id is not None:
                raise DurableActivationError("another Top-5 activation is active")
            if any(
                item.get("status")
                in {"PREPARED", "EXECUTING", "PRODUCTION_VERIFIED", "ACTIVE"}
                for item in records.values()
            ):
                raise DurableActivationError(
                    "another Top-5 activation is pending or active"
                )
            now = plan.prepared_at
            record = Top5DurableActivationRecordV1(plan, "PREPARED", now, now).payload()
            records[plan.activation_id] = record
            state.update({"records": records, "revision": int(state["revision"]) + 1})
            self._write_unlocked(state)
            return record

    def execute(
        self,
        plan: Top5DurableActivationPlanV1,
        *,
        now: datetime,
        explicit_execute: bool,
        execution_binding: object | None = None,
        verified_authorization: object | None = None,
        runtime: object | None = None,
        fixture: object | None = None,
        lifecycles: Sequence[Top5SignalLifecycle] | None = None,
    ) -> dict[str, object]:
        """Execute one exact signed canary and retain its private evidence."""
        if explicit_execute is not True:
            raise DurableActivationError("explicit execute flag is required")
        plan.validate(now=now)
        from src.football.top5_activation_authorization import (
            Top5ActivationExecutionBindingV1,
            VerifiedTop5ActivationAuthorizationV1,
        )

        if not isinstance(
            execution_binding, Top5ActivationExecutionBindingV1
        ) or not isinstance(
            verified_authorization, VerifiedTop5ActivationAuthorizationV1
        ):
            raise DurableActivationError(
                "cryptographic activation authorization and lifecycle binding are required"
            )
        try:
            verified_authorization.assert_valid_at(now)
        except ProductionContractError as exc:
            raise DurableActivationError(str(exc)) from exc
        if (
            execution_binding.durable_plan_digest != plan.plan_digest
            or execution_binding.activation_id != plan.activation_id
            or execution_binding.activation_league != plan.activation_league
        ):
            raise DurableActivationError(
                "signed execution binding differs from the exact prepared plan"
            )
        for name, expected in execution_binding.expected_signed_claims().items():
            if verified_authorization.payload.get(name) != expected:
                raise DurableActivationError(
                    f"signed authorization binding mismatch: {name}"
                )
        if (
            runtime is None
            or not callable(getattr(runtime, "execute", None))
            or not callable(getattr(runtime, "rollback_execution", None))
        ):
            raise DurableActivationError(PRODUCTION_RUNTIME_REQUIRED)
        if fixture is None:
            raise DurableActivationError("exact canonical fixture is required")
        with self._locked():
            state = self._read_unlocked()
            record = state["records"].get(plan.activation_id)
            if (
                not isinstance(record, dict)
                or record.get("status") != "PREPARED"
                or record.get("plan_digest") != plan.plan_digest
            ):
                raise DurableActivationError(
                    "exact prepared activation plan is missing or changed"
                )
            record = dict(record)
            record.update(
                {"status": "EXECUTING", "updated_at": _utc(now, "now").isoformat()}
            )
            record["record_digest"] = _sha(
                {key: value for key, value in record.items() if key != "record_digest"}
            )
            records = dict(state["records"])
            records[plan.activation_id] = record
            state.update({"records": records, "revision": int(state["revision"]) + 1})
            self._write_unlocked(state)
        runtime_returned = False
        try:
            result = runtime.execute(
                plan,
                execution_binding,
                verified_authorization,
                fixture=fixture,
                lifecycles=lifecycles,
            )
            runtime_returned = True
            if not isinstance(result, Mapping):
                raise DurableActivationError("one-shot runtime result is malformed")
            result = dict(result)
            claimed_result_digest = result.get("result_digest")
            if claimed_result_digest != _sha(
                {key: value for key, value in result.items() if key != "result_digest"}
            ):
                raise DurableActivationError("one-shot result digest mismatch")
            if (
                result.get("schema_version") != "top5-one-shot-production-result-v1"
                or result.get("activation_id") != plan.activation_id
                or result.get("activation_plan_digest")
                != execution_binding.activation_plan_digest
                or result.get("provider_authority") != ACTIVE_PROVIDER
                or result.get("provider_request_count") != 1
                or result.get("retry_count") != 0
                or result.get("no_bet") is not True
                or result.get("publication") is not False
                or result.get("recurring_scheduler") is not False
                or result.get("betting") is not False
                or result.get("ledger_mutation") is not False
            ):
                raise DurableActivationError(
                    "one-shot runtime result failed validation"
                )
            _validate_runtime_lifecycle_evidence(
                result, execution_binding.lifecycle_stage
            )
            with self._locked():
                state = self._read_unlocked()
                current = state["records"].get(plan.activation_id)
                if (
                    not isinstance(current, dict)
                    or current.get("status") != "EXECUTING"
                    or current.get("plan_digest") != plan.plan_digest
                ):
                    raise DurableActivationError("executing activation state changed")
                current = dict(current)
                current.update(
                    {
                        "status": "PRODUCTION_VERIFIED",
                        "execution_result": result,
                        "execution_result_digest": _sha(result),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                current["record_digest"] = _sha(
                    {
                        key: value
                        for key, value in current.items()
                        if key != "record_digest"
                    }
                )
                records = dict(state["records"])
                records[plan.activation_id] = current
                state.update(
                    {
                        "active_activation_id": plan.activation_id,
                        "records": records,
                        "revision": int(state["revision"]) + 1,
                    }
                )
                self._write_unlocked(state)
                return json.loads(json.dumps(current))
        except Exception as exc:
            if runtime_returned:
                try:
                    runtime.rollback_execution(execution_binding)
                except Exception:  # noqa: BLE001 - rollback must cover every failure.
                    raise DurableActivationError(
                        "one-shot failed and route-consumer rollback could not be verified"
                    ) from None
            try:
                self.rollback(
                    plan.activation_id,
                    plan.plan_digest,
                    now=datetime.now(timezone.utc),
                )
            except Exception:  # noqa: BLE001 - fail closed if rollback itself fails.
                raise DurableActivationError(
                    "one-shot failed and activation rollback could not be verified"
                ) from None
            if isinstance(exc, DurableActivationError):
                raise
            raise DurableActivationError("one-shot runtime failed closed") from None

    def rollback(
        self, activation_id: str, plan_digest: str, *, now: datetime
    ) -> dict[str, object]:
        activation_id = _text(activation_id, "activation_id")
        plan_digest = _digest(plan_digest, "plan_digest")
        now = _utc(now, "now")
        with self._locked():
            state = self._read_unlocked()
            records = dict(state["records"])
            record = records.get(activation_id)
            if not isinstance(record, dict) or record.get("plan_digest") != plan_digest:
                raise DurableActivationError(
                    "rollback requires the exact activation plan digest"
                )
            if record.get("status") not in {
                "PREPARED",
                "EXECUTING",
                "PRODUCTION_VERIFIED",
                "ACTIVE",
            }:
                if record.get("status") == "ROLLED_BACK":
                    return record
                raise DurableActivationError(
                    "activation is not in a rollback-capable state"
                )
            plan = record.get("plan")
            if not isinstance(plan, dict):
                raise DurableActivationError("persisted rollback plan is invalid")
            pre_activation_snapshot = plan.get("pre_activation_snapshot")
            if not isinstance(pre_activation_snapshot, dict):
                raise DurableActivationError("persisted rollback snapshot is invalid")
            if plan.get("pre_activation_snapshot_digest") != _sha(
                {
                    key: value
                    for key, value in pre_activation_snapshot.items()
                    if key != "snapshot_digest"
                }
            ):
                raise DurableActivationError("persisted rollback snapshot is invalid")
            rollback_body = {
                "activation_id": activation_id,
                "plan_digest": plan_digest,
                "pre_activation_snapshot_digest": plan[
                    "pre_activation_snapshot_digest"
                ],
                "restored_configuration_digest": plan[
                    "pre_activation_configuration_digest"
                ],
                "restored_provider_order": pre_activation_snapshot.get(
                    "provider_order"
                ),
                "active_league": None,
                "restored_status": "DISABLED",
                "provider_authority": ACTIVE_PROVIDER,
                "publication_enabled": False,
                "scheduler_registered": False,
                "betting_enabled": False,
                "ledger_mutation_enabled": False,
                "rolled_back_at": now.isoformat(),
            }
            record = dict(record)
            record.update(
                {
                    "status": "ROLLED_BACK",
                    "updated_at": now.isoformat(),
                    "rollback_evidence_digest": _sha(rollback_body),
                }
            )
            record["record_digest"] = _sha(
                {key: value for key, value in record.items() if key != "record_digest"}
            )
            records[activation_id] = record
            if state["active_activation_id"] == activation_id:
                state["active_activation_id"] = None
            state.update({"records": records, "revision": int(state["revision"]) + 1})
            self._write_unlocked(state)
            return record


class _StateLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise DurableActivationError("activation lock must not be a symlink")
        self.fd = os.open(
            self.path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(self.fd, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def activation_health_payload(
    store: DurableTop5ActivationStore, activation_id: str | None = None
) -> dict[str, object]:
    """Report durable control state without asserting provider/model health."""
    snapshot = store.status(activation_id)
    record = snapshot.get("record")
    plan = record.get("plan") if isinstance(record, Mapping) else None
    return {
        "schema_version": DURABLE_ACTIVATION_SCHEMA_VERSION,
        "activation_id": snapshot["activation_id"],
        "plan_digest": record.get("plan_digest")
        if isinstance(record, Mapping)
        else None,
        "activation_league": plan.get("activation_league")
        if isinstance(plan, Mapping)
        else None,
        "source_sha": plan.get("source_sha") if isinstance(plan, Mapping) else None,
        "research_sha": plan.get("research_sha") if isinstance(plan, Mapping) else None,
        "model_identity": plan.get("model_identity")
        if isinstance(plan, Mapping)
        else None,
        "model_artifact_hash": plan.get("model_artifact_hash")
        if isinstance(plan, Mapping)
        else None,
        "signal_time_approval_identity": plan.get("signal_time_approval_identity")
        if isinstance(plan, Mapping)
        else None,
        "production_configuration_digest": plan.get("target_configuration_digest")
        if isinstance(plan, Mapping)
        else None,
        "current_routing_configuration_digest": plan.get(
            "pre_activation_configuration_digest"
        )
        if isinstance(plan, Mapping)
        else None,
        "pre_activation_configuration_digest": plan.get(
            "pre_activation_configuration_digest"
        )
        if isinstance(plan, Mapping)
        else None,
        "last_state_transition": record.get("updated_at")
        if isinstance(record, Mapping)
        else None,
        "status": record.get("status", "DISABLED")
        if isinstance(record, Mapping)
        else "DISABLED",
        "activation_state": record.get("status", "DISABLED")
        if isinstance(record, Mapping)
        else "DISABLED",
        "production_provider_authority": ACTIVE_PROVIDER,
        "candidate_provider_authority": False,
        "provider_health": "NOT_OBSERVED",
        "model_health": "NOT_OBSERVED",
        "rollback_ready": snapshot["rollback_ready"],
        "rollback_scope": "DURABLE_CONTROL_STATE_ONLY",
        "live_routing_rollback_ready": False,
        "publication_enabled": False,
        "scheduler_registered": False,
        "betting_enabled": False,
        "ledger_mutation_enabled": False,
        "runtime_state_external": True,
    }


__all__ = [
    "ACTIVATION_PLAN_SCHEMA_VERSION",
    "ACTIVATION_RUNTIME_STATE_PATH",
    "DURABLE_ACTIVATION_SCHEMA_VERSION",
    "PRODUCTION_RUNTIME_REQUIRED",
    "DurableActivationError",
    "DurableTop5ActivationStore",
    "Top5DurableActivationPlanV1",
    "activation_health_payload",
    "prepare_top5_durable_activation_plan",
]
