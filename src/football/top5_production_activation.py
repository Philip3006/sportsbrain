"""Fail-closed Top-5 production-routing activation and rollback contract.

This module is the small adapter between the accepted Top-5 release evidence
and a future production routing writer.  It does not register a provider,
change the active scheduler, publish a signal, place a bet, or write runtime
state by itself.  A caller must provide the exact current routing snapshot and
an explicitly approved provider/activation authorization before a transition
plan can be prepared.

The in-memory state store below exists only for deterministic contract tests.
It models the atomic state transition a future operator-owned routing writer
must implement; it is deliberately not connected to the live application.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from src.football.production_contracts import ProductionContractError, _utc
from src.football.provider_cascade.contracts import (
    CANDIDATE_ONLY_PROVIDER_IDENTITIES,
    ProviderCascadeConfig,
    ProviderConfig,
    digest_record,
)
from src.football.top5_b2_qualification_batch_orchestrator import (
    Builder2FiveLeagueReceiptPackageV1,
    Builder2QualificationBatchError,
)
from src.football.top5_controlled_release import (
    ApprovedProviderResultAuthority,
    ControlledActivationAuthorization,
)
from src.football.top5_controlled_shadow_provider_qualification import TOP5_LEAGUES

TOP5_PRODUCTION_ROUTING_CONTRACT_VERSION = "top5-production-routing-activation-v1"
TOP5_CURRENT_PROVIDER_ORDER = ("the_odds_api",)
_HEX_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProductionContractError(f"{name} must be non-empty text")
    return value.strip()


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _sha256(value: object, name: str) -> str:
    value = _text(value, name)
    if _HEX_DIGEST_RE.fullmatch(value) is None:
        raise ProductionContractError(f"{name} must be a 64-character SHA-256 digest")
    return value.lower()


def _hash(value: object, name: str) -> str:
    value = _text(value, name)
    if len(value) not in (40, 64) or any(
        char not in "0123456789abcdefABCDEF" for char in value
    ):
        raise ProductionContractError(f"{name} must be a hexadecimal digest")
    return value.lower()


def _unique_order(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ProductionContractError(f"{name} must be a provider sequence")
    try:
        result = tuple(_text(item, f"{name} provider") for item in value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ProductionContractError(f"{name} must be a provider sequence") from exc
    if not result:
        raise ProductionContractError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise ProductionContractError(f"{name} contains duplicate providers")
    return result


def _provider_payload(provider: ProviderConfig) -> dict[str, object]:
    """Return a secret-free, deterministic ProviderConfig representation."""

    return {
        "name": provider.name,
        "enabled": provider.enabled,
        "league_allowlist": sorted(provider.league_allowlist),
        "market_allowlist": list(provider.market_allowlist),
        "bookmakers": list(provider.bookmakers),
        "request_budget": provider.request_budget,
        "quota_reserve": provider.quota_reserve,
        "timeout_seconds": provider.timeout_seconds,
        "max_attempts": provider.max_attempts,
        "shadow_only": provider.shadow_only,
        "quality_eligible": provider.quality_eligible,
        "candidate_only": provider.candidate_only,
        "credentials_required": provider.credentials_required,
        "credential_env": list(provider.credential_env),
        "credential_available": provider.credential_available,
        "initial_quota": provider.initial_quota.as_payload(),
        "request_cost": provider.request_cost,
        "adapter_version": provider.adapter_version,
        "source_timestamp_required": provider.source_timestamp_required,
    }


def provider_cascade_config_digest(config: ProviderCascadeConfig) -> str:
    """Hash only the secret-free ProviderCascadeConfig contract."""

    config.validate()
    return digest_record(
        {
            "provider_order": list(config.provider_order),
            "providers": {
                name: _provider_payload(config.providers[name])
                for name in sorted(config.providers)
            },
            "global_request_budget": config.global_request_budget,
            "per_run_cap": config.per_run_cap,
            "allow_candidate_only": config.allow_candidate_only,
            "fail_closed": config.fail_closed,
            "live_calls_authorized": config.live_calls_authorized,
            "controlled_shadow_run_ref": config.controlled_shadow_run_ref,
            "controlled_shadow_authorized_providers": list(
                config.controlled_shadow_authorized_providers
            ),
        }
    )


@dataclass(frozen=True)
class Top5ProductionRoutingSnapshot:
    """Exact operator-captured state before a future Top-5 transition."""

    provider_order: tuple[str, ...]
    adapter_registry: tuple[str, ...]
    provider_config_digest: str
    snapshot_id: str
    top5_scheduler_enabled: bool = False
    publication_enabled: bool = False
    no_bet: bool = True
    ledger_mutation_enabled: bool = False
    snapshot_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_order", _unique_order(self.provider_order, "provider_order")
        )
        object.__setattr__(
            self,
            "adapter_registry",
            _unique_order(self.adapter_registry, "adapter_registry"),
        )
        object.__setattr__(
            self,
            "provider_config_digest",
            _sha256(self.provider_config_digest, "provider_config_digest"),
        )
        object.__setattr__(self, "snapshot_id", _text(self.snapshot_id, "snapshot_id"))
        if not self.snapshot_digest:
            object.__setattr__(self, "snapshot_digest", self.computed_digest)

    @property
    def computed_digest(self) -> str:
        return _digest(self._payload(include_digest=False))

    def _payload(self, *, include_digest: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "contract_version": TOP5_PRODUCTION_ROUTING_CONTRACT_VERSION,
            "provider_order": list(self.provider_order),
            "adapter_registry": list(self.adapter_registry),
            "provider_config_digest": self.provider_config_digest,
            "snapshot_id": self.snapshot_id,
            "top5_scheduler_enabled": self.top5_scheduler_enabled,
            "publication_enabled": self.publication_enabled,
            "no_bet": self.no_bet,
            "ledger_mutation_enabled": self.ledger_mutation_enabled,
        }
        if include_digest:
            payload["snapshot_digest"] = self.snapshot_digest
        return payload

    def validate(self) -> None:
        _unique_order(self.provider_order, "provider_order")
        _unique_order(self.adapter_registry, "adapter_registry")
        if set(self.adapter_registry) != set(self.provider_order):
            raise ProductionContractError(
                "routing snapshot adapter registry does not match provider order"
            )
        _sha256(self.provider_config_digest, "provider_config_digest")
        _text(self.snapshot_id, "snapshot_id")
        if self.top5_scheduler_enabled:
            raise ProductionContractError(
                "Top-5 activation snapshot must not have a scheduler enabled"
            )
        if self.publication_enabled:
            raise ProductionContractError(
                "Top-5 activation snapshot must keep publication disabled"
            )
        if not self.no_bet or self.ledger_mutation_enabled:
            raise ProductionContractError(
                "Top-5 activation snapshot violates no-bet/ledger safety"
            )
        if self.snapshot_digest.lower() != self.computed_digest.lower():
            raise ProductionContractError("routing snapshot digest mismatch")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return self._payload(include_digest=True)


def current_top5_routing_snapshot(
    *, snapshot_id: str = "operator-capture"
) -> Top5ProductionRoutingSnapshot:
    """Capture the current disabled Top-5 cascade without network or writes."""

    config = ProviderCascadeConfig.default()
    return Top5ProductionRoutingSnapshot(
        provider_order=config.provider_order,
        adapter_registry=tuple(sorted(config.providers)),
        provider_config_digest=provider_cascade_config_digest(config),
        snapshot_id=snapshot_id,
    )


@dataclass(frozen=True)
class Top5ProductionActivationContract:
    """All exact bindings required for one future controlled route switch."""

    league_scope: tuple[str, ...]
    approved_provider_identity: str
    receipt_package_digest: str
    authority_authorization_id: str
    activation_authorization_id: str
    ceo_shadow_authorization_id: str
    activation_expires_at: datetime
    expected_current_provider_order: tuple[str, ...]
    target_provider_order: tuple[str, ...]
    candidate_configuration_digest: str
    candidate_adapter_source_sha: str
    target_provider_config_digest: str
    rollback_target: str
    rollback_snapshot_digest: str
    publication_enabled: bool = False
    top5_scheduler_enabled: bool = False
    no_bet: bool = True
    ledger_mutation_enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "league_scope", _unique_order(self.league_scope, "league_scope")
        )
        object.__setattr__(
            self,
            "expected_current_provider_order",
            _unique_order(
                self.expected_current_provider_order, "expected_current_provider_order"
            ),
        )
        object.__setattr__(
            self,
            "target_provider_order",
            _unique_order(self.target_provider_order, "target_provider_order"),
        )
        object.__setattr__(
            self,
            "approved_provider_identity",
            _text(self.approved_provider_identity, "approved_provider_identity"),
        )
        object.__setattr__(
            self,
            "receipt_package_digest",
            _sha256(self.receipt_package_digest, "receipt_package_digest"),
        )
        object.__setattr__(
            self,
            "candidate_configuration_digest",
            _sha256(
                self.candidate_configuration_digest,
                "candidate_configuration_digest",
            ),
        )
        object.__setattr__(
            self,
            "candidate_adapter_source_sha",
            _hash(self.candidate_adapter_source_sha, "candidate_adapter_source_sha"),
        )
        object.__setattr__(
            self,
            "target_provider_config_digest",
            _sha256(
                self.target_provider_config_digest, "target_provider_config_digest"
            ),
        )
        object.__setattr__(
            self,
            "rollback_snapshot_digest",
            _sha256(self.rollback_snapshot_digest, "rollback_snapshot_digest"),
        )
        object.__setattr__(
            self,
            "authority_authorization_id",
            _text(self.authority_authorization_id, "authority_authorization_id"),
        )
        object.__setattr__(
            self,
            "activation_authorization_id",
            _text(self.activation_authorization_id, "activation_authorization_id"),
        )
        object.__setattr__(
            self,
            "ceo_shadow_authorization_id",
            _text(self.ceo_shadow_authorization_id, "ceo_shadow_authorization_id"),
        )
        object.__setattr__(
            self, "rollback_target", _text(self.rollback_target, "rollback_target")
        )
        object.__setattr__(
            self,
            "activation_expires_at",
            _utc(self.activation_expires_at, "activation_expires_at"),
        )

    def validate(self) -> None:
        if tuple(self.league_scope) != TOP5_LEAGUES:
            raise ProductionContractError(
                "production activation requires the canonical five-league scope"
            )
        if self.approved_provider_identity in CANDIDATE_ONLY_PROVIDER_IDENTITIES:
            raise ProductionContractError(
                "candidate-only provider cannot be production authority"
            )
        if any(
            provider in CANDIDATE_ONLY_PROVIDER_IDENTITIES
            for provider in self.expected_current_provider_order
            + self.target_provider_order
        ):
            raise ProductionContractError(
                "candidate-only provider cannot enter production routing"
            )
        if set(self.expected_current_provider_order) - set(TOP5_CURRENT_PROVIDER_ORDER):
            raise ProductionContractError(
                "current production routing contains a non-canonical provider"
            )
        if set(self.target_provider_order) - set(TOP5_CURRENT_PROVIDER_ORDER):
            raise ProductionContractError(
                "target production routing contains a non-canonical provider"
            )
        if set(self.target_provider_order) - (
            set(self.expected_current_provider_order)
            | {self.approved_provider_identity}
        ):
            raise ProductionContractError(
                "target provider order contains an unapproved provider"
            )
        if not set(self.expected_current_provider_order).issubset(
            self.target_provider_order
        ):
            raise ProductionContractError(
                "target provider order must preserve the current provider order"
            )
        if self.approved_provider_identity not in self.target_provider_order:
            raise ProductionContractError(
                "target provider order does not include the approved provider"
            )
        if self.approved_provider_identity not in TOP5_CURRENT_PROVIDER_ORDER:
            raise ProductionContractError(
                "approved provider is not in the canonical production repertoire"
            )
        if self.publication_enabled or self.top5_scheduler_enabled:
            raise ProductionContractError(
                "activation contract cannot enable publication or a scheduler"
            )
        if not self.no_bet or self.ledger_mutation_enabled:
            raise ProductionContractError(
                "activation contract violates no-bet/ledger safety"
            )

    @property
    def semantic_digest(self) -> str:
        self.validate()
        return _digest(self.as_payload(include_digest=False))

    def as_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        self.validate()
        payload: dict[str, object] = {
            "contract_version": TOP5_PRODUCTION_ROUTING_CONTRACT_VERSION,
            "league_scope": list(self.league_scope),
            "approved_provider_identity": self.approved_provider_identity,
            "receipt_package_digest": self.receipt_package_digest,
            "authority_authorization_id": self.authority_authorization_id,
            "activation_authorization_id": self.activation_authorization_id,
            "ceo_shadow_authorization_id": self.ceo_shadow_authorization_id,
            "activation_expires_at": self.activation_expires_at.isoformat(),
            "expected_current_provider_order": list(
                self.expected_current_provider_order
            ),
            "target_provider_order": list(self.target_provider_order),
            "candidate_configuration_digest": self.candidate_configuration_digest,
            "candidate_adapter_source_sha": self.candidate_adapter_source_sha,
            "target_provider_config_digest": self.target_provider_config_digest,
            "rollback_target": self.rollback_target,
            "rollback_snapshot_digest": self.rollback_snapshot_digest,
            "publication_enabled": self.publication_enabled,
            "top5_scheduler_enabled": self.top5_scheduler_enabled,
            "no_bet": self.no_bet,
            "ledger_mutation_enabled": self.ledger_mutation_enabled,
        }
        if include_digest:
            payload["semantic_digest"] = self.semantic_digest
        return payload

    def bind(
        self,
        receipt_package: Builder2FiveLeagueReceiptPackageV1,
        authority: ApprovedProviderResultAuthority,
        activation: ControlledActivationAuthorization,
        current: Top5ProductionRoutingSnapshot,
        *,
        now: datetime,
    ) -> None:
        self.validate()
        if not isinstance(receipt_package, Builder2FiveLeagueReceiptPackageV1):
            raise ProductionContractError(
                "the canonical five-league receipt package is required"
            )
        try:
            receipt_package.validate()
        except Builder2QualificationBatchError as exc:
            raise ProductionContractError(
                "canonical five-league receipt package is invalid"
            ) from exc
        dossier = receipt_package.dossier
        if receipt_package.package_digest.lower() != self.receipt_package_digest.lower():
            raise ProductionContractError("qualification receipt package digest mismatch")
        if (
            tuple(receipt.qualification_receipt_id for receipt in receipt_package.receipts)
            != dossier.receipt_ids
            or tuple(receipt.receipt_digest for receipt in receipt_package.receipts)
            != dossier.receipt_digests
        ):
            raise ProductionContractError(
                "receipt package/dossier receipt identity binding mismatch"
            )
        if any(
            receipt.provider_identity != dossier.provider_identity
            for receipt in receipt_package.receipts
        ):
            raise ProductionContractError(
                "receipt package contains mixed provider evidence"
            )
        if dossier.configuration_digest != self.candidate_configuration_digest:
            raise ProductionContractError("candidate configuration digest mismatch")
        if dossier.adapter_source_sha.lower() != self.candidate_adapter_source_sha.lower():
            raise ProductionContractError("candidate adapter source SHA mismatch")
        authority.validate(now=now)
        activation.validate(now=now)
        current.validate()
        if dossier.provider_identity not in CANDIDATE_ONLY_PROVIDER_IDENTITIES:
            raise ProductionContractError(
                "receipt package provider is not canonical candidate evidence"
            )
        if authority.approved_odds_provider != self.approved_provider_identity:
            raise ProductionContractError(
                "approved production provider identity mismatch"
            )
        if authority.approved_odds_provider in CANDIDATE_ONLY_PROVIDER_IDENTITIES:
            raise ProductionContractError(
                "candidate-only provider cannot receive production authority"
            )
        if authority.authority_decision_id != self.authority_authorization_id:
            raise ProductionContractError(
                "provider authority authorization ID mismatch"
            )
        if authority.league_code not in self.league_scope:
            raise ProductionContractError(
                "provider authority league is outside activation scope"
            )
        if set(authority.approved_provider_set) != {self.approved_provider_identity}:
            raise ProductionContractError(
                "provider authority is broader than the canonical production provider"
            )
        if activation.authorization_id != self.activation_authorization_id:
            raise ProductionContractError("activation authorization ID mismatch")
        if activation.provider_authority != authority:
            raise ProductionContractError(
                "activation/provider authority binding mismatch"
            )
        if activation.expires_at != self.activation_expires_at:
            raise ProductionContractError("activation expiry binding mismatch")
        if activation.ceo_shadow_authorization_id != dossier.ceo_authorization_id:
            raise ProductionContractError("CEO shadow authorization binding mismatch")
        if (
            activation.controlled_shadow_run_id != dossier.controlled_shadow_run_id
            or activation.qualification_session_id != dossier.qualification_session_id
        ):
            raise ProductionContractError("receipt package run/session binding mismatch")
        package_fixture_scope = tuple(
            receipt_package.dossier.bindings[index].fixture_key
            for index in range(len(receipt_package.dossier.bindings))
        )
        if set(package_fixture_scope) != set(activation.fixture_scope):
            raise ProductionContractError("receipt package fixture scope binding mismatch")
        if current.provider_order != self.expected_current_provider_order:
            raise ProductionContractError(
                "unexpected current production provider order"
            )
        if current.snapshot_digest.lower() != self.rollback_snapshot_digest.lower():
            raise ProductionContractError(
                "rollback snapshot does not match current routing"
            )
        if current.provider_config_digest.lower() == self.target_provider_config_digest.lower():
            raise ProductionContractError(
                "target provider/config digest is not a new routing target"
            )
        if now > self.activation_expires_at:
            raise ProductionContractError("activation authorization is stale")


@dataclass(frozen=True)
class Top5ProductionActivationPlan:
    contract: Top5ProductionActivationContract
    pre_activation_snapshot: Top5ProductionRoutingSnapshot
    prepared_at: datetime
    executed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "prepared_at", _utc(self.prepared_at, "prepared_at"))

    def validate(
        self,
        receipt_package: Builder2FiveLeagueReceiptPackageV1,
        authority: ApprovedProviderResultAuthority,
        activation: ControlledActivationAuthorization,
        *,
        now: datetime,
    ) -> None:
        if self.executed:
            raise ProductionContractError("activation plan is already executed")
        self.pre_activation_snapshot.validate()
        self.contract.bind(
            receipt_package,
            authority,
            activation,
            self.pre_activation_snapshot,
            now=now,
        )
        if (
            self.pre_activation_snapshot.snapshot_digest
            != self.contract.rollback_snapshot_digest
        ):
            raise ProductionContractError(
                "activation plan rollback target is not exact"
            )

    @property
    def plan_digest(self) -> str:
        return _digest(
            {
                "contract": self.contract.as_payload(include_digest=False),
                "pre_activation_snapshot": self.pre_activation_snapshot.as_payload(),
                "prepared_at": self.prepared_at.isoformat(),
            }
        )

    def target_snapshot(self) -> Top5ProductionRoutingSnapshot:
        self.contract.validate()
        return Top5ProductionRoutingSnapshot(
            provider_order=self.contract.target_provider_order,
            adapter_registry=self.contract.target_provider_order,
            provider_config_digest=self.contract.target_provider_config_digest,
            snapshot_id=f"active:{self.contract.activation_authorization_id}",
        )

    def as_payload(self) -> dict[str, object]:
        self.contract.validate()
        self.pre_activation_snapshot.validate()
        return {
            "contract": self.contract.as_payload(),
            "pre_activation_snapshot": self.pre_activation_snapshot.as_payload(),
            "prepared_at": self.prepared_at.isoformat(),
            "plan_digest": self.plan_digest,
            "executed": False,
        }


def prepare_top5_production_activation(
    contract: Top5ProductionActivationContract,
    receipt_package: Builder2FiveLeagueReceiptPackageV1,
    authority: ApprovedProviderResultAuthority,
    activation: ControlledActivationAuthorization,
    current: Top5ProductionRoutingSnapshot,
    *,
    prepared_at: datetime,
) -> Top5ProductionActivationPlan:
    """Validate all bindings and return a non-executed activation plan."""

    plan = Top5ProductionActivationPlan(contract, current, prepared_at)
    plan.validate(receipt_package, authority, activation, now=prepared_at)
    return plan


class InMemoryTop5RoutingState:
    """Deterministic test double for an operator-owned routing state writer."""

    def __init__(self, snapshot: Top5ProductionRoutingSnapshot) -> None:
        snapshot.validate()
        self._snapshot = snapshot
        self._active_plan_digest: str | None = None
        self._rollback_snapshot: Top5ProductionRoutingSnapshot | None = None

    @property
    def snapshot(self) -> Top5ProductionRoutingSnapshot:
        return self._snapshot

    def activate(
        self,
        plan: Top5ProductionActivationPlan,
        receipt_package: Builder2FiveLeagueReceiptPackageV1,
        authority: ApprovedProviderResultAuthority,
        activation: ControlledActivationAuthorization,
        *,
        now: datetime,
    ) -> Top5ProductionRoutingSnapshot:
        if self._active_plan_digest is not None:
            raise ProductionContractError(
                "a Top-5 routing activation is already active"
            )
        if (
            self._snapshot.snapshot_digest
            != plan.pre_activation_snapshot.snapshot_digest
        ):
            raise ProductionContractError(
                "current routing drifted since pre-activation snapshot"
            )
        plan.validate(receipt_package, authority, activation, now=now)
        target = plan.target_snapshot()
        self._rollback_snapshot = self._snapshot
        self._active_plan_digest = plan.plan_digest
        self._snapshot = target
        return target

    def rollback(
        self, plan: Top5ProductionActivationPlan
    ) -> Top5ProductionRoutingSnapshot:
        if (
            self._active_plan_digest != plan.plan_digest
            or self._rollback_snapshot is None
        ):
            raise ProductionContractError(
                "rollback target is not the active exact activation"
            )
        if (
            self._snapshot.snapshot_id
            != f"active:{plan.contract.activation_authorization_id}"
        ):
            raise ProductionContractError(
                "active routing state does not match activation"
            )
        restored = self._rollback_snapshot
        restored.validate()
        self._snapshot = restored
        self._rollback_snapshot = None
        self._active_plan_digest = None
        return restored


__all__ = [
    "TOP5_CURRENT_PROVIDER_ORDER",
    "TOP5_PRODUCTION_ROUTING_CONTRACT_VERSION",
    "InMemoryTop5RoutingState",
    "Top5ProductionActivationContract",
    "Top5ProductionActivationPlan",
    "Top5ProductionRoutingSnapshot",
    "current_top5_routing_snapshot",
    "prepare_top5_production_activation",
    "provider_cascade_config_digest",
]
