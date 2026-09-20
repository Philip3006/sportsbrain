"""Fail-closed tests for the prepared Top-5 routing transition seam."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from src.football.production_contracts import (
    ProductionContractError,
    SignalTimeContract,
)
from src.football.provider_cascade.contracts import CANDIDATE_ONLY_PROVIDER_IDENTITIES
from src.football.top5_b2_qualification_batch_orchestrator import (
    build_five_league_shadow_package,
    consume_five_league_shadow_package,
)
from src.football.top5_controlled_release import (
    ApprovedProviderResultAuthority,
    ControlledActivationAuthorization,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    TOP5_LEAGUES,
    MinimumSamplePolicy,
)
from src.football.top5_production_activation import (
    InMemoryTop5RoutingState,
    Top5ProductionActivationContract,
    Top5ProductionRoutingSnapshot,
    current_top5_routing_snapshot,
    prepare_top5_production_activation,
    provider_cascade_config_digest,
)
from tests.football.test_top5_b2_five_league_receipt import _canonical_run_and_manifests
from tests.football.test_top5_real_shadow_session import BASE

CANDIDATE_PROVIDER = next(iter(CANDIDATE_ONLY_PROVIDER_IDENTITIES))
PRODUCTION_PROVIDER = "the_odds_api"
NOW = BASE


def _package():
    run, manifests = _canonical_run_and_manifests()
    return consume_five_league_shadow_package(
        build_five_league_shadow_package(run, manifests)
    )


def _authority() -> tuple[
    ApprovedProviderResultAuthority, ControlledActivationAuthorization
]:
    provider_authority = ApprovedProviderResultAuthority(
        authority_decision_id="authority:top5-provider",
        league_code="BL1",
        approved_odds_provider=PRODUCTION_PROVIDER,
        approved_provider_set=(PRODUCTION_PROVIDER,),
        approved_result_source="result-source:top5",
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    signal_time = SignalTimeContract(
        30,
        180,
        300,
        approval_ref="activation:top5-provider",
    )
    activation = ControlledActivationAuthorization(
        authorization_id="activation:top5-provider",
        activation_id="activation-run:top5-provider",
        league_code="BL1",
        candidate_id="top5-model-candidate",
        model_identity="top5-model-candidate",
        source_sha="2" * 40,
        research_sha="3" * 40,
        model_artifact_hash="4" * 64,
        signal_time_experiment_id="signal-time:top5",
        signal_time_contract=signal_time,
        minimum_sample_policy=MinimumSamplePolicy(1, 1),
        provider_authority=provider_authority,
        controlled_shadow_run_id="run:activation",
        qualification_session_id="session:activation",
        ceo_shadow_authorization_id="ceo:qualification",
        fixture_scope=("BL1|fixture-1",),
        rollback_pointer="safe-disabled:top5",
        authorization_token="activation-token",
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    return provider_authority, activation


def _context() -> tuple[
    Top5ProductionActivationContract,
    object,
    ApprovedProviderResultAuthority,
    ControlledActivationAuthorization,
    Top5ProductionRoutingSnapshot,
]:
    package = _package()
    authority, activation = _authority()
    activation = replace(
        activation,
        controlled_shadow_run_id=package.dossier.controlled_shadow_run_id,
        qualification_session_id=package.dossier.qualification_session_id,
        ceo_shadow_authorization_id=package.dossier.ceo_authorization_id,
        fixture_scope=tuple(binding.fixture_key for binding in package.dossier.bindings),
    )
    current = Top5ProductionRoutingSnapshot(
        provider_order=("the_odds_api",),
        adapter_registry=("the_odds_api",),
        provider_config_digest="5" * 64,
        snapshot_id="snapshot:before-top5",
    )
    contract = Top5ProductionActivationContract(
        league_scope=TOP5_LEAGUES,
        approved_provider_identity=PRODUCTION_PROVIDER,
        receipt_package_digest=package.package_digest,
        authority_authorization_id=authority.authority_decision_id,
        activation_authorization_id=activation.authorization_id,
        ceo_shadow_authorization_id=package.dossier.ceo_authorization_id,
        activation_expires_at=activation.expires_at,
        expected_current_provider_order=current.provider_order,
        target_provider_order=(PRODUCTION_PROVIDER,),
        candidate_configuration_digest=package.dossier.configuration_digest,
        candidate_adapter_source_sha=package.dossier.adapter_source_sha,
        target_provider_config_digest="6" * 64,
        rollback_target=activation.rollback_pointer,
        rollback_snapshot_digest=current.snapshot_digest,
    )
    return contract, package, authority, activation, current


def test_current_snapshot_is_the_odds_api_only_and_disabled() -> None:
    snapshot = current_top5_routing_snapshot()
    assert snapshot.provider_order == ("the_odds_api",)
    assert snapshot.adapter_registry == ("the_odds_api",)
    assert snapshot.top5_scheduler_enabled is False
    assert snapshot.publication_enabled is False
    assert snapshot.no_bet is True
    assert snapshot.ledger_mutation_enabled is False
    snapshot.validate()


def test_prepare_binds_receipt_authority_expiry_and_current_route_exactly() -> None:
    contract, package, authority, activation, current = _context()
    plan = prepare_top5_production_activation(
        contract,
        package,
        authority,
        activation,
        current,
        prepared_at=NOW,
    )
    assert plan.executed is False
    assert plan.contract.receipt_package_digest == package.package_digest
    assert plan.contract.authority_authorization_id == authority.authority_decision_id
    assert plan.contract.activation_authorization_id == activation.authorization_id
    assert plan.target_snapshot().provider_order == (PRODUCTION_PROVIDER,)
    assert plan.target_snapshot().publication_enabled is False
    assert plan.target_snapshot().top5_scheduler_enabled is False


def test_prepare_requires_the_complete_five_receipt_package() -> None:
    contract, package, authority, activation, current = _context()
    with pytest.raises(ProductionContractError, match="canonical five-league receipt package"):
        prepare_top5_production_activation(
            contract,
            package.receipts[0],
            authority,
            activation,
            current,
            prepared_at=NOW,
        )
    incomplete = replace(package, receipts=package.receipts[:4])
    with pytest.raises(ProductionContractError, match="canonical five-league receipt package"):
        prepare_top5_production_activation(
            contract,
            incomplete,
            authority,
            activation,
            current,
            prepared_at=NOW,
        )


def test_candidate_evidence_provider_cannot_be_production_authority_or_route() -> None:
    contract, package, authority, activation, current = _context()
    with pytest.raises(ProductionContractError, match="candidate-only provider"):
        replace(
            contract,
            approved_provider_identity=CANDIDATE_PROVIDER,
            target_provider_order=(CANDIDATE_PROVIDER,),
        ).validate()
    candidate_authority = replace(
        authority,
        approved_odds_provider=CANDIDATE_PROVIDER,
        approved_provider_set=(CANDIDATE_PROVIDER,),
    )
    candidate_activation = replace(activation, provider_authority=candidate_authority)
    with pytest.raises(ProductionContractError, match="approved production provider"):
        prepare_top5_production_activation(
            contract,
            package,
            candidate_authority,
            candidate_activation,
            current,
            prepared_at=NOW,
        )


def test_package_and_dossier_binding_drift_fails_before_prepare() -> None:
    contract, package, authority, activation, current = _context()
    altered_dossier = replace(
        package.dossier, controlled_shadow_run_id="run:altered"
    )
    altered_package = replace(package, dossier=altered_dossier)
    with pytest.raises(ProductionContractError, match="canonical five-league receipt package"):
        prepare_top5_production_activation(
            contract,
            altered_package,
            authority,
            activation,
            current,
            prepared_at=NOW,
        )


def test_single_receipt_cannot_cross_apply_boundary_or_mutate_state() -> None:
    contract, package, authority, activation, current = _context()
    plan = prepare_top5_production_activation(
        contract,
        package,
        authority,
        activation,
        current,
        prepared_at=NOW,
    )
    state = InMemoryTop5RoutingState(current)
    with pytest.raises(ProductionContractError, match="canonical five-league receipt package"):
        state.activate(
            plan,
            package.receipts[0],
            authority,
            activation,
            now=NOW,
        )
    assert state.snapshot == current


@pytest.mark.parametrize(
    "change, message",
    [
        ("receipt", "receipt package digest"),
        ("authority", "authority authorization ID"),
        ("activation", "activation authorization ID"),
        ("expiry", "activation expiry"),
    ],
)
def test_exact_authorization_bindings_fail_closed(change: str, message: str) -> None:
    contract, package, authority, activation, current = _context()
    if change == "receipt":
        contract = replace(contract, receipt_package_digest="7" * 64)
    elif change == "authority":
        contract = replace(contract, authority_authorization_id="authority:wrong")
    elif change == "activation":
        contract = replace(contract, activation_authorization_id="activation:wrong")
    else:
        contract = replace(
            contract, activation_expires_at=activation.expires_at + timedelta(seconds=1)
        )
    with pytest.raises(ProductionContractError, match=message):
        prepare_top5_production_activation(
            contract,
            package,
            authority,
            activation,
            current,
            prepared_at=NOW,
        )


def test_stale_activation_authorization_fails_closed() -> None:
    contract, package, authority, activation, current = _context()
    stale_activation = replace(
        activation,
        expires_at=NOW - timedelta(seconds=1),
    )
    stale_contract = replace(
        contract,
        activation_expires_at=stale_activation.expires_at,
    )
    with pytest.raises(ProductionContractError, match="expired"):
        prepare_top5_production_activation(
            stale_contract,
            package,
            authority,
            stale_activation,
            current,
            prepared_at=NOW,
        )


def test_provider_order_and_snapshot_drift_fail_closed() -> None:
    contract, package, authority, activation, current = _context()
    drifted = replace(
        current,
        provider_order=("unexpected_provider",),
        adapter_registry=("unexpected_provider",),
        snapshot_digest="",
    )
    with pytest.raises(
        ProductionContractError, match="current production provider order"
    ):
        prepare_top5_production_activation(
            contract,
            package,
            authority,
            activation,
            drifted,
            prepared_at=NOW,
        )


def test_publication_scheduler_and_ledger_flags_cannot_be_enabled() -> None:
    contract, _package_obj, _authority, _activation, current = _context()
    with pytest.raises(ProductionContractError, match="publication or a scheduler"):
        replace(contract, publication_enabled=True).validate()
    with pytest.raises(ProductionContractError, match="no-bet/ledger"):
        replace(contract, ledger_mutation_enabled=True).validate()
    with pytest.raises(ProductionContractError, match="publication"):
        replace(current, publication_enabled=True).validate()


def test_unapproved_target_provider_is_rejected() -> None:
    contract, _package_obj, _authority_obj, _activation, _current = _context()
    with pytest.raises(ProductionContractError, match="non-canonical provider"):
        replace(
            contract, target_provider_order=("unknown-provider", PRODUCTION_PROVIDER)
        ).validate()


def test_activation_is_atomic_and_rollback_restores_exact_snapshot() -> None:
    contract, package, authority, activation, current = _context()
    plan = prepare_top5_production_activation(
        contract,
        package,
        authority,
        activation,
        current,
        prepared_at=NOW,
    )
    state = InMemoryTop5RoutingState(current)
    activated = state.activate(
        plan,
        package,
        authority,
        activation,
        now=NOW,
    )
    assert activated.provider_order == (PRODUCTION_PROVIDER,)
    assert activated.publication_enabled is False
    assert activated.top5_scheduler_enabled is False
    assert activated.no_bet is True
    assert activated.ledger_mutation_enabled is False
    restored = state.rollback(plan)
    assert restored == current
    assert state.snapshot == current


def test_activation_rejects_routing_drift_before_state_change() -> None:
    contract, package, authority, activation, current = _context()
    plan = prepare_top5_production_activation(
        contract,
        package,
        authority,
        activation,
        current,
        prepared_at=NOW,
    )
    drifted = replace(current, snapshot_id="snapshot:drifted", snapshot_digest="")
    state = InMemoryTop5RoutingState(drifted)
    with pytest.raises(ProductionContractError, match="drifted"):
        state.activate(plan, package, authority, activation, now=NOW)
    assert state.snapshot == drifted


def test_provider_config_digest_is_deterministic_and_changes_with_config() -> None:
    from dataclasses import replace as dataclass_replace

    from src.football.provider_cascade.contracts import ProviderCascadeConfig

    config = ProviderCascadeConfig.default()
    assert provider_cascade_config_digest(config) == provider_cascade_config_digest(
        config
    )
    changed = dataclass_replace(config, per_run_cap=0)
    assert provider_cascade_config_digest(config) != provider_cascade_config_digest(
        changed
    )
