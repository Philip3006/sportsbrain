from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from typing import ClassVar

import pytest

import src.football.top5_activation_route_state as route_state_module
from src.football.production_contracts import MarketSnapshot, ProductionContractError
from src.football.top5_activation_authorization import (
    verify_activation_authorization,
)
from src.football.top5_activation_route_state import (
    DurableTop5ProductionRouteStateStore,
    Top5ProductionRouteConsumer,
)
from src.football.top5_durable_activation import (
    DurableTop5ActivationStore,
    _sha,
)
from src.football.top5_one_shot_runtime import (
    OneShotExecutionError,
    OneShotHttpResult,
    TheOddsApiOneShotHttpTransport,
    Top5OneShotProductionRuntime,
    the_odds_api_one_shot_request_shape,
    the_odds_api_one_shot_request_shape_digest,
)
from src.football.top5_research_binding import M5_CANDIDATE_ID, inventory_for
from src.football.top5_signal_lifecycle import (
    DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    SignalLifecycleStage,
    create_initial_signal,
)
from tests.football.test_top5_activation_authorization import (
    NOW,
    SIGNER_ID,
    _resign,
    _signed_context,
)


@pytest.fixture(autouse=True)
def _freeze_route_consumer_wall_clock(monkeypatch):
    class FrozenDateTime(route_state_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(route_state_module, "datetime", FrozenDateTime)


class MemoryLifecycleStore:
    def __init__(self, lifecycle=None, *, fail_save=False):
        self.value = lifecycle
        self.fail_save = fail_save

    def load(self, lifecycle_id):
        if self.value is None or self.value.lifecycle_id != lifecycle_id:
            return None
        return self.value

    def save(self, lifecycle):
        if self.fail_save:
            raise OSError("offline persistence failure")
        self.value = lifecycle
        return lifecycle


class FakeTransport:
    def __init__(
        self,
        fixture,
        event_id,
        now,
        *,
        payload=None,
        status=200,
        payload_valid=True,
        error=None,
    ):
        self.calls = []
        self.now = now
        self.payload = (
            payload
            if payload is not None
            else [
                {
                    "id": event_id,
                    "sport_key": "soccer_germany_bundesliga",
                    "commence_time": fixture.kickoff.isoformat(),
                    "home_team": fixture.home_team,
                    "away_team": fixture.away_team,
                    "bookmakers": [
                        {
                            "key": "book-a",
                            "last_update": (now - timedelta(seconds=10)).isoformat(),
                            "markets": [
                                {
                                    "key": "h2h",
                                    "last_update": (
                                        now - timedelta(seconds=10)
                                    ).isoformat(),
                                    "outcomes": [
                                        {"name": fixture.home_team, "price": 2.2},
                                        {"name": "Draw", "price": 3.4},
                                        {"name": fixture.away_team, "price": 3.6},
                                    ],
                                }
                            ],
                        },
                        {
                            "key": "book-b",
                            "last_update": (now - timedelta(seconds=8)).isoformat(),
                            "markets": [
                                {
                                    "key": "h2h",
                                    "last_update": (
                                        now - timedelta(seconds=8)
                                    ).isoformat(),
                                    "outcomes": [
                                        {"name": fixture.home_team, "price": 2.1},
                                        {"name": "Draw", "price": 3.5},
                                        {"name": fixture.away_team, "price": 3.7},
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ]
        )
        self.status = status
        self.payload_valid = payload_valid
        self.error = error

    def request(self, binding, *, api_key):
        self.calls.append((binding, api_key))
        if self.error is not None:
            raise self.error
        body = json.dumps(self.payload, sort_keys=True).encode()
        return OneShotHttpResult(
            status_code=self.status,
            headers={
                "x-requests-used": "1",
                "x-requests-remaining": "499",
                "x-requests-limit": "500",
                "content-type": "application/json",
                "authorization": "Bearer injected-test-key",
            },
            payload=self.payload,
            response_digest=hashlib.sha256(body).hexdigest(),
            request_started_at=now_utc(binding, self.now, seconds=1),
            response_finished_at=now_utc(binding, self.now, seconds=2),
            request_shape_digest=binding.request_shape_digest,
            request_count=1,
            retry_count=0,
            payload_valid=self.payload_valid,
        )


def now_utc(binding, base, *, seconds):
    # The helper is separate so tests can override the fixed captured time
    # without embedding a live clock into their fake transport.
    return base + timedelta(seconds=seconds)


def _bind_test_plan_to_canonical_m5(
    plan,
    binding,
    signer,
    public_path,
    envelope,
    *,
    now,
    issued_at=None,
    expires_at=None,
):
    """The shared B1 fixture uses a placeholder hash; exercise the runner's
    exact frozen M5 hash gate with a test-only rebound of that fixture.
    """
    model_hash = inventory_for(
        binding.activation_league, M5_CANDIDATE_ID
    ).model_artifact_hash
    target_configuration = {
        "schema_version": "top5-controlled-activation-plan-v1",
        "activation_id": plan.activation_id,
        "activation_league": plan.activation_league,
        "provider_authority": "the_odds_api",
        "candidate_provider_authority": False,
        "receipt_package_digest": plan.receipt_package_digest,
        "b1_manifest_digest": plan.b1_manifest_digest,
        "source_sha": plan.source_sha.lower(),
        "research_sha": plan.research_sha.lower(),
        "model_identity": plan.model_identity,
        "model_artifact_hash": model_hash,
        "signal_time_approval_identity": plan.signal_time_approval_identity,
        "publication_enabled": False,
        "scheduler_registered": False,
        "betting_enabled": False,
        "ledger_mutation_enabled": False,
    }
    rebound_plan = replace(
        plan,
        model_artifact_hash=model_hash,
        target_configuration_digest=_sha(target_configuration),
    )
    rebound_binding = replace(
        binding,
        durable_plan_digest=rebound_plan.plan_digest,
        model_artifact_hash=model_hash,
    )
    updates = dict(rebound_binding.expected_signed_claims())
    if issued_at is not None:
        updates["issued_at"] = issued_at.isoformat()
    if expires_at is not None:
        updates["expires_at"] = expires_at.isoformat()
    rebound_envelope = _resign(envelope, signer, **updates)
    verified = verify_activation_authorization(
        rebound_envelope,
        public_key_file=public_path,
        expected_signer_key_id=SIGNER_ID,
        expected_binding=rebound_binding,
        now=now,
    )
    return rebound_plan, rebound_binding, verified, rebound_envelope


def _canonical_m5_initial_lifecycle(lifecycle, fixture, model_hash):
    initial = lifecycle.initial_version
    snapshot = MarketSnapshot(
        fixture_key=initial.fixture_key,
        captured_at=initial.odds_captured_at,
        kind=initial.snapshot_kind,
        source=initial.snapshot_source,
        odds=initial.market_odds,
        snapshot_id=initial.snapshot_id,
    )
    return create_initial_signal(
        fixture=fixture,
        snapshot=snapshot,
        now=initial.prediction_generated_at,
        market_id=initial.market_id,
        outcome_id=initial.outcome_id,
        candidate_id=initial.candidate_id,
        model_identity=initial.model_identity,
        provider_identity=initial.provider_identity,
        probabilities=initial.probabilities,
        source_sha=initial.source_sha,
        research_sha=initial.research_sha,
        model_artifact_hash=model_hash,
        eligibility_decision=initial.eligibility_decision,
        decision_id=initial.decision_id,
        decision_reason=initial.decision_reason,
        contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
        implied_probabilities=initial.implied_probabilities,
        edges=initial.edges,
        confidence_metadata=initial.confidence_metadata,
    )


def _runtime_context(
    tmp_path,
    *,
    stage="REFINEMENT",
    payload=None,
    status=200,
    payload_valid=True,
    fail_save=False,
    budget=True,
):
    (
        package,
        _bundle,
        plan,
        fixture,
        lifecycle,
        binding,
        signer,
        public_path,
        envelope,
        _verified,
    ) = _signed_context(tmp_path)
    plan, binding, verified, _envelope = _bind_test_plan_to_canonical_m5(
        plan, binding, signer, public_path, envelope, now=NOW
    )
    lifecycle = _canonical_m5_initial_lifecycle(
        lifecycle,
        fixture,
        inventory_for(binding.activation_league, M5_CANDIDATE_ID).model_artifact_hash,
    )
    selected = next(
        item for item in package.dossier.bindings if item.league == fixture.league_code
    )
    store = MemoryLifecycleStore(lifecycle, fail_save=fail_save)
    transport = FakeTransport(
        fixture,
        selected.provider_event_id,
        NOW,
        payload=payload,
        status=status,
        payload_valid=payload_valid,
    )
    times = iter(
        (
            NOW,
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=3),
            NOW + timedelta(seconds=4),
            NOW + timedelta(seconds=5),
        )
    )
    credential_calls = []
    success_calls = []
    failure_calls = []
    runtime = Top5OneShotProductionRuntime(
        route_store=DurableTop5ProductionRouteStateStore(tmp_path / "route.json"),
        transport=transport,
        lifecycle_store=store,
        clock=lambda: next(times),
        credential_loader=lambda: (
            credential_calls.append("read") or "injected-test-key"
        ),
        budget_available=lambda _now: budget,
        budget_success=lambda used, remaining: success_calls.append((used, remaining)),
        budget_failure=lambda status_code: failure_calls.append(status_code),
    )
    return (
        package,
        plan,
        fixture,
        lifecycle,
        binding,
        verified,
        selected,
        store,
        transport,
        runtime,
        credential_calls,
        success_calls,
        failure_calls,
    )


def test_signed_refinement_executes_one_request_and_persists_verified_route(tmp_path):
    (
        _package,
        plan,
        fixture,
        _lifecycle,
        binding,
        verified,
        selected,
        lifecycle_store,
        transport,
        runtime,
        credential_calls,
        successes,
        _failures,
    ) = _runtime_context(tmp_path)
    result = runtime.execute(
        plan,
        binding,
        verified,
        fixture=fixture,
        expected_provider_event_id=selected.provider_event_id,
        lifecycle=lifecycle_store.value,
    )
    assert result["schema_version"] == "top5-one-shot-production-result-v1"
    assert result["provider_request_count"] == 1
    assert result["retry_count"] == 0
    assert result["snapshot_kind"] == "signal_time"
    assert result["odds"] == {"home": 2.15, "draw": 3.45, "away": 3.65}
    assert result["model_identity"] == "M5_market_preclose"
    assert result["health_status"] == "HEALTHY"
    assert "authorization" not in result["safe_quota_headers"]
    assert "injected-test-key" not in repr(result)
    assert result["no_bet"] is True
    assert result["publication"] is False
    assert result["recurring_scheduler"] is False
    assert result["betting"] is False
    assert result["ledger_mutation"] is False
    assert len(transport.calls) == len(credential_calls) == 1
    assert successes == [(1, 499)]
    assert result["production_evidence_digest"]
    assert (
        result["verified_route_readback"]["production_evidence_digest"]
        == result["production_evidence_digest"]
    )
    route_state = runtime.route_store.read()
    assert (
        route_state["records"][binding.activation_id]["status"] == "PRODUCTION_VERIFIED"
    )
    assert (
        route_state["current_route"]["activation_mode"]
        == "CONTROLLED_ONE_SHOT_PRODUCTION_VERIFIED"
    )
    restarted = DurableTop5ProductionRouteStateStore(runtime.route_store.path)
    assert (
        restarted.read()["records"][binding.activation_id]["status"]
        == "PRODUCTION_VERIFIED"
    )


def test_signed_initial_execution_uses_exact_due_stage_and_is_still_one_shot(
    tmp_path, monkeypatch
):
    (
        package,
        _bundle,
        plan,
        fixture,
        _old_lifecycle,
        binding,
        signer,
        public_path,
        envelope,
        _verified,
    ) = _signed_context(tmp_path)
    initial_time = fixture.kickoff - timedelta(hours=24)

    class FrozenInitialDateTime(route_state_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return initial_time if tz is None else initial_time.astimezone(tz)

    monkeypatch.setattr(route_state_module, "datetime", FrozenInitialDateTime)
    plan = replace(plan, prepared_at=initial_time - timedelta(minutes=5))
    plan, binding, _verified, envelope = _bind_test_plan_to_canonical_m5(
        plan,
        binding,
        signer,
        public_path,
        envelope,
        now=initial_time,
        issued_at=initial_time - timedelta(minutes=1),
        expires_at=initial_time + timedelta(minutes=10),
    )
    initial_binding = replace(
        binding,
        lifecycle_stage="INITIAL",
        lifecycle_stage_contract_id=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.stage_contract_id(
            SignalLifecycleStage.INITIAL
        ),
        lifecycle_timing_bounds={
            "minimum_minutes_before_kickoff": DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.initial.minimum_minutes_before_kickoff,
            "maximum_minutes_before_kickoff": DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.initial.maximum_minutes_before_kickoff,
        },
    )
    initial_envelope = _resign(
        envelope,
        signer,
        **initial_binding.expected_signed_claims(),
        issued_at=(initial_time - timedelta(minutes=1)).isoformat(),
        expires_at=(initial_time + timedelta(minutes=10)).isoformat(),
    )
    verified = verify_activation_authorization(
        initial_envelope,
        public_key_file=public_path,
        expected_signer_key_id=SIGNER_ID,
        expected_binding=initial_binding,
        now=initial_time,
    )
    selected = next(
        item for item in package.dossier.bindings if item.league == fixture.league_code
    )
    transport = FakeTransport(fixture, selected.provider_event_id, initial_time)
    clock_values = iter(
        (
            initial_time,
            initial_time + timedelta(seconds=1),
            initial_time + timedelta(seconds=3),
            initial_time + timedelta(seconds=4),
        )
    )
    runtime = Top5OneShotProductionRuntime(
        route_store=DurableTop5ProductionRouteStateStore(
            tmp_path / "initial-route.json"
        ),
        transport=transport,
        lifecycle_store=MemoryLifecycleStore(),
        clock=lambda: next(clock_values),
        credential_loader=lambda: "injected-test-key",
        budget_available=lambda _now: True,
        budget_success=lambda *_args: None,
    )
    result = runtime.execute(
        plan,
        initial_binding,
        verified,
        fixture=fixture,
        expected_provider_event_id=selected.provider_event_id,
    )
    assert result["lifecycle_version"] == 1
    assert transport.calls[0][0].lifecycle_stage == "INITIAL"


def test_provider_budget_block_refuses_before_credential_and_transport(tmp_path):
    (
        _package,
        plan,
        fixture,
        lifecycle,
        binding,
        verified,
        selected,
        _store,
        transport,
        runtime,
        credential_calls,
        _successes,
        _failures,
    ) = _runtime_context(tmp_path, budget=False)
    with pytest.raises(OneShotExecutionError, match="provider budget"):
        runtime.execute(
            plan,
            binding,
            verified,
            fixture=fixture,
            expected_provider_event_id=selected.provider_event_id,
            lifecycle=lifecycle,
        )
    assert transport.calls == []
    assert credential_calls == []


@pytest.mark.parametrize(
    "tamper",
    [
        "unsigned",
        "expired",
        "fixture",
        "league",
        "stage",
        "model",
        "provider",
        "signed_scope",
    ],
)
def test_preflight_scope_failures_make_zero_provider_calls(tmp_path, tamper):
    (
        _package,
        plan,
        fixture,
        lifecycle,
        binding,
        verified,
        selected,
        _store,
        transport,
        runtime,
        credential_calls,
        _successes,
        _failures,
    ) = _runtime_context(tmp_path)
    if tamper == "unsigned":
        verified = replace(verified, _verification_seal=object())
    elif tamper == "expired":
        expires = datetime.fromisoformat(str(verified.payload["expires_at"]))
        runtime.clock = lambda: expires + timedelta(seconds=1)
    elif tamper == "fixture":
        fixture = replace(fixture, home_team=fixture.home_team + " Other")
    elif tamper == "league":
        binding = replace(binding, activation_league="EPL")
    elif tamper == "stage":
        binding = replace(binding, lifecycle_stage="INITIAL")
    elif tamper == "model":
        binding = replace(binding, model_artifact_hash="f" * 40)
    elif tamper == "provider":
        binding = replace(binding, provider_authority="therundown_experimental")
    elif tamper == "signed_scope":
        binding = replace(binding, signal_time_approval_identity="different-approval")

    with pytest.raises(ProductionContractError):
        runtime.execute(
            plan,
            binding,
            verified,
            fixture=fixture,
            expected_provider_event_id=selected.provider_event_id,
            lifecycle=lifecycle,
        )
    assert transport.calls == []
    assert credential_calls == []
    assert runtime.route_store.read()["current_route"]["activation_mode"] == "DISABLED"


def test_not_due_refinement_refuses_before_credential_and_provider(tmp_path):
    (
        package,
        _bundle,
        plan,
        fixture,
        lifecycle,
        binding,
        signer,
        public_path,
        envelope,
        _verified,
    ) = _signed_context(tmp_path)
    plan = replace(plan, prepared_at=NOW - timedelta(minutes=5))
    plan, binding, verified, _envelope = _bind_test_plan_to_canonical_m5(
        plan,
        binding,
        signer,
        public_path,
        envelope,
        now=NOW,
        issued_at=NOW - timedelta(minutes=10),
        expires_at=NOW + timedelta(minutes=10),
    )
    lifecycle = _canonical_m5_initial_lifecycle(
        lifecycle,
        fixture,
        inventory_for(binding.activation_league, M5_CANDIDATE_ID).model_artifact_hash,
    )
    selected = next(
        item for item in package.dossier.bindings if item.league == fixture.league_code
    )
    transport = FakeTransport(fixture, selected.provider_event_id, NOW)
    credential_calls = []
    runtime = Top5OneShotProductionRuntime(
        route_store=DurableTop5ProductionRouteStateStore(tmp_path / "not-due.json"),
        transport=transport,
        lifecycle_store=MemoryLifecycleStore(lifecycle),
        clock=lambda: NOW - timedelta(minutes=1),
        credential_loader=lambda: credential_calls.append(1) or "injected-test-key",
        budget_available=lambda _now: True,
    )
    with pytest.raises(OneShotExecutionError, match="not due"):
        runtime.execute(
            plan,
            binding,
            verified,
            fixture=fixture,
            expected_provider_event_id=selected.provider_event_id,
            lifecycle=lifecycle,
        )
    assert transport.calls == []
    assert credential_calls == []


@pytest.mark.parametrize(
    "failure",
    [
        "http",
        "malformed_json",
        "wrong_event",
        "missing_fixture",
        "ambiguous_fixture",
        "stale_odds",
        "closing_odds",
        "missing_bookmaker",
        "malformed_1x2",
        "credential",
        "transport",
        "model",
        "lifecycle",
        "health",
    ],
)
def test_post_route_failures_consume_at_most_one_request_and_roll_back(
    tmp_path, failure
):
    (
        package,
        _bundle,
        plan,
        fixture,
        lifecycle,
        binding,
        signer,
        public_path,
        envelope,
        _verified,
    ) = _signed_context(tmp_path)
    plan, binding, verified, _envelope = _bind_test_plan_to_canonical_m5(
        plan, binding, signer, public_path, envelope, now=NOW
    )
    lifecycle = _canonical_m5_initial_lifecycle(
        lifecycle,
        fixture,
        inventory_for(binding.activation_league, M5_CANDIDATE_ID).model_artifact_hash,
    )
    selected = next(
        item for item in package.dossier.bindings if item.league == fixture.league_code
    )
    payload = None
    status = 200
    payload_valid = True
    fail_save = failure == "lifecycle"
    fail_health = failure == "health"
    if failure == "http":
        status = 403
    elif failure == "malformed_json":
        payload_valid = False
    elif failure == "wrong_event":
        payload = [{"id": "different", "sport_key": "soccer_germany_bundesliga"}]
    elif failure == "missing_fixture":
        payload = []
    elif failure == "ambiguous_fixture":
        payload = FakeTransport(fixture, selected.provider_event_id, NOW).payload * 2
    elif failure == "stale_odds":
        payload = FakeTransport(
            fixture, selected.provider_event_id, NOW - timedelta(hours=1)
        ).payload
    elif failure == "closing_odds":
        payload = FakeTransport(fixture, selected.provider_event_id, NOW).payload
        for bookmaker in payload[0]["bookmakers"]:
            bookmaker["markets"][0]["last_update"] = (
                fixture.kickoff + timedelta(minutes=1)
            ).isoformat()
    elif failure == "missing_bookmaker":
        payload = FakeTransport(
            fixture,
            selected.provider_event_id,
            NOW,
            payload=[
                {
                    "id": selected.provider_event_id,
                    "sport_key": "soccer_germany_bundesliga",
                    "commence_time": fixture.kickoff.isoformat(),
                    "home_team": fixture.home_team,
                    "away_team": fixture.away_team,
                    "bookmakers": [],
                }
            ],
        ).payload
    elif failure == "malformed_1x2":
        payload = FakeTransport(fixture, selected.provider_event_id, NOW).payload
        for bookmaker in payload[0]["bookmakers"]:
            bookmaker["markets"][0]["outcomes"] = [
                outcome
                for outcome in bookmaker["markets"][0]["outcomes"]
                if outcome["name"] != "Draw"
            ]
    lifecycle_store = MemoryLifecycleStore(lifecycle, fail_save=fail_save)
    transport = FakeTransport(
        fixture,
        selected.provider_event_id,
        NOW,
        payload=payload,
        status=status,
        payload_valid=payload_valid,
        error=RuntimeError("fake transport failure")
        if failure == "transport"
        else None,
    )
    if failure == "model":

        class BrokenModel:
            identity = "M5_market_preclose"

            def predict(self, _model_input):
                raise RuntimeError("private model error")

        model = BrokenModel()
    else:
        model = None
    clock_values = iter(
        (
            NOW,
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=3),
            NOW + timedelta(seconds=4),
            NOW + timedelta(seconds=5),
        )
    )
    credential_calls = []
    failures = []

    def load_credential():
        credential_calls.append(1)
        if failure == "credential":
            raise OSError("fake credential access failure")
        return "injected-test-key"

    runtime = Top5OneShotProductionRuntime(
        route_store=DurableTop5ProductionRouteStateStore(tmp_path / f"{failure}.json"),
        transport=transport,
        lifecycle_store=lifecycle_store,
        clock=lambda: next(clock_values),
        credential_loader=load_credential,
        budget_available=lambda _now: True,
        budget_success=lambda *_args: None,
        budget_failure=lambda value: failures.append(value),
        model=model,
    )
    if fail_health:

        def reject_health(*_args, **_kwargs):
            raise OneShotExecutionError("fake health evidence rejection")

        runtime.route_store.mark_production_verified = reject_health
    with pytest.raises(OneShotExecutionError):
        runtime.execute(
            plan,
            binding,
            verified,
            fixture=fixture,
            expected_provider_event_id=selected.provider_event_id,
            lifecycle=lifecycle,
        )
    assert len(transport.calls) == (0 if failure == "credential" else 1)
    assert len(credential_calls) == 1
    assert runtime.route_store.read()["current_route"]["activation_mode"] == "DISABLED"
    assert (
        runtime.route_store.read()["records"][binding.activation_id]["status"]
        == "ROLLED_BACK"
    )
    assert (
        Top5ProductionRouteConsumer(runtime.route_store).verify_disabled_after_rollback(
            binding.activation_id, binding.activation_plan_digest
        )["activation_mode"]
        == "DISABLED"
    )
    assert failures == ([403] if failure == "http" else [])


def test_request_shape_is_exact_and_credential_free_in_signed_digest():
    payload = the_odds_api_one_shot_request_shape_digest("BL1")
    assert len(payload) == 64


def test_route_transition_failure_after_persisted_executing_rolls_back(tmp_path):
    (
        _package,
        plan,
        fixture,
        lifecycle,
        binding,
        verified,
        selected,
        _store,
        transport,
        runtime,
        credential_calls,
        _successes,
        _failures,
    ) = _runtime_context(tmp_path)

    class FailingRouteStore(DurableTop5ProductionRouteStateStore):
        def begin_execution(self, *args, **kwargs):
            super().begin_execution(*args, **kwargs)
            raise OSError("simulated post-transition failure")

    store = FailingRouteStore(tmp_path / "transition-failure.json")
    runtime.route_store = store
    runtime.consumer = Top5ProductionRouteConsumer(store)
    with pytest.raises(OneShotExecutionError):
        runtime.execute(
            plan,
            binding,
            verified,
            fixture=fixture,
            expected_provider_event_id=selected.provider_event_id,
            lifecycle=lifecycle,
        )
    assert transport.calls == []
    assert credential_calls == []
    assert (
        runtime.consumer.verify_disabled_after_rollback(
            binding.activation_id, binding.activation_plan_digest
        )["activation_mode"]
        == "DISABLED"
    )


def test_transport_uses_one_exact_no_retry_no_redirect_http_call(tmp_path):
    _package, _bundle, _plan, _fixture, _lifecycle, binding, *_rest = _signed_context(
        tmp_path
    )

    class FakeResponse:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {
            "Content-Type": "application/json",
            "X-Requests-Used": "1",
            "X-Requests-Remaining": "499",
            "X-Requests-Limit": "500",
        }
        content = b"[]"

    class FakeSession:
        def __init__(self):
            self.mounts = {}
            self.calls = []

        def mount(self, prefix, adapter):
            self.mounts[prefix] = adapter

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResponse()

        def close(self):
            return None

    session = FakeSession()
    transport = TheOddsApiOneShotHttpTransport(session_factory=lambda: session)
    result = transport.request(binding, api_key="synthetic-test-only")
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == the_odds_api_one_shot_request_shape("BL1")["endpoint"]
    assert kwargs["params"] == {
        **the_odds_api_one_shot_request_shape("BL1")["query"],
        "apiKey": "synthetic-test-only",
    }
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == 15.0
    assert session.mounts["https://"].max_retries.total == 0
    assert session.mounts["http://"].max_retries.total == 0
    assert result.request_count == 1
    assert result.retry_count == 0
    assert "synthetic-test-only" not in repr(result)


def test_durable_activation_store_verifies_one_shot_result_and_safety(tmp_path):
    (
        _package,
        plan,
        fixture,
        lifecycle,
        binding,
        verified,
        selected,
        _lifecycle_store,
        _transport,
        runtime,
        _credential_calls,
        _successes,
        _failures,
    ) = _runtime_context(tmp_path)
    store = DurableTop5ActivationStore(tmp_path / "activation.json")
    store.prepare(plan, now=NOW)
    record = store.execute(
        plan,
        now=NOW,
        explicit_execute=True,
        execution_binding=binding,
        verified_authorization=verified,
        runtime=runtime,
        fixture=fixture,
        expected_provider_event_id=selected.provider_event_id,
        lifecycle=lifecycle,
    )
    assert record["status"] == "PRODUCTION_VERIFIED"
    assert record["execution_result_digest"] == _sha(record["execution_result"])
    assert record["execution_result"]["provider_request_count"] == 1
    assert record["execution_result"]["retry_count"] == 0
    assert record["publication_enabled"] is False
    assert record["scheduler_registered"] is False
    assert record["betting_enabled"] is False
    assert record["ledger_mutation_enabled"] is False
    assert record["execution_result"]["no_bet"] is True
    assert record["execution_result"]["publication"] is False


def test_durable_result_validation_failure_rolls_back_route_consumer(tmp_path):
    (
        _package,
        plan,
        fixture,
        lifecycle,
        binding,
        verified,
        selected,
        _lifecycle_store,
        _transport,
        runtime,
        _credential_calls,
        _successes,
        _failures,
    ) = _runtime_context(tmp_path)
    activation_store = DurableTop5ActivationStore(tmp_path / "activation-invalid.json")
    activation_store.prepare(plan, now=NOW)

    class InvalidResultRuntime:
        def execute(self, *args, **kwargs):
            result = runtime.execute(*args, **kwargs)
            result["publication"] = True
            return result

        def rollback_execution(self, exact_binding):
            return runtime.rollback_execution(exact_binding)

    with pytest.raises(ProductionContractError, match="result digest mismatch"):
        activation_store.execute(
            plan,
            now=NOW,
            explicit_execute=True,
            execution_binding=binding,
            verified_authorization=verified,
            runtime=InvalidResultRuntime(),
            fixture=fixture,
            expected_provider_event_id=selected.provider_event_id,
            lifecycle=lifecycle,
        )
    assert (
        runtime.consumer.verify_disabled_after_rollback(
            binding.activation_id, binding.activation_plan_digest
        )["activation_mode"]
        == "DISABLED"
    )
    assert (
        activation_store.status(binding.activation_id)["record"]["status"]
        == "ROLLED_BACK"
    )


def test_rolled_back_route_blocks_reexecution_before_credentials_or_request(tmp_path):
    (
        _package,
        plan,
        fixture,
        lifecycle,
        binding,
        verified,
        selected,
        _store,
        transport,
        runtime,
        credential_calls,
        _successes,
        _failures,
    ) = _runtime_context(tmp_path, status=403)
    with pytest.raises(OneShotExecutionError):
        runtime.execute(
            plan,
            binding,
            verified,
            fixture=fixture,
            expected_provider_event_id=selected.provider_event_id,
            lifecycle=lifecycle,
        )
    assert (
        runtime.consumer.verify_disabled_after_rollback(
            binding.activation_id, binding.activation_plan_digest
        )["activation_mode"]
        == "DISABLED"
    )
    runtime.clock = lambda: NOW + timedelta(seconds=10)
    with pytest.raises(OneShotExecutionError):
        runtime.execute(
            plan,
            binding,
            verified,
            fixture=fixture,
            expected_provider_event_id=selected.provider_event_id,
            lifecycle=lifecycle,
        )
    assert len(transport.calls) == 1
    assert len(credential_calls) == 1
