from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
)
from src.football.top5_activation_authorization import (
    ACTIVATION_AUTHORIZATION_SCHEMA,
    ActivationAuthorizationError,
    VerifiedTop5ActivationAuthorizationV1,
    build_signed_activation_execution_binding,
    canonical_authorization_bytes,
    validate_activation_signal_snapshot,
    verify_activation_authorization,
)
from src.football.top5_durable_activation import (
    PRODUCTION_RUNTIME_REQUIRED,
    DurableActivationError,
    DurableTop5ActivationStore,
    prepare_top5_durable_activation_plan,
)
from src.football.top5_production_activation import current_top5_routing_snapshot
from src.football.top5_signal_lifecycle import (
    DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    create_initial_signal,
)
from tests.football.test_top5_durable_activation import NOW, _inputs

SIGNER_ID = "philip-test-key-v1"
AUTHORIZATION_ID = "philip:top5:activation:offline-test-v1"


def _signed_context(tmp_path, *, lifecycle_stage="REFINEMENT", key=None, nonce=None):
    package, bundle, authority, activation, _snapshot, _base_plan = _inputs()
    baseline = current_top5_routing_snapshot(
        snapshot_id="top5-disabled-route-code-baseline"
    )
    plan = prepare_top5_durable_activation_plan(
        receipt_package=package,
        b1_acceptance_bundle=bundle,
        authority=authority,
        activation=activation,
        current_snapshot=baseline,
        signal_time_approval_identity=activation.signal_time_contract.approval_ref,
        now=NOW,
    )
    selected = next(
        item
        for item in package.dossier.bindings
        if item.league == plan.activation_league
    )
    fixture = Fixture(
        fixture_key=selected.fixture_key,
        league_code=selected.league,
        home_team=selected.home_team,
        away_team=selected.away_team,
        kickoff=selected.kickoff,
    )
    initial_time = fixture.kickoff - timedelta(hours=24)
    initial_snapshot = MarketSnapshot(
        fixture.fixture_key,
        initial_time,
        MarketSnapshotKind.SIGNAL_TIME,
        "the_odds_api:top5",
        {"home": 2.1},
        "initial-lifecycle-snapshot",
    )
    lifecycle = create_initial_signal(
        fixture=fixture,
        snapshot=initial_snapshot,
        now=initial_time,
        market_id="h2h",
        outcome_id="home",
        candidate_id=plan.model_identity,
        model_identity=plan.model_identity,
        probabilities={"home": 0.45, "draw": 0.25, "away": 0.30},
        source_sha=plan.source_sha,
        research_sha=plan.research_sha,
        model_artifact_hash=plan.model_artifact_hash,
        eligibility_decision=True,
        decision_id="initial:test-decision",
        decision_reason="offline contract test",
        contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    )
    binding = build_signed_activation_execution_binding(
        plan=plan,
        receipt_package=package,
        b1_acceptance_bundle=bundle,
        fixture=fixture,
        activation_authorization_id=AUTHORIZATION_ID,
        lifecycle_contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
        lifecycle_stage=lifecycle_stage,
        now=NOW,
        lifecycle=lifecycle,
    )
    signer = key or Ed25519PrivateKey.generate()
    public_key = signer.public_key()
    public_path = tmp_path / "trusted-philip-ed25519.pem"
    public_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_path.chmod(0o600)
    raw_public = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    payload = {
        "schema_version": ACTIVATION_AUTHORIZATION_SCHEMA,
        **binding.expected_signed_claims(),
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "authorization_nonce": nonce or "n" * 32,
        "signer_key_id": SIGNER_ID,
        "signer_public_key_fingerprint": hashlib.sha256(raw_public).hexdigest(),
    }
    envelope = {
        "schema_version": ACTIVATION_AUTHORIZATION_SCHEMA,
        "payload": payload,
        "signature": base64.b64encode(
            signer.sign(canonical_authorization_bytes(payload))
        ).decode("ascii"),
    }
    verified = verify_activation_authorization(
        envelope,
        public_key_file=public_path,
        expected_signer_key_id=SIGNER_ID,
        expected_binding=binding,
        now=NOW,
    )
    return (
        package,
        bundle,
        plan,
        fixture,
        lifecycle,
        binding,
        signer,
        public_path,
        envelope,
        verified,
    )


def _resign(envelope, key, **updates):
    payload = dict(envelope["payload"])
    payload.update(updates)
    return {
        "schema_version": envelope["schema_version"],
        "payload": payload,
        "signature": base64.b64encode(
            key.sign(canonical_authorization_bytes(payload))
        ).decode("ascii"),
    }


