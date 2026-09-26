"""Manual, exact-scope Top-5 production canary runtime.

This runner is intentionally not registered with a scheduler or publication
path. Its only provider capability is one signed, one-league The Odds API
request; every failure after EXECUTING restores and rereads the disabled route.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import requests
from requests.adapters import HTTPAdapter

from src.football.odds.base import canonical_team
from src.football.odds.the_odds_api import parse_top5_consensus_h2h
from src.football.production_contracts import (
    Fixture,
    MarketSnapshot,
    MarketSnapshotKind,
    PredictionArtifact,
    PredictionInput,
    ProductionContractError,
    SignalTimeContract,
    _utc,
)
from src.football.top5_activation_authorization import (
    Top5ActivationExecutionBindingV1,
    VerifiedTop5ActivationAuthorizationV1,
    the_odds_api_one_shot_request_shape,
    the_odds_api_one_shot_request_shape_digest,
)
from src.football.top5_activation_route_state import (
    PRODUCTION_EVIDENCE_SCHEMA,
    DurableTop5ProductionRouteStateStore,
    Top5ProductionRouteConsumer,
)
from src.football.top5_durable_activation import (
    Top5DurableActivationPlanV1,
    _sha,
)
from src.football.top5_research_binding import (
    FROZEN_RESEARCH_SHA,
    M5_CANDIDATE_ID,
    M5_INFERENCE_IMPLEMENTATION,
    Top5M5FeatureAdapter,
    Top5M5MarketModel,
    inventory_for,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_signal_lifecycle import (
    DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    TOP5_H2H_OUTCOMES,
    LifecyclePlanStatus,
    RefinementClassification,
    SignalLifecycleError,
    SignalLifecycleStage,
    Top5SignalLifecycle,
    Top5SignalLifecycleStore,
    canonical_top5_h2h_lifecycle_set,
    create_initial_signal,
    plan_signal_lifecycle,
    refine_signal,
)
from src.football.top5_the_odds_api_fixture_source import TOP5_SPORT_KEYS
from src.signals import provider_budget

ONE_SHOT_RESULT_SCHEMA = "top5-one-shot-production-result-v1"
SAFE_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "date",
        "x-requests-used",
        "x-requests-remaining",
        "x-requests-limit",
        "x-rate-limit",
        "x-rate-limit-remaining",
    }
)


class OneShotExecutionError(ProductionContractError):
    """A canary failed closed. Exception text never contains provider content."""


@dataclass(frozen=True)
class OneShotHttpResult:
    status_code: int
    headers: Mapping[str, str]
    payload: object
    response_digest: str
    request_started_at: datetime
    response_finished_at: datetime
    request_shape_digest: str
    request_count: int = 1
    retry_count: int = 0
    payload_valid: bool = True


class OneShotTransport(Protocol):
    def request(
        self,
        binding: Top5ActivationExecutionBindingV1,
        *,
        api_key: str,
    ) -> OneShotHttpResult: ...


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _safe_headers(headers: Mapping[str, object]) -> dict[str, str]:
    return {
        str(key).casefold(): str(value)
        for key, value in headers.items()
        if str(key).casefold() in SAFE_RESPONSE_HEADERS
    }


class TheOddsApiOneShotHttpTransport:
    """One request, no retry, no redirect, for the exact signed request shape."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout must be finite and positive")
        self.timeout_seconds = timeout_seconds
        self.session_factory = session_factory

    def request(
        self,
        binding: Top5ActivationExecutionBindingV1,
        *,
        api_key: str,
    ) -> OneShotHttpResult:
        expected_digest = the_odds_api_one_shot_request_shape_digest(
            binding.activation_league
        )
        if (
            binding.provider_authority != "the_odds_api"
            or binding.request_shape_digest != expected_digest
            or not isinstance(api_key, str)
            or not api_key
        ):
            raise OneShotExecutionError("signed provider request contract is invalid")
        shape = the_odds_api_one_shot_request_shape(binding.activation_league)
        endpoint = shape["endpoint"]
        query = dict(shape["query"])
        if (
            not isinstance(endpoint, str)
            or shape["request_count"] != 1
            or shape["retry_count"] != 0
        ):
            raise OneShotExecutionError("provider request contract is invalid")
        query["apiKey"] = api_key
        session = self.session_factory()
        adapter = HTTPAdapter(max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        started = datetime.now(timezone.utc)
        try:
            response = session.get(
                endpoint,
                params=query,
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
            body = bytes(response.content)
            finished = datetime.now(timezone.utc)
            response_digest = hashlib.sha256(body).hexdigest()
            status = int(response.status_code)
            headers = _safe_headers(response.headers)
            payload: object = None
            payload_valid = True
            if 200 <= status < 300:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload_valid = False
            return OneShotHttpResult(
                status_code=status,
                headers=headers,
                payload=payload,
                response_digest=response_digest,
                request_started_at=started,
                response_finished_at=finished,
                request_shape_digest=expected_digest,
                payload_valid=payload_valid,
            )
        except OneShotExecutionError:
            raise
        except requests.RequestException:
            # Exception strings can contain the credential-bearing URL.
            raise OneShotExecutionError("provider transport failed") from None
        finally:
            session.close()


def _load_protected_api_key() -> str:
    # This lazy import also loads the existing .env convention. It is called
    # only after signature, route, lifecycle, and provider-budget checks.
    from src.data.odds_api import get_api_key

    return get_api_key()


def _quota_counters(headers: Mapping[str, str]) -> tuple[int, int, int]:
    values: list[int] = []
    for name in ("x-requests-used", "x-requests-remaining", "x-requests-limit"):
        raw = headers.get(name)
        if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
            raise OneShotExecutionError("provider quota headers are incomplete")
        values.append(int(raw))
    used, remaining, limit = values
    if limit <= 0 or used < 0 or remaining < 0 or used > limit or remaining > limit:
        raise OneShotExecutionError("provider quota headers are contradictory")
    if used + remaining != limit:
        raise OneShotExecutionError("provider quota counters do not reconcile")
    return used, remaining, limit


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OneShotExecutionError("provider timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OneShotExecutionError("provider timestamp is malformed") from exc
    try:
        return _utc(parsed, "provider_timestamp")
    except ProductionContractError as exc:
        raise OneShotExecutionError("provider timestamp is invalid") from exc


def _select_event(
    payload: object,
    *,
    fixture: Fixture,
) -> Mapping[str, object]:
    """Select one Odds API event only by the exact signed canonical fixture.

    Provider-native IDs are namespace-specific and are observed only after the
    fixture match; candidate-provider IDs never participate in selection.
    """
    if not isinstance(payload, list):
        raise OneShotExecutionError("provider response is not an event list")
    expected_sport_key = TOP5_SPORT_KEYS[fixture.league_code]
    matches: list[Mapping[str, object]] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        try:
            commence = _parse_timestamp(item.get("commence_time"))
        except OneShotExecutionError:
            continue
        if (
            item.get("sport_key") == expected_sport_key
            and isinstance(item.get("home_team"), str)
            and isinstance(item.get("away_team"), str)
            and canonical_team(str(item["home_team"]))
            == canonical_team(fixture.home_team)
            and canonical_team(str(item["away_team"]))
            == canonical_team(fixture.away_team)
            and commence == fixture.kickoff
        ):
            matches.append(item)
    if len(matches) != 1:
        raise OneShotExecutionError(
            "authorized provider fixture is missing or ambiguous"
        )
    provider_event_id = matches[0].get("id")
    if (
        not isinstance(provider_event_id, str)
        or not provider_event_id
        or provider_event_id != provider_event_id.strip()
        or len(provider_event_id) > 256
        or any(not character.isprintable() for character in provider_event_id)
    ):
        raise OneShotExecutionError("Odds API event identity is missing or invalid")
    return matches[0]


def _consensus_snapshot(
    event: Mapping[str, object],
    *,
    fixture: Fixture,
    now: datetime,
    binding: Top5ActivationExecutionBindingV1,
    response_digest: str,
) -> tuple[MarketSnapshot, str]:
    raw_bookmakers = event.get("bookmakers")
    if not isinstance(raw_bookmakers, list) or not raw_bookmakers:
        raise OneShotExecutionError("provider event has no bookmaker markets")
    bookmakers: list[dict[str, object]] = []
    source_times: list[datetime] = []
    for bookmaker in raw_bookmakers:
        if not isinstance(bookmaker, Mapping):
            raise OneShotExecutionError("provider bookmaker entry is malformed")
        markets = bookmaker.get("markets")
        if not isinstance(markets, list):
            raise OneShotExecutionError("provider bookmaker markets are malformed")
        h2h = [
            market
            for market in markets
            if isinstance(market, Mapping) and market.get("key") == "h2h"
        ]
        if not h2h:
            continue
        if len(h2h) != 1:
            raise OneShotExecutionError(
                "provider returned duplicate regulation h2h markets"
            )
        market = h2h[0]
        timestamp = market.get("last_update", bookmaker.get("last_update"))
        source_times.append(_parse_timestamp(timestamp))
        outcomes = market.get("outcomes")
        if not isinstance(outcomes, list):
            raise OneShotExecutionError("provider h2h outcomes are malformed")
        bookmakers.append(
            {
                "key": bookmaker.get("key"),
                "markets": [{"key": "h2h", "outcomes": outcomes}],
            }
        )
    if not bookmakers or not source_times:
        raise OneShotExecutionError("provider response has no timestamped h2h market")
    captured_at = min(source_times)
    age = (now - captured_at).total_seconds()
    if age < 0 or age > binding.maximum_odds_age_seconds:
        raise OneShotExecutionError("provider odds are future-dated or stale")
    try:
        quote = parse_top5_consensus_h2h(
            bookmakers, fixture.home_team, fixture.away_team
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise OneShotExecutionError("provider regulation 1X2 is invalid") from exc
    odds = {
        "home": float(quote.h2h_home),
        "draw": float(quote.h2h_draw),
        "away": float(quote.h2h_away),
    }
    if any(not math.isfinite(value) or value <= 1.0 for value in odds.values()):
        raise OneShotExecutionError("provider regulation 1X2 is invalid")
    snapshot_id = "top5-one-shot-snapshot-" + _sha(
        {
            "activation_plan_digest": binding.activation_plan_digest,
            "provider_event_id": event["id"],
            "response_digest": response_digest,
            "captured_at": captured_at.isoformat(),
            "odds": odds,
        }
    )
    snapshot = MarketSnapshot(
        fixture_key=fixture.fixture_key,
        captured_at=captured_at,
        kind=MarketSnapshotKind.SIGNAL_TIME,
        source="the_odds_api:top5_manual_one_shot",
        odds=odds,
        snapshot_id=snapshot_id,
    )
    snapshot.validate()
    return snapshot, str(event["id"])


def _probabilities(value: Mapping[str, float]) -> dict[str, float]:
    required = {"home", "draw", "away"}
    if set(value) != required:
        raise OneShotExecutionError("M5 returned an invalid probability set")
    result = {key: float(value[key]) for key in required}
    if any(not math.isfinite(item) or item < 0 or item > 1 for item in result.values()):
        raise OneShotExecutionError("M5 returned invalid probabilities")
    if not math.isclose(sum(result.values()), 1.0, abs_tol=1e-9):
        raise OneShotExecutionError("M5 probabilities do not sum to one")
    return result


class Top5OneShotProductionRuntime:
    """Exact one-league canary runner; never schedules or publishes output."""

    def __init__(
        self,
        *,
        route_store: DurableTop5ProductionRouteStateStore,
        transport: OneShotTransport | None = None,
        lifecycle_store: Top5SignalLifecycleStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        credential_loader: Callable[[], str] = _load_protected_api_key,
        budget_available: Callable[[datetime], bool] | None = None,
        budget_success: Callable[[int, int], None] | None = None,
        budget_failure: Callable[[int], None] | None = None,
        feature_adapter: Top5M5FeatureAdapter | None = None,
        model: Top5M5MarketModel | None = None,
    ) -> None:
        self.route_store = route_store
        self.consumer = Top5ProductionRouteConsumer(route_store)
        self.transport = transport or TheOddsApiOneShotHttpTransport()
        self.lifecycle_store = lifecycle_store or Top5SignalLifecycleStore()
        self.clock = clock
        self.credential_loader = credential_loader
        self.budget_available = budget_available or (
            lambda now: provider_budget.is_provider_available(
                "the_odds_api", allow_quota_revalidation=False, now=now
            )
        )
        self.budget_success = budget_success or self._record_budget_success
        self.budget_failure = budget_failure or self._record_budget_failure
        self.feature_adapter = feature_adapter or Top5M5FeatureAdapter()
        self.model = model or Top5M5MarketModel()

    def rollback_execution(
        self, binding: Top5ActivationExecutionBindingV1
    ) -> dict[str, object]:
        """Restore and consumer-verify this exact canary route as disabled."""
        state = self.route_store.read()
        record = state["records"].get(binding.activation_id)
        if (
            not isinstance(record, dict)
            or record.get("activation_plan_digest") != binding.activation_plan_digest
        ):
            raise OneShotExecutionError("route rollback binding is unavailable")
        if record.get("status") != "ROLLED_BACK":
            self.route_store.rollback(
                binding.activation_id,
                binding.activation_plan_digest,
                now=_utc(self.clock(), "rollback_now"),
            )
        return self.consumer.verify_disabled_after_rollback(
            binding.activation_id, binding.activation_plan_digest
        )

    @staticmethod
    def _record_budget_success(used: int, remaining: int) -> None:
        provider_budget.record_success("the_odds_api", quota_remaining=remaining)
        from src.data.odds_api import _log_usage

        _log_usage(used, remaining, observed_at=datetime.now(timezone.utc))

    @staticmethod
    def _record_budget_failure(status: int) -> None:
        provider_budget.record_error(
            "the_odds_api",
            status,
            open_circuit=status in {401, 403, 429},
        )

    def execute(
        self,
        plan: Top5DurableActivationPlanV1,
        binding: Top5ActivationExecutionBindingV1,
        authorization: VerifiedTop5ActivationAuthorizationV1,
        *,
        fixture: Fixture,
        lifecycles: Sequence[Top5SignalLifecycle] | None = None,
    ) -> dict[str, object]:
        preflight_now = _utc(self.clock(), "preflight_now")
        plan.validate(now=preflight_now)
        if plan.prepared_at > preflight_now:
            raise OneShotExecutionError("activation plan is not prepared yet")
        authorization.assert_valid_at(preflight_now)
        fixture.validate()
        if (
            binding.activation_id != plan.activation_id
            or binding.durable_plan_digest != plan.plan_digest
            or binding.activation_league != plan.activation_league
            or fixture.league_code != binding.activation_league
            or fixture.fixture_key != binding.fixture_key
            or make_fixture_key(
                fixture.league_code,
                fixture.home_team,
                fixture.away_team,
                fixture.kickoff,
            )
            != binding.fixture_key
            or binding.provider_authority != "the_odds_api"
            or binding.request_shape_digest
            != the_odds_api_one_shot_request_shape_digest(binding.activation_league)
            or binding.retry_budget != 0
            or binding.model_identity != M5_CANDIDATE_ID
            or binding.research_sha != FROZEN_RESEARCH_SHA
            or binding.model_artifact_hash
            != inventory_for(
                binding.activation_league, M5_CANDIDATE_ID
            ).model_artifact_hash
            or binding.lifecycle_contract_id
            != DEFAULT_SIGNAL_LIFECYCLE_CONTRACT.contract_id
        ):
            raise OneShotExecutionError("signed one-shot plan is not canonical")
        inventory = inventory_for(binding.activation_league, M5_CANDIDATE_ID)
        if inventory.model_artifact_hash != binding.model_artifact_hash:
            raise OneShotExecutionError("authorized M5 artifact is not canonical")
        if binding.lifecycle_stage not in {"INITIAL", "REFINEMENT"}:
            raise OneShotExecutionError("lifecycle stage is unsupported")
        if binding.lifecycle_stage == "INITIAL":
            if lifecycles is not None:
                raise OneShotExecutionError(
                    "INITIAL cannot replace existing lifecycles"
                )
            resolved_lifecycles: dict[str, Top5SignalLifecycle] = {}
            lifecycle_for_plan = None
        else:
            if lifecycles is None:
                raise OneShotExecutionError(
                    "REFINEMENT requires the complete existing lifecycle set"
                )
            try:
                resolved_lifecycles = canonical_top5_h2h_lifecycle_set(lifecycles)
            except SignalLifecycleError as exc:
                raise OneShotExecutionError(str(exc)) from exc
            for outcome, existing in resolved_lifecycles.items():
                current = existing.current_version
                initial = existing.initial_version
                if (
                    len(existing.versions) != 1
                    or existing.withdrawn
                    or current.version_number != 1
                    or current.stage is not SignalLifecycleStage.INITIAL
                    or existing.contract.contract_id != binding.lifecycle_contract_id
                    or initial.fixture_key != fixture.fixture_key
                    or initial.league_code != fixture.league_code
                    or initial.kickoff != fixture.kickoff
                    or initial.market_id != "h2h"
                    or initial.outcome_id != outcome
                    or initial.candidate_id != M5_CANDIDATE_ID
                    or initial.model_identity != binding.model_identity
                    or initial.source_sha != binding.source_sha
                    or initial.research_sha != binding.research_sha
                    or initial.model_artifact_hash != binding.model_artifact_hash
                ):
                    raise OneShotExecutionError(
                        "existing lifecycle set differs from signed INITIAL scope"
                    )
                stored = self.lifecycle_store.load(existing.lifecycle_id)
                if (
                    stored is None
                    or stored.lifecycle_digest != existing.lifecycle_digest
                ):
                    raise OneShotExecutionError(
                        "persisted lifecycle differs from supplied lifecycle set"
                    )
            lifecycle_for_plan = resolved_lifecycles["home"]
        lifecycle_plan = plan_signal_lifecycle(
            fixture,
            preflight_now,
            lifecycle_for_plan,
            contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
        )
        expected_due = (
            (LifecyclePlanStatus.INITIAL_DUE, SignalLifecycleStage.INITIAL)
            if binding.lifecycle_stage == "INITIAL"
            else (LifecyclePlanStatus.REFINEMENT_DUE, SignalLifecycleStage.REFINED)
        )
        if (lifecycle_plan.status, lifecycle_plan.due_stage) != expected_due:
            raise OneShotExecutionError("authorized lifecycle stage is not due")
        signal_time = SignalTimeContract(
            minimum_minutes_before_kickoff=binding.lifecycle_timing_bounds[
                "minimum_minutes_before_kickoff"
            ],
            maximum_minutes_before_kickoff=binding.lifecycle_timing_bounds[
                "maximum_minutes_before_kickoff"
            ],
            maximum_odds_age_seconds=binding.maximum_odds_age_seconds,
            approval_ref=binding.signal_time_approval_identity,
        )
        signal_time.validate()
        if binding.maximum_odds_age_seconds != 900 or not self.budget_available(
            preflight_now
        ):
            raise OneShotExecutionError("provider budget or freshness gate is blocked")

        route_started = False
        request_started = False
        route_record = None
        try:
            self.route_store.prepare(binding, now=preflight_now)
            route_started = True
            self.route_store.authorize(binding, authorization, now=preflight_now)
            self.route_store.begin_execution(binding, authorization, now=preflight_now)
            route_evidence = self.consumer.consume_exact(binding, authorization)
            before_request = _utc(self.clock(), "provider_preflight_now")
            authorization.assert_valid_at(before_request)
            lifecycle_recheck = plan_signal_lifecycle(
                fixture,
                before_request,
                lifecycle_for_plan,
                contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
            )
            if (lifecycle_recheck.status, lifecycle_recheck.due_stage) != expected_due:
                raise OneShotExecutionError("lifecycle stage ceased to be due")
            if not self.budget_available(before_request):
                raise OneShotExecutionError("provider budget became blocked")
            api_key = self.credential_loader()
            if not isinstance(api_key, str) or not api_key:
                raise OneShotExecutionError("The Odds API credential is unavailable")
            request_started = True
            try:
                response = self.transport.request(binding, api_key=api_key)
            finally:
                api_key = ""
            if (
                response.request_count != 1
                or response.retry_count != 0
                or response.request_shape_digest != binding.request_shape_digest
                or len(response.response_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in response.response_digest
                )
            ):
                raise OneShotExecutionError(
                    "provider transport violated one-shot contract"
                )
            if not 200 <= response.status_code < 300:
                self.budget_failure(response.status_code)
                raise OneShotExecutionError(
                    "The Odds API returned a non-success status"
                )
            safe_headers = _safe_headers(response.headers)
            used, remaining, limit = _quota_counters(safe_headers)
            self.budget_success(used, remaining)
            if not response.payload_valid:
                raise OneShotExecutionError("provider response JSON is malformed")
            response_finished = _utc(
                response.response_finished_at, "response_finished_at"
            )
            request_started_at = _utc(response.request_started_at, "request_started_at")
            if not before_request <= request_started_at <= response_finished:
                raise OneShotExecutionError("provider response clock is invalid")
            post_response_now = _utc(self.clock(), "post_response_now")
            if response_finished > post_response_now:
                raise OneShotExecutionError(
                    "provider response timestamp is future-dated"
                )
            event = _select_event(
                response.payload,
                fixture=fixture,
            )
            snapshot, provider_event_id = _consensus_snapshot(
                event,
                fixture=fixture,
                now=post_response_now,
                binding=binding,
                response_digest=response.response_digest,
            )
            if not signal_time.accepts(
                fixture.kickoff, snapshot.captured_at, post_response_now
            ):
                raise OneShotExecutionError("snapshot violates Signal-Time contract")
            features = self.feature_adapter.build(fixture, snapshot)
            model_input = PredictionInput.create(
                fixture,
                snapshot,
                features,
                signal_time=signal_time,
                now=post_response_now,
            )
            probabilities = _probabilities(self.model.predict(model_input))
            prediction_id = "top5-one-shot-" + _sha(
                {
                    "activation_id": binding.activation_id,
                    "snapshot_id": snapshot.snapshot_id,
                    "model_identity": binding.model_identity,
                }
            )
            prediction = PredictionArtifact(
                prediction_id=prediction_id,
                fixture_key=fixture.fixture_key,
                league_code=fixture.league_code,
                model_adapter_id=M5_CANDIDATE_ID,
                generated_at=post_response_now,
                snapshot_id=snapshot.snapshot_id,
                snapshot_kind=MarketSnapshotKind.SIGNAL_TIME,
                probabilities=probabilities,
            )
            prediction.validate()
            if binding.lifecycle_stage == "INITIAL":
                next_lifecycles = {
                    outcome: create_initial_signal(
                        fixture=fixture,
                        snapshot=snapshot,
                        now=post_response_now,
                        market_id="h2h",
                        outcome_id=outcome,
                        candidate_id=M5_CANDIDATE_ID,
                        model_identity=binding.model_identity,
                        probabilities=probabilities,
                        source_sha=binding.source_sha,
                        research_sha=binding.research_sha,
                        model_artifact_hash=binding.model_artifact_hash,
                        eligibility_decision=True,
                        decision_id=binding.activation_authorization_id,
                        decision_reason="signed manual canary; no wager/actionability threshold",
                        contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
                        confidence_metadata={
                            "canary_execution_id": binding.activation_id,
                            "request_shape_digest": binding.request_shape_digest,
                        },
                    )
                    for outcome in TOP5_H2H_OUTCOMES
                }
            else:
                next_lifecycles = {}
                for outcome in TOP5_H2H_OUTCOMES:
                    existing = resolved_lifecycles[outcome]
                    prior_probability = float(
                        existing.current_version.probabilities[outcome]
                    )
                    next_probability = probabilities[outcome]
                    classification = (
                        RefinementClassification.STRENGTHENED
                        if next_probability > prior_probability
                        else RefinementClassification.WEAKENED
                        if next_probability < prior_probability
                        else RefinementClassification.UNCHANGED
                    )
                    next_lifecycles[outcome] = refine_signal(
                        existing,
                        fixture=fixture,
                        snapshot=snapshot,
                        now=post_response_now,
                        probabilities=probabilities,
                        eligibility_decision=True,
                        withdrawal_authorized=False,
                        decision_id=binding.activation_authorization_id,
                        decision_reason="signed manual canary; deterministic probability comparison",
                        classification=classification,
                        confidence_metadata={
                            "canary_execution_id": binding.activation_id,
                            "request_shape_digest": binding.request_shape_digest,
                        },
                    )
            persisted_lifecycles: dict[str, Top5SignalLifecycle] = {}
            for outcome in TOP5_H2H_OUTCOMES:
                next_lifecycle = next_lifecycles[outcome]
                persisted = self.lifecycle_store.save(next_lifecycle)
                if persisted.lifecycle_digest != next_lifecycle.lifecycle_digest:
                    raise OneShotExecutionError(
                        f"{outcome} lifecycle persistence write-back differs"
                    )
                read_back = self.lifecycle_store.load(persisted.lifecycle_id)
                if (
                    read_back is None
                    or read_back.lifecycle_digest != persisted.lifecycle_digest
                ):
                    raise OneShotExecutionError(
                        f"{outcome} lifecycle persistence read-back differs"
                    )
                persisted_lifecycles[outcome] = read_back
            # Re-read the complete set after all writes; no partial set may pass.
            for outcome in TOP5_H2H_OUTCOMES:
                read_back = self.lifecycle_store.load(
                    persisted_lifecycles[outcome].lifecycle_id
                )
                if (
                    read_back is None
                    or read_back.lifecycle_digest
                    != persisted_lifecycles[outcome].lifecycle_digest
                ):
                    raise OneShotExecutionError(
                        "complete lifecycle set persistence verification failed"
                    )
                persisted_lifecycles[outcome] = read_back
            lifecycle_ids = {
                outcome: persisted_lifecycles[outcome].lifecycle_id
                for outcome in TOP5_H2H_OUTCOMES
            }
            lifecycle_versions = {
                outcome: persisted_lifecycles[outcome].current_version.version_number
                for outcome in TOP5_H2H_OUTCOMES
            }
            lifecycle_version_digests = {
                outcome: persisted_lifecycles[outcome].current_version.version_digest
                for outcome in TOP5_H2H_OUTCOMES
            }
            lifecycle_digests = {
                outcome: persisted_lifecycles[outcome].lifecycle_digest
                for outcome in TOP5_H2H_OUTCOMES
            }
            lifecycle_set_digest = _sha(lifecycle_digests)
            health_status = "HEALTHY"
            finished_at = _utc(self.clock(), "execution_finished_at")
            result_body: dict[str, object] = {
                "schema_version": ONE_SHOT_RESULT_SCHEMA,
                "activation_id": binding.activation_id,
                "activation_plan_digest": binding.activation_plan_digest,
                "signed_authorization_digest": authorization.authorization_digest,
                "authorization_nonce": authorization.nonce,
                "league": fixture.league_code,
                "fixture_key": fixture.fixture_key,
                "lifecycle_contract_id": binding.lifecycle_contract_id,
                "lifecycle_stage": binding.lifecycle_stage,
                "provider_event_id": provider_event_id,
                "provider_authority": "the_odds_api",
                "provider_request_count": 1,
                "retry_count": 0,
                "request_shape_digest": binding.request_shape_digest,
                "http_status": response.status_code,
                "safe_quota_headers": safe_headers,
                "quota_requests_used": used,
                "quota_requests_remaining": remaining,
                "quota_requests_limit": limit,
                "response_digest": response.response_digest,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_kind": MarketSnapshotKind.SIGNAL_TIME.value,
                "snapshot_source": snapshot.source,
                "captured_at": snapshot.captured_at.isoformat(),
                "odds": dict(snapshot.odds),
                "model_identity": binding.model_identity,
                "model_artifact_hash": binding.model_artifact_hash,
                "source_sha": binding.source_sha,
                "research_sha": binding.research_sha,
                "inference_implementation": M5_INFERENCE_IMPLEMENTATION,
                "prediction_id": prediction.prediction_id,
                "candidate_id": M5_CANDIDATE_ID,
                "prediction_timestamp": prediction.generated_at.isoformat(),
                "probabilities": dict(prediction.probabilities),
                "lifecycle_ids": lifecycle_ids,
                "lifecycle_versions": lifecycle_versions,
                "lifecycle_version_digests": lifecycle_version_digests,
                "lifecycle_digests": lifecycle_digests,
                "lifecycle_set_digest": lifecycle_set_digest,
                "route_state_consumed": True,
                "route_state_revision": route_evidence["state_revision"],
                "route_state_digest": route_evidence["route_digest"],
                "execution_started_at": preflight_now.isoformat(),
                "request_started_at": request_started_at.isoformat(),
                "response_finished_at": response_finished.isoformat(),
                "execution_finished_at": finished_at.isoformat(),
                "health_status": health_status,
                "no_bet": True,
                "publication": False,
                "recurring_scheduler": False,
                "betting": False,
                "ledger_mutation": False,
            }
            production_evidence = {
                name: result_body[name]
                for name in (
                    "schema_version",
                    "activation_id",
                    "activation_plan_digest",
                    "signed_authorization_digest",
                    "authorization_nonce",
                    "league",
                    "fixture_key",
                    "lifecycle_contract_id",
                    "lifecycle_stage",
                    "model_identity",
                    "model_artifact_hash",
                    "source_sha",
                    "research_sha",
                    "provider_authority",
                    "request_shape_digest",
                    "provider_request_count",
                    "retry_count",
                    "http_status",
                    "provider_event_id",
                    "snapshot_id",
                    "lifecycle_ids",
                    "lifecycle_versions",
                    "lifecycle_version_digests",
                    "lifecycle_digests",
                    "lifecycle_set_digest",
                    "response_digest",
                    "captured_at",
                    "request_started_at",
                    "response_finished_at",
                    "execution_finished_at",
                    "route_state_consumed",
                    "health_status",
                    "no_bet",
                    "publication",
                    "recurring_scheduler",
                    "betting",
                    "ledger_mutation",
                )
            }
            production_evidence["schema_version"] = PRODUCTION_EVIDENCE_SCHEMA
            production_evidence.update(
                {
                    "snapshot_valid": True,
                    "snapshot_fresh": True,
                    "model_inference_succeeded": True,
                    "lifecycle_persisted": True,
                }
            )
            route_record = self.route_store.mark_production_verified(
                binding,
                authorization,
                production_evidence,
                now=finished_at,
            )
            verified_readback = self.consumer.verify_production_verified(
                binding, authorization
            )
            result_body["production_evidence"] = production_evidence
            result_body["production_evidence_digest"] = _sha(production_evidence)
            result_body["route_record_digest"] = route_record["record_digest"]
            result_body["verified_route_readback"] = verified_readback
            result_body["result_digest"] = _sha(result_body)
            return result_body
        except Exception as exc:  # noqa: BLE001 - all execution failures trigger rollback.
            if request_started and not isinstance(exc, OneShotExecutionError):
                safe_error = OneShotExecutionError("one-shot execution failed closed")
            else:
                safe_error = exc
            if route_started:
                try:
                    self.rollback_execution(binding)
                except Exception:  # noqa: BLE001 - rollback failure is terminal.
                    raise OneShotExecutionError(
                        "one-shot failed and route rollback could not be verified"
                    ) from None
            if isinstance(safe_error, OneShotExecutionError):
                raise safe_error from None
            raise OneShotExecutionError("one-shot execution failed closed") from None


__all__ = [
    "ONE_SHOT_RESULT_SCHEMA",
    "OneShotExecutionError",
    "OneShotHttpResult",
    "OneShotTransport",
    "TheOddsApiOneShotHttpTransport",
    "Top5OneShotProductionRuntime",
]
