from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from src.football.top5_activation_authorization import (
    VerifiedTop5ActivationAuthorizationV1,
    verify_activation_authorization,
)
from src.football.top5_activation_route_state import (
    DurableTop5ProductionRouteStateStore,
    Top5ProductionRouteConsumer,
    Top5RouteStateError,
)
from tests.football.test_top5_activation_authorization import (
    NOW,
    SIGNER_ID,
    _resign,
    _signed_context,
)


def test_route_record_is_durable_and_exactly_restored_without_claiming_live_rollback(
    tmp_path,
):
    (
        _package,
        _bundle,
        _plan,
        _fixture,
        _lifecycle,
        binding,
        _signer,
        _public_path,
        _envelope,
        verified,
    ) = _signed_context(tmp_path)
    path = tmp_path / "external" / "route-state.json"
    store = DurableTop5ProductionRouteStateStore(path)
    baseline = store.read()["current_route"]
    prepared = store.prepare(binding, now=NOW)
    assert prepared["status"] == "PREPARED"
    assert store.prepare(binding, now=NOW) == prepared

    authorized = store.authorize(binding, verified, now=NOW)
    assert authorized["status"] == "AUTHORIZED"
    assert store.authorize(binding, verified, now=NOW) == authorized
    assert authorized["authorization_nonce"] == verified.nonce

    executing = store.begin_execution(binding, verified, now=NOW + timedelta(seconds=1))
    assert executing["status"] == "EXECUTING"
    active_record = store.read()["current_route"]
    assert active_record["activation_mode"] == "CONTROLLED_ONE_SHOT_EXECUTING"
    assert active_record["provider_authority"] == "the_odds_api"
    for flag in ("publication", "recurring_scheduler", "betting", "ledger_mutation"):
        assert active_record[flag] is False

    restarted = DurableTop5ProductionRouteStateStore(path)
    assert restarted.read()["current_route"] == active_record
    rolled_back = restarted.rollback(
        binding.activation_id,
        binding.activation_plan_digest,
        now=NOW + timedelta(seconds=2),
    )
    assert rolled_back["status"] == "ROLLED_BACK"
    final_store = DurableTop5ProductionRouteStateStore(path)
    state = final_store.read()
    assert state["current_route"] == baseline
    assert [row["transition"] for row in state["audit_history"]] == [
        "PREPARED",
        "AUTHORIZED",
        "EXECUTING",
        "ROLLBACK",
        "ROLLED_BACK",
    ]
    assert path.stat().st_mode & 0o077 == 0
    rollback_readback = Top5ProductionRouteConsumer(
        final_store
    ).verify_disabled_after_rollback(
        binding.activation_id, binding.activation_plan_digest
    )
    assert rollback_readback["activation_mode"] == "DISABLED"
    assert final_store.read()["current_route"] == baseline


def test_signed_nonce_cannot_authorize_a_second_activation(tmp_path):
    (
        _package,
        _bundle,
        _plan,
        _fixture,
        _lifecycle,
        binding,
        signer,
        public_path,
        envelope,
        verified,
    ) = _signed_context(tmp_path)
    store = DurableTop5ProductionRouteStateStore(tmp_path / "route-state.json")
    store.prepare(binding, now=NOW)
    store.authorize(binding, verified, now=NOW)
    store.begin_execution(binding, verified, now=NOW + timedelta(seconds=1))
    store.rollback(
        binding.activation_id,
        binding.activation_plan_digest,
        now=NOW + timedelta(seconds=2),
    )

    second_binding = replace(binding, activation_id="activation:other-one-shot")
    second_envelope = _resign(
        envelope,
        signer,
        **second_binding.expected_signed_claims(),
    )
    second_verified = verify_activation_authorization(
        second_envelope,
        public_key_file=public_path,
        expected_signer_key_id=SIGNER_ID,
        expected_binding=second_binding,
        now=NOW,
    )
    store.prepare(second_binding, now=NOW + timedelta(seconds=3))
    with pytest.raises(Top5RouteStateError, match="nonce replay"):
        store.authorize(second_binding, second_verified, now=NOW + timedelta(seconds=3))


def test_expired_signature_cannot_enter_executing_after_authorization(tmp_path):
    *_, binding, _signer, _public_path, _envelope, verified = _signed_context(tmp_path)
    store = DurableTop5ProductionRouteStateStore(tmp_path / "route-state.json")
    store.prepare(binding, now=NOW)
    store.authorize(binding, verified, now=NOW)
    before = store.read()
    with pytest.raises(Top5RouteStateError, match="expired or not yet valid"):
        store.begin_execution(
            binding,
            verified,
            now=NOW + timedelta(minutes=11),
        )
    assert store.read() == before


def test_unverified_authorization_object_cannot_change_route_state(tmp_path):
    *_, binding, _signer, _public_path, _envelope, verified = _signed_context(tmp_path)
    store = DurableTop5ProductionRouteStateStore(tmp_path / "route-state.json")
    store.prepare(binding, now=NOW)
    forged = VerifiedTop5ActivationAuthorizationV1(
        payload=binding.expected_signed_claims(),
        authorization_digest=verified.authorization_digest,
        signer_key_id=verified.signer_key_id,
        signer_fingerprint=verified.signer_fingerprint,
        nonce=verified.nonce,
        _verification_seal=None,
    )
    with pytest.raises(Top5RouteStateError, match="not produced by signature"):
        store.authorize(binding, forged, now=NOW)
    assert store.read()["records"][binding.activation_id]["status"] == "PREPARED"


def test_route_cannot_be_reprepared_under_changed_binding_or_fake_candidate_authority(
    tmp_path,
):
    *_, binding, _signer, _public_path, _envelope, _verified = _signed_context(tmp_path)
    store = DurableTop5ProductionRouteStateStore(tmp_path / "route-state.json")
    store.prepare(binding, now=NOW)
    with pytest.raises(Top5RouteStateError, match="another activation is pending"):
        store.prepare(replace(binding, activation_id="activation:second"), now=NOW)
    with pytest.raises(Top5RouteStateError, match="already bound to another plan"):
        store.prepare(replace(binding, fixture_key="fixture:other"), now=NOW)
    with pytest.raises(Top5RouteStateError, match="provider/retry"):
        store.prepare(
            replace(binding, provider_authority="therundown_experimental"), now=NOW
        )
    with pytest.raises(Top5RouteStateError, match="provider/retry"):
        store.prepare(replace(binding, retry_budget=1), now=NOW)
    with pytest.raises(Top5RouteStateError, match="disabled route baseline"):
        store.prepare(
            replace(binding, pre_activation_routing_configuration_digest="0" * 64),
            now=NOW,
        )