def test_exact_ed25519_authorization_is_accepted_and_separate_from_signal_time(
    tmp_path,
):
    *_, binding, _signer, public_path, envelope, verified = _signed_context(tmp_path)
    assert verified.payload["authorization_id"] == AUTHORIZATION_ID
    assert binding.activation_authorization_id != binding.signal_time_approval_identity
    assert (
        verified.payload["signal_time_approval_identity"]
        == binding.signal_time_approval_identity
    )
    assert verified.payload["provider_authority"] == "the_odds_api"
    assert verified.payload["retry_budget"] == 0
    assert verified.payload["publication"] is False
    assert verified.payload["betting"] is False
    assert verified.payload["ledger_mutation"] is False
    assert verified.payload["recurring_scheduler"] is False
    assert public_path.stat().st_mode & 0o077 == 0
    assert "private" not in json.dumps(envelope).lower()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("activation_id", "activation:other"),
        ("activation_plan_digest", "0" * 64),
        ("activation_league", "EPL"),
        ("fixture_key", "fixture:other"),
        ("model_identity", "model:other"),
        ("provider_authority", "therundown_experimental"),
        ("lifecycle_contract_id", "lifecycle:other"),
        ("lifecycle_stage", "INITIAL"),
        ("lifecycle_stage_contract_id", "stage:other"),
        ("lifecycle_timing_bounds", {"minimum_minutes_before_kickoff": 1}),
        ("rollback_snapshot_digest", "1" * 64),
        ("model_artifact_hash", "9" * 64),
        ("source_sha", "3" * 40),
        ("research_sha", "4" * 40),
        ("five_league_evidence_digest", "5" * 64),
        ("b1_acceptance_manifest_digest", "6" * 64),
        ("signal_time_approval_identity", "approval:other"),
        ("publication", True),
    ],
)
def test_any_bound_claim_mutation_is_rejected(tmp_path, field, replacement):
    *_, binding, _signer, public_path, envelope, _verified = _signed_context(tmp_path)
    mutated = dict(envelope)
    mutated["payload"] = {**envelope["payload"], field: replacement}
    with pytest.raises(ActivationAuthorizationError):
        verify_activation_authorization(
            mutated,
            public_key_file=public_path,
            expected_signer_key_id=SIGNER_ID,
            expected_binding=binding,
            now=NOW,
        )


