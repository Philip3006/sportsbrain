from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import src.football.top5_canary_orchestrator as orchestrator
from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
)
from src.football.top5_canary_batch_storage import Top5CanaryBatchState
from src.football.top5_one_shot_runtime import (
    ONE_SHOT_RESULT_SCHEMA,
    _top5_value_decision,
    one_shot_capture_digest,
)
from src.football.top5_publisher import TOP5_PUBLIC_RELEASE_LEAGUES
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_signal_lifecycle import (
    DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    create_initial_signal,
    top5_h2h_lifecycle_set_digest,
)

NOW = datetime(2026, 10, 8, 15, 22, 30, tzinfo=timezone.utc)
ODDS = {"home": 2.2, "draw": 3.5, "away": 4.2}
PROBABILITIES = {"home": 0.45, "draw": 0.30, "away": 0.25}


def _fixture(league: str, *, now: datetime = NOW) -> Fixture:
    home, away = f"{league} Home", f"{league} Away"
    kickoff = now + timedelta(hours=24)
    return Fixture(
        make_fixture_key(league, home, away, kickoff), league, home, away, kickoff
    )


def _lifecycle_set(fixture: Fixture, activation_id: str, captured_at: datetime):
    prediction_at = fixture.kickoff - timedelta(hours=24)
    snapshot = MarketSnapshot(
        fixture_key=fixture.fixture_key,
        captured_at=captured_at,
        kind=MarketSnapshotKind.SIGNAL_TIME,
        source="the_odds_api:prematch",
        odds=ODDS,
        snapshot_id=f"snapshot:{fixture.league_code}",
    )
    decision = _top5_value_decision(
        fixture=fixture,
        probabilities=PROBABILITIES,
        odds=ODDS,
        activation_id=activation_id,
        snapshot_id=snapshot.snapshot_id,
    )
    outcome_decisions = decision["outcomes"]
    implied = {
        outcome: outcome_decisions[outcome]["market_implied_probability"]
        for outcome in ("home", "draw", "away")
    }
    edges = {
        outcome: (PROBABILITIES[outcome] - implied[outcome]) * 100
        for outcome in ("home", "draw", "away")
    }
    edges.update(
        {
            f"expected_value_{outcome}": outcome_decisions[outcome]["expected_value"]
            for outcome in ("home", "draw", "away")
        }
    )
    model_hash = "b" * 64
    return tuple(
        create_initial_signal(
            fixture=fixture,
            snapshot=snapshot,
            now=prediction_at,
            market_id="h2h",
            outcome_id=outcome,
            candidate_id=orchestrator.M5_CANDIDATE_ID,
            model_identity="model:test-m5",
            provider_identity="the_odds_api",
            probabilities=PROBABILITIES,
            source_sha="c" * 40,
            research_sha="d" * 40,
            model_artifact_hash=model_hash,
            eligibility_decision=outcome_decisions[outcome]["state"] == "SIGNAL",
            decision_id=decision["decision_id"],
            decision_reason=outcome_decisions[outcome]["reason"],
            contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
            implied_probabilities=implied,
            edges=edges,
            confidence_metadata={
                "signal_decision_id": decision["decision_id"],
                "signal_decision_digest": decision["decision_digest"],
                "signal_decision_schema_version": decision["schema_version"],
                "signal_decision_detector": decision["detector"],
                "signal_decision_policy": decision["policy"],
                "signal_state": outcome_decisions[outcome]["state"],
                "signal_confidence": outcome_decisions[outcome]["confidence"],
            },
        )
        for outcome in ("home", "draw", "away")
    )


