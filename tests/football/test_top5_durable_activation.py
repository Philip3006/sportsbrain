from __future__ import annotations

import json
import stat
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from functools import lru_cache

import pytest

from scripts.top5_controlled_activation import (
    _match_prepared_plan_timestamp,
)
from scripts.top5_controlled_activation import _read as read_activation_input
from src.football.production_contracts import (
    ProductionContractError,
    SignalTimeContract,
)
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
from src.football.top5_durable_activation import (
    PRODUCTION_RUNTIME_BLOCKER,
    DurableActivationError,
    DurableTop5ActivationStore,
    activation_health_payload,
    prepare_top5_durable_activation_plan,
)
from src.football.top5_production_activation import current_top5_routing_snapshot
from tests.football.test_top5_b2_five_league_receipt import _canonical_run_and_manifests
from tests.football.test_top5_final_acceptance import (
    MODEL_ARTIFACT_HASH,
    NOW_ACCEPTANCE,
    SOURCE_SHA,
    _bundle,
)

NOW = NOW_ACCEPTANCE


@lru_cache(maxsize=1)
def _base_inputs():
    run, manifests = _canonical_run_and_manifests()
    package = consume_five_league_shadow_package(
        build_five_league_shadow_package(run, manifests)
    )
    acceptance_bundle = _bundle()
    acceptance_bundle["controlled_shadow"] = run.as_payload()
    b1_result_ids = {
        "run": package.dossier.controlled_shadow_run_id,
        "session": package.dossier.qualification_session_id,
        "ceo": package.dossier.ceo_authorization_id,
    }
    assert {
        "run": run.controlled_shadow_run_id,
        "session": run.qualification_session_id,
        "ceo": run.authorization_id,
    } == b1_result_ids
    authority = ApprovedProviderResultAuthority(
        authority_decision_id="authority:top5-one-league",
        league_code="BL1",
        approved_odds_provider="the_odds_api",
        approved_provider_set=("the_odds_api",),
        approved_result_source="results:canonical",
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    selected = next(
        binding for binding in package.dossier.bindings if binding.league == "BL1"
    )
    authorization = ControlledActivationAuthorization(
        authorization_id="activation-auth:one-league",
        activation_id="activation:BL1:one-shot",
        league_code="BL1",
        candidate_id=acceptance_bundle["model_runtime"]["model_identity"],
        model_identity=acceptance_bundle["model_runtime"]["model_identity"],
        source_sha=SOURCE_SHA,
        research_sha=acceptance_bundle["model_runtime"]["research_sha"],
        model_artifact_hash=MODEL_ARTIFACT_HASH,
        signal_time_experiment_id="signal-time-approved:BL1",
        signal_time_contract=SignalTimeContract(
            30, 180, 300, "activation-auth:one-league"
        ),
        minimum_sample_policy=MinimumSamplePolicy(1, 1),
        provider_authority=authority,
        controlled_shadow_run_id=package.dossier.controlled_shadow_run_id,
        qualification_session_id=package.dossier.qualification_session_id,
        ceo_shadow_authorization_id=package.dossier.ceo_authorization_id,
        fixture_scope=(selected.fixture_key,),
        rollback_pointer="safe-disabled:BL1",
        authorization_token="offline-test-token-not-persisted",
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=20),
    )
    snapshot = current_top5_routing_snapshot(
        snapshot_id="snapshot:disabled-before-canary"
    )
    plan = prepare_top5_durable_activation_plan(
        receipt_package=package,
        b1_acceptance_bundle=acceptance_bundle,
        authority=authority,
        activation=authorization,
        current_snapshot=snapshot,
        signal_time_approval_identity="activation-auth:one-league",
        now=NOW,
    )
    return package, acceptance_bundle, authority, authorization, snapshot, plan


def _inputs():
    package, acceptance_bundle, authority, authorization, snapshot, plan = (
        _base_inputs()
    )
    return (
        package,
        deepcopy(acceptance_bundle),
        authority,
        authorization,
        snapshot,
        plan,
    )


def test_plan_keeps_five_league_evidence_and_one_league_activation_only():
    package, _bundle_value, _authority_value, _auth, _snapshot, plan = _inputs()
    assert package.dossier.leagues == TOP5_LEAGUES
    assert plan.evidence_leagues == TOP5_LEAGUES
    assert plan.activation_league == "BL1"
    assert plan.provider_authority == "the_odds_api"
    assert plan.candidate_provider == "therundown_experimental"
    assert plan.publication_enabled is False
    assert plan.scheduler_registered is False
    assert plan.betting_enabled is False
    assert plan.ledger_mutation_enabled is False


