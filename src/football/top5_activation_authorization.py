"""Signed, exact-scope authorization for a future Top-5 one-shot canary.

This module verifies public-key-only Ed25519 approvals and the canonical
two-stage lifecycle contract. It never loads signing material, calls a
provider, or changes production state.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    ProductionContractError,
    _utc,
)
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS
from src.football.top5_b2_qualification_batch_orchestrator import (
    Builder2FiveLeagueReceiptPackageV1,
)
from src.football.top5_controlled_release import ControlledActivationAuthorization
from src.football.top5_durable_activation import (
    DurableActivationError,
    Top5DurableActivationPlanV1,
    _sha,
)
from src.football.top5_final_acceptance import (
    Top5FinalAcceptanceError,
    verify_final_acceptance,
)
from src.football.top5_signal_lifecycle import (
    DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    LifecyclePlanStatus,
    SignalLifecycleStage,
    Top5SignalLifecycle,
    Top5SignalLifecycleContract,
    _validate_snapshot_source_binding,
    plan_signal_lifecycle,
)
from src.runtime.paths import ROOT

ACTIVATION_AUTHORIZATION_SCHEMA = "top5-philip-activation-authorization-v1"
ACTIVATION_AUTHORIZATION_ACTION = "controlled_top5_activation"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_VERIFICATION_SEAL = object()


class ActivationAuthorizationError(ProductionContractError):
    """Invalid signature, lifecycle binding, or activation authorization."""


def canonical_authorization_bytes(payload: Mapping[str, object]) -> bytes:
    """The exact UTF-8 canonical JSON representation signed by Philip."""
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ActivationAuthorizationError(
            "authorization payload is not canonical JSON"
        ) from exc


def _parse_time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ActivationAuthorizationError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActivationAuthorizationError(
            f"{name} must be an ISO-8601 timestamp"
        ) from exc
    try:
        return _utc(parsed, name)
    except ProductionContractError as exc:
        raise ActivationAuthorizationError(str(exc)) from exc


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        raise ActivationAuthorizationError(f"{name} must be a SHA-256 digest")
    return value


def _source_sha(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) not in (40, 64):
        raise ActivationAuthorizationError(
            f"{name} must be a 40-64 character source SHA"
        )
    if any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ActivationAuthorizationError(f"{name} must be a hexadecimal source SHA")
    return value.lower()


def the_odds_api_one_shot_request_shape(league_code: str) -> dict[str, object]:
    """Return the one permitted production odds request shape for one league.

    The only accepted region is the existing disabled Top-5 mapping's `eu`
    region. The provider key is supplied by that canonical league config; no
    credential or live provider client is imported here.
    """
    adapter = TOP5_LEAGUE_ADAPTERS.get(league_code)
    if adapter is None:
        raise ActivationAuthorizationError("one-shot request league is unsupported")
    config = adapter.config
    mapping = config.provider_mapping
    if (
        mapping is None
        or mapping.provider_name != "the_odds_api"
        or mapping.sport_key != config.provider_sport_key
        or mapping.regions != ("eu",)
    ):
        raise ActivationAuthorizationError(
            "one-shot request provider/region mapping is not canonical"
        )
    return {
        "method": "GET",
        "endpoint": (
            f"https://api.the-odds-api.com/v4/sports/{config.provider_sport_key}/odds"
        ),
        "query": {
            "regions": "eu",
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
        "request_count": 1,
        "retry_count": 0,
    }


def the_odds_api_one_shot_request_shape_digest(league_code: str) -> str:
    """Digest the exact credential-free one-shot request contract."""
    return _sha(the_odds_api_one_shot_request_shape(league_code))


@dataclass(frozen=True)
class Top5ActivationExecutionBindingV1:
    """Exact, non-authorizing execution plan derived from accepted evidence."""

    durable_plan_digest: str
    activation_authorization_id: str
    activation_id: str
    activation_league: str
    fixture_key: str
    five_league_evidence_digest: str
    b1_acceptance_manifest_digest: str
    source_sha: str
    research_sha: str
    model_identity: str
    model_artifact_hash: str
    provider_authority: str
    request_shape_digest: str
    lifecycle_contract_id: str
    lifecycle_stage: str
    lifecycle_stage_contract_id: str
    lifecycle_timing_bounds: Mapping[str, int]
    signal_time_approval_identity: str
    maximum_odds_age_seconds: int
    retry_budget: int
    pre_activation_routing_configuration_digest: str
    rollback_snapshot_digest: str

    def _core_payload(self) -> dict[str, object]:
        return {
            "schema_version": "top5-controlled-activation-execution-plan-v1",
            "durable_plan_digest": self.durable_plan_digest,
            "activation_authorization_id": self.activation_authorization_id,
            "activation_id": self.activation_id,
            "activation_league": self.activation_league,
            "fixture_key": self.fixture_key,
            "five_league_evidence_digest": self.five_league_evidence_digest,
            "b1_acceptance_manifest_digest": self.b1_acceptance_manifest_digest,
            "source_sha": self.source_sha,
            "research_sha": self.research_sha,
            "model_identity": self.model_identity,
            "model_artifact_hash": self.model_artifact_hash,
            "provider_authority": self.provider_authority,
            "request_shape_digest": self.request_shape_digest,
            "lifecycle_contract_id": self.lifecycle_contract_id,
            "lifecycle_stage": self.lifecycle_stage,
            "lifecycle_stage_contract_id": self.lifecycle_stage_contract_id,
            "lifecycle_timing_bounds": dict(self.lifecycle_timing_bounds),
            "signal_time_approval_identity": self.signal_time_approval_identity,
            "maximum_odds_age_seconds": self.maximum_odds_age_seconds,
            "retry_budget": self.retry_budget,
            "pre_activation_routing_configuration_digest": self.pre_activation_routing_configuration_digest,
            "rollback_snapshot_digest": self.rollback_snapshot_digest,
        }

    @property
    def activation_plan_digest(self) -> str:
        return _sha(self._core_payload())

    def expected_signed_claims(self) -> dict[str, object]:
        """Return the exact payload fields derived from the accepted plan."""
        return {
            "action": ACTIVATION_AUTHORIZATION_ACTION,
            "authorization_id": self.activation_authorization_id,
            "activation_id": self.activation_id,
            "activation_league": self.activation_league,
            "fixture_key": self.fixture_key,
            "activation_plan_digest": self.activation_plan_digest,
            "five_league_evidence_digest": self.five_league_evidence_digest,
            "b1_acceptance_manifest_digest": self.b1_acceptance_manifest_digest,
            "source_sha": self.source_sha,
            "research_sha": self.research_sha,
            "model_identity": self.model_identity,
            "model_artifact_hash": self.model_artifact_hash,
            "provider_authority": self.provider_authority,
            "request_shape_digest": self.request_shape_digest,
            "lifecycle_contract_id": self.lifecycle_contract_id,
            "lifecycle_stage": self.lifecycle_stage,
            "lifecycle_stage_contract_id": self.lifecycle_stage_contract_id,
            "lifecycle_timing_bounds": dict(self.lifecycle_timing_bounds),
            "signal_time_approval_identity": self.signal_time_approval_identity,
            "maximum_odds_age_seconds": self.maximum_odds_age_seconds,
            "retry_budget": self.retry_budget,
            "pre_activation_routing_configuration_digest": self.pre_activation_routing_configuration_digest,
            "rollback_snapshot_digest": self.rollback_snapshot_digest,
            "publication": False,
            "betting": False,
            "ledger_mutation": False,
            "recurring_scheduler": False,
        }


def build_activation_execution_binding(
    *,
    plan: Top5DurableActivationPlanV1,
    receipt_package: Builder2FiveLeagueReceiptPackageV1,
    activation: ControlledActivationAuthorization,
    fixture: Fixture,
    lifecycle_contract: Top5SignalLifecycleContract,
    lifecycle_stage: str,
    now: datetime,
    lifecycle: Top5SignalLifecycle | None = None,
) -> Top5ActivationExecutionBindingV1:
    """Bind one explicit canonical lifecycle stage to the existing B4/B1/B2 plan."""
    now_utc = _utc(now, "now")
    plan.validate(now=now_utc)
    try:
        receipt_package.validate()
        activation.validate(now=now_utc)
        fixture.validate()
        lifecycle_contract.validate()
    except (DurableActivationError, ProductionContractError) as exc:
        raise ActivationAuthorizationError(
            f"activation evidence binding is invalid: {exc}"
        ) from exc

    if lifecycle_contract.contract_id != DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.contract_id:
        raise ActivationAuthorizationError("non-canonical lifecycle contract")
    if lifecycle_stage not in {"INITIAL", "REFINEMENT"}:
        raise ActivationAuthorizationError(
            "lifecycle stage must be explicitly INITIAL or REFINEMENT"
        )
    selected = next(
        (
            binding
            for binding in receipt_package.dossier.bindings
            if binding.league == plan.activation_league
        ),
        None,
    )
    if selected is None:
        raise ActivationAuthorizationError("selected league is absent from B2 evidence")
    if (
        fixture.league_code != plan.activation_league
        or fixture.fixture_key != selected.fixture_key
        or fixture.home_team != selected.home_team
        or fixture.away_team != selected.away_team
        or fixture.kickoff != selected.kickoff
        or activation.fixture_scope != (fixture.fixture_key,)
    ):
        raise ActivationAuthorizationError(
            "activation fixture differs from exact B2 selected fixture"
        )
    if (
        activation.activation_id != plan.activation_id
        or activation.authorization_id != plan.activation_authorization_id
        or activation.league_code != plan.activation_league
        or activation.source_sha.lower() != plan.source_sha.lower()
        or activation.research_sha.lower() != plan.research_sha.lower()
        or activation.model_identity != plan.model_identity
        or activation.model_artifact_hash.lower() != plan.model_artifact_hash.lower()
        or activation.provider_authority.approved_odds_provider != "the_odds_api"
    ):
        raise ActivationAuthorizationError(
            "activation authorization differs from the exact prepared plan"
        )
    if receipt_package.package_digest != plan.receipt_package_digest:
        raise ActivationAuthorizationError(
            "five-league evidence package digest mismatch"
        )
    if receipt_package.dossier.provider_identity != "therundown_experimental":
        raise ActivationAuthorizationError(
            "five-league candidate evidence identity mismatch"
        )

    lifecycle_plan = plan_signal_lifecycle(
        fixture, now_utc, lifecycle, contract=lifecycle_contract
    )
    expected_status = (
        LifecyclePlanStatus.INITIAL_DUE
        if lifecycle_stage == "INITIAL"
        else LifecyclePlanStatus.REFINEMENT_DUE
    )
    expected_due_stage = (
        SignalLifecycleStage.INITIAL
        if lifecycle_stage == "INITIAL"
        else SignalLifecycleStage.REFINED
    )
    if (
        lifecycle_plan.status is not expected_status
        or lifecycle_plan.due_stage is not expected_due_stage
    ):
        raise ActivationAuthorizationError(
            f"selected lifecycle stage is not due: {lifecycle_plan.status.value}"
        )
    policy = lifecycle_contract.policy_for(expected_due_stage)
    stage_id = lifecycle_contract.stage_contract_id(expected_due_stage)
    bounds = {
        "minimum_minutes_before_kickoff": policy.minimum_minutes_before_kickoff,
        "maximum_minutes_before_kickoff": policy.maximum_minutes_before_kickoff,
    }
    if (
        plan.signal_time_approval_identity
        != activation.signal_time_contract.approval_ref
    ):
        raise ActivationAuthorizationError(
            "separate Signal-Time approval binding differs from activation plan"
        )

    binding = Top5ActivationExecutionBindingV1(
        durable_plan_digest=plan.plan_digest,
        activation_authorization_id=plan.activation_authorization_id,
        activation_id=plan.activation_id,
        activation_league=plan.activation_league,
        fixture_key=fixture.fixture_key,
        five_league_evidence_digest=plan.receipt_package_digest,
        b1_acceptance_manifest_digest=plan.b1_manifest_digest,
        source_sha=plan.source_sha.lower(),
        research_sha=plan.research_sha.lower(),
        model_identity=plan.model_identity,
        model_artifact_hash=plan.model_artifact_hash.lower(),
        provider_authority="the_odds_api",
        request_shape_digest=the_odds_api_one_shot_request_shape_digest(
            plan.activation_league
        ),
        lifecycle_contract_id=lifecycle_contract.contract_id,
        lifecycle_stage=lifecycle_stage,
        lifecycle_stage_contract_id=stage_id,
        lifecycle_timing_bounds=bounds,
        signal_time_approval_identity=plan.signal_time_approval_identity,
        maximum_odds_age_seconds=policy.maximum_odds_age_seconds,
        retry_budget=policy.retry_budget,
        pre_activation_routing_configuration_digest=plan.pre_activation_configuration_digest,
        rollback_snapshot_digest=plan.pre_activation_snapshot_digest,
    )
    for name in (
        "durable_plan_digest",
        "five_league_evidence_digest",
        "b1_acceptance_manifest_digest",
        "request_shape_digest",
        "pre_activation_routing_configuration_digest",
        "rollback_snapshot_digest",
    ):
        _digest(getattr(binding, name), name)
    _source_sha(binding.source_sha, "source_sha")
    _source_sha(binding.research_sha, "research_sha")
    _source_sha(binding.model_artifact_hash, "model_artifact_hash")
    if binding.retry_budget != 0 or binding.maximum_odds_age_seconds != 900:
        raise ActivationAuthorizationError("canonical lifecycle safety bounds changed")
    return binding


def build_signed_activation_execution_binding(
    *,
    plan: Top5DurableActivationPlanV1,
    receipt_package: Builder2FiveLeagueReceiptPackageV1,
    b1_acceptance_bundle: Mapping[str, object],
    fixture: Fixture,
    activation_authorization_id: str,
    lifecycle_contract: Top5SignalLifecycleContract,
    lifecycle_stage: str,
    now: datetime,
    lifecycle: Top5SignalLifecycle | None = None,
) -> Top5ActivationExecutionBindingV1:
    """Build the V2 signed intent without treating legacy bearer data as authority.

    ``activation_authorization_id`` is the distinct Philip approval identity.
    The legacy plan's Signal-Time approval identity is bound separately and is
    never compared with this signed authorization identity.
    """
    now_utc = _utc(now, "now")
    plan.validate(now=now_utc)
    fixture.validate()
    lifecycle_contract.validate()
    try:
        receipt_package.validate()
        acceptance = verify_final_acceptance(b1_acceptance_bundle, now=now_utc)
    except (DurableActivationError, Top5FinalAcceptanceError) as exc:
        raise ActivationAuthorizationError(
            f"B1/B2 activation evidence is invalid: {exc}"
        ) from exc
    if lifecycle_contract.contract_id != DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.contract_id:
        raise ActivationAuthorizationError("non-canonical lifecycle contract")
    if lifecycle_stage not in {"INITIAL", "REFINEMENT"}:
        raise ActivationAuthorizationError(
            "lifecycle stage must be explicitly INITIAL or REFINEMENT"
        )
    if (
        not isinstance(activation_authorization_id, str)
        or not activation_authorization_id.strip()
    ):
        raise ActivationAuthorizationError("activation authorization ID is required")
    manifest = acceptance.get("manifest")
    if not isinstance(manifest, Mapping):
        raise ActivationAuthorizationError("verified B1 acceptance manifest is missing")
    if (
        manifest.get("manifest_digest") != plan.b1_manifest_digest
        or receipt_package.package_digest != plan.receipt_package_digest
        or acceptance.get("status") != "TOP5_FINAL_ACCEPTANCE_VERIFIED"
    ):
        raise ActivationAuthorizationError(
            "verified B1/B2 evidence differs from prepared plan"
        )
    model = b1_acceptance_bundle.get("model_runtime")
    if not isinstance(model, Mapping) or any(
        str(model.get(field, "")).lower() != expected.lower()
        for field, expected in (
            ("source_sha", plan.source_sha),
            ("research_sha", plan.research_sha),
            ("model_identity", plan.model_identity),
            ("model_artifact_hash", plan.model_artifact_hash),
        )
    ):
        raise ActivationAuthorizationError(
            "B1 model/source/Research binding differs from plan"
        )
    selected = next(
        (
            item
            for item in receipt_package.dossier.bindings
            if item.league == plan.activation_league
        ),
        None,
    )
    if selected is None or (
        fixture.league_code,
        fixture.fixture_key,
        fixture.home_team,
        fixture.away_team,
        fixture.kickoff,
    ) != (
        plan.activation_league,
        selected.fixture_key,
        selected.home_team,
        selected.away_team,
        selected.kickoff,
    ):
        raise ActivationAuthorizationError(
            "selected fixture differs from canonical B2 evidence"
        )
    if (
        plan.provider_authority != "the_odds_api"
        or receipt_package.dossier.provider_identity != "therundown_experimental"
        or receipt_package.dossier.candidate_provider_identity
        != "therundown_experimental"
    ):
        raise ActivationAuthorizationError(
            "provider authority/candidate binding is invalid"
        )
    lifecycle_plan = plan_signal_lifecycle(
        fixture, now_utc, lifecycle, contract=lifecycle_contract
    )
    expected_status = (
        LifecyclePlanStatus.INITIAL_DUE
        if lifecycle_stage == "INITIAL"
        else LifecyclePlanStatus.REFINEMENT_DUE
    )
    expected_due = (
        SignalLifecycleStage.INITIAL
        if lifecycle_stage == "INITIAL"
        else SignalLifecycleStage.REFINED
    )
    if (
        lifecycle_plan.status is not expected_status
        or lifecycle_plan.due_stage is not expected_due
    ):
        raise ActivationAuthorizationError(
            f"selected lifecycle stage is not due: {lifecycle_plan.status.value}"
        )
    policy = lifecycle_contract.policy_for(expected_due)
    binding = Top5ActivationExecutionBindingV1(
        durable_plan_digest=plan.plan_digest,
        activation_authorization_id=activation_authorization_id.strip(),
        activation_id=plan.activation_id,
        activation_league=plan.activation_league,
        fixture_key=fixture.fixture_key,
        five_league_evidence_digest=plan.receipt_package_digest,
        b1_acceptance_manifest_digest=plan.b1_manifest_digest,
        source_sha=plan.source_sha.lower(),
        research_sha=plan.research_sha.lower(),
        model_identity=plan.model_identity,
        model_artifact_hash=plan.model_artifact_hash.lower(),
        provider_authority=plan.provider_authority,
        request_shape_digest=the_odds_api_one_shot_request_shape_digest(
            plan.activation_league
        ),
        lifecycle_contract_id=lifecycle_contract.contract_id,
        lifecycle_stage=lifecycle_stage,
        lifecycle_stage_contract_id=lifecycle_contract.stage_contract_id(expected_due),
        lifecycle_timing_bounds={
            "minimum_minutes_before_kickoff": policy.minimum_minutes_before_kickoff,
            "maximum_minutes_before_kickoff": policy.maximum_minutes_before_kickoff,
        },
        signal_time_approval_identity=plan.signal_time_approval_identity,
        maximum_odds_age_seconds=policy.maximum_odds_age_seconds,
        retry_budget=policy.retry_budget,
        pre_activation_routing_configuration_digest=plan.pre_activation_configuration_digest,
        rollback_snapshot_digest=plan.pre_activation_snapshot_digest,
    )
    for name in (
        "durable_plan_digest",
        "five_league_evidence_digest",
        "b1_acceptance_manifest_digest",
        "request_shape_digest",
        "pre_activation_routing_configuration_digest",
        "rollback_snapshot_digest",
    ):
        _digest(getattr(binding, name), name)
    _source_sha(binding.source_sha, "source_sha")
    _source_sha(binding.research_sha, "research_sha")
    _source_sha(binding.model_artifact_hash, "model_artifact_hash")
    if binding.maximum_odds_age_seconds != 900 or binding.retry_budget != 0:
        raise ActivationAuthorizationError("canonical lifecycle safety bounds changed")
    return binding


def validate_activation_signal_snapshot(
    *,
    fixture: Fixture,
    snapshot: MarketSnapshot,
    lifecycle_contract: Top5SignalLifecycleContract,
    lifecycle_stage: str,
    now: datetime,
) -> None:
    """Validate a single real Signal-Time snapshot against the canonical stage."""
    fixture.validate()
    snapshot.validate()
    lifecycle_contract.validate()
    if lifecycle_contract.contract_id != DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.contract_id:
        raise ActivationAuthorizationError("non-canonical lifecycle contract")
    stage = {
        "INITIAL": SignalLifecycleStage.INITIAL,
        "REFINEMENT": SignalLifecycleStage.REFINED,
    }.get(lifecycle_stage)
    if stage is None:
        raise ActivationAuthorizationError("lifecycle stage must be explicit")
    now_utc = _utc(now, "now")
    if snapshot.kind is not MarketSnapshotKind.SIGNAL_TIME:
        raise ActivationAuthorizationError(
            "closing odds are forbidden in activation inference"
        )
    if snapshot.fixture_key != fixture.fixture_key:
        raise ActivationAuthorizationError("snapshot fixture identity mismatch")
    if snapshot.captured_at > now_utc:
        raise ActivationAuthorizationError("snapshot timestamp is in the future")
    try:
        _validate_snapshot_source_binding(
            lifecycle_contract.provider_authority_expectation,
            snapshot.source,
        )
    except ProductionContractError as exc:
        raise ActivationAuthorizationError(
            "snapshot provider provenance is invalid"
        ) from exc
    signal_time = lifecycle_contract.as_signal_time_contract(stage)
    if not signal_time.accepts(fixture.kickoff, snapshot.captured_at, now_utc):
        raise ActivationAuthorizationError(
            "snapshot is stale or outside the selected lifecycle stage window"
        )


@dataclass(frozen=True)
class VerifiedTop5ActivationAuthorizationV1:
    payload: Mapping[str, object]
    authorization_digest: str
    signer_key_id: str
    signer_fingerprint: str
    nonce: str
    _verification_seal: object = field(repr=False, compare=False)

    def assert_verified(self) -> None:
        if self._verification_seal is not _VERIFICATION_SEAL:
            raise ActivationAuthorizationError(
                "authorization object was not produced by signature verification"
            )

    def assert_valid_at(self, now: datetime) -> None:
        self.assert_verified()
        issued_at = _parse_time(self.payload.get("issued_at"), "issued_at")
        expires_at = _parse_time(self.payload.get("expires_at"), "expires_at")
        if not issued_at <= _utc(now, "now") <= expires_at:
            raise ActivationAuthorizationError(
                "activation authorization is expired or not yet valid"
            )


def _freeze_payload(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_payload(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_payload(item) for item in value)
    return value


def _load_approved_public_key(path: Path) -> tuple[Ed25519PublicKey, str]:
    if not path.is_absolute():
        raise ActivationAuthorizationError("public-key path must be absolute")
    if path.is_symlink():
        raise ActivationAuthorizationError("public-key path must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ActivationAuthorizationError(
            "approved public key is unavailable"
        ) from exc
    if ROOT.resolve() == resolved or ROOT.resolve() in resolved.parents:
        raise ActivationAuthorizationError("public key must be outside the checkout")
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ActivationAuthorizationError("public key must be a regular file")
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ActivationAuthorizationError("public key file must be owner-only")
        if metadata.st_size > 64_000:
            raise ActivationAuthorizationError(
                "public key file exceeds the safe size limit"
            )
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(64_001)
        key = serialization.load_pem_public_key(raw)
    except ActivationAuthorizationError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise ActivationAuthorizationError(
            "approved public key is unreadable or invalid"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
    if len(raw) > 64_000:
        raise ActivationAuthorizationError(
            "public key file exceeds the safe size limit"
        )
    if not isinstance(key, Ed25519PublicKey):
        raise ActivationAuthorizationError("approved key must be Ed25519")
    raw_public = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return key, hashlib.sha256(raw_public).hexdigest()


_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "action",
        "authorization_id",
        "activation_id",
        "activation_league",
        "fixture_key",
        "activation_plan_digest",
        "five_league_evidence_digest",
        "b1_acceptance_manifest_digest",
        "source_sha",
        "research_sha",
        "model_identity",
        "model_artifact_hash",
        "provider_authority",
        "request_shape_digest",
        "lifecycle_contract_id",
        "lifecycle_stage",
        "lifecycle_stage_contract_id",
        "lifecycle_timing_bounds",
        "signal_time_approval_identity",
        "maximum_odds_age_seconds",
        "retry_budget",
        "pre_activation_routing_configuration_digest",
        "rollback_snapshot_digest",
        "publication",
        "betting",
        "ledger_mutation",
        "recurring_scheduler",
        "issued_at",
        "expires_at",
        "authorization_nonce",
        "signer_key_id",
        "signer_public_key_fingerprint",
    }
)


def verify_activation_authorization(
    envelope: object,
    *,
    public_key_file: Path,
    expected_signer_key_id: str,
    expected_binding: Top5ActivationExecutionBindingV1,
    now: datetime,
) -> VerifiedTop5ActivationAuthorizationV1:
    """Verify exact canonical claims and detached Ed25519 signature."""
    if not isinstance(envelope, Mapping) or set(envelope) != {
        "schema_version",
        "payload",
        "signature",
    }:
        raise ActivationAuthorizationError("authorization envelope fields are invalid")
    if envelope.get("schema_version") != ACTIVATION_AUTHORIZATION_SCHEMA:
        raise ActivationAuthorizationError(
            "unsupported activation authorization schema"
        )
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping) or set(payload) != _PAYLOAD_FIELDS:
        raise ActivationAuthorizationError(
            "signed authorization payload fields are invalid"
        )
    if payload.get("schema_version") != ACTIVATION_AUTHORIZATION_SCHEMA:
        raise ActivationAuthorizationError("signed payload schema mismatch")
    for key, expected in expected_binding.expected_signed_claims().items():
        if payload.get(key) != expected:
            raise ActivationAuthorizationError(
                f"signed activation binding mismatch: {key}"
            )
    for key in (
        "authorization_id",
        "activation_id",
        "activation_league",
        "fixture_key",
        "model_identity",
        "lifecycle_contract_id",
        "lifecycle_stage_contract_id",
        "signer_key_id",
    ):
        if not isinstance(payload.get(key), str) or not str(payload[key]).strip():
            raise ActivationAuthorizationError(f"{key} must be non-empty text")
    if payload["provider_authority"] != "the_odds_api":
        raise ActivationAuthorizationError("production provider must be the_odds_api")
    if payload["lifecycle_stage"] not in {"INITIAL", "REFINEMENT"}:
        raise ActivationAuthorizationError("signed lifecycle stage is invalid")
    if payload["lifecycle_timing_bounds"] != dict(
        expected_binding.lifecycle_timing_bounds
    ):
        raise ActivationAuthorizationError("signed lifecycle timing bounds mismatch")
    timing_bounds = payload["lifecycle_timing_bounds"]
    if not isinstance(timing_bounds, Mapping) or any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in timing_bounds.values()
    ):
        raise ActivationAuthorizationError("lifecycle timing bounds must be integers")
    if (
        isinstance(payload["maximum_odds_age_seconds"], bool)
        or not isinstance(payload["maximum_odds_age_seconds"], int)
        or isinstance(payload["retry_budget"], bool)
        or not isinstance(payload["retry_budget"], int)
    ):
        raise ActivationAuthorizationError("lifecycle safety bounds must be integers")
    if payload["maximum_odds_age_seconds"] != 900 or payload["retry_budget"] != 0:
        raise ActivationAuthorizationError("signed lifecycle safety bounds mismatch")
    if any(
        payload.get(name) is not False
        for name in ("publication", "betting", "ledger_mutation", "recurring_scheduler")
    ):
        raise ActivationAuthorizationError(
            "signed authorization enables a prohibited capability"
        )
    nonce = payload.get("authorization_nonce")
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ActivationAuthorizationError("authorization nonce is invalid")
    if (
        not isinstance(expected_signer_key_id, str)
        or not expected_signer_key_id.strip()
    ):
        raise ActivationAuthorizationError("trusted signer key ID is required")
    if payload.get("signer_key_id") != expected_signer_key_id:
        raise ActivationAuthorizationError(
            "signer key ID differs from trusted configuration"
        )
    fingerprint = _digest(
        payload.get("signer_public_key_fingerprint"), "signer_public_key_fingerprint"
    )
    key, actual_fingerprint = _load_approved_public_key(public_key_file)
    if fingerprint != actual_fingerprint:
        raise ActivationAuthorizationError("signer public-key fingerprint mismatch")
    issued_at = _parse_time(payload.get("issued_at"), "issued_at")
    expires_at = _parse_time(payload.get("expires_at"), "expires_at")
    now_utc = _utc(now, "now")
    if expires_at <= issued_at or not issued_at <= now_utc <= expires_at:
        raise ActivationAuthorizationError(
            "activation authorization is expired or not yet valid"
        )
    signature = envelope.get("signature")
    if not isinstance(signature, str):
        raise ActivationAuthorizationError("detached signature must be base64 text")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ActivationAuthorizationError("detached signature is malformed") from exc
    try:
        key.verify(signature_bytes, canonical_authorization_bytes(payload))
    except InvalidSignature as exc:
        raise ActivationAuthorizationError(
            "activation signature verification failed"
        ) from exc
    authorization_digest = hashlib.sha256(
        canonical_authorization_bytes(payload)
    ).hexdigest()
    return VerifiedTop5ActivationAuthorizationV1(
        payload=_freeze_payload(payload),  # type: ignore[arg-type]
        authorization_digest=authorization_digest,
        signer_key_id=expected_signer_key_id,
        signer_fingerprint=actual_fingerprint,
        nonce=nonce,
        _verification_seal=_VERIFICATION_SEAL,
    )


__all__ = [
    "ACTIVATION_AUTHORIZATION_ACTION",
    "ACTIVATION_AUTHORIZATION_SCHEMA",
    "ActivationAuthorizationError",
    "Top5ActivationExecutionBindingV1",
    "VerifiedTop5ActivationAuthorizationV1",
    "build_activation_execution_binding",
    "build_signed_activation_execution_binding",
    "canonical_authorization_bytes",
    "validate_activation_signal_snapshot",
    "verify_activation_authorization",
]