def _result(run, lifecycles, *, now: datetime = NOW):
    fixture = run.fixture
    captured_at = now - timedelta(seconds=45)
    lifecycle_digest = top5_h2h_lifecycle_set_digest(lifecycles)
    result = {
        "schema_version": ONE_SHOT_RESULT_SCHEMA,
        "activation_id": run.binding.activation_id,
        "activation_plan_digest": run.binding.activation_plan_digest,
        "signed_authorization_digest": run.authorization.authorization_digest,
        "league": fixture.league_code,
        "fixture_key": fixture.fixture_key,
        "provider_event_id": f"odds-event:{fixture.league_code}",
        "provider_authority": "the_odds_api",
        "provider_request_count": 1,
        "retry_count": 0,
        "request_shape_digest": "e" * 64,
        "http_status": 200,
        "response_digest": "f" * 64,
        "snapshot_id": f"snapshot:{fixture.league_code}",
        "snapshot_source": "the_odds_api:prematch",
        "captured_at": captured_at.isoformat(),
        "odds": dict(ODDS),
        "model_identity": "model:test-m5",
        "model_artifact_hash": "b" * 64,
        "source_sha": "c" * 40,
        "research_sha": "d" * 40,
        "prediction_id": f"prediction:{fixture.league_code}",
        "candidate_id": orchestrator.M5_CANDIDATE_ID,
        "prediction_timestamp": now.isoformat(),
        "probabilities": dict(PROBABILITIES),
        "signal_decision": _top5_value_decision(
            fixture=fixture,
            probabilities=PROBABILITIES,
            odds=ODDS,
            activation_id=run.binding.activation_id,
            snapshot_id=f"snapshot:{fixture.league_code}",
        ),
        "lifecycle_set_digest": lifecycle_digest,
        "execution_started_at": (now - timedelta(seconds=60)).isoformat(),
        "request_started_at": (now - timedelta(seconds=50)).isoformat(),
        "response_finished_at": captured_at.isoformat(),
        "execution_finished_at": (now - timedelta(seconds=20)).isoformat(),
        "no_bet": True,
        "publication": False,
        "recurring_scheduler": False,
        "betting": False,
        "ledger_mutation": False,
    }
    result["capture_result_digest"] = one_shot_capture_digest(result)
    return result