@pytest.mark.parametrize(
    "mutation", ("scope", "signal_time", "candidate_authority", "fixture")
)
def test_plan_rejects_scope_authority_signal_or_fixture_drift(mutation):
    package, b1_bundle, authority, activation, snapshot, _plan = _inputs()
    if mutation == "scope":
        package = replace(package, receipts=package.receipts[:4])
    elif mutation == "signal_time":
        with pytest.raises(
            DurableActivationError, match="Signal-Time approval identity"
        ):
            prepare_top5_durable_activation_plan(
                receipt_package=package,
                b1_acceptance_bundle=b1_bundle,
                authority=authority,
                activation=activation,
                current_snapshot=snapshot,
                signal_time_approval_identity="different-signal-approval",
                now=NOW,
            )
        return
    elif mutation == "candidate_authority":
        authority = replace(
            authority,
            approved_odds_provider="therundown_experimental",
            approved_provider_set=("therundown_experimental",),
        )
        activation = replace(activation, provider_authority=authority)
    else:
        activation = replace(activation, fixture_scope=("BL1|wrong-fixture",))
    with pytest.raises((DurableActivationError, ProductionContractError)):
        prepare_top5_durable_activation_plan(
            receipt_package=package,
            b1_acceptance_bundle=b1_bundle,
            authority=authority,
            activation=activation,
            current_snapshot=snapshot,
            signal_time_approval_identity="activation-auth:one-league",
            now=NOW,
        )


def test_external_state_store_roundtrips_idempotently_without_bearer_token(tmp_path):
    *_context, plan = _inputs()
    path = tmp_path / "runtime-state" / "football" / "top5" / "state.json"
    store = DurableTop5ActivationStore(path)
    first = store.prepare(plan, now=NOW)
    second = DurableTop5ActivationStore(path).prepare(plan, now=NOW)
    assert first == second
    assert first["status"] == "PREPARED"
    serialized = path.read_text(encoding="utf-8")
    assert "offline-test-token-not-persisted" not in serialized
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    status = activation_health_payload(DurableTop5ActivationStore(path))
    assert status["activation_id"] == plan.activation_id
    assert status["activation_league"] == "BL1"
    assert status["provider_health"] == "NOT_OBSERVED"
    assert status["model_health"] == "NOT_OBSERVED"
    assert status["rollback_ready"] is True
    assert status["candidate_provider_authority"] is False
    assert status["publication_enabled"] is False
    assert status["source_sha"] == plan.source_sha.lower()
    assert status["research_sha"] == plan.research_sha.lower()
    assert status["model_identity"] == plan.model_identity
    assert status["signal_time_approval_identity"] == plan.signal_time_approval_identity
    assert status["pre_activation_configuration_digest"] == (
        plan.pre_activation_configuration_digest
    )
    assert status["last_state_transition"] == first["updated_at"]


def test_execute_is_fail_closed_without_runtime_and_does_not_consume_or_activate(
    tmp_path,
):
    *_context, plan = _inputs()
    path = tmp_path / "external-state.json"
    store = DurableTop5ActivationStore(path)
    store.prepare(plan, now=NOW)
    before = path.read_bytes()
    with pytest.raises(DurableActivationError, match=PRODUCTION_RUNTIME_BLOCKER):
        store.execute(plan, now=NOW, explicit_execute=True)
    assert path.read_bytes() == before
    state = store.status(plan.activation_id)
    assert state["record"]["status"] == "PREPARED"
    assert state["active_activation_id"] is None


