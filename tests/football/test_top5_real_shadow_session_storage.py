"""Storage, resume, digest, and CLI seam tests for real-shadow sessions."""
from __future__ import annotations

import json
from pathlib import Path

from src.football.top5_real_shadow_session import RealShadowSession, RejectionRecord
from src.football.top5_real_shadow_session_storage import RealShadowSessionStore
from tests.football.test_top5_real_shadow_session import (
    BASE,
    INTEGRATION_SHA,
    experiment,
    observation,
)


def test_store_round_trip_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    session = RealShadowSession.create("shadow-session:storage", experiment=experiment(), league_scope=("BL1",), integration_sha=INTEGRATION_SHA, created_at=BASE, fixture_mode=True)
    session.record_observation(observation())
    session.finalize_predictions()
    store = RealShadowSessionStore(tmp_path / "session.json")
    path = store.save(session)
    first_digest = session.session_digest()
    resumed = store.load(session.session_id)
    assert resumed.session_digest() == first_digest
    assert store.save(resumed) == path
    assert store.load(session.session_id).session_digest() == first_digest


def test_store_rejects_removing_an_existing_prediction(tmp_path: Path) -> None:
    session = RealShadowSession.create("shadow-session:append-only", experiment=experiment(), league_scope=("BL1",), integration_sha=INTEGRATION_SHA, created_at=BASE, fixture_mode=True)
    session.record_observation(observation())
    session.finalize_predictions()
    store = RealShadowSessionStore(tmp_path / "session.json")
    store.save(session)
    session.predictions.clear()
    item = observation()
    session.rejections[item.fixture_key] = RejectionRecord(item.fixture_key, item.league_code, item.provider_identity, item.observation_digest(), "tampered removal", item.captured_at)
    try:
        store.save(session)
    except ValueError as exc:
        assert "predictions cannot be removed" in str(exc)
    else:
        raise AssertionError("append-only prediction removal was accepted")


def test_serialized_payload_contains_no_secret_bearing_fields(tmp_path: Path) -> None:
    session = RealShadowSession.create("shadow-session:secrets", experiment=experiment(), league_scope=("BL1",), integration_sha=INTEGRATION_SHA, created_at=BASE, fixture_mode=True)
    session.record_observation(observation())
    session.finalize_predictions()
    output = tmp_path / "session.json"
    RealShadowSessionStore(output).save(session)
    payload = json.loads(output.read_text())
    assert "api_key" not in json.dumps(payload).lower()
    assert "authorization" not in json.dumps(payload).lower()
    assert "response_body" not in json.dumps(payload).lower()


def test_store_rejects_immutable_core_mutation(tmp_path: Path) -> None:
    session = RealShadowSession.create("shadow-session:immutable", experiment=experiment(), league_scope=("BL1",), integration_sha=INTEGRATION_SHA, created_at=BASE, fixture_mode=True)
    session.record_observation(observation())
    session.finalize_predictions()
    store = RealShadowSessionStore(tmp_path / "session.json")
    store.save(session)
    session.created_at = BASE.replace(hour=13)
    try:
        store.save(session)
    except ValueError as exc:
        assert "immutable core" in str(exc)
    else:
        raise AssertionError("immutable session core mutation was accepted")


def test_resume_rejects_mutated_safety_flags(tmp_path: Path) -> None:
    session = RealShadowSession.create("shadow-session:safety", experiment=experiment(), league_scope=("BL1",), integration_sha=INTEGRATION_SHA, created_at=BASE, fixture_mode=True)
    session.record_observation(observation())
    session.finalize_predictions()
    output = tmp_path / "session.json"
    store = RealShadowSessionStore(output)
    store.save(session)
    payload = json.loads(output.read_text())
    payload["session"]["no_bet"] = False
    output.write_text(json.dumps(payload))
    try:
        store.load(session.session_id)
    except ValueError as exc:
        assert "NO-BET" in str(exc)
    else:
        raise AssertionError("mutated safety flags were accepted")
