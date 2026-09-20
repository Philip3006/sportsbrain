"""Offline acceptance for the guarded Top-5 delivery executor and CLI."""

from __future__ import annotations

import hashlib
import json
from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO

import pytest

from scripts.top5_public_delivery import main as delivery_cli
from scripts.top5_publication_delivery_acceptance import validate_delivery
from src.football.top5_public_delivery import (
    TOP5_DELIVERY_ACCEPTANCE_REQUIRED,
    TOP5_DELIVERY_DRY_RUN,
    TOP5_DELIVERY_IDEMPOTENT,
    TOP5_DELIVERY_ROLLBACK_REQUIRED,
    TOP5_DELIVERY_ROLLBACK_SUCCEEDED,
    InMemoryDeliveryCapabilityConsumer,
    InMemoryStaticDeliveryTransport,
    InMemoryWorkerDeliveryTransport,
    Top5CanonicalDeliveryAdapter,
    Top5DeliveryAttestation,
    Top5DeliveryError,
    Top5DeliveryExecutor,
    artifact_from_mapping,
    artifact_to_mapping,
    plan_from_mapping,
    plan_to_mapping,
)
from src.notifications.public_serializer import serialize_public_product
from tests.football.test_top5_public_delivery import (
    BASE,
    _published_batch_artifact,
)


def _authorization(plan, artifact, *, now=BASE, **overrides):
    nonce = "delivery-capability-nonce-" + "x" * 32
    unsigned = {
        "schema_version": "top5-delivery-attestation-v1",
        "artifact_digest": artifact.artifact_digest,
        "generation_id": plan.generation_id,
        "activation_id": plan.activation_id,
        "provider_authority": plan.provider_authority,
        "controlled_shadow_run_id": plan.controlled_shadow_run_id,
        "qualification_session_id": plan.qualification_session_id,
        "publication_authorization_id": plan.publication_authorization_id,
        "league_codes": ["EPL", "BL1", "LL", "SA", "L1"],
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "publication_authorized": True,
        "no_bet": True,
        "capability_id": "top5-capability:delivery-test",
        "capability_nonce_digest": hashlib.sha256(nonce.encode()).hexdigest(),
    }
    unsigned.update(overrides)
    unsigned["attestation_digest"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    attestation = Top5DeliveryAttestation.from_mapping(unsigned)
    capability = {
        "capability_id": "top5-capability:delivery-test",
        "capability_nonce": nonce,
    }
    return attestation, capability


def _context():
    artifact = _published_batch_artifact()
    current = {"football": []}
    plan = Top5CanonicalDeliveryAdapter().build_plan(current, artifact)
    attestation, capability = _authorization(plan, artifact)
    return artifact, current, plan, attestation, capability


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(child) for key, child in value.items()}
    if hasattr(value, "items"):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    return value


def _transports(current):
    payload = json.dumps(
        serialize_public_product(_plain(current)),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return (
        InMemoryStaticDeliveryTransport(initial_payload=payload),
        InMemoryWorkerDeliveryTransport(initial_payload=payload),
    )


def _execute(static, worker, *, current, artifact, plan, attestation, capability, now=BASE, dry_run=False):
    return Top5DeliveryExecutor(
        static_transport=static,
        worker_transport=worker,
        capability_consumer=InMemoryDeliveryCapabilityConsumer(),
        clock=lambda: now,
    ).execute(
        artifact=artifact,
        current_public_snapshot=current,
        plan=plan,
        attestation=attestation,
        capability=capability,
        dry_run=dry_run,
    )


def test_successful_transaction_uses_one_exact_payload_and_stops_for_acceptance():
    artifact, current, plan, attestation, capability = _context()
    static, worker = _transports(current)
    result = _execute(
        static,
        worker,
        current=current,
        artifact=artifact,
        plan=plan,
        attestation=attestation,
        capability=capability,
    )
    assert result.status == TOP5_DELIVERY_ACCEPTANCE_REQUIRED
    assert result.states == (
        "PREPARED",
        "STATIC_STAGED",
        "WORKER_WRITTEN",
        "STATIC_COMMITTED",
        "ACCEPTANCE_REQUIRED",
    )
    assert static.payload == plan.static_payload
    assert worker.payload == plan.worker_payload
    assert worker.written_payloads == [plan.worker_payload]
    assert plan.static_payload == plan.worker_payload


def test_default_dry_run_performs_zero_transport_mutation():
    artifact, current, plan, attestation, capability = _context()
    static, worker = _transports(current)
    result = _execute(
        static,
        worker,
        current=current,
        artifact=artifact,
        plan=plan,
        attestation=attestation,
        capability=capability,
        dry_run=True,
    )
    assert result.status == TOP5_DELIVERY_DRY_RUN
    assert static.stage_calls == static.commit_calls == static.rollback_calls == 0
    assert worker.write_calls == worker.restore_calls == 0


def test_real_execution_requires_capability_consumer_and_current_target_digests():
    artifact, current, plan, attestation, capability = _context()
    static, worker = _transports(current)
    with pytest.raises(Top5DeliveryError, match="capability validation"):
        Top5DeliveryExecutor(
            static_transport=static,
            worker_transport=worker,
            clock=lambda: BASE,
        ).execute(
            artifact=artifact,
            current_public_snapshot=current,
            plan=plan,
            attestation=attestation,
            capability=capability,
            dry_run=False,
        )
    assert static.stage_calls == 0
    assert worker.write_calls == 0

    static, worker = _transports(current)
    static.payload = b"{}"
    with pytest.raises(Top5DeliveryError, match="digest does not match"):
        _execute(
            static,
            worker,
            current=current,
            artifact=artifact,
            plan=plan,
            attestation=attestation,
            capability=capability,
        )
    assert static.stage_calls == 0
    assert worker.write_calls == 0


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"publication_authorized": False}, "publication authorization"),
        ({"no_bet": False}, "cannot enable betting"),
    ],
)
def test_missing_or_unsafe_authorization_fails_before_transport(changes, message):
    artifact, current, plan, _attestation, capability = _context()
    attestation, capability = _authorization(plan, artifact, **changes)
    static, worker = _transports(current)
    with pytest.raises(Top5DeliveryError, match=message):
        _execute(
            static,
            worker,
            current=current,
            artifact=artifact,
            plan=plan,
            attestation=attestation,
            capability=capability,
        )
    assert static.stage_calls == 0
    assert worker.write_calls == 0