def test_rollback_requires_exact_plan_and_preserves_auditable_record(tmp_path):
    *_context, plan = _inputs()
    path = tmp_path / "external-state.json"
    store = DurableTop5ActivationStore(path)
    store.prepare(plan, now=NOW)
    with pytest.raises(DurableActivationError, match="exact activation plan digest"):
        store.rollback(plan.activation_id, "0" * 64, now=NOW)
    rolled = store.rollback(
        plan.activation_id, plan.plan_digest, now=NOW + timedelta(seconds=1)
    )
    assert rolled["status"] == "ROLLED_BACK"
    assert rolled["rollback_evidence_digest"]
    assert rolled["plan_digest"] == plan.plan_digest
    assert rolled["publication_enabled"] is False
    assert rolled["scheduler_registered"] is False
    assert rolled["betting_enabled"] is False
    assert rolled["ledger_mutation_enabled"] is False
    again = store.rollback(
        plan.activation_id, plan.plan_digest, now=NOW + timedelta(seconds=2)
    )
    assert again == rolled
    assert store.status(plan.activation_id)["record"]["status"] == "ROLLED_BACK"


def test_state_symlink_and_digest_tampering_fail_closed(tmp_path):
    *_context, plan = _inputs()
    path = tmp_path / "state.json"
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(DurableActivationError, match="symlink"):
        DurableTop5ActivationStore(path).status()

    path.unlink()
    store = DurableTop5ActivationStore(path)
    store.prepare(plan, now=NOW)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["revision"] += 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(DurableActivationError, match="digest"):
        store.status()


def test_activation_store_does_not_touch_unrelated_external_runtime_state(tmp_path):
    *_context, plan = _inputs()
    unrelated = tmp_path / "football" / "tennis" / "runtime-state.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('{"owner":"tennis"}', encoding="utf-8")
    original = unrelated.read_bytes()
    store = DurableTop5ActivationStore(
        tmp_path / "football" / "top5" / "activation-state.json"
    )
    store.prepare(plan, now=NOW)
    store.rollback(plan.activation_id, plan.plan_digest, now=NOW + timedelta(seconds=1))
    assert unrelated.read_bytes() == original


@pytest.mark.parametrize("mutation", ("expired", "not_authorized", "multi_league"))
def test_missing_expired_or_non_single_league_authorization_fails_closed(mutation):
    package, b1_bundle, authority, activation, snapshot, _plan = _inputs()
    if mutation == "expired":
        now = NOW + timedelta(minutes=21)
    elif mutation == "not_authorized":
        activation = replace(activation, activation_authorized=False)
        now = NOW
    else:
        activation = replace(activation, league_code="EPL,BL1")
        now = NOW
    with pytest.raises((DurableActivationError, ProductionContractError)):
        prepare_top5_durable_activation_plan(
            receipt_package=package,
            b1_acceptance_bundle=b1_bundle,
            authority=authority,
            activation=activation,
            current_snapshot=snapshot,
            signal_time_approval_identity="activation-auth:one-league",
            now=now,
        )


def test_conflicting_second_preparation_is_rejected(tmp_path):
    *_context, plan = _inputs()
    store = DurableTop5ActivationStore(tmp_path / "external-state.json")
    store.prepare(plan, now=NOW)
    second = replace(plan, activation_id="activation:BL1:second")
    with pytest.raises(DurableActivationError, match="another Top-5 activation"):
        store.prepare(second, now=NOW)


def test_cli_replay_binds_to_original_prepared_plan_and_rejects_drift(tmp_path):
    *_context, plan = _inputs()
    store = DurableTop5ActivationStore(tmp_path / "external-state.json")
    record = store.prepare(plan, now=NOW)
    regenerated = replace(plan, prepared_at=NOW + timedelta(seconds=5))
    rebound = _match_prepared_plan_timestamp(regenerated, record, now=NOW)
    assert rebound.plan_digest == record["plan_digest"]

    changed = replace(regenerated, source_sha="2" * 40)
    with pytest.raises(DurableActivationError, match="differs from the exact prepared"):
        _match_prepared_plan_timestamp(changed, record, now=NOW)


def test_activation_cli_input_must_be_absolute_user_owned_and_private(tmp_path):
    path = tmp_path / "authorization.json"
    path.write_text('{"safe":"value"}', encoding="utf-8")
    path.chmod(0o600)
    assert read_activation_input(path) == {"safe": "value"}
    with pytest.raises(ValueError, match="absolute"):
        read_activation_input(path.relative_to(path.parent))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="unreadable or malformed"):
        read_activation_input(path)


def test_activation_cli_rejects_symlink_input(tmp_path):
    target = tmp_path / "authorization-target.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    symlink = tmp_path / "authorization-link.json"
    symlink.symlink_to(target)
    with pytest.raises(ValueError, match="unreadable or malformed"):
        read_activation_input(symlink)
