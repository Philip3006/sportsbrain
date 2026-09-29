from __future__ import annotations

import hashlib
import json
import os
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
    TOP5_H2H_OUTCOMES,
    RefinementClassification,
    SignalLifecycleError,
    SignalLifecycleStage,
    Top5SignalLifecycleSetStore,
    canonical_top5_h2h_lifecycle_set,
    create_initial_signal,
    lifecycle_set_state_path,
    parse_top5_h2h_lifecycle_set,
    refine_signal,
    top5_h2h_lifecycle_set_digest,
    top5_h2h_lifecycle_set_id,
    top5_h2h_lifecycle_set_identity_for_scope,
    top5_h2h_lifecycle_set_payload,
)
from src.football.top5_signal_lifecycle_public_adapter import (
    Top5SignalLifecyclePublicAdapterError,
    project_top5_signal_lifecycles,
)
from src.signals import provider_budget
from tests.football.test_top5_activation_authorization import (
    NOW,
    SIGNER_ID,
    _resign,
    _signed_context,
)

CANDIDATE_PROVIDER_EVENT_ID = "therundown-event-123"
ODDS_API_PROVIDER_EVENT_ID = "the-odds-api-event-987"


@pytest.fixture(autouse=True)
def _freeze_route_consumer_wall_clock(monkeypatch):
    class FrozenDateTime(route_state_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(route_state_module, "datetime", FrozenDateTime)


class MemoryLifecycleSetStore:
    """Atomic in-memory test double for the production set-store contract."""

    def __init__(self, lifecycles=None, *, fail_commit=False):
        self.values = {}
        self.set_members = {}
        self.fail_commit = fail_commit
        self.commit_calls = 0
        self.load_calls = 0
        if lifecycles:
            self._seed(lifecycles)

    def _seed(self, lifecycles):
        ordered = canonical_top5_h2h_lifecycle_set(lifecycles)
        lifecycle_set_id = top5_h2h_lifecycle_set_id(tuple(ordered.values()))
        self.set_members[lifecycle_set_id] = tuple(
            ordered[outcome].lifecycle_id for outcome in TOP5_H2H_OUTCOMES
        )
        self.values.update(
            {lifecycle.lifecycle_id: lifecycle for lifecycle in ordered.values()}
        )

    def load_for_scope(self, **scope):
        lifecycle_set_id = top5_h2h_lifecycle_set_identity_for_scope(**scope)
        self.load_calls += 1
        member_ids = self.set_members.get(lifecycle_set_id)
        if member_ids is None:
            return None
        try:
            return tuple(self.values[identity] for identity in member_ids)
        except KeyError as exc:
            raise SignalLifecycleError("memory lifecycle set is incomplete") from exc

    def commit(self, lifecycles):
        self.commit_calls += 1
        if self.fail_commit:
            raise OSError("injected atomic lifecycle-set failure")
        ordered = canonical_top5_h2h_lifecycle_set(lifecycles)
        lifecycle_set_id = top5_h2h_lifecycle_set_id(tuple(ordered.values()))
        member_ids = self.set_members.get(lifecycle_set_id)
        if member_ids is not None:
            current = tuple(self.values[identity] for identity in member_ids)
            if top5_h2h_lifecycle_set_digest(current) == top5_h2h_lifecycle_set_digest(
                tuple(ordered.values())
            ):
                return current
            Top5SignalLifecycleSetStore._validate_transition(current, ordered)
        elif Top5SignalLifecycleSetStore._state_kind(ordered) != "INITIAL":
            raise SignalLifecycleError("memory set must begin with INITIAL")
        self.values.update(
            {lifecycle.lifecycle_id: lifecycle for lifecycle in ordered.values()}
        )
        self.set_members[lifecycle_set_id] = tuple(
            ordered[outcome].lifecycle_id for outcome in TOP5_H2H_OUTCOMES
        )
        return tuple(ordered[outcome] for outcome in TOP5_H2H_OUTCOMES)

    def by_outcome(self):
        return {
            lifecycle.initial_version.outcome_id: lifecycle
            for lifecycle in self.values.values()
        }


class InstrumentedCanonicalLifecycleSetStore(Top5SignalLifecycleSetStore):
    """Track calls while exercising the real atomic external set store."""

    def __init__(self, root):
        super().__init__(root=root)
        self.commit_calls = 0
        self.load_calls = 0

    def load_for_scope(self, **scope):
        self.load_calls += 1
        return super().load_for_scope(**scope)

    def commit(self, lifecycles):
        self.commit_calls += 1
        return super().commit(lifecycles)


class FakeTransport:
    def __init__(
        self,
        fixture,
        now,
        *,
        odds_api_event_id=ODDS_API_PROVIDER_EVENT_ID,
        payload=None,
        status=200,
        payload_valid=True,
        error=None,
    ):
        self.calls = []
        self.results = []
        self.now = now
        self.payload = (
            payload
            if payload is not None
            else [
                {
                    "id": odds_api_event_id,
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
        result = OneShotHttpResult(
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
        self.results.append(result)
        return result


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


def _canonical_m5_initial_lifecycles(lifecycles, fixture, model_hash):
    first = lifecycles[0].initial_version
    snapshot = MarketSnapshot(
        fixture_key=first.fixture_key,
        captured_at=first.odds_captured_at,
        kind=first.snapshot_kind,
        source=first.snapshot_source,
        odds=first.market_odds,
        snapshot_id=first.snapshot_id,
    )
    return tuple(
        create_initial_signal(
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
        for lifecycle in lifecycles
        for initial in (lifecycle.initial_version,)
    )


def _runtime_context(
    tmp_path,
    *,
    stage="REFINEMENT",
    payload=None,
    status=200,
    payload_valid=True,
    fail_commit=False,
    budget=True,
    durable_set_store=False,
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
    lifecycles = _canonical_m5_initial_lifecycles(
        lifecycle,
        fixture,
        inventory_for(binding.activation_league, M5_CANDIDATE_ID).model_artifact_hash,
    )
    selected = next(
        item for item in package.dossier.bindings if item.league == fixture.league_code
    )
    if durable_set_store:
        store = InstrumentedCanonicalLifecycleSetStore(
            tmp_path / "authoritative-lifecycle-sets"
        )
        store.commit(lifecycles)
    else:
        store = MemoryLifecycleSetStore(lifecycles, fail_commit=fail_commit)
    transport = FakeTransport(
        fixture,
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
        lifecycle_set_store=store,
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
        lifecycles,
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


def _project_runtime_lifecycle_set(store, result, fixture, lifecycles=None):
    if lifecycles is None:
        lifecycles = store.load_for_scope(
            lifecycle_contract_id=result["lifecycle_contract_id"],
            league_code=fixture.league_code,
            fixture_key=fixture.fixture_key,
            market_id="h2h",
            candidate_id=result["candidate_id"],
            model_identity=result["model_identity"],
        )
        assert lifecycles is not None
    return project_top5_signal_lifecycles(
        lifecycles,
        prediction_probabilities=result["probabilities"],
        fixture_identity=fixture.fixture_key,
        league_identity=fixture.league_code,
        candidate_identity=result["candidate_id"],
        model_identity=result["model_identity"],
        provider_authority=result["provider_authority"],
        prediction_timestamp=result["prediction_timestamp"],
        signal_timestamp=result["captured_at"],
        snapshot_id=result["snapshot_id"],
        provenance={
            "source_sha": result["source_sha"],
            "research_sha": result["research_sha"],
            "model_artifact_hash": result["model_artifact_hash"],
        },
        market_identity="h2h",
    )


def _clone_initial_lifecycle(lifecycle, fixture, **overrides):
    initial = lifecycle.initial_version
    snapshot = MarketSnapshot(
        fixture_key=fixture.fixture_key,
        captured_at=initial.odds_captured_at,
        kind=initial.snapshot_kind,
        source=initial.snapshot_source,
        odds=initial.market_odds,
        snapshot_id=initial.snapshot_id,
    )
    values = {
        "fixture": fixture,
        "snapshot": snapshot,
        "now": initial.prediction_generated_at,
        "market_id": initial.market_id,
        "outcome_id": initial.outcome_id,
        "candidate_id": initial.candidate_id,
        "model_identity": initial.model_identity,
        "provider_identity": initial.provider_identity,
        "probabilities": initial.probabilities,
        "source_sha": initial.source_sha,
        "research_sha": initial.research_sha,
        "model_artifact_hash": initial.model_artifact_hash,
        "eligibility_decision": initial.eligibility_decision,
        "decision_id": initial.decision_id,
        "decision_reason": initial.decision_reason,
        "contract": lifecycle.contract,
        "implied_probabilities": initial.implied_probabilities,
        "edges": initial.edges,
        "confidence_metadata": initial.confidence_metadata,
    }
    values.update(overrides)
    return create_initial_signal(**values)


def test_signed_refinement_executes_one_request_and_persists_verified_route(tmp_path):
    (
        package,
        plan,
        fixture,
        initial_lifecycles,
        binding,
        verified,
        selected,
        lifecycle_store,
        transport,
        runtime,
        credential_calls,
        successes,
        _failures,
    ) = _runtime_context(tmp_path, durable_set_store=True)
    candidate_evidence = {
        "provider_identity": package.dossier.provider_identity,
        "provider_event_id": CANDIDATE_PROVIDER_EVENT_ID,
    }
    assert candidate_evidence["provider_identity"] == "therundown_experimental"
    assert selected.provider_event_id != ODDS_API_PROVIDER_EVENT_ID
    initial_ids = {
        lifecycle.initial_version.outcome_id: lifecycle.lifecycle_id
        for lifecycle in initial_lifecycles
    }
    initial_probabilities = {
        lifecycle.initial_version.outcome_id: lifecycle.initial_version.probabilities[
            lifecycle.initial_version.outcome_id
        ]
        for lifecycle in initial_lifecycles
    }
    initial_snapshot_ids = {
        lifecycle.initial_version.snapshot_id for lifecycle in initial_lifecycles
    }
    result = runtime.execute(
        plan,
        binding,
        verified,
        fixture=fixture,
        lifecycles=initial_lifecycles,
    )
    assert result["schema_version"] == "top5-one-shot-production-result-v1"
    assert result["provider_request_count"] == 1
    assert result["retry_count"] == 0
    assert result["provider_authority"] == "the_odds_api"
    assert result["provider_event_id"] == ODDS_API_PROVIDER_EVENT_ID
    assert result["provider_event_id"] != candidate_evidence["provider_event_id"]
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
    assert set(result["lifecycle_ids"]) == set(TOP5_H2H_OUTCOMES)
    assert set(result["lifecycle_versions"]) == set(TOP5_H2H_OUTCOMES)
    assert set(result["lifecycle_version_digests"]) == set(TOP5_H2H_OUTCOMES)
    assert set(result["lifecycle_digests"]) == set(TOP5_H2H_OUTCOMES)
    assert result["lifecycle_versions"] == dict.fromkeys(TOP5_H2H_OUTCOMES, 2)
    assert result["lifecycle_ids"] == initial_ids
    assert result["lifecycle_set_digest"] == _sha(result["lifecycle_digests"])
    assert result["snapshot_id"] not in initial_snapshot_ids
    restarted_store = Top5SignalLifecycleSetStore(root=lifecycle_store.root)
    restarted_lifecycles = restarted_store.load_for_scope(
        lifecycle_contract_id=binding.lifecycle_contract_id,
        league_code=fixture.league_code,
        fixture_key=fixture.fixture_key,
        market_id="h2h",
        candidate_id=M5_CANDIDATE_ID,
        model_identity=binding.model_identity,
    )
    assert restarted_lifecycles is not None
    assert (
        top5_h2h_lifecycle_set_digest(restarted_lifecycles)
        == result["lifecycle_set_digest"]
    )
    assert all(
        item.current_version.version_number == 2 for item in restarted_lifecycles
    )
    public_lifecycles = _project_runtime_lifecycle_set(restarted_store, result, fixture)
    assert set(public_lifecycles) == set(TOP5_H2H_OUTCOMES)
    assert {public["lifecycle_version"] for public in public_lifecycles.values()} == {2}
    assert all(
        public["current_probability"] == result["probabilities"][outcome]
        for outcome, public in public_lifecycles.items()
    )
    for outcome, public in public_lifecycles.items():
        before = initial_probabilities[outcome]
        after = result["probabilities"][outcome]
        expected_classification = (
            "STRENGTHENED"
            if after > before
            else "WEAKENED"
            if after < before
            else "UNCHANGED"
        )
        assert public["probability_delta"] == after - before
        assert public["refinement_classification"] == expected_classification
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


@pytest.mark.parametrize(
    "failure_point",
    [None, "during_staging", "before_replace", "after_commit_before_route_verify"],
)
def test_signed_initial_execution_commits_complete_set_atomically_or_recovers(
    tmp_path, monkeypatch, failure_point
):
    (
        _package,
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
    transport = FakeTransport(fixture, initial_time)
    clock_values = iter(
        (
            initial_time,
            initial_time + timedelta(seconds=1),
            initial_time + timedelta(seconds=3),
            initial_time + timedelta(seconds=4),
            initial_time + timedelta(seconds=5),
        )
    )
    set_root = tmp_path / "external-runtime-state" / "lifecycle-sets"
    if failure_point == "during_staging":

        class StagingFailureStore(InstrumentedCanonicalLifecycleSetStore):
            @staticmethod
            def _write_staged_payload(descriptor, encoded):
                os.write(descriptor, encoded[: max(1, len(encoded) // 3)])
                os.close(descriptor)
                raise OSError("injected failure during lifecycle-set staging")

        lifecycle_set_store = StagingFailureStore(set_root)
    elif failure_point == "before_replace":

        class ReplaceFailureStore(InstrumentedCanonicalLifecycleSetStore):
            @staticmethod
            def _replace_staged(_temporary, _path):
                raise OSError("injected failure before lifecycle-set replace")

        lifecycle_set_store = ReplaceFailureStore(set_root)
    else:
        lifecycle_set_store = InstrumentedCanonicalLifecycleSetStore(set_root)
    credential_calls = []
    runtime = Top5OneShotProductionRuntime(
        route_store=DurableTop5ProductionRouteStateStore(
            tmp_path / "initial-route.json"
        ),
        transport=transport,
        lifecycle_set_store=lifecycle_set_store,
        clock=lambda: next(clock_values),
        credential_loader=lambda: credential_calls.append(1) or "injected-test-key",
        budget_available=lambda _now: True,
        budget_success=lambda *_args: None,
    )
    if failure_point == "after_commit_before_route_verify":

        def reject_production_verification(*_args, **_kwargs):
            raise OneShotExecutionError("injected route verification failure")

        runtime.route_store.mark_production_verified = reject_production_verification
    if failure_point is not None:
        with pytest.raises(OneShotExecutionError):
            runtime.execute(
                plan,
                initial_binding,
                verified,
                fixture=fixture,
            )
        assert len(transport.calls) == 1
        assert len(credential_calls) == 1
        set_after_failure = lifecycle_set_store.load_for_scope(
            lifecycle_contract_id=initial_binding.lifecycle_contract_id,
            league_code=fixture.league_code,
            fixture_key=fixture.fixture_key,
            market_id="h2h",
            candidate_id=M5_CANDIDATE_ID,
            model_identity=initial_binding.model_identity,
        )
        if failure_point == "after_commit_before_route_verify":
            assert set_after_failure is not None
            assert all(
                item.current_version.version_number == 1 for item in set_after_failure
            )
            restarted = Top5SignalLifecycleSetStore(root=set_root).load_for_scope(
                lifecycle_contract_id=initial_binding.lifecycle_contract_id,
                league_code=fixture.league_code,
                fixture_key=fixture.fixture_key,
                market_id="h2h",
                candidate_id=M5_CANDIDATE_ID,
                model_identity=initial_binding.model_identity,
            )
            assert restarted is not None
            assert top5_h2h_lifecycle_set_digest(restarted) == (
                top5_h2h_lifecycle_set_digest(set_after_failure)
            )
            assert len(list(set_root.glob("*.json"))) == 1
            retry_transport = FakeTransport(fixture, initial_time)
            retry_credentials = []
            retry_runtime = Top5OneShotProductionRuntime(
                route_store=DurableTop5ProductionRouteStateStore(
                    tmp_path / "initial-route-recovery-blocked.json"
                ),
                transport=retry_transport,
                lifecycle_set_store=Top5SignalLifecycleSetStore(root=set_root),
                clock=lambda: initial_time,
                credential_loader=lambda: (
                    retry_credentials.append(1) or "injected-test-key"
                ),
                budget_available=lambda _now: True,
            )
            with pytest.raises(OneShotExecutionError, match="already exists"):
                retry_runtime.execute(plan, initial_binding, verified, fixture=fixture)
            assert retry_transport.calls == []
            assert retry_credentials == []
        else:
            assert set_after_failure is None
            assert not list(set_root.glob("*.json"))
            assert not list(set_root.glob(".*.tmp"))
        assert (
            runtime.route_store.read()["records"][initial_binding.activation_id][
                "status"
            ]
            == "ROLLED_BACK"
        )
        assert all(
            record.get("status") != "PRODUCTION_VERIFIED"
            for record in runtime.route_store.read()["records"].values()
        )
        if failure_point == "after_commit_before_route_verify":
            return

        # A process restart sees no authoritative partial set. A fresh signed
        # INITIAL attempt can commit the complete set without poisoned members.
        restarted_store = Top5SignalLifecycleSetStore(root=set_root)
        assert (
            restarted_store.load_for_scope(
                lifecycle_contract_id=initial_binding.lifecycle_contract_id,
                league_code=fixture.league_code,
                fixture_key=fixture.fixture_key,
                market_id="h2h",
                candidate_id=M5_CANDIDATE_ID,
                model_identity=initial_binding.model_identity,
            )
            is None
        )
        retry_transport = FakeTransport(fixture, initial_time)
        retry_clock_values = iter(
            (
                initial_time,
                initial_time + timedelta(seconds=1),
                initial_time + timedelta(seconds=3),
                initial_time + timedelta(seconds=4),
            )
        )
        retry_runtime = Top5OneShotProductionRuntime(
            route_store=DurableTop5ProductionRouteStateStore(
                tmp_path / "initial-route-retry.json"
            ),
            transport=retry_transport,
            lifecycle_set_store=Top5SignalLifecycleSetStore(root=set_root),
            clock=lambda: next(retry_clock_values),
            credential_loader=lambda: "injected-test-key",
            budget_available=lambda _now: True,
            budget_success=lambda *_args: None,
        )
        retried = retry_runtime.execute(
            plan, initial_binding, verified, fixture=fixture
        )
        assert retried["lifecycle_versions"] == dict.fromkeys(TOP5_H2H_OUTCOMES, 1)
        assert len(retry_transport.calls) == 1
        return
    result = runtime.execute(plan, initial_binding, verified, fixture=fixture)
    assert result["lifecycle_versions"] == dict.fromkeys(TOP5_H2H_OUTCOMES, 1)
    assert set(result["lifecycle_ids"]) == set(TOP5_H2H_OUTCOMES)
    assert len(set(result["lifecycle_ids"].values())) == 3
    persisted = lifecycle_set_store.load_for_scope(
        lifecycle_contract_id=initial_binding.lifecycle_contract_id,
        league_code=fixture.league_code,
        fixture_key=fixture.fixture_key,
        market_id="h2h",
        candidate_id=M5_CANDIDATE_ID,
        model_identity=initial_binding.model_identity,
    )
    assert persisted is not None
    fresh_store = Top5SignalLifecycleSetStore(root=set_root)
    after_restart = fresh_store.load_for_scope(
        lifecycle_contract_id=initial_binding.lifecycle_contract_id,
        league_code=fixture.league_code,
        fixture_key=fixture.fixture_key,
        market_id="h2h",
        candidate_id=M5_CANDIDATE_ID,
        model_identity=initial_binding.model_identity,
    )
    assert after_restart is not None
    assert (
        top5_h2h_lifecycle_set_digest(after_restart) == result["lifecycle_set_digest"]
    )
    assert all(item.current_version.version_number == 1 for item in after_restart)
    assert lifecycle_set_store.commit_calls == 1
    assert lifecycle_set_store.load_calls >= 2
    assert all(
        lifecycle_set_state_path(
            top5_h2h_lifecycle_set_id(persisted), root=set_root
        ).is_file()
        for lifecycle in persisted
    )
    assert len(list(set_root.glob("*.json"))) == 1
    assert all(
        lifecycle.initial_version.snapshot_id == result["snapshot_id"]
        and dict(lifecycle.initial_version.probabilities) == result["probabilities"]
        for lifecycle in persisted
    )
    assert {item.initial_version.decision_id for item in persisted} == {
        initial_binding.activation_authorization_id
    }
    assert set(_project_runtime_lifecycle_set(fresh_store, result, fixture)) == set(
        TOP5_H2H_OUTCOMES
    )
    with pytest.raises(Top5SignalLifecyclePublicAdapterError):
        _project_runtime_lifecycle_set(
            lifecycle_set_store, result, fixture, lifecycles=persisted[:1]
        )
    assert transport.calls[0][0].lifecycle_stage == "INITIAL"


def _refined_store_lifecycles(lifecycles, fixture):
    prior = lifecycles[0].initial_version
    now = fixture.kickoff - timedelta(minutes=90)
    snapshot = MarketSnapshot(
        fixture_key=fixture.fixture_key,
        captured_at=now - timedelta(seconds=10),
        kind=prior.snapshot_kind,
        source=prior.snapshot_source,
        odds=prior.market_odds,
        snapshot_id="atomic-store-refinement-snapshot",
    )
    probabilities = {"home": 0.45, "draw": 0.30, "away": 0.25}
    refined = []
    for lifecycle in lifecycles:
        outcome = lifecycle.initial_version.outcome_id
        before = lifecycle.current_version.probabilities[outcome]
        after = probabilities[outcome]
        classification = (
            RefinementClassification.STRENGTHENED
            if after > before
            else RefinementClassification.WEAKENED
            if after < before
            else RefinementClassification.UNCHANGED
        )
        refined.append(
            refine_signal(
                lifecycle,
                fixture=fixture,
                snapshot=snapshot,
                now=now,
                probabilities=probabilities,
                eligibility_decision=True,
                withdrawal_authorized=False,
                decision_id="separate-test-refinement-authorization",
                decision_reason="deterministic per-outcome test refinement",
                classification=classification,
            )
        )
    return tuple(refined)


def _canonical_store_initial_set(tmp_path):
    (
        _package,
        _bundle,
        _plan,
        fixture,
        lifecycle,
        binding,
        _signer,
        _public_path,
        _envelope,
        _verified,
    ) = _signed_context(tmp_path)
    initial = _canonical_m5_initial_lifecycles(
        lifecycle,
        fixture,
        inventory_for(binding.activation_league, M5_CANDIDATE_ID).model_artifact_hash,
    )
    return fixture, initial, binding


def test_lifecycle_set_store_identity_initial_commit_and_restart(tmp_path):
    fixture, initial, binding = _canonical_store_initial_set(tmp_path)
    root = tmp_path / "lifecycle-set-store"
    store = Top5SignalLifecycleSetStore(root=root)
    committed = store.commit(initial)
    lifecycle_set_id = top5_h2h_lifecycle_set_id(initial)
    path = lifecycle_set_state_path(lifecycle_set_id, root=root)
    assert path.is_file()
    assert path.stat().st_mode & 0o077 == 0
    assert root.stat().st_mode & 0o077 == 0
    assert len(list(root.glob("*.json"))) == 1
    stored_envelope = json.loads(path.read_text(encoding="utf-8"))
    assert stored_envelope["schema_version"] == "top5-signal-lifecycle-set-store-v1"
    assert stored_envelope["lifecycle_set_id"] == lifecycle_set_id
    assert stored_envelope["outcome_keys"] == list(TOP5_H2H_OUTCOMES)
    assert set(stored_envelope["lifecycles"]) == set(TOP5_H2H_OUTCOMES)
    assert set(stored_envelope["shared_identity"]) == {
        "lifecycle_contract_id",
        "league_code",
        "fixture_key",
        "market_id",
        "candidate_id",
        "model_identity",
    }
    assert top5_h2h_lifecycle_set_id(tuple(reversed(initial))) == lifecycle_set_id
    assert (
        top5_h2h_lifecycle_set_identity_for_scope(
            lifecycle_contract_id=binding.lifecycle_contract_id,
            league_code=fixture.league_code,
            fixture_key=fixture.fixture_key,
            market_id="h2h",
            candidate_id=M5_CANDIDATE_ID,
            model_identity=binding.model_identity,
        )
        == lifecycle_set_id
    )
    replay = store.commit(initial)
    assert top5_h2h_lifecycle_set_digest(replay) == top5_h2h_lifecycle_set_digest(
        committed
    )
    restarted = Top5SignalLifecycleSetStore(root=root)
    loaded = restarted.load(lifecycle_set_id)
    assert loaded is not None
    assert [item.initial_version.outcome_id for item in loaded] == list(
        TOP5_H2H_OUTCOMES
    )
    assert top5_h2h_lifecycle_set_digest(loaded) == top5_h2h_lifecycle_set_digest(
        initial
    )


def test_lifecycle_set_store_refinement_is_one_atomic_append_and_replay(tmp_path):
    fixture, initial, _binding = _canonical_store_initial_set(tmp_path)
    root = tmp_path / "lifecycle-set-store"
    initial_store = Top5SignalLifecycleSetStore(root=root)
    initial_store.commit(initial)
    refined = _refined_store_lifecycles(initial, fixture)
    lifecycle_set_id = top5_h2h_lifecycle_set_id(initial)
    assert top5_h2h_lifecycle_set_id(refined) == lifecycle_set_id

    class ReplaceFailureStore(Top5SignalLifecycleSetStore):
        @staticmethod
        def _replace_staged(_temporary, _path):
            raise OSError("injected failure before set replacement")

    with pytest.raises(OSError, match="before set replacement"):
        ReplaceFailureStore(root=root).commit(refined)
    after_failed_replace = Top5SignalLifecycleSetStore(root=root).load(lifecycle_set_id)
    assert after_failed_replace is not None
    assert all(
        item.current_version.version_number == 1 for item in after_failed_replace
    )
    assert top5_h2h_lifecycle_set_digest(after_failed_replace) == (
        top5_h2h_lifecycle_set_digest(initial)
    )

    committed = Top5SignalLifecycleSetStore(root=root).commit(refined)
    assert all(item.current_version.version_number == 2 for item in committed)
    assert {
        outcome: committed[index].lifecycle_id
        for index, outcome in enumerate(TOP5_H2H_OUTCOMES)
    } == {
        outcome: initial[index].lifecycle_id
        for index, outcome in enumerate(TOP5_H2H_OUTCOMES)
    }
    assert len({item.current_version.snapshot_id for item in committed}) == 1
    assert (
        len({item.current_version.prediction_generated_at for item in committed}) == 1
    )
    replay = Top5SignalLifecycleSetStore(root=root).commit(refined)
    assert top5_h2h_lifecycle_set_digest(replay) == top5_h2h_lifecycle_set_digest(
        committed
    )
    restarted = Top5SignalLifecycleSetStore(root=root).load(lifecycle_set_id)
    assert restarted is not None
    assert all(item.current_version.version_number == 2 for item in restarted)
    with pytest.raises(SignalLifecycleError):
        initial_store.commit(initial)
    mixed = (refined[0], initial[1], initial[2])
    with pytest.raises(SignalLifecycleError):
        initial_store.commit(mixed)


def test_lifecycle_set_store_rejects_symlinked_authoritative_file(tmp_path):
    _fixture, initial, _binding = _canonical_store_initial_set(tmp_path)
    root = tmp_path / "lifecycle-set-store"
    lifecycle_set_id = top5_h2h_lifecycle_set_id(initial)
    target_path = lifecycle_set_state_path(lifecycle_set_id, root=root)
    target_path.parent.mkdir(mode=0o700, parents=True)
    external_target = tmp_path / "redirected-lifecycle-set.json"
    external_target.write_text("{}", encoding="utf-8")
    target_path.symlink_to(external_target)
    with pytest.raises(SignalLifecycleError, match="symlink"):
        Top5SignalLifecycleSetStore(root=root).load(lifecycle_set_id)


def test_lifecycle_set_store_does_not_follow_symlinked_lock(tmp_path):
    _fixture, initial, _binding = _canonical_store_initial_set(tmp_path)
    root = tmp_path / "lifecycle-set-store"
    lifecycle_set_id = top5_h2h_lifecycle_set_id(initial)
    path = lifecycle_set_state_path(lifecycle_set_id, root=root)
    root.mkdir(mode=0o700, parents=True)
    external_lock = tmp_path / "external-lock"
    external_lock.write_text("", encoding="utf-8")
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.symlink_to(external_lock)
    with pytest.raises(OSError):
        Top5SignalLifecycleSetStore(root=root).load(lifecycle_set_id)
    assert external_lock.read_text(encoding="utf-8") == ""


def test_provider_budget_block_refuses_before_credential_and_transport(tmp_path):
    (
        _package,
        plan,
        fixture,
        lifecycle,
        binding,
        verified,
        _selected_candidate_binding,
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
            lifecycles=lifecycle,
        )
    assert transport.calls == []
    assert credential_calls == []


def test_one_shot_default_budget_gate_uses_fresh_quota_contract(tmp_path, monkeypatch):
    observed = []

    def strict_budget_gate(*, now):
        observed.append(now)
        return False

    monkeypatch.setattr(
        provider_budget, "is_top5_provider_available", strict_budget_gate
    )
    runtime = Top5OneShotProductionRuntime(
        route_store=DurableTop5ProductionRouteStateStore(tmp_path / "route.json"),
        lifecycle_set_store=MemoryLifecycleSetStore(),
    )

    assert runtime.budget_available(NOW) is False
    assert observed == [NOW]


@pytest.mark.parametrize(
    "tamper",
    [
        "unsigned",
        "expired",
        "fixture_home",
        "fixture_away",
        "fixture_kickoff",
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
        _selected_candidate_binding,
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
    elif tamper == "fixture_home":
        fixture = replace(fixture, home_team=fixture.home_team + " Other")
    elif tamper == "fixture_away":
        fixture = replace(fixture, away_team=fixture.away_team + " Other")
    elif tamper == "fixture_kickoff":
        fixture = replace(fixture, kickoff=fixture.kickoff + timedelta(minutes=1))
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
            lifecycles=lifecycle,
        )
    assert transport.calls == []
    assert credential_calls == []
    assert runtime.route_store.read()["current_route"]["activation_mode"] == "DISABLED"


def test_lifecycle_state_cli_envelope_requires_exact_canonical_three_set(tmp_path):
    (
        *_,
        lifecycles,
        _binding,
        _verified,
        _selected,
        _store,
        _transport,
        _runtime,
        _creds,
        _ok,
        _fail,
    ) = _runtime_context(tmp_path)
    envelope = top5_h2h_lifecycle_set_payload(lifecycles)
    parsed = parse_top5_h2h_lifecycle_set(envelope)
    assert [item.initial_version.outcome_id for item in parsed] == list(
        TOP5_H2H_OUTCOMES
    )
    for invalid in (
        {**envelope, "schema_version": "wrong"},
        {**envelope, "unexpected": True},
        {**envelope, "lifecycles": envelope["lifecycles"][:2]},
        {
            **envelope,
            "lifecycles": [
                envelope["lifecycles"][0],
                envelope["lifecycles"][0],
                envelope["lifecycles"][2],
            ],
        },
        {
            **envelope,
            "lifecycles": [*envelope["lifecycles"], envelope["lifecycles"][0]],
        },
    ):
        with pytest.raises(SignalLifecycleError):
            parse_top5_h2h_lifecycle_set(invalid)


@pytest.mark.parametrize(
    "mismatch",
    [
        "missing_home",
        "missing_draw",
        "missing_away",
        "duplicate",
        "extra",
        "mixed_fixture",
        "mixed_candidate",
        "mixed_model",
        "mixed_source",
        "mixed_research",
        "mixed_artifact",
    ],
)
def test_incomplete_or_mixed_lifecycle_set_rejects_before_request(tmp_path, mismatch):
    (
        _package,
        plan,
        fixture,
        lifecycles,
        binding,
        verified,
        _selected,
        store,
        transport,
        runtime,
        credential_calls,
        _successes,
        _failures,
    ) = _runtime_context(tmp_path)
    supplied = list(lifecycles)
    if mismatch.startswith("missing_"):
        supplied = [
            lifecycle
            for lifecycle in supplied
            if lifecycle.initial_version.outcome_id != mismatch.removeprefix("missing_")
        ]
    elif mismatch == "duplicate":
        supplied[-1] = supplied[0]
    elif mismatch == "extra":
        supplied.append(supplied[0])
    elif mismatch == "mixed_fixture":
        other_fixture = replace(fixture, fixture_key=fixture.fixture_key + "-other")
        supplied[0] = _clone_initial_lifecycle(supplied[0], other_fixture)
    elif mismatch == "mixed_candidate":
        supplied[0] = _clone_initial_lifecycle(
            supplied[0], fixture, candidate_id="M5_other_candidate"
        )
    elif mismatch == "mixed_model":
        supplied[0] = _clone_initial_lifecycle(
            supplied[0], fixture, model_identity="M5_other_model"
        )
    elif mismatch == "mixed_source":
        supplied[0] = _clone_initial_lifecycle(
            supplied[0], fixture, source_sha="2" * 40
        )
    elif mismatch == "mixed_research":
        supplied[0] = _clone_initial_lifecycle(
            supplied[0], fixture, research_sha="3" * 40
        )
    elif mismatch == "mixed_artifact":
        supplied[0] = _clone_initial_lifecycle(
            supplied[0], fixture, model_artifact_hash="4" * 64
        )
    with pytest.raises(OneShotExecutionError):
        runtime.execute(
            plan,
            binding,
            verified,
            fixture=fixture,
            lifecycles=tuple(supplied),
        )
    assert transport.calls == []
    assert credential_calls == []
    assert store.commit_calls == 0


def test_supplied_lifecycle_must_match_durable_store_exactly(tmp_path):
    (
        _package,
        plan,
        fixture,
        lifecycles,
        binding,
        verified,
        _selected,
        store,
        transport,
        runtime,
        credential_calls,
        _successes,
        _failures,
    ) = _runtime_context(tmp_path)
    replacement = _clone_initial_lifecycle(
        lifecycles[0], fixture, decision_reason="durable mismatch"
    )
    store.values[replacement.lifecycle_id] = replacement
    with pytest.raises(OneShotExecutionError, match="lifecycle"):
        runtime.execute(
            plan,
            binding,
            verified,
            fixture=fixture,
            lifecycles=lifecycles,
        )
    assert transport.calls == []
    assert credential_calls == []


@pytest.mark.parametrize("withdrawn", [False, True])
def test_refinement_rejects_advanced_or_withdrawn_lifecycle_set_before_request(
    tmp_path, withdrawn
):
    (
        _package,
        plan,
        fixture,
        initial_lifecycles,
        binding,
        verified,
        _selected,
        _store,
        transport,
        runtime,
        credential_calls,
        _successes,
        _failures,
    ) = _runtime_context(tmp_path)
    snapshot = MarketSnapshot(
        fixture.fixture_key,
        NOW - timedelta(seconds=10),
        initial_lifecycles[0].initial_version.snapshot_kind,
        "the_odds_api:top5",
        {"home": 2.1, "draw": 3.5, "away": 3.7},
        "advanced-lifecycle-snapshot",
    )
    supplied = tuple(
        refine_signal(
            lifecycle,
            fixture=fixture,
            snapshot=snapshot,
            now=NOW,
            probabilities={"home": 0.4, "draw": 0.3, "away": 0.3},
            eligibility_decision=not withdrawn,
            withdrawal_authorized=withdrawn,
            decision_id="offline-advanced-lifecycle",
            decision_reason="offline lifecycle-set rejection test",
            classification=(
                RefinementClassification.WITHDRAWN
                if withdrawn
                else RefinementClassification.STRENGTHENED
            ),
        )
        for lifecycle in initial_lifecycles
    )
    with pytest.raises(OneShotExecutionError):
        runtime.execute(
            plan,
            binding,
            verified,
            fixture=fixture,
            lifecycles=supplied,
        )
    assert transport.calls == []
    assert credential_calls == []


def test_not_due_refinement_refuses_before_credential_and_provider(tmp_path):
    (
        _package,
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
    lifecycles = _canonical_m5_initial_lifecycles(
        lifecycle,
        fixture,
        inventory_for(binding.activation_league, M5_CANDIDATE_ID).model_artifact_hash,
    )
    transport = FakeTransport(fixture, NOW)
    credential_calls = []
    runtime = Top5OneShotProductionRuntime(
        route_store=DurableTop5ProductionRouteStateStore(tmp_path / "not-due.json"),
        transport=transport,
        lifecycle_set_store=MemoryLifecycleSetStore(lifecycles),
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
            lifecycles=lifecycles,
        )
    assert transport.calls == []
    assert credential_calls == []


@pytest.mark.parametrize(
    "failure",
    [
        "http",
        "malformed_json",
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
        _package,
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
    lifecycles = _canonical_m5_initial_lifecycles(
        lifecycle,
        fixture,
        inventory_for(binding.activation_league, M5_CANDIDATE_ID).model_artifact_hash,
    )
    payload = None
    status = 200
    payload_valid = True
    fail_commit = failure == "lifecycle"
    fail_health = failure == "health"
    if failure == "http":
        status = 403
    elif failure == "malformed_json":
        payload_valid = False
    elif failure == "missing_fixture":
        payload = []
    elif failure == "ambiguous_fixture":
        payload = FakeTransport(fixture, NOW).payload * 2
    elif failure == "stale_odds":
        payload = FakeTransport(fixture, NOW - timedelta(hours=1)).payload
    elif failure == "closing_odds":
        payload = FakeTransport(fixture, NOW).payload
        for bookmaker in payload[0]["bookmakers"]:
            bookmaker["markets"][0]["last_update"] = (
                fixture.kickoff + timedelta(minutes=1)
            ).isoformat()
    elif failure == "missing_bookmaker":
        payload = FakeTransport(
            fixture,
            NOW,
            payload=[
                {
                    "id": ODDS_API_PROVIDER_EVENT_ID,
                    "sport_key": "soccer_germany_bundesliga",
                    "commence_time": fixture.kickoff.isoformat(),
                    "home_team": fixture.home_team,
                    "away_team": fixture.away_team,
                    "bookmakers": [],
                }
            ],
        ).payload
    elif failure == "malformed_1x2":
        payload = FakeTransport(fixture, NOW).payload
        for bookmaker in payload[0]["bookmakers"]:
            bookmaker["markets"][0]["outcomes"] = [
                outcome
                for outcome in bookmaker["markets"][0]["outcomes"]
                if outcome["name"] != "Draw"
            ]
    if failure in {"health", "lifecycle"}:
        if failure == "lifecycle":
            lifecycle_set_root = tmp_path / "post-commit-route-failure-set"
            Top5SignalLifecycleSetStore(root=lifecycle_set_root).commit(lifecycles)

            class RefinementReplaceFailureStore(InstrumentedCanonicalLifecycleSetStore):
                @staticmethod
                def _replace_staged(_temporary, _path):
                    raise OSError("injected refinement failure before replace")

            lifecycle_store = RefinementReplaceFailureStore(lifecycle_set_root)
        else:
            lifecycle_store = InstrumentedCanonicalLifecycleSetStore(
                tmp_path / "post-commit-route-failure-set"
            )
            lifecycle_store.commit(lifecycles)
    else:
        lifecycle_store = MemoryLifecycleSetStore(lifecycles, fail_commit=fail_commit)
    transport = FakeTransport(
        fixture,
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
        lifecycle_set_store=lifecycle_store,
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
            lifecycles=lifecycles,
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
    if failure == "lifecycle":
        unchanged = Top5SignalLifecycleSetStore(
            root=lifecycle_store.root
        ).load_for_scope(
            lifecycle_contract_id=binding.lifecycle_contract_id,
            league_code=fixture.league_code,
            fixture_key=fixture.fixture_key,
            market_id="h2h",
            candidate_id=M5_CANDIDATE_ID,
            model_identity=binding.model_identity,
        )
        assert unchanged is not None
        assert all(item.current_version.version_number == 1 for item in unchanged)
        assert top5_h2h_lifecycle_set_digest(unchanged) == (
            top5_h2h_lifecycle_set_digest(lifecycles)
        )
    if failure == "health":
        persisted_after_restart = Top5SignalLifecycleSetStore(
            root=lifecycle_store.root
        ).load_for_scope(
            lifecycle_contract_id=binding.lifecycle_contract_id,
            league_code=fixture.league_code,
            fixture_key=fixture.fixture_key,
            market_id="h2h",
            candidate_id=M5_CANDIDATE_ID,
            model_identity=binding.model_identity,
        )
        assert persisted_after_restart is not None
        assert all(
            item.current_version.version_number == 2 for item in persisted_after_restart
        )
        assert len(transport.calls) == 1
        runtime.clock = lambda: NOW + timedelta(seconds=10)
        with pytest.raises(OneShotExecutionError):
            runtime.execute(
                plan,
                binding,
                verified,
                fixture=fixture,
                lifecycles=lifecycles,
            )
        assert len(transport.calls) == 1
        assert len(credential_calls) == 1


@pytest.mark.parametrize(
    "mismatch",
    [
        "wrong_kickoff",
        "wrong_home",
        "wrong_away",
        "reversed_teams",
        "wrong_sport",
        "no_match",
        "multiple_matches",
        "missing_id",
        "blank_id",
    ],
)
def test_odds_api_fixture_mismatch_fails_after_exactly_one_request(tmp_path, mismatch):
    (
        _package,
        plan,
        fixture,
        lifecycle,
        binding,
        verified,
        _selected_candidate_binding,
        _store,
        transport,
        runtime,
        credential_calls,
        _successes,
        _failures,
    ) = _runtime_context(tmp_path)
    event = dict(transport.payload[0])
    if mismatch == "wrong_kickoff":
        event["commence_time"] = (fixture.kickoff + timedelta(minutes=1)).isoformat()
    elif mismatch == "wrong_home":
        event["home_team"] = "Different Home"
    elif mismatch == "wrong_away":
        event["away_team"] = "Different Away"
    elif mismatch == "reversed_teams":
        event["home_team"], event["away_team"] = (
            event["away_team"],
            event["home_team"],
        )
    elif mismatch == "wrong_sport":
        event["sport_key"] = "soccer_wrong_league"
    elif mismatch == "no_match":
        transport.payload = []
    elif mismatch == "multiple_matches":
        transport.payload = [event, dict(event)]
    elif mismatch == "missing_id":
        event.pop("id")
    elif mismatch == "blank_id":
        event["id"] = "  "
    if mismatch not in {"no_match", "multiple_matches"}:
        transport.payload = [event]

    with pytest.raises(OneShotExecutionError):
        runtime.execute(
            plan,
            binding,
            verified,
            fixture=fixture,
            lifecycles=lifecycle,
        )

    assert len(transport.calls) == 1
    assert len(transport.results) == 1
    assert transport.results[0].request_count == 1
    assert transport.results[0].retry_count == 0
    assert len(credential_calls) == 1
    assert (
        runtime.route_store.read()["records"][binding.activation_id]["status"]
        == "ROLLED_BACK"
    )


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
        _selected_candidate_binding,
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
            lifecycles=lifecycle,
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
        _selected_candidate_binding,
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
        lifecycles=lifecycle,
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
        _selected_candidate_binding,
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
            lifecycles=lifecycle,
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
        _selected_candidate_binding,
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
            lifecycles=lifecycle,
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
            lifecycles=lifecycle,
        )
    assert len(transport.calls) == 1
    assert len(credential_calls) == 1
