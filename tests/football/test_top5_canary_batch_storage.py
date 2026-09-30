"""Offline contract tests for durable Top-5 batch storage and rollback."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
)
from src.football.top5_canary_batch_storage import (
    Top5CanaryBatchState,
    Top5CanaryBatchStorageError,
    Top5CanaryBatchStore,
    canonical_top5_canary_batch,
    execute_stored_top5_batch,
    rollback_committed_top5_batch,
)
from src.football.top5_one_shot_runtime import TheOddsApiOneShotHttpTransport
from src.football.top5_public_delivery import (
    InMemoryDeliveryCapabilityConsumer,
    InMemoryStaticDeliveryTransport,
    InMemoryWorkerDeliveryTransport,
    Top5CanonicalDeliveryAdapter,
    Top5DeliveryExecutor,
)
from src.football.top5_publisher import (
    ControlledTop5PublicationPayload,
    InMemoryTop5PublicationStore,
    Top5PublicationAuthorization,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_signal_lifecycle import (
    RefinementClassification,
    create_initial_signal,
    refine_signal,
)
from src.notifications.public_serializer import serialize_public_product
from tests.football.test_top5_public_delivery import BASE, LEAGUES, _controlled_payload
from tests.football.test_top5_public_delivery_executor import _authorization

SOURCE_SHA = "a" * 64
RESEARCH_SHA = "a" * 64
MODEL_HASH = "a" * 64
ODDS = {"home": 2.2, "draw": 3.5, "away": 4.2}
INITIAL_PROBABILITIES = {"home": 0.50, "draw": 0.28, "away": 0.22}
REFINED_PROBABILITIES = {"home": 0.53, "draw": 0.26, "away": 0.21}


def _make_lifecycles(league: str, *, refined: bool):
    kickoff = BASE + timedelta(hours=24)
    home, away = f"{league} Home FC", f"{league} Away FC"
    fixture_key = make_fixture_key(league, home, away, kickoff)
    fixture = Fixture(fixture_key, league, home, away, kickoff)
    initial_at = kickoff - timedelta(hours=24)
    initial_capture = initial_at - timedelta(minutes=2)
    initial_snapshot = MarketSnapshot(
        fixture_key=fixture_key,
        captured_at=initial_capture,
        kind=MarketSnapshotKind.SIGNAL_TIME,
        source="the_odds_api:prematch",
        odds=ODDS,
        snapshot_id=f"snapshot:{league}:initial",
    )
    implied = {key: (1 / value) for key, value in ODDS.items()}
    total = sum(implied.values())
    implied = {key: value / total for key, value in implied.items()}
    initial = tuple(
        create_initial_signal(
            fixture=fixture,
            snapshot=initial_snapshot,
            now=initial_at,
            market_id="h2h",
            outcome_id=outcome,
            candidate_id="candidate:top5-delivery-test",
            model_identity="model:top5-delivery-test",
            provider_identity="the_odds_api",
            probabilities=INITIAL_PROBABILITIES,
            source_sha=SOURCE_SHA,
            research_sha=RESEARCH_SHA,
            model_artifact_hash=MODEL_HASH,
            eligibility_decision=True,
            decision_id=f"activation-auth:{league}:initial",
            decision_reason="authorized offline test decision",
            implied_probabilities=implied,
        )
        for outcome in ("home", "draw", "away")
    )
    if not refined:
        return (
            initial,
            kickoff,
            home,
            away,
            initial_at,
            initial_capture,
            INITIAL_PROBABILITIES,
        )

    refinement_at = kickoff - timedelta(minutes=90)
    refinement_capture = refinement_at - timedelta(minutes=2)
    refinement_snapshot = MarketSnapshot(
        fixture_key=fixture_key,
        captured_at=refinement_capture,
        kind=MarketSnapshotKind.SIGNAL_TIME,
        source="the_odds_api:prematch",
        odds=ODDS,
        snapshot_id=f"snapshot:{league}:refinement",
    )
    refined = tuple(
        refine_signal(
            lifecycle,
            fixture=fixture,
            snapshot=refinement_snapshot,
            now=refinement_at,
            probabilities=REFINED_PROBABILITIES,
            eligibility_decision=True,
            withdrawal_authorized=False,
            decision_id=f"activation-auth:{league}:refinement",
            decision_reason="authorized offline refinement decision",
            classification=RefinementClassification.STRENGTHENED,
            implied_probabilities=implied,
        )
        for lifecycle in initial
    )
    return (
        refined,
        kickoff,
        home,
        away,
        refinement_at,
        refinement_capture,
        REFINED_PROBABILITIES,
    )


def _artifact(*, refined: bool = False):
    generated_at = BASE + timedelta(hours=22, minutes=30) if refined else BASE
    payloads: list[ControlledTop5PublicationPayload] = []
    for league in LEAGUES:
        lifecycles, kickoff, home, away, prediction_at, captured_at, probabilities = (
            _make_lifecycles(league, refined=refined)
        )
        base = _controlled_payload(league)
        fixture_key = make_fixture_key(league, home, away, kickoff)
        source = {
            "fixture": {
                "fixture_key": fixture_key,
                "home_team": home,
                "away_team": away,
                "kickoff": kickoff.isoformat(),
            },
            "fixture_key": fixture_key,
            "league": league,
            "model_identity": base.model_identity,
            "source_sha": base.source_sha,
            "research_sha": base.research_sha,
            "signal_time_experiment_id": base.signal_time_experiment_id,
            "activation_id": base.activation_id,
            "evidence_digest": base.evidence_digest,
            "controlled_shadow_run_id": base.controlled_shadow_run_id,
            "qualification_session_id": base.qualification_session_id,
            "prediction_id": f"prediction:{league}:top5-canary-test",
            "prediction_timestamp": prediction_at.isoformat(),
            "signal_timestamp": captured_at.isoformat(),
            "snapshot_id": f"snapshot:{league}:{'refinement' if refined else 'initial'}",
            "snapshot_kind": "SIGNAL_TIME",
            "probabilities": probabilities,
            "odds": ODDS,
            "top5_signal_lifecycles": lifecycles,
            "no_bet": True,
            "closing_used_for_prediction": False,
        }
        payloads.append(
            replace(
                base,
                generated_at=generated_at,
                football_records=(source,),
            )
        )

    authorizations = {
        payload.league_code: Top5PublicationAuthorization(
            publication_authorization_id=f"publication-auth:{payload.league_code}",
            activation_id=payload.activation_id,
            league_code=payload.league_code,
            candidate_id=payload.candidate_id,
            model_identity=payload.model_identity,
            source_sha=payload.source_sha,
            research_sha=payload.research_sha,
            model_artifact_hash=payload.model_artifact_hash,
            signal_time_experiment_id=payload.signal_time_experiment_id,
            publication_token=f"offline-token:{payload.league_code}",
            issued_at=generated_at - timedelta(minutes=1),
            expires_at=generated_at + timedelta(hours=1),
        )
        for payload in payloads
    }
    binding_names = (
        "activation_id",
        "league_code",
        "candidate_id",
        "model_identity",
        "source_sha",
        "research_sha",
        "model_artifact_hash",
        "signal_time_experiment_id",
        "provider_authority",
        "result_authority",
        "evidence_digest",
        "controlled_shadow_run_id",
        "qualification_session_id",
    )
    bindings = {
        payload.league_code: {
            "active": True,
            **{
                name: payload.activation_id
                if name == "activation_id"
                else getattr(payload, name)
                for name in binding_names
            },
        }
        for payload in payloads
    }
    return InMemoryTop5PublicationStore().publish_batch(
        tuple(payloads),
        authorizations,
        activation_bindings=bindings,
        now=generated_at,
    ), generated_at


def _delivery(artifact, now):
    current = {"updated": BASE.isoformat(), "football": []}
    plan = Top5CanonicalDeliveryAdapter().build_plan(current, artifact)
    attestation, capability = _authorization(plan, artifact, now=now)
    old_payload = json.dumps(
        serialize_public_product(current), sort_keys=True, separators=(",", ":")
    ).encode()
    static = InMemoryStaticDeliveryTransport(initial_payload=old_payload)
    worker = InMemoryWorkerDeliveryTransport(initial_payload=old_payload)
    executor = Top5DeliveryExecutor(
        static_transport=static,
        worker_transport=worker,
        capability_consumer=InMemoryDeliveryCapabilityConsumer(),
        clock=lambda: now,
    )
    return current, plan, attestation, capability, static, worker, executor


def test_canonical_batch_is_exactly_five_private_initial_fixture_records():
    artifact, _ = _artifact()
    batch = canonical_top5_canary_batch(artifact)
    assert batch["schema_version"] == "top5-canary-signal-batch-v1"
    assert batch["fixture_count"] == 5
    assert {row["league"] for row in batch["fixture_records"]} == set(LEAGUES)
    assert {row["lifecycle_stage"] for row in batch["fixture_records"]} == {"INITIAL"}
    assert all(row["refinement_timestamp"] is None for row in batch["fixture_records"])
    assert all(row["signal_state"] == "SIGNAL" for row in batch["fixture_records"])
    assert all(row["no_bet"] is True for row in batch["fixture_records"])
    assert all(
        "stake" not in json.dumps(row).lower() for row in batch["fixture_records"]
    )
    assert "bankroll" not in json.dumps(batch).lower()


def test_canonical_batch_accepts_valid_refinement_and_binds_both_timestamps():
    artifact, _ = _artifact(refined=True)
    batch = canonical_top5_canary_batch(artifact)
    assert {row["lifecycle_stage"] for row in batch["fixture_records"]} == {
        "REFINEMENT"
    }
    assert all(row["refinement_timestamp"] for row in batch["fixture_records"])
    assert all(
        row["initial_timestamp"] < row["refinement_timestamp"]
        for row in batch["fixture_records"]
    )


def test_stale_odds_and_malformed_probabilities_fail_before_any_public_write(tmp_path):
    artifact, now = _artifact()
    payload = artifact.payloads[0]
    record = dict(payload.football_records[0])
    record["signal_timestamp"] = (now - timedelta(seconds=901)).isoformat()
    bad_payload = replace(payload, football_records=(record,))
    bad_artifact = replace(artifact, payloads=(bad_payload, *artifact.payloads[1:]))
    with pytest.raises(Top5CanaryBatchStorageError):
        canonical_top5_canary_batch(bad_artifact)

    record["signal_timestamp"] = payload.football_records[0]["signal_timestamp"]
    record["probabilities"] = {"home": 0.8, "draw": 0.3, "away": -0.1}
    bad_probability_artifact = replace(
        artifact,
        payloads=(replace(payload, football_records=(record,)), *artifact.payloads[1:]),
    )
    current, plan, attestation, capability, static, worker, executor = _delivery(
        artifact, now
    )
    with pytest.raises(Top5CanaryBatchStorageError):
        execute_stored_top5_batch(
            store=Top5CanaryBatchStore(tmp_path),
            executor=executor,
            artifact=bad_probability_artifact,
            current_public_snapshot=current,
            plan=plan,
            attestation=attestation,
            capability=capability,
            now=now,
        )
    assert static.commit_calls == static.stage_calls == 0
    assert worker.write_calls == 0


def test_incomplete_five_fixture_batch_and_missing_publication_authorization_fail():
    artifact, _ = _artifact()
    with pytest.raises((Top5CanaryBatchStorageError, ValueError)):
        replace(artifact, payloads=artifact.payloads[:-1]).validate()

    public = dict(artifact.public_product)
    release = dict(public["top5_release"])
    release.pop("publication_authorization_id")
    public["top5_release"] = release
    unsigned = json.dumps(
        public, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    missing_auth = replace(
        artifact,
        public_product=public,
        artifact_digest=hashlib.sha256(unsigned.encode()).hexdigest(),
    )
    with pytest.raises(
        Top5CanaryBatchStorageError, match="publication_authorization_id"
    ):
        canonical_top5_canary_batch(missing_auth)


def test_store_hides_preparing_state_and_exposes_only_committed_public_schema(tmp_path):
    artifact, now = _artifact()
    current, plan, _attestation, _capability, _static, _worker, _executor = _delivery(
        artifact, now
    )
    store = Top5CanaryBatchStore(tmp_path)
    batch = canonical_top5_canary_batch(artifact)
    previous = json.dumps(
        serialize_public_product(current), sort_keys=True, separators=(",", ":")
    ).encode()
    record = store.prepare(
        canonical_batch=batch,
        artifact_digest=artifact.artifact_digest,
        public_payload=plan.serialized_payload,
        previous_payload=previous,
        now=now,
    )
    assert record["state"] == Top5CanaryBatchState.PREPARING.value
    with pytest.raises(Top5CanaryBatchStorageError, match="uncommitted"):
        store.committed_public_product(batch["batch_id"])

    digest = hashlib.sha256(plan.serialized_payload).hexdigest()
    store.transition(
        batch["batch_id"],
        Top5CanaryBatchState.COMMITTED,
        event="public_adapter_updated",
        public_digest=digest,
        now=now,
    )
    projection = store.committed_public_product(batch["batch_id"])
    assert projection["top5_release"]["batch_state"] == "COMMITTED"
    assert len(projection["football"]) == 15


def test_idempotent_batch_execution_records_one_public_generation_and_zero_providers(
    tmp_path, monkeypatch
):
    def provider_tripwire(*_args, **_kwargs):
        pytest.fail(
            "provider transport must not be used by persistence/public delivery"
        )

    monkeypatch.setattr(TheOddsApiOneShotHttpTransport, "request", provider_tripwire)
    artifact, now = _artifact()
    current, plan, attestation, capability, _static, worker, executor = _delivery(
        artifact, now
    )
    store = Top5CanaryBatchStore(tmp_path)
    first = execute_stored_top5_batch(
        store=store,
        executor=executor,
        artifact=artifact,
        current_public_snapshot=current,
        plan=plan,
        attestation=attestation,
        capability=capability,
        now=now,
    )
    second = execute_stored_top5_batch(
        store=store,
        executor=executor,
        artifact=artifact,
        current_public_snapshot=current,
        plan=plan,
        attestation=attestation,
        capability=capability,
        now=now,
    )
    assert first.status == "ACCEPTANCE_REQUIRED"
    assert second.status == "TOP5_DELIVERY_IDEMPOTENT"
    assert worker.write_calls == 1
    assert (
        store.load(plan.generation_id)["state"] == Top5CanaryBatchState.COMMITTED.value
    )
    assert len(store.load(plan.generation_id)["events"]) >= 3


def test_partial_worker_failure_is_rolled_back_and_never_committed(tmp_path):
    artifact, now = _artifact()
    current, plan, attestation, capability, _static, worker, executor = _delivery(
        artifact, now
    )
    worker.fail_write = True
    result = execute_stored_top5_batch(
        store=Top5CanaryBatchStore(tmp_path),
        executor=executor,
        artifact=artifact,
        current_public_snapshot=current,
        plan=plan,
        attestation=attestation,
        capability=capability,
        now=now,
    )
    assert result.status == "TOP5_DELIVERY_ROLLBACK_SUCCEEDED"
    assert (
        executor.static_transport.current_payload()
        == executor.worker_transport.current_payload()
    )
    assert (
        Top5CanaryBatchStore(tmp_path).load(plan.generation_id)["state"]
        == "ROLLED_BACK"
    )


def test_operator_rollback_restores_verified_previous_generation_and_audits(tmp_path):
    artifact, now = _artifact()
    current, plan, attestation, capability, static, worker, executor = _delivery(
        artifact, now
    )
    store = Top5CanaryBatchStore(tmp_path)
    execute_stored_top5_batch(
        store=store,
        executor=executor,
        artifact=artifact,
        current_public_snapshot=current,
        plan=plan,
        attestation=attestation,
        capability=capability,
        now=now,
    )
    result = rollback_committed_top5_batch(
        store=store,
        batch_id=plan.generation_id,
        expected_generation_id=plan.generation_id,
        expected_current_digest=plan.public_product_digest,
        static_transport=static,
        worker_transport=worker,
        now=now + timedelta(seconds=1),
    )
    assert result["status"] == "ROLLED_BACK"
    assert store.load(plan.generation_id)["state"] == "ROLLED_BACK"
    assert static.current_payload() == worker.current_payload()
    assert json.loads(worker.current_payload())["football"] == []
    assert (
        store.load(plan.generation_id)["events"][-1]["event"]
        == "operator_rollback_verified"
    )


def test_rollback_verification_failure_fails_closed_and_retains_audit(tmp_path):
    artifact, now = _artifact()
    current, plan, attestation, capability, static, worker, executor = _delivery(
        artifact, now
    )
    store = Top5CanaryBatchStore(tmp_path)
    execute_stored_top5_batch(
        store=store,
        executor=executor,
        artifact=artifact,
        current_public_snapshot=current,
        plan=plan,
        attestation=attestation,
        capability=capability,
        now=now,
    )
    worker.fail_restore = True
    with pytest.raises(
        Top5CanaryBatchStorageError, match="rollback failed verification"
    ):
        rollback_committed_top5_batch(
            store=store,
            batch_id=plan.generation_id,
            expected_generation_id=plan.generation_id,
            expected_current_digest=plan.public_product_digest,
            static_transport=static,
            worker_transport=worker,
            now=now + timedelta(seconds=2),
        )
    record = store.load(plan.generation_id)
    assert record["state"] == "FAILED"
    assert record["canonical_batch_digest"]
    assert record["events"][-1]["event"] == "operator_rollback_unverified"


def test_tampered_storage_state_is_rejected(tmp_path):
    artifact, now = _artifact()
    current, plan, _attestation, _capability, *_ = _delivery(artifact, now)
    store = Top5CanaryBatchStore(tmp_path)
    batch = canonical_top5_canary_batch(artifact)
    previous = json.dumps(
        serialize_public_product(current), sort_keys=True, separators=(",", ":")
    ).encode()
    store.prepare(
        canonical_batch=batch,
        artifact_digest=artifact.artifact_digest,
        public_payload=plan.serialized_payload,
        previous_payload=previous,
        now=now,
    )
    path = store._batch_path(batch["batch_id"])
    corrupted = json.loads(path.read_text())
    corrupted["state"] = "COMMITTED"
    path.write_text(json.dumps(corrupted))
    with pytest.raises(Top5CanaryBatchStorageError, match="digest mismatch"):
        store.load(batch["batch_id"])
