"""Fail-closed orchestration for one explicitly authorized five-league canary.

This module is manual-call-only. It is not imported by a scheduler, GET route,
PWA, monitoring path, or provider discovery path. Provider work is delegated to
the existing one-shot runtime; public delivery is delegated to the existing
Top-5 committed-batch storage and canonical delivery executor.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.football.production_contracts import Fixture, ProductionContractError, _utc
from src.football.top5_activation_authorization import (
    Top5ActivationExecutionBindingV1,
    VerifiedTop5ActivationAuthorizationV1,
)
from src.football.top5_canary_batch_storage import (
    Top5CanaryBatchState,
    Top5CanaryBatchStorageError,
    Top5CanaryBatchStore,
    execute_stored_top5_batch,
)
from src.football.top5_durable_activation import Top5DurableActivationPlanV1
from src.football.top5_one_shot_runtime import (
    ONE_SHOT_RESULT_SCHEMA,
    Top5OneShotProductionRuntime,
    one_shot_capture_digest,
)
from src.football.top5_public_delivery import (
    Top5CanonicalDeliveryAdapter,
    Top5DeliveryAttestation,
    Top5DeliveryExecutor,
)
from src.football.top5_publisher import (
    TOP5_PUBLIC_RELEASE_LEAGUES,
    ControlledTop5PublicationPayload,
    InMemoryTop5PublicationStore,
    PublishedTop5BatchArtifact,
    Top5PublicationAuthorization,
)
from src.football.top5_research_binding import M5_CANDIDATE_ID
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_signal_lifecycle import (
    Top5SignalLifecycle,
    canonical_top5_h2h_lifecycle_set,
    top5_h2h_lifecycle_set_digest,
)
from src.signals import provider_budget

_LEAGUES = tuple(TOP5_PUBLIC_RELEASE_LEAGUES)
_OUTCOMES = ("home", "draw", "away")
_EVENT_LOG = logging.getLogger("sportsbrain.top5_canary")
_PUBLICATION_AUTH_FIELDS = (
    "activation_id",
    "league_code",
    "candidate_id",
    "model_identity",
    "source_sha",
    "research_sha",
    "model_artifact_hash",
    "signal_time_experiment_id",
)


class Top5CanaryOrchestrationError(ProductionContractError):
    """The five-fixture canary was incomplete or failed a required gate."""


class _CountingOneShotTransport:
    """Count and cap calls crossing the existing one-shot transport seam."""

    def __init__(self, delegate: object, on_request: Callable[[], None]) -> None:
        self.delegate = delegate
        self.on_request = on_request
        self.request_count = 0

    def request(self, *args: object, **kwargs: object) -> object:
        self.request_count += 1
        if self.request_count > 1:
            raise Top5CanaryOrchestrationError(
                "a fixture attempted more than one provider request"
            )
        self.on_request()
        return self.delegate.request(*args, **kwargs)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Top5OneShotFixtureRun:
    runtime: Top5OneShotProductionRuntime
    plan: Top5DurableActivationPlanV1
    binding: Top5ActivationExecutionBindingV1
    authorization: VerifiedTop5ActivationAuthorizationV1
    fixture: Fixture
    lifecycles: Sequence[Top5SignalLifecycle] | None = None


@dataclass(frozen=True)
class Top5CanaryCapture:
    artifact: PublishedTop5BatchArtifact
    provider_request_count: int
    retry_count: int
    resumed_fixture_count: int
    fixture_identities: Mapping[str, str]
    capture_digests: Mapping[str, str]


def _emit(
    event_sink: Callable[[Mapping[str, object]], None] | None,
    event: str,
    **fields: object,
) -> None:
    payload = {"schema_version": "top5-canary-orchestration-event-v1", "event": event}
    payload.update(fields)
    if event_sink is not None:
        event_sink(payload)
    else:
        _EVENT_LOG.info("%s", json.dumps(payload, sort_keys=True, allow_nan=False))


def _failure_reason_code(exc: Exception) -> str:
    if isinstance(exc, Top5CanaryBatchStorageError):
        return "BATCH_STORAGE_REJECTED"
    if isinstance(exc, Top5CanaryOrchestrationError):
        return "ORCHESTRATION_GUARD_REJECTED"
    if isinstance(exc, ProductionContractError):
        return "PRODUCTION_CONTRACT_REJECTED"
    return "RUNTIME_OR_PROVIDER_FAILURE"


def _validate_runs(
    runs: Sequence[Top5OneShotFixtureRun], *, now: datetime
) -> tuple[Top5OneShotFixtureRun, ...]:
    if (
        not isinstance(runs, Sequence)
        or isinstance(runs, (str, bytes))
        or len(runs) != 5
    ):
        raise Top5CanaryOrchestrationError("exactly five fixture runs are required")
    ordered = tuple(sorted(runs, key=lambda run: run.fixture.league_code))
    if tuple(run.fixture.league_code for run in ordered) != tuple(sorted(_LEAGUES)):
        raise Top5CanaryOrchestrationError(
            "fixture runs must contain EPL, BL1, LL, SA and L1 exactly once"
        )
    if len({run.fixture.fixture_key for run in ordered}) != 5:
        raise Top5CanaryOrchestrationError("fixture identities must be unique")
    if len({run.binding.activation_id for run in ordered}) != 1:
        raise Top5CanaryOrchestrationError(
            "all five fixtures must use the same explicitly authorized activation"
        )
    if len({run.binding.lifecycle_stage for run in ordered}) != 1:
        raise Top5CanaryOrchestrationError(
            "mixed lifecycle stages are not one canary batch"
        )

    route_roots: set[str] = set()
    for run in ordered:
        if not isinstance(run.runtime, Top5OneShotProductionRuntime):
            raise Top5CanaryOrchestrationError("canonical one-shot runtime is required")
        run.fixture.validate()
        run.plan.validate(now=now)
        run.authorization.assert_valid_at(now)
        if (
            run.plan.activation_id != run.binding.activation_id
            or run.plan.activation_league != run.fixture.league_code
            or run.binding.activation_league != run.fixture.league_code
            or run.binding.fixture_key != run.fixture.fixture_key
            or make_fixture_key(
                run.fixture.league_code,
                run.fixture.home_team,
                run.fixture.away_team,
                run.fixture.kickoff,
            )
            != run.fixture.fixture_key
            or run.binding.provider_authority != "the_odds_api"
            or run.binding.retry_budget != 0
            or run.binding.model_identity != run.plan.model_identity
            or run.binding.source_sha != run.plan.source_sha
            or run.binding.research_sha != run.plan.research_sha
            or run.binding.model_artifact_hash != run.plan.model_artifact_hash
            or run.binding.lifecycle_stage not in {"INITIAL", "REFINEMENT"}
        ):
            raise Top5CanaryOrchestrationError(
                "fixture, one-shot binding, and signed activation do not agree"
            )
        route_root = str(Path(run.runtime.route_store.path).resolve())
        if route_root in route_roots:
            raise Top5CanaryOrchestrationError(
                "each fixture requires isolated durable route state"
            )
        route_roots.add(route_root)
    return ordered


def _validate_publication_inputs(
    runs: Sequence[Top5OneShotFixtureRun],
    *,
    publication_authorizations: Mapping[str, Top5PublicationAuthorization],
    activation_bindings: Mapping[str, Mapping[str, object]],
    now: datetime,
) -> None:
    if set(publication_authorizations) != set(_LEAGUES) or set(
        activation_bindings
    ) != set(_LEAGUES):
        raise Top5CanaryOrchestrationError(
            "five publication authorizations and active bindings are required"
        )
    common: dict[str, object] = {}
    for run in runs:
        league = run.fixture.league_code
        authorization = publication_authorizations[league]
        activation = activation_bindings[league]
        authorization.validate(now=now)
        expected = {
            "activation_id": run.binding.activation_id,
            "league_code": league,
            "candidate_id": M5_CANDIDATE_ID,
            "model_identity": run.binding.model_identity,
            "source_sha": run.binding.source_sha,
            "research_sha": run.binding.research_sha,
            "model_artifact_hash": run.binding.model_artifact_hash,
            "signal_time_experiment_id": run.binding.signal_time_approval_identity,
        }
        if any(getattr(authorization, key) != value for key, value in expected.items()):
            raise Top5CanaryOrchestrationError(
                "publication authorization differs from signed one-shot scope"
            )
        if activation.get("active") is not True or any(
            activation.get(key) != value for key, value in expected.items()
        ):
            raise Top5CanaryOrchestrationError(
                "active publication binding differs from signed one-shot scope"
            )
        for key in (
            "provider_authority",
            "result_authority",
            "evidence_digest",
            "controlled_shadow_run_id",
            "qualification_session_id",
        ):
            value = activation.get(key)
            if not isinstance(value, str) or not value.strip():
                raise Top5CanaryOrchestrationError(
                    f"active publication binding is missing {key}"
                )
        if activation.get("provider_authority") != "the_odds_api":
            raise Top5CanaryOrchestrationError(
                "active publication provider authority is not canonical"
            )
        for key in (
            "activation_id",
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
        ):
            value = activation.get(key)
            if key in common and common[key] != value:
                raise Top5CanaryOrchestrationError(
                    f"five-fixture publication binding is inconsistent: {key}"
                )
            common[key] = value


def _recover_capture(
    run: Top5OneShotFixtureRun, *, now: datetime
) -> tuple[dict[str, object], tuple[Top5SignalLifecycle, ...]] | None:
    state = run.runtime.route_store.read()
    record = state["records"].get(run.binding.activation_id)
    if record is None:
        return None
    if not isinstance(record, Mapping):
        raise Top5CanaryOrchestrationError("private route evidence is malformed")
    status = record.get("status")
    evidence = record.get("production_evidence")
    if (
        status not in {"PRODUCTION_VERIFIED", "ROLLED_BACK"}
        or not isinstance(evidence, Mapping)
        or record.get("activation_plan_digest") != run.binding.activation_plan_digest
        or evidence.get("signed_authorization_digest")
        != run.authorization.authorization_digest
        or evidence.get("authorization_nonce") != run.authorization.nonce
        or evidence.get("league") != run.fixture.league_code
        or evidence.get("fixture_key") != run.fixture.fixture_key
        or evidence.get("provider_authority") != "the_odds_api"
        or evidence.get("provider_request_count") != 1
        or evidence.get("retry_count") != 0
        or evidence.get("no_bet") is not True
        or evidence.get("publication") is not False
        or evidence.get("betting") is not False
        or evidence.get("ledger_mutation") is not False
    ):
        # A route with uncertain request outcome is never automatically replayed.
        raise Top5CanaryOrchestrationError(
            "existing route evidence is incomplete; automatic provider replay is refused"
        )
    recovered = dict(evidence)
    recovered["schema_version"] = ONE_SHOT_RESULT_SCHEMA
    if one_shot_capture_digest(recovered) != recovered.get("capture_result_digest"):
        raise Top5CanaryOrchestrationError("private capture evidence digest is invalid")
    try:
        captured_at = datetime.fromisoformat(
            str(recovered["captured_at"]).replace("Z", "+00:00")
        )
        captured_at = _utc(captured_at, "captured_at")
    except (TypeError, ValueError, ProductionContractError) as exc:
        raise Top5CanaryOrchestrationError(
            "private capture timestamp is invalid"
        ) from exc
    if (now - captured_at).total_seconds() < 0 or (
        now - captured_at
    ).total_seconds() > 900:
        raise Top5CanaryOrchestrationError("resumed capture odds are no longer fresh")

    lifecycles = run.runtime.lifecycle_set_store.load_for_scope(
        lifecycle_contract_id=run.binding.lifecycle_contract_id,
        league_code=run.fixture.league_code,
        fixture_key=run.fixture.fixture_key,
        market_id="h2h",
        candidate_id=M5_CANDIDATE_ID,
        model_identity=run.binding.model_identity,
    )
    if lifecycles is None:
        raise Top5CanaryOrchestrationError("private lifecycle evidence is missing")
    ordered_lifecycles = canonical_top5_h2h_lifecycle_set(lifecycles)
    if top5_h2h_lifecycle_set_digest(
        tuple(ordered_lifecycles.values())
    ) != recovered.get("lifecycle_set_digest"):
        raise Top5CanaryOrchestrationError("private lifecycle evidence digest differs")
    expected_stage = (
        "INITIAL" if run.binding.lifecycle_stage == "INITIAL" else "REFINED"
    )
    if any(
        ordered_lifecycles[outcome].current_version.stage.value != expected_stage
        for outcome in _OUTCOMES
    ):
        raise Top5CanaryOrchestrationError(
            "resumed lifecycle stage differs from the authorized batch"
        )
    if status == "PRODUCTION_VERIFIED":
        run.runtime.rollback_execution(run.binding)
    else:
        run.runtime.consumer.verify_disabled_after_rollback(
            run.binding.activation_id, run.binding.activation_plan_digest
        )
    return recovered, tuple(ordered_lifecycles[outcome] for outcome in _OUTCOMES)


def _validate_result(
    result: Mapping[str, object],
    run: Top5OneShotFixtureRun,
    *,
    now: datetime,
) -> None:
    http_status = result.get("http_status")
    probabilities = result.get("probabilities")
    odds = result.get("odds")
    if not isinstance(probabilities, Mapping) or set(probabilities) != set(_OUTCOMES):
        raise Top5CanaryOrchestrationError(
            "one-shot model probabilities are incomplete"
        )
    normalized_probabilities: list[float] = []
    for outcome in _OUTCOMES:
        value = probabilities[outcome]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Top5CanaryOrchestrationError(
                "one-shot model probabilities are malformed"
            )
        probability = float(value)
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise Top5CanaryOrchestrationError(
                "one-shot model probabilities are invalid"
            )
        normalized_probabilities.append(probability)
    if not math.isclose(sum(normalized_probabilities), 1.0, abs_tol=1e-6):
        raise Top5CanaryOrchestrationError(
            "one-shot model probabilities do not sum to one"
        )
    if not isinstance(odds, Mapping) or set(odds) != set(_OUTCOMES):
        raise Top5CanaryOrchestrationError("one-shot 1X2 odds are incomplete")
    for outcome in _OUTCOMES:
        value = odds[outcome]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Top5CanaryOrchestrationError("one-shot 1X2 odds are malformed")
        price = float(value)
        if not math.isfinite(price) or price <= 1:
            raise Top5CanaryOrchestrationError("one-shot 1X2 odds are invalid")
    digest_fields = ("response_digest", "capture_result_digest", "lifecycle_set_digest")
    if any(
        not isinstance(result.get(name), str)
        or len(str(result.get(name))) != 64
        or any(character not in "0123456789abcdef" for character in str(result[name]))
        for name in digest_fields
    ):
        raise Top5CanaryOrchestrationError("one-shot evidence digest is malformed")
    provider_event_id = result.get("provider_event_id")
    if (
        not isinstance(provider_event_id, str)
        or not provider_event_id
        or provider_event_id != provider_event_id.strip()
        or len(provider_event_id) > 256
        or any(not character.isprintable() for character in provider_event_id)
    ):
        raise Top5CanaryOrchestrationError("provider event identity is malformed")
    if (
        result.get("schema_version") != ONE_SHOT_RESULT_SCHEMA
        or result.get("activation_id") != run.binding.activation_id
        or result.get("activation_plan_digest") != run.binding.activation_plan_digest
        or result.get("signed_authorization_digest")
        != run.authorization.authorization_digest
        or result.get("league") != run.fixture.league_code
        or result.get("fixture_key") != run.fixture.fixture_key
        or result.get("provider_authority") != "the_odds_api"
        or result.get("request_shape_digest") != run.binding.request_shape_digest
        or result.get("model_identity") != run.binding.model_identity
        or result.get("source_sha") != run.binding.source_sha
        or result.get("research_sha") != run.binding.research_sha
        or result.get("model_artifact_hash") != run.binding.model_artifact_hash
        or result.get("candidate_id") != M5_CANDIDATE_ID
        or result.get("provider_request_count") != 1
        or result.get("retry_count") != 0
        or isinstance(http_status, bool)
        or not isinstance(http_status, int)
        or not 200 <= http_status < 300
        or result.get("no_bet") is not True
        or result.get("publication") is not False
        or result.get("recurring_scheduler") is not False
        or result.get("betting") is not False
        or result.get("ledger_mutation") is not False
        or not isinstance(result.get("signal_decision"), Mapping)
        or result.get("capture_result_digest") != one_shot_capture_digest(result)
    ):
        raise Top5CanaryOrchestrationError("one-shot result violates canary contract")
    try:
        captured_at = _utc(
            datetime.fromisoformat(str(result["captured_at"]).replace("Z", "+00:00")),
            "captured_at",
        )
        execution_started = _utc(
            datetime.fromisoformat(
                str(result["execution_started_at"]).replace("Z", "+00:00")
            ),
            "execution_started_at",
        )
        request_started = _utc(
            datetime.fromisoformat(
                str(result["request_started_at"]).replace("Z", "+00:00")
            ),
            "request_started_at",
        )
        response_finished = _utc(
            datetime.fromisoformat(
                str(result["response_finished_at"]).replace("Z", "+00:00")
            ),
            "response_finished_at",
        )
        execution_finished = _utc(
            datetime.fromisoformat(
                str(result["execution_finished_at"]).replace("Z", "+00:00")
            ),
            "execution_finished_at",
        )
    except (TypeError, ValueError, ProductionContractError) as exc:
        raise Top5CanaryOrchestrationError(
            "one-shot capture timestamp is malformed"
        ) from exc
    if (
        not execution_started
        <= request_started
        <= response_finished
        <= execution_finished
    ):
        raise Top5CanaryOrchestrationError(
            "one-shot execution timestamps are inconsistent"
        )
    capture_age = (now - captured_at).total_seconds()
    if capture_age < 0:
        raise Top5CanaryOrchestrationError(
            "canary odds are future-dated at batch validation"
        )
    if capture_age > 900:
        raise Top5CanaryOrchestrationError("canary odds are stale at batch validation")


def _build_publication_payloads(
    captured: Mapping[
        str,
        tuple[
            Top5OneShotFixtureRun, Mapping[str, object], tuple[Top5SignalLifecycle, ...]
        ],
    ],
    *,
    publication_authorizations: Mapping[str, Top5PublicationAuthorization],
    activation_bindings: Mapping[str, Mapping[str, object]],
    now: datetime,
) -> tuple[tuple[ControlledTop5PublicationPayload, ...], datetime]:
    if set(publication_authorizations) != set(_LEAGUES) or set(
        activation_bindings
    ) != set(_LEAGUES):
        raise Top5CanaryOrchestrationError(
            "five explicit publication authorizations and activation bindings are required"
        )
    payloads: list[ControlledTop5PublicationPayload] = []
    artifact_times: list[datetime] = []
    for league in _LEAGUES:
        run, result, lifecycles = captured[league]
        auth = publication_authorizations[league]
        auth.validate(now=now)
        activation = activation_bindings[league]
        if activation.get("active") is not True:
            raise Top5CanaryOrchestrationError(
                "publication requires an already active explicit authorization"
            )
        expected = {
            "activation_id": run.binding.activation_id,
            "league_code": league,
            "candidate_id": result["candidate_id"],
            "model_identity": result["model_identity"],
            "source_sha": result["source_sha"],
            "research_sha": result["research_sha"],
            "model_artifact_hash": result["model_artifact_hash"],
            "signal_time_experiment_id": run.binding.signal_time_approval_identity,
        }
        if any(activation.get(name) != value for name, value in expected.items()):
            raise Top5CanaryOrchestrationError(
                "active publication binding differs from one-shot evidence"
            )
        for name in (
            "result_authority",
            "evidence_digest",
            "controlled_shadow_run_id",
            "qualification_session_id",
        ):
            if (
                not isinstance(activation.get(name), str)
                or not activation[name].strip()
            ):
                raise Top5CanaryOrchestrationError(
                    f"active publication binding is missing {name}"
                )
        for name in _PUBLICATION_AUTH_FIELDS:
            actual = (
                activation["activation_id"]
                if name == "activation_id"
                else activation.get(name)
            )
            if getattr(auth, name) != actual:
                raise Top5CanaryOrchestrationError(
                    f"publication authorization does not bind {name}"
                )
        fixture = run.fixture
        decision = result["signal_decision"]
        prediction_at = _utc(
            datetime.fromisoformat(
                str(result["prediction_timestamp"]).replace("Z", "+00:00")
            ),
            "prediction_timestamp",
        )
        captured_at = _utc(
            datetime.fromisoformat(str(result["captured_at"]).replace("Z", "+00:00")),
            "captured_at",
        )
        result_authority = str(activation["result_authority"])
        evidence_digest = str(activation["evidence_digest"])
        run_id = str(activation["controlled_shadow_run_id"])
        qualification_id = str(activation["qualification_session_id"])
        record: dict[str, object] = {
            "fixture": {
                "fixture_key": fixture.fixture_key,
                "home_team": fixture.home_team,
                "away_team": fixture.away_team,
                "kickoff": fixture.kickoff.isoformat(),
            },
            "fixture_key": fixture.fixture_key,
            "league": league,
            "model_identity": result["model_identity"],
            "model_version": result["model_artifact_hash"],
            "source_sha": result["source_sha"],
            "research_sha": result["research_sha"],
            "signal_time_experiment_id": run.binding.signal_time_approval_identity,
            "activation_id": run.binding.activation_id,
            "activation_authorization_id": result["signed_authorization_digest"],
            "evidence_digest": evidence_digest,
            "provider_response_digest": result["response_digest"],
            "capture_result_digest": result["capture_result_digest"],
            "http_status": result["http_status"],
            "provider_event_id": result["provider_event_id"],
            "controlled_shadow_run_id": run_id,
            "qualification_session_id": qualification_id,
            "result_authority": result_authority,
            "prediction_id": result["prediction_id"],
            "prediction_timestamp": prediction_at.isoformat(),
            "signal_timestamp": captured_at.isoformat(),
            "captured_at": captured_at.isoformat(),
            "snapshot_id": result["snapshot_id"],
            "snapshot_source": result["snapshot_source"],
            "probabilities": dict(result["probabilities"]),
            "odds": dict(result["odds"]),
            "top5_signal_decision": dict(decision),
            "top5_signal_lifecycles": lifecycles,
            "no_bet": True,
            "closing_used_for_prediction": False,
        }
        payload = ControlledTop5PublicationPayload(
            artifact_path=f"docs/data/top5/published/{league}/canary.json",
            activation_id=run.binding.activation_id,
            league_code=league,
            candidate_id=str(result["candidate_id"]),
            model_identity=str(result["model_identity"]),
            signal_time_experiment_id=run.binding.signal_time_approval_identity,
            source_sha=str(result["source_sha"]),
            research_sha=str(result["research_sha"]),
            model_artifact_hash=str(result["model_artifact_hash"]),
            provider_authority="the_odds_api",
            result_authority=result_authority,
            evidence_digest=evidence_digest,
            controlled_shadow_run_id=run_id,
            qualification_session_id=qualification_id,
            generated_at=prediction_at,
            football_records=(record,),
            health={"activation_state": "controlled", "no_bet": True},
            no_bet=True,
            publication_enabled=True,
        )
        payload.validate()
        auth.binds(payload)
        payloads.append(payload)
        artifact_times.append(
            _utc(
                datetime.fromisoformat(
                    str(result["execution_finished_at"]).replace("Z", "+00:00")
                ),
                "execution_finished_at",
            )
        )
        artifact_times.append(auth.issued_at)
    if len({payload.activation_id for payload in payloads}) != 1:
        raise Top5CanaryOrchestrationError("activation identity differs across batch")
    return tuple(payloads), max(artifact_times)


def capture_top5_canary_batch(
    runs: Sequence[Top5OneShotFixtureRun],
    *,
    publication_authorizations: Mapping[str, Top5PublicationAuthorization],
    activation_bindings: Mapping[str, Mapping[str, object]],
    now: datetime,
    event_sink: Callable[[Mapping[str, object]], None] | None = None,
) -> Top5CanaryCapture:
    """Run or safely resume exactly one authorized one-shot per Top-5 league."""

    now = _utc(now, "now")
    ordered = _validate_runs(runs, now=now)
    _validate_publication_inputs(
        ordered,
        publication_authorizations=publication_authorizations,
        activation_bindings=activation_bindings,
        now=now,
    )
    captured: dict[
        str,
        tuple[
            Top5OneShotFixtureRun, Mapping[str, object], tuple[Top5SignalLifecycle, ...]
        ],
    ] = {}
    request_count = 0
    resumed_count = 0
    _emit(
        event_sink,
        "batch_started",
        batch_id=ordered[0].binding.activation_id,
        fixture_count=5,
        lifecycle_stage=ordered[0].binding.lifecycle_stage,
        provider_authority="the_odds_api",
        batch_state=Top5CanaryBatchState.PREPARING.value,
    )
    try:
        for run in ordered:
            recovered = _recover_capture(run, now=now)
            if recovered is None:
                fresh_gate = getattr(
                    provider_budget, "is_top5_provider_available", None
                )
                if not callable(fresh_gate):
                    raise Top5CanaryOrchestrationError(
                        "canonical fresh Top-5 quota gate is unavailable; provider call refused"
                    )

                def budget_check(at: datetime, gate=fresh_gate) -> bool:
                    try:
                        return gate(now=_utc(at, "quota_gate_now")) is True
                    except TypeError:
                        return False

                # Override the legacy default so freshness is checked before
                # the runtime can load a credential or invoke transport.
                original_budget_check = run.runtime.budget_available
                run.runtime.budget_available = budget_check
                original_transport = run.runtime.transport
                counted_transport = _CountingOneShotTransport(
                    original_transport, lambda: None
                )
                run.runtime.transport = counted_transport  # type: ignore[assignment]
                try:
                    result = run.runtime.execute(
                        run.plan,
                        run.binding,
                        run.authorization,
                        fixture=run.fixture,
                        lifecycles=run.lifecycles,
                    )
                finally:
                    run.runtime.transport = original_transport
                    run.runtime.budget_available = original_budget_check
                    request_count += counted_transport.request_count
                if counted_transport.request_count != 1:
                    raise Top5CanaryOrchestrationError(
                        "one-shot runtime did not make exactly one provider request"
                    )
                _validate_result(result, run, now=now)
                run.runtime.rollback_execution(run.binding)
                lifecycles = run.runtime.lifecycle_set_store.load_for_scope(
                    lifecycle_contract_id=run.binding.lifecycle_contract_id,
                    league_code=run.fixture.league_code,
                    fixture_key=run.fixture.fixture_key,
                    market_id="h2h",
                    candidate_id=M5_CANDIDATE_ID,
                    model_identity=run.binding.model_identity,
                )
                if lifecycles is None:
                    raise Top5CanaryOrchestrationError(
                        "one-shot lifecycle read-back is missing"
                    )
                ordered_lifecycles = canonical_top5_h2h_lifecycle_set(lifecycles)
                if top5_h2h_lifecycle_set_digest(
                    tuple(ordered_lifecycles.values())
                ) != result.get("lifecycle_set_digest"):
                    raise Top5CanaryOrchestrationError(
                        "one-shot lifecycle read-back digest differs"
                    )
                expected_stage = (
                    "INITIAL" if run.binding.lifecycle_stage == "INITIAL" else "REFINED"
                )
                if any(
                    ordered_lifecycles[outcome].current_version.stage.value
                    != expected_stage
                    for outcome in _OUTCOMES
                ):
                    raise Top5CanaryOrchestrationError(
                        "one-shot lifecycle stage differs from the authorized batch"
                    )
                lifecycle_tuple = tuple(
                    ordered_lifecycles[outcome] for outcome in _OUTCOMES
                )
                _emit(
                    event_sink,
                    "fixture_capture_verified",
                    batch_id=run.binding.activation_id,
                    league=run.fixture.league_code,
                    fixture_identity=run.fixture.fixture_key,
                    request_count=1,
                    retry_count=0,
                    decision_state=result["signal_decision"].get("state"),
                    capture_digest=result["capture_result_digest"],
                )
            else:
                result, lifecycle_tuple = recovered
                resumed_count += 1
                _emit(
                    event_sink,
                    "fixture_capture_resumed",
                    batch_id=run.binding.activation_id,
                    league=run.fixture.league_code,
                    fixture_identity=run.fixture.fixture_key,
                    request_count=0,
                    retry_count=0,
                    decision_state=result["signal_decision"].get("state"),
                    capture_digest=result["capture_result_digest"],
                )
            captured[run.fixture.league_code] = (run, result, lifecycle_tuple)

        provider_ids = [
            str(captured[league][1]["provider_event_id"]) for league in _LEAGUES
        ]
        fixture_keys = {
            league: str(captured[league][1]["fixture_key"]) for league in _LEAGUES
        }
        if len(set(provider_ids)) != 5 or len(set(fixture_keys.values())) != 5:
            raise Top5CanaryOrchestrationError(
                "provider event and canonical fixture identities must each be unique"
            )
        for run in ordered:
            _validate_result(captured[run.fixture.league_code][1], run, now=now)
        payloads, artifact_at = _build_publication_payloads(
            captured,
            publication_authorizations=publication_authorizations,
            activation_bindings=activation_bindings,
            now=now,
        )
        publisher = InMemoryTop5PublicationStore()
        artifact = publisher.publish_batch(
            payloads,
            publication_authorizations,
            activation_bindings=activation_bindings,
            now=artifact_at,
        )
        if artifact.published_at != artifact_at:
            raise Top5CanaryOrchestrationError(
                "batch publication timestamp is not deterministic"
            )
        capture_digests = {
            league: str(captured[league][1]["capture_result_digest"])
            for league in _LEAGUES
        }
        _emit(
            event_sink,
            "validation_passed",
            batch_id=ordered[0].binding.activation_id,
            fixture_count=5,
            request_count=request_count,
            retry_count=0,
            artifact_digest=artifact.artifact_digest,
        )
        return Top5CanaryCapture(
            artifact=artifact,
            provider_request_count=request_count,
            retry_count=0,
            resumed_fixture_count=resumed_count,
            fixture_identities=fixture_keys,
            capture_digests=capture_digests,
        )
    except Exception as exc:
        # The error text is deliberately omitted from logs; request exceptions
        # can contain protected transport details. No four-fixture artifact is
        # produced and no public delivery is called on this path.
        _emit(
            event_sink,
            "batch_validation_failed",
            batch_id=ordered[0].binding.activation_id,
            captured_fixture_count=len(captured),
            provider_request_count=request_count,
            retry_count=0,
            error_type=type(exc).__name__,
            failure_reason=_failure_reason_code(exc),
            state=Top5CanaryBatchState.FAILED.value,
            no_public_write=True,
        )
        if isinstance(exc, Top5CanaryOrchestrationError):
            raise
        raise Top5CanaryOrchestrationError(
            "five-fixture canary failed closed; partial public delivery was refused"
        ) from None


def deliver_top5_canary_batch(
    *,
    artifact: PublishedTop5BatchArtifact,
    store: Top5CanaryBatchStore,
    executor: Top5DeliveryExecutor,
    current_public_snapshot: Mapping[str, object],
    attestation: Top5DeliveryAttestation,
    capability: Mapping[str, object],
    now: datetime,
    event_sink: Callable[[Mapping[str, object]], None] | None = None,
) -> object:
    """Deliver one already authorized artifact through the COMMITTED-only seam."""

    now = _utc(now, "now")
    if not isinstance(attestation, Top5DeliveryAttestation):
        raise Top5CanaryOrchestrationError(
            "separate explicit batch publication attestation is required"
        )
    try:
        plan = Top5CanonicalDeliveryAdapter().build_plan(
            current_public_snapshot, artifact
        )
        result = execute_stored_top5_batch(
            store=store,
            executor=executor,
            artifact=artifact,
            current_public_snapshot=current_public_snapshot,
            plan=plan,
            attestation=attestation,
            capability=capability,
            now=now,
        )
        stored = store.load(plan.generation_id)
        if (
            stored is None
            or stored.get("state") != Top5CanaryBatchState.COMMITTED.value
        ):
            raise Top5CanaryOrchestrationError(
                "public delivery did not reach verified COMMITTED state"
            )
        _emit(
            event_sink,
            "storage_committed",
            batch_id=plan.generation_id,
            storage_state=Top5CanaryBatchState.COMMITTED.value,
            public_digest=plan.public_product_digest,
            provider_request_count=0,
        )
        _emit(
            event_sink,
            "public_adapter_updated",
            batch_id=plan.generation_id,
            storage_state=Top5CanaryBatchState.COMMITTED.value,
            public_digest=plan.public_product_digest,
            delivery_status=getattr(result, "status", "unknown"),
            provider_request_count=0,
        )
        return result
    except Exception as exc:
        state = None
        try:
            record = store.load(getattr(locals().get("plan"), "generation_id", ""))
            state = record.get("state") if record else None
        except (OSError, Top5CanaryBatchStorageError):
            state = Top5CanaryBatchState.FAILED.value
        if state == Top5CanaryBatchState.ROLLED_BACK.value:
            _emit(
                event_sink,
                "rollback_executed",
                batch_id=getattr(locals().get("plan"), "generation_id", "unknown"),
                storage_state=state,
                provider_request_count=0,
            )
        _emit(
            event_sink,
            "public_delivery_failed",
            batch_id=getattr(locals().get("plan"), "generation_id", "unknown"),
            storage_state=state or Top5CanaryBatchState.FAILED.value,
            error_type=type(exc).__name__,
            failure_reason=_failure_reason_code(exc),
            provider_request_count=0,
        )
        if isinstance(exc, Top5CanaryOrchestrationError):
            raise
        raise Top5CanaryOrchestrationError(
            "authorized batch delivery failed closed"
        ) from None


__all__ = [
    "Top5CanaryCapture",
    "Top5CanaryOrchestrationError",
    "Top5OneShotFixtureRun",
    "capture_top5_canary_batch",
    "deliver_top5_canary_batch",
]