def test_wrong_public_key_and_signer_fingerprint_are_rejected(tmp_path):
    *_, binding, signer, public_path, envelope, _verified = _signed_context(tmp_path)
    wrong = Ed25519PrivateKey.generate()
    wrong_path = tmp_path / "wrong-key.pem"
    wrong_path.write_bytes(
        wrong.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    wrong_path.chmod(0o600)
    wrong_public = wrong.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    wrong_signed = _resign(
        envelope,
        signer,
        signer_public_key_fingerprint=hashlib.sha256(wrong_public).hexdigest(),
    )
    with pytest.raises(ActivationAuthorizationError, match="signature verification"):
        verify_activation_authorization(
            wrong_signed,
            public_key_file=wrong_path,
            expected_signer_key_id=SIGNER_ID,
            expected_binding=binding,
            now=NOW,
        )
    bad_fingerprint = _resign(envelope, signer, signer_public_key_fingerprint="0" * 64)
    with pytest.raises(ActivationAuthorizationError, match="fingerprint"):
        verify_activation_authorization(
            bad_fingerprint,
            public_key_file=public_path,
            expected_signer_key_id=SIGNER_ID,
            expected_binding=binding,
            now=NOW,
        )


def test_public_key_must_be_external_owner_only_regular_and_not_a_symlink(tmp_path):
    *_, binding, _signer, public_path, envelope, _verified = _signed_context(tmp_path)
    public_path.chmod(0o644)
    with pytest.raises(ActivationAuthorizationError, match="owner-only"):
        verify_activation_authorization(
            envelope,
            public_key_file=public_path,
            expected_signer_key_id=SIGNER_ID,
            expected_binding=binding,
            now=NOW,
        )
    public_path.chmod(0o600)
    alias = tmp_path / "public-key-alias.pem"
    alias.symlink_to(public_path)
    with pytest.raises(ActivationAuthorizationError, match="not be a symlink"):
        verify_activation_authorization(
            envelope,
            public_key_file=alias,
            expected_signer_key_id=SIGNER_ID,
            expected_binding=binding,
            now=NOW,
        )


@pytest.mark.parametrize(
    "time_updates",
    [
        {
            "issued_at": (NOW - timedelta(minutes=20)).isoformat(),
            "expires_at": (NOW - timedelta(minutes=1)).isoformat(),
        },
        {
            "issued_at": (NOW + timedelta(minutes=1)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        },
    ],
)
def test_expired_and_not_yet_valid_authorizations_fail(tmp_path, time_updates):
    *_, binding, signer, public_path, envelope, _verified = _signed_context(tmp_path)
    invalid = _resign(envelope, signer, **time_updates)
    with pytest.raises(ActivationAuthorizationError, match="expired or not yet valid"):
        verify_activation_authorization(
            invalid,
            public_key_file=public_path,
            expected_signer_key_id=SIGNER_ID,
            expected_binding=binding,
            now=NOW,
        )


def test_lifecycle_stage_must_be_explicit_and_due(tmp_path):
    package, bundle, _authority, _activation, _snapshot, plan = _inputs()
    selected = next(
        item
        for item in package.dossier.bindings
        if item.league == plan.activation_league
    )
    fixture = Fixture(
        selected.fixture_key,
        selected.league,
        selected.home_team,
        selected.away_team,
        selected.kickoff,
    )
    with pytest.raises(ActivationAuthorizationError, match="not due"):
        build_signed_activation_execution_binding(
            plan=plan,
            receipt_package=package,
            b1_acceptance_bundle=bundle,
            fixture=fixture,
            activation_authorization_id=AUTHORIZATION_ID,
            lifecycle_contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
            lifecycle_stage="INITIAL",
            now=NOW,
        )
    with pytest.raises(ActivationAuthorizationError, match="explicit"):
        build_signed_activation_execution_binding(
            plan=plan,
            receipt_package=package,
            b1_acceptance_bundle=bundle,
            fixture=fixture,
            activation_authorization_id=AUTHORIZATION_ID,
            lifecycle_contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
            lifecycle_stage="",
            now=NOW,
        )


def test_signal_snapshot_requires_fresh_signal_time_and_the_odds_api(tmp_path):
    package, _bundle, _authority, _activation, _snapshot, plan = _inputs()
    selected = next(
        item
        for item in package.dossier.bindings
        if item.league == plan.activation_league
    )
    fixture = Fixture(
        selected.fixture_key,
        selected.league,
        selected.home_team,
        selected.away_team,
        selected.kickoff,
    )
    good = MarketSnapshot(
        fixture.fixture_key,
        NOW - timedelta(seconds=60),
        MarketSnapshotKind.SIGNAL_TIME,
        "the_odds_api:top5",
        {"home": 2.1},
        "signal-snapshot",
    )
    validate_activation_signal_snapshot(
        fixture=fixture,
        snapshot=good,
        lifecycle_contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
        lifecycle_stage="REFINEMENT",
        now=NOW,
    )
    for snapshot in (
        replace(good, captured_at=NOW + timedelta(seconds=1)),
        replace(good, captured_at=NOW - timedelta(seconds=901)),
        replace(good, kind=MarketSnapshotKind.CLOSING),
        replace(good, source="therundown_experimental"),
        replace(good, fixture_key="fixture:wrong"),
    ):
        with pytest.raises(ActivationAuthorizationError):
            validate_activation_signal_snapshot(
                fixture=fixture,
                snapshot=snapshot,
                lifecycle_contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
                lifecycle_stage="REFINEMENT",
                now=NOW,
            )


def test_durable_execution_requires_genuine_runtime_and_route_consumer(tmp_path):
    (
        _package,
        _bundle,
        plan,
        _fixture,
        _lifecycle,
        binding,
        _signer,
        _public_path,
        _envelope,
        verified,
    ) = _signed_context(tmp_path)
    state_path = tmp_path / "activation-state.json"
    store = DurableTop5ActivationStore(state_path)
    store.prepare(plan, now=NOW)
    before = state_path.read_bytes()
    with pytest.raises(DurableActivationError, match=PRODUCTION_RUNTIME_REQUIRED):
        store.execute(
            plan,
            now=NOW,
            explicit_execute=True,
            execution_binding=binding,
            verified_authorization=verified,
        )
    assert state_path.read_bytes() == before
    assert store.status(plan.activation_id)["record"]["status"] == "PREPARED"


def test_forged_verified_marker_cannot_reach_execution_runtime(tmp_path):
    (
        _package,
        _bundle,
        plan,
        _fixture,
        _lifecycle,
        binding,
        _signer,
        _public_path,
        _envelope,
        verified,
    ) = _signed_context(tmp_path)
    forged = VerifiedTop5ActivationAuthorizationV1(
        payload=binding.expected_signed_claims(),
        authorization_digest=verified.authorization_digest,
        signer_key_id=verified.signer_key_id,
        signer_fingerprint=verified.signer_fingerprint,
        nonce=verified.nonce,
        _verification_seal=None,
    )
    state_path = tmp_path / "forged-state.json"
    store = DurableTop5ActivationStore(state_path)
    store.prepare(plan, now=NOW)
    before = state_path.read_bytes()
    with pytest.raises(DurableActivationError, match="not produced by signature"):
        store.execute(
            plan,
            now=NOW,
            explicit_execute=True,
            execution_binding=binding,
            verified_authorization=forged,
        )
    assert state_path.read_bytes() == before