class _Transport:
    def __init__(self, *, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def request(self, *_args, **_kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated transport failure")
        return object()


class _Runtime:
    def __init__(self, fixture, activation_id, *, fail=False, gate=True):
        self.fixture = fixture
        self.activation_id = activation_id
        self.transport = _Transport(fail=fail)
        self.route_store = SimpleNamespace(
            read=lambda: {"records": {}}, path=f"/private/{fixture.league_code}.json"
        )
        self.lifecycle_set_store = SimpleNamespace(
            load_for_scope=lambda **_scope: self.lifecycles
        )
        self.lifecycles = ()
        self.credential_calls = 0
        self.budget_available = lambda _at: gate
        self.execute_calls = 0

    def execute(self, _plan, _binding, _authorization, *, fixture, lifecycles):
        self.execute_calls += 1
        if not self.budget_available(NOW):
            raise RuntimeError("quota gate blocked")
        self.credential_calls += 1
        self.transport.request(None, api_key="test-only")
        return self.result

    def rollback_execution(self, _binding):
        return {"verified_disabled": True}


def _runs(*, fail_first=False, gate=True):
    result = []
    activation_id = "canary-identity"
    for league in sorted(TOP5_PUBLIC_RELEASE_LEAGUES):
        fixture = _fixture(league)
        binding = SimpleNamespace(
            activation_id=activation_id,
            lifecycle_stage="INITIAL",
            activation_plan_digest=f"plan:{league}",
            lifecycle_contract_id=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.contract_id,
            model_identity="model:test-m5",
            request_shape_digest="e" * 64,
            source_sha="c" * 40,
            research_sha="d" * 40,
            model_artifact_hash="b" * 64,
        )
        authorization = SimpleNamespace(
            authorization_digest=f"{league.lower():<64}"[:64], nonce=f"nonce:{league}"
        )
        runtime = _Runtime(
            fixture,
            activation_id,
            fail=fail_first and not result,
            gate=gate,
        )
        run = orchestrator.Top5OneShotFixtureRun(
            runtime=runtime,
            plan=SimpleNamespace(),
            binding=binding,
            authorization=authorization,
            fixture=fixture,
            lifecycles=None,
        )
        runtime.lifecycles = _lifecycle_set(
            fixture, activation_id, NOW - timedelta(seconds=45)
        )
        runtime.result = _result(run, runtime.lifecycles)
        result.append(run)
    return tuple(result)


def _patch_manual_gates(monkeypatch, *, artifact_at=NOW):
    monkeypatch.setattr(orchestrator, "_validate_runs", lambda runs, **_kw: tuple(runs))
    monkeypatch.setattr(
        orchestrator, "_validate_publication_inputs", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_publication_payloads",
        lambda captured, **_kwargs: (tuple(captured), artifact_at),
    )


def test_exact_five_scope_rejects_missing_duplicate_and_unsupported_leagues():
    activation = "one-authorized-batch"
    stage = "INITIAL"
    valid = [
        SimpleNamespace(
            fixture=SimpleNamespace(league_code=league, fixture_key=f"key:{league}"),
            binding=SimpleNamespace(activation_id=activation, lifecycle_stage=stage),
        )
        for league in sorted(TOP5_PUBLIC_RELEASE_LEAGUES)
    ]
    with pytest.raises(orchestrator.Top5CanaryOrchestrationError, match="exactly five"):
        orchestrator._validate_runs(valid[:-1], now=NOW)
    duplicate_league = [*valid[:-1], valid[0]]
    with pytest.raises(orchestrator.Top5CanaryOrchestrationError, match="EPL"):
        orchestrator._validate_runs(duplicate_league, now=NOW)
    duplicate_fixture = [
        *valid[:-1],
        SimpleNamespace(
            fixture=SimpleNamespace(
                league_code=valid[-1].fixture.league_code, fixture_key="key:EPL"
            ),
            binding=SimpleNamespace(activation_id=activation, lifecycle_stage=stage),
        ),
    ]
    with pytest.raises(
        orchestrator.Top5CanaryOrchestrationError, match="fixture identities"
    ):
        orchestrator._validate_runs(duplicate_fixture, now=NOW)
    unsupported = [
        *valid[:-1],
        SimpleNamespace(
            fixture=SimpleNamespace(league_code="MLS", fixture_key="key:MLS"),
            binding=SimpleNamespace(activation_id=activation, lifecycle_stage=stage),
        ),
    ]
    with pytest.raises(orchestrator.Top5CanaryOrchestrationError, match="EPL"):
        orchestrator._validate_runs(unsupported, now=NOW)


def test_five_fixture_initial_capture_uses_quota_gate_and_produces_one_complete_batch(
    monkeypatch,
):
    _patch_manual_gates(monkeypatch)
    monkeypatch.setattr(
        orchestrator.provider_budget,
        "is_top5_provider_available",
        lambda *, now: now == NOW,
        raising=False,
    )
    runs = _runs()
    events = []

    class _Publisher:
        def publish_batch(self, payloads, *_args, now, **_kwargs):
            assert len(payloads) == 5
            return SimpleNamespace(published_at=now, artifact_digest="a" * 64)

    monkeypatch.setattr(orchestrator, "InMemoryTop5PublicationStore", _Publisher)
    capture = orchestrator.capture_top5_canary_batch(
        runs,
        publication_authorizations={},
        activation_bindings={},
        now=NOW,
        event_sink=events.append,
    )
    assert capture.provider_request_count == 5
    assert capture.retry_count == 0
    assert len(capture.fixture_identities) == 5
    assert all(run.runtime.transport.calls == 1 for run in runs)
    assert all(run.runtime.credential_calls == 1 for run in runs)
    assert [event["event"] for event in events].count("fixture_capture_verified") == 5
    assert events[-1]["event"] == "validation_passed"


def test_quota_gate_failure_precedes_credential_and_transport(monkeypatch):
    _patch_manual_gates(monkeypatch)
    monkeypatch.setattr(
        orchestrator.provider_budget,
        "is_top5_provider_available",
        lambda **_kwargs: False,
        raising=False,
    )
    runs = _runs(gate=True)
    # Runtime gate is replaced by the canonical gate in the orchestration path.
    events = []
    with pytest.raises(orchestrator.Top5CanaryOrchestrationError):
        orchestrator.capture_top5_canary_batch(
            runs,
            publication_authorizations={},
            activation_bindings={},
            now=NOW,
            event_sink=events.append,
        )
    assert all(run.runtime.transport.calls == 0 for run in runs)
    assert all(run.runtime.credential_calls == 0 for run in runs)
    failure = events[-1]
    assert failure["event"] == "batch_validation_failed"
    assert failure["provider_request_count"] == 0
    assert failure["no_public_write"] is True


def test_missing_freshness_gate_fails_before_credentials_or_transport(monkeypatch):
    _patch_manual_gates(monkeypatch)
    monkeypatch.delattr(
        orchestrator.provider_budget, "is_top5_provider_available", raising=False
    )
    runs = _runs()
    events = []
    with pytest.raises(orchestrator.Top5CanaryOrchestrationError, match="fresh Top-5"):
        orchestrator.capture_top5_canary_batch(
            runs,
            publication_authorizations={},
            activation_bindings={},
            now=NOW,
            event_sink=events.append,
        )
    assert all(run.runtime.credential_calls == 0 for run in runs)
    assert all(run.runtime.transport.calls == 0 for run in runs)
    assert events[-1]["provider_request_count"] == 0


def test_missing_publication_authorizations_fail_in_preflight():
    runs = _runs()
    with pytest.raises(
        orchestrator.Top5CanaryOrchestrationError,
        match="five publication authorizations",
    ):
        orchestrator._validate_publication_inputs(
            runs,
            publication_authorizations={},
            activation_bindings={},
            now=NOW,
        )


def test_partial_capture_failure_counts_transport_attempt_and_never_builds_batch(
    monkeypatch,
):
    _patch_manual_gates(monkeypatch)
    monkeypatch.setattr(
        orchestrator.provider_budget,
        "is_top5_provider_available",
        lambda **_kwargs: True,
        raising=False,
    )
    runs = _runs(fail_first=True)
    events = []
    payload_builder_calls = []
    monkeypatch.setattr(
        orchestrator,
        "_build_publication_payloads",
        lambda *_args, **_kwargs: payload_builder_calls.append(True),
    )
    with pytest.raises(orchestrator.Top5CanaryOrchestrationError):
        orchestrator.capture_top5_canary_batch(
            runs,
            publication_authorizations={},
            activation_bindings={},
            now=NOW,
            event_sink=events.append,
        )
    assert runs[0].runtime.transport.calls == 1
    assert runs[0].runtime.credential_calls == 1
    assert all(run.runtime.execute_calls == 0 for run in runs[1:])
    assert payload_builder_calls == []
    assert events[-1]["provider_request_count"] == 1
    assert events[-1]["captured_fixture_count"] == 0
    assert events[-1]["no_public_write"] is True


def test_orchestrator_rejects_invalid_probabilities_and_future_or_stale_capture():
    run = _runs()[0]
    lifecycles = run.runtime.lifecycles
    result = dict(run.runtime.result)
    result["probabilities"] = {"home": 0.5, "draw": 0.5, "away": 0.5}
    result["capture_result_digest"] = one_shot_capture_digest(result)
    with pytest.raises(orchestrator.Top5CanaryOrchestrationError, match="sum to one"):
        orchestrator._validate_result(result, run, now=NOW)

    result = dict(run.runtime.result)
    result["captured_at"] = (NOW + timedelta(seconds=1)).isoformat()
    result["capture_result_digest"] = one_shot_capture_digest(result)
    with pytest.raises(orchestrator.Top5CanaryOrchestrationError, match="future-dated"):
        orchestrator._validate_result(result, run, now=NOW)

    result = dict(run.runtime.result)
    result["captured_at"] = (NOW - timedelta(seconds=901)).isoformat()
    result["capture_result_digest"] = one_shot_capture_digest(result)
    with pytest.raises(orchestrator.Top5CanaryOrchestrationError, match="stale"):
        orchestrator._validate_result(result, run, now=NOW)
    assert len(lifecycles) == 3


def test_no_signal_outcomes_are_preserved_without_fabricating_actionability():
    run = _runs()[0]
    decision = run.runtime.result["signal_decision"]
    outcomes = decision["outcomes"]
    lifecycles = {
        lifecycle.initial_version.outcome_id: lifecycle
        for lifecycle in run.runtime.lifecycles
    }
    assert outcomes["home"]["state"] == "NO_SIGNAL"
    assert lifecycles["home"].current_version.eligibility_decision is False
    assert (
        lifecycles["home"].current_version.decision_reason == outcomes["home"]["reason"]
    )
    assert decision["detector"] == "src.betting.value_detector.detect_value"


def test_resuming_all_verified_private_captures_uses_zero_provider_requests(
    monkeypatch,
):
    _patch_manual_gates(monkeypatch)
    runs = _runs()

    def recover(run, *, now):
        return run.runtime.result, run.runtime.lifecycles

    monkeypatch.setattr(orchestrator, "_recover_capture", recover)
    monkeypatch.setattr(
        orchestrator.provider_budget,
        "is_top5_provider_available",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("quota gate should be skipped")
        ),
        raising=False,
    )

    class _Publisher:
        def publish_batch(self, payloads, *_args, now, **_kwargs):
            assert len(payloads) == 5
            return SimpleNamespace(published_at=now, artifact_digest="a" * 64)

    monkeypatch.setattr(orchestrator, "InMemoryTop5PublicationStore", _Publisher)
    capture = orchestrator.capture_top5_canary_batch(
        runs,
        publication_authorizations={},
        activation_bindings={},
        now=NOW,
    )
    assert capture.provider_request_count == 0
    assert capture.resumed_fixture_count == 5
    assert all(run.runtime.execute_calls == 0 for run in runs)
    assert all(run.runtime.credential_calls == 0 for run in runs)
    assert all(run.runtime.transport.calls == 0 for run in runs)


def test_delivery_delegates_to_committed_batch_store_and_checks_commit(monkeypatch):
    class _Attestation:
        pass

    class _Adapter:
        def build_plan(self, snapshot, artifact):
            return SimpleNamespace(
                generation_id="generation-1", public_product_digest="b" * 64
            )

    class _Store:
        def load(self, _generation_id):
            return {"state": Top5CanaryBatchState.COMMITTED.value}

    calls = []
    monkeypatch.setattr(orchestrator, "Top5DeliveryAttestation", _Attestation)
    monkeypatch.setattr(orchestrator, "Top5CanonicalDeliveryAdapter", _Adapter)
    monkeypatch.setattr(
        orchestrator,
        "execute_stored_top5_batch",
        lambda **kwargs: calls.append(kwargs) or SimpleNamespace(status="COMMITTED"),
    )
    events = []
    result = orchestrator.deliver_top5_canary_batch(
        artifact=object(),
        store=_Store(),
        executor=object(),
        current_public_snapshot={},
        attestation=_Attestation(),
        capability={},
        now=NOW,
        event_sink=events.append,
    )
    assert result.status == "COMMITTED"
    assert len(calls) == 1
    assert calls[0]["store"].__class__ is _Store
    assert calls[0]["now"] == NOW
    assert [event["event"] for event in events] == [
        "storage_committed",
        "public_adapter_updated",
    ]


def test_delivery_refuses_anything_but_committed_state(monkeypatch):
    class _Attestation:
        pass

    class _Adapter:
        def build_plan(self, snapshot, artifact):
            return SimpleNamespace(
                generation_id="generation-1", public_product_digest="b" * 64
            )

    class _Store:
        def load(self, _generation_id):
            return {"state": Top5CanaryBatchState.PREPARING.value}

    monkeypatch.setattr(orchestrator, "Top5DeliveryAttestation", _Attestation)
    monkeypatch.setattr(orchestrator, "Top5CanonicalDeliveryAdapter", _Adapter)
    monkeypatch.setattr(
        orchestrator,
        "execute_stored_top5_batch",
        lambda **_kwargs: SimpleNamespace(status="PREPARING"),
    )
    with pytest.raises(orchestrator.Top5CanaryOrchestrationError, match="COMMITTED"):
        orchestrator.deliver_top5_canary_batch(
            artifact=object(),
            store=_Store(),
            executor=object(),
            current_public_snapshot={},
            attestation=_Attestation(),
            capability={},
            now=NOW,
        )
