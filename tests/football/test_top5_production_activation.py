"""Fail-closed tests for the prepared Top-5 routing transition seam."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from src.football.production_contracts import (
    ProductionContractError,
    SignalTimeContract,
)
from src.football.top5_builder2_qualification_receipt import (
    RECEIPT_SCHEMA_VERSION,
    Builder2QualificationReceiptV1,
    ProviderQualificationStatus,
    semantic_digest,
)
from src.football.top5_controlled_release import (
    ApprovedProviderResultAuthority,
    ControlledActivationAuthorization,
)
from src.football.top5_controlled_shadow_provider_qualification import (
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
from tests.football.test_top5_real_shadow_session import BASE

PROVIDER = "therundown_experimental"
NOW = BASE


def _receipt() -> Builder2QualificationReceiptV1:
    result_digest = "d" * 64
    receipt = Builder2QualificationReceiptV1(
        schema_version=RECEIPT_SCHEMA_VERSION,
        qualification_receipt_id=f"b2qr-{result_digest[:24]}",
        qualification_report_identity=(
            f"session:activation:{ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED.value}"
        ),
        qualification_report_digest="a" * 64,
        qualification_result_digest=result_digest,
        qualification_session_id="session:activation",
        controlled_shadow_run_id="run:activation",
        ceo_authorization_id="ceo:qualification",
        fixture_key="BL1|fixture-1",
        provider_identity=PROVIDER,
        provider_event_id="event-1",
        provider_request_id="request-1",
        observation_id="observation-1",
        observation_digest="b" * 64,
        normalized_record_digest="c" * 64,
        cascade_evidence_digest="e" * 64,
        capture_attestation_digest="f" * 64,
        adapter_version="therundown-v1",
        adapter_source_sha="1" * 40,
        qualification_status=ProviderQualificationStatus.REAL_OBSERVATION_VALIDATED,
        accepted=True,
        prediction_input_allowed=True,
        no_bet=True,
        publication=False,
        production_activation=False,
        monetary_spend_authorized=False,
    )
    return replace(
        receipt,
        receipt_digest=semantic_digest(receipt._payload(include_receipt_digest=False)),
    )


def _authority() -> tuple[
    ApprovedProviderResultAuthority, ControlledActivationAuthorization
]:
    provider_authority = ApprovedProviderResultAuthority(
        authority_decision_id="authority:top5-provider",
        league_code="BL1",
        approved_odds_provider=PROVIDER,
        approved_provider_set=(PROVIDER,),
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
        fixture_scope=("BL1|fixture-1",),
        rollback_pointer="safe-disabled:top5",
        authorization_token="activation-token",
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    return provider_authority, activation


def _context() -> tuple[
    Top5ProductionActivationContract,
    Builder2QualificationReceiptV1,
    ApprovedProviderResultAuthority,
    ControlledActivationAuthorization,
    Top5ProductionRoutingSnapshot,
]:
    receipt = _receipt()
    authority, activation = _authority()
    current = Top5ProductionRoutingSnapshot(
        provider_order=("the_odds_api",),
        adapter_registry=("the_odds_api",),
        provider_config_digest="5" * 64,
        snapshot_id="snapshot:before-top5",
    )
    contract = Top5ProductionActivationContract(
        league_scope=("BL1",),
        approved_provider_identity=PROVIDER,
        receipt_digest=receipt.receipt_digest,
        authority_authorization_id=authority.authority_decision_id,
        activation_authorization_id=activation.authorization_id,
        activation_expires_at=activation.expires_at,
        expected_current_provider_order=current.provider_order,
        target_provider_order=(PROVIDER, "the_odds_api"),
        expected_candidate_adapter_config_digest="6" * 64,
        rollback_target=activation.rollback_pointer,
        rollback_snapshot_digest=current.snapshot_digest,
    )
    return contract, receipt, authority, activation, current


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
    contract, receipt, authority, activation, current = _context()
    plan = prepare_top5_production_activation(
        contract,
        receipt,
        authority,
        activation,
        current,
        prepared_at=NOW,
    )
    assert plan.executed is False
    assert plan.contract.receipt_digest == receipt.receipt_digest
    assert plan.contract.authority_authorization_id == authority.authority_decision_id
    assert plan.contract.activation_authorization_id == activation.authorization_id
    assert plan.target_snapshot().provider_order == (PROVIDER, "the_odds_api")
    assert plan.target_snapshot().publication_enabled is False
    assert plan.target_snapshot().top5_scheduler_enabled is False


@pytest.mark.parametrize(
    "change, message",
    [
        ("receipt", "receipt digest"),
        ("authority", "authority authorization ID"),
        ("activation", "activation authorization ID"),
        ("expiry", "activation expiry"),
    ],
)
def test_exact_authorization_bindings_fail_closed(change: str, message: str) -> None:
    contract, receipt, authority, activation, current = _context()
    if change == "receipt":
        contract = replace(contract, receipt_digest="7" * 64)
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
            receipt,
            authority,
            activation,
            current,
            prepared_at=NOW,
        )


def test_stale_activation_authorization_fails_closed() -> None:
    contract, receipt, authority, activation, current = _context()
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
            receipt,
            authority,
            stale_activation,
            current,
            prepared_at=NOW,
        )


def test_provider_order_and_snapshot_drift_fail_closed() -> None:
    contract, receipt, authority, activation, current = _context()
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
            receipt,
            authority,
            activation,
            drifted,
            prepared_at=NOW,
        )


def test_publication_scheduler_and_ledger_flags_cannot_be_enabled() -> None:
    contract, _receipt, _authority, _activation, current = _context()
    with pytest.raises(ProductionContractError, match="publication or a scheduler"):
        replace(contract, publication_enabled=True).validate()
    with pytest.raises(ProductionContractError, match="no-bet/ledger"):
        replace(contract, ledger_mutation_enabled=True).validate()
    with pytest.raises(ProductionContractError, match="publication"):
        replace(current, publication_enabled=True).validate()


def test_unapproved_target_provider_is_rejected() -> None:
    contract, _receipt_obj, _authority_obj, _activation, _current = _context()
    with pytest.raises(ProductionContractError, match="unapproved provider"):
        replace(
            contract, target_provider_order=("unknown-provider", PROVIDER)
        ).validate()


def test_activation_is_atomic_and_rollback_restores_exact_snapshot() -> None:
    contract, receipt, authority, activation, current = _context()
    plan = prepare_top5_production_activation(
        contract,
        receipt,
        authority,
        activation,
        current,
        prepared_at=NOW,
    )
    state = InMemoryTop5RoutingState(current)
    activated = state.activate(
        plan,
        receipt,
        authority,
        activation,
        now=NOW,
    )
    assert activated.provider_order == (PROVIDER, "the_odds_api")
    assert activated.publication_enabled is False
    assert activated.top5_scheduler_enabled is False
    assert activated.no_bet is True
    assert activated.ledger_mutation_enabled is False
    restored = state.rollback(plan)
    assert restored == current
    assert state.snapshot == current


def test_activation_rejects_routing_drift_before_state_change() -> None:
    contract, receipt, authority, activation, current = _context()
    plan = prepare_top5_production_activation(
        contract,
        receipt,
        authority,
        activation,
        current,
        prepared_at=NOW,
    )
    drifted = replace(current, snapshot_id="snapshot:drifted", snapshot_digest="")
    state = InMemoryTop5RoutingState(drifted)
    with pytest.raises(ProductionContractError, match="drifted"):
        state.activate(plan, receipt, authority, activation, now=NOW)
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