def test_expired_capability_attestation_and_wrong_capability_fail_closed():
    artifact, current, plan, _attestation, capability = _context()
    attestation, capability = _authorization(
        plan,
        artifact,
        now=BASE - timedelta(hours=2),
    )
    static, worker = _transports(current)
    with pytest.raises(Top5DeliveryError, match="expired"):
        _execute(
            static,
            worker,
            current=current,
            artifact=artifact,
            plan=plan,
            attestation=attestation,
            capability=capability,
            now=BASE,
    )
    attestation, capability = _authorization(plan, artifact)
    capability["capability_nonce"] = "wrong-" + "y" * 32
    static, worker = _transports(current)
    with pytest.raises(Top5DeliveryError, match="binding mismatch"):
        _execute(
            static,
            worker,
            current=current,
            artifact=artifact,
            plan=plan,
            attestation=attestation,
            capability=capability,
        )
    assert static.stage_calls == 0
    assert worker.write_calls == 0


def test_wrong_plan_digest_and_synthetic_artifact_fail_before_mutation():
    _artifact, current, plan, attestation, capability = _context()
    forged = dict(plan_to_mapping(plan))
    forged_manifest = dict(forged["manifest"])
    forged_manifest["public_product_digest"] = "f" * 64
    forged["manifest"] = forged_manifest
    with pytest.raises(Top5DeliveryError, match="digest"):
        plan_from_mapping(forged)

    synthetic_product = json.loads(plan.serialized_payload)
    synthetic_product["football"][0]["evidence_kind"] = "TEST_FIXTURE"
    synthetic_artifact = _published_batch_artifact()
    synthetic_artifact = synthetic_artifact.__class__(
        payloads=synthetic_artifact.payloads,
        public_product=synthetic_product,
        artifact_digest=hashlib.sha256(
            json.dumps(synthetic_product, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        published_at=synthetic_artifact.published_at,
    )
    with pytest.raises(Top5DeliveryError, match="artifact"):
        Top5CanonicalDeliveryAdapter().build_plan(current, synthetic_artifact)
    assert attestation and capability


def test_worker_failure_rolls_back_static_stage_and_static_failure_never_writes_worker():
    artifact, current, plan, attestation, capability = _context()
    static, worker = _transports(current)
    worker.fail_write = True
    result = _execute(
        static,
        worker,
        current=current,
        artifact=artifact,
        plan=plan,
        attestation=attestation,
        capability=capability,
    )
    assert result.status == TOP5_DELIVERY_ROLLBACK_SUCCEEDED
    assert static.staged is None
    expected = static.payload
    assert static.payload == expected
    assert worker.payload == expected

    static, worker = _transports(current)
    static.fail_stage = True
    result = _execute(
        static,
        worker,
        current=current,
        artifact=artifact,
        plan=plan,
        attestation=attestation,
        capability=capability,
    )
    assert result.status == TOP5_DELIVERY_ROLLBACK_SUCCEEDED
    assert worker.write_calls == 0


def test_static_commit_failure_attempts_worker_restore_and_can_report_hard_rollback():
    artifact, current, plan, attestation, capability = _context()
    static, worker = _transports(current)
    static.fail_commit = True
    expected = worker.payload
    result = _execute(
        static,
        worker,
        current=current,
        artifact=artifact,
        plan=plan,
        attestation=attestation,
        capability=capability,
    )
    assert result.status == TOP5_DELIVERY_ROLLBACK_REQUIRED
    assert result.worker_restored is True
    assert worker.payload == expected

    static, worker = _transports(current)
    static.fail_commit = True
    worker.fail_restore = True
    result = _execute(
        static,
        worker,
        current=current,
        artifact=artifact,
        plan=plan,
        attestation=attestation,
        capability=capability,
    )
    assert result.status == TOP5_DELIVERY_ROLLBACK_REQUIRED
    assert result.worker_restored is False


def test_idempotent_duplicate_and_conflicting_or_newer_current_generation_are_blocked():
    artifact, _current, plan, attestation, capability = _context()
    static, worker = _transports(plan.public_product)
    idempotent = _execute(
        static,
        worker,
        current=plan.public_product,
        artifact=artifact,
        plan=plan,
        attestation=attestation,
        capability=capability,
    )
    assert idempotent.status == TOP5_DELIVERY_IDEMPOTENT
    assert static.stage_calls == worker.write_calls == 0

    conflict = json.loads(plan.serialized_payload)
    conflict["top5_release"]["generation_id"] = "top5-generation-v1:conflict"
    with pytest.raises(Top5DeliveryError, match="same activation"):
        _execute(
            _transports(conflict)[0],
            _transports(conflict)[1],
            current=conflict,
            artifact=artifact,
            plan=plan,
            attestation=attestation,
            capability=capability,
        )

    newer = json.loads(plan.serialized_payload)
    newer["top5_release"]["generation_id"] = "top5-generation-v1:newer"
    newer["top5_release"]["published_at"] = (BASE + timedelta(hours=2)).isoformat()
    with pytest.raises(Top5DeliveryError, match="newer"):
        _execute(
            _transports(newer)[0],
            _transports(newer)[1],
            current=newer,
            artifact=artifact,
            plan=plan,
            attestation=attestation,
            capability=capability,
        )


def test_plan_artifact_round_trip_acceptance_manifest_and_cli_dry_run(tmp_path):
    artifact, current, plan, attestation, capability = _context()
    artifact_path = tmp_path / "artifact.json"
    current_path = tmp_path / "current.json"
    plan_path = tmp_path / "plan.json"
    attestation_path = tmp_path / "attestation.json"
    capability_path = tmp_path / "capability.json"
    artifact_path.write_text(json.dumps(artifact_to_mapping(artifact), sort_keys=True))
    current_path.write_text(json.dumps(current, sort_keys=True))
    attestation_path.write_text(json.dumps(attestation.as_payload(), sort_keys=True))
    capability_path.write_text(json.dumps(capability, sort_keys=True))
    assert artifact_from_mapping(json.loads(artifact_path.read_text())).artifact_digest == artifact.artifact_digest
    assert plan_from_mapping(plan_to_mapping(plan)).public_product_digest == plan.public_product_digest

    output = StringIO()
    with redirect_stdout(output):
        assert delivery_cli(
            [
                "prepare",
                "--artifact",
                str(artifact_path),
                "--current-snapshot",
                str(current_path),
                "--plan-output",
                str(plan_path),
            ]
        ) == 0
    assert json.loads(output.getvalue())["status"] == "TOP5_DELIVERY_PREPARED"
    assert plan_path.exists()

    output = StringIO()
    with redirect_stdout(output):
        assert delivery_cli(
            [
                "execute",
                "--artifact",
                str(artifact_path),
                "--current-snapshot",
                str(current_path),
                "--plan",
                str(plan_path),
                "--attestation",
                str(attestation_path),
                "--capability-token",
                str(capability_path),
                "--now",
                (BASE + timedelta(minutes=1)).isoformat(),
            ]
        ) == 0
    assert json.loads(output.getvalue())["status"] == TOP5_DELIVERY_DRY_RUN


def test_acceptance_manifest_accepts_the_exact_plan_payload():
    artifact, current, plan, attestation, _capability = _context()
    result = validate_delivery(
        json.loads(plan.worker_payload),
        json.loads(plan.static_payload),
        {
            "publication_authorized": True,
            "generation_id": plan.generation_id,
            "activation_id": plan.activation_id,
            "provider_authority": plan.provider_authority,
        },
        now=BASE + timedelta(minutes=1),
        expected_public_product_digest=plan.public_product_digest,
        delivery_manifest=plan.manifest(),
    )
    assert result["status"] == "TOP5_DELIVERY_VERIFIED"
    assert attestation.artifact_digest == artifact.artifact_digest
    assert current == {"football": []}
