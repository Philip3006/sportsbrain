"""Acceptance matrix for the The Odds API-only Football release seam."""

from __future__ import annotations

import pytest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.football.production_contracts import Fixture, ProductionContractError
from src.football.provider_cascade import (
    DEFAULT_PROVIDER_ORDER,
    DECOMMISSIONED_FOOTBALL_PROVIDERS,
    MARKET_PREMATCH_1X2,
    AdapterResult,
    CascadeTimingPolicy,
    NetworkAuthorizationContract,
    NormalizedOddsObservation,
    ProviderCascadeConfig,
    ProviderCascadeRouter,
    ProviderConfig,
    ProviderState,
    QuotaSnapshot,
    QuotaStateStore,
    RequestBudgetManager,
    TransportCapability,
    build_provider_readiness_view,
    release_day_preflight,
)
from src.football.provider_cascade.router import DEFAULT_ADAPTERS
from src.football.provider_cascade.preparation import preparation_from_input_payload
from src.football.top5_shadow_provider_redundancy import (
    ProviderReadinessState,
    supported_candidate_adapters,
)

NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
FIXTURE = Fixture(
    "EPL:credit-aware-fixture", "EPL", "Home FC", "Away FC", NOW + timedelta(hours=2)
)
TIMING = CascadeTimingPolicy(900, 90)


def _provider(
    *,
    remaining: int | None = 10,
    reset_at: datetime | None = None,
    credentials_required: bool = False,
    credential_available: bool | None = True,
    request_budget: int = 1,
    request_cost: int = 1,
) -> ProviderConfig:
    return ProviderConfig(
        name="the_odds_api",
        bookmakers=("test-book",),
        credentials_required=credentials_required,
        credential_env=("ODDS_API_KEY",) if credentials_required else (),
        credential_available=credential_available,
        initial_quota=QuotaSnapshot(remaining=remaining, reset_at=reset_at),
        request_budget=request_budget,
        request_cost=request_cost,
        adapter_version="the-odds-api:test",
    )


def _config(
    *,
    remaining: int | None = 10,
    reset_at: datetime | None = None,
    credentials_required: bool = False,
    credential_available: bool | None = True,
    global_request_budget: int = 1,
    per_run_cap: int = 1,
    request_budget: int = 1,
    request_cost: int = 1,
) -> ProviderCascadeConfig:
    return ProviderCascadeConfig(
        provider_order=("the_odds_api",),
        providers={
            "the_odds_api": _provider(
                remaining=remaining,
                reset_at=reset_at,
                credentials_required=credentials_required,
                credential_available=credential_available,
                request_budget=request_budget,
                request_cost=request_cost,
            )
        },
        global_request_budget=global_request_budget,
        per_run_cap=per_run_cap,
    )


def _observation(*, captured_at: datetime = NOW) -> NormalizedOddsObservation:
    return NormalizedOddsObservation(
        league_code=FIXTURE.league_code,
        fixture_key=FIXTURE.fixture_key,
        provider_fixture_id="toa-event",
        home_team=FIXTURE.home_team,
        away_team=FIXTURE.away_team,
        kickoff_utc=FIXTURE.kickoff,
        market_type=MARKET_PREMATCH_1X2,
        home_odds=2.1,
        draw_odds=3.4,
        away_odds=3.8,
        provider_identity="the_odds_api",
        bookmaker_identity="test-book",
        source_timestamp=captured_at,
        captured_at=captured_at,
        request_identity="request-toa",
        request_started_at=NOW,
        request_completed_at=NOW,
        latency_ms=3,
        provider_priority=0,
        fallback_depth=0,
        source_provenance="injected:the_odds_api",
        raw_record_digest="a" * 64,
        adapter_version="the-odds-api:test",
    )


def _result(
    state: ProviderState = ProviderState.AVAILABLE,
    *,
    observation: NormalizedOddsObservation | None = None,
    status_code: int | None = None,
) -> AdapterResult:
    return AdapterResult(
        state=state,
        reason=state.value,
        observation=observation
        or (_observation() if state is ProviderState.AVAILABLE else None),
        status_code=status_code or (200 if state is ProviderState.AVAILABLE else 503),
        network_called=True,
        latency_ms=3,
        quota_after=QuotaSnapshot(remaining=9),
    )


class FakeAdapter:
    transport_capability = TransportCapability.TEST_INJECTED

    def __init__(self, result: AdapterResult) -> None:
        self.result = result
        self.calls = 0

    def fetch(self, fixture, config, **kwargs):
        self.calls += 1
        return self.result


def _route(
    config: ProviderCascadeConfig,
    adapter: FakeAdapter,
    *,
    budget: RequestBudgetManager | None = None,
):
    return ProviderCascadeRouter(
        config,
        adapters={"the_odds_api": adapter},
        budget=budget,
        now=NOW,
        timing_policy=TIMING,
    ).route(FIXTURE, now=NOW)


def _readiness_inputs() -> dict[str, object]:
    return {
        "readiness_states": {
            "the_odds_api": ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION.value
        },
        "parser_readiness": {"the_odds_api": "SUPPORTED"},
        "quota_header_readiness": {"the_odds_api": "SUPPORTED"},
        "health_readiness": {"the_odds_api": "SUPPORTED"},
    }


def test_a_zero_quota_makes_zero_calls_and_blocks() -> None:
    adapter = FakeAdapter(_result())
    result = _route(_config(remaining=0), adapter)
    assert result.observation is None
    assert result.trace.fail_closed is True
    assert result.trace.attempts[0].state is ProviderState.QUOTA_EXHAUSTED
    assert adapter.calls == 0
    assert result.trace.attempts[0].network_called is False


def test_b_reset_boundary_not_reached_makes_zero_calls_and_blocks() -> None:
    adapter = FakeAdapter(_result())
    result = _route(
        _config(remaining=0, reset_at=NOW + timedelta(minutes=1)), adapter
    )
    assert result.observation is None
    assert adapter.calls == 0
    assert result.trace.attempts[0].state is ProviderState.QUOTA_EXHAUSTED


def test_c_reset_boundary_exposes_one_future_bounded_revalidation_without_call(
    tmp_path: Path,
) -> None:
    reset_at = NOW - timedelta(minutes=1)
    config = _config(remaining=0, reset_at=reset_at)
    store = QuotaStateStore(tmp_path / "quota.json")
    store.update(
        "the_odds_api",
        quota=QuotaSnapshot(remaining=0, reset_at=reset_at),
        observed_at=NOW - timedelta(days=1),
        state=ProviderState.QUOTA_EXHAUSTED.value,
        source="test_reset_boundary",
    )
    view = build_provider_readiness_view(
        config, quota_state_store=store, now=NOW, **_readiness_inputs()
    )
    record = view.providers[0]
    assert record.state == "QUOTA_REVALIDATION_ELIGIBLE"
    assert record.quota_revalidation_eligible is True
    budget = RequestBudgetManager(config, quota_state_store=store, now=NOW)
    decision = budget.preflight("the_odds_api", now=NOW)
    assert decision.allowed is True
    assert decision.reason == "quota_reset_revalidation_allowed"
    assert budget.counters("the_odds_api").requests_attempted == 0


def test_d_fresh_positive_quota_is_ready_with_explicit_authorization(tmp_path: Path) -> None:
    config = _config()
    store = QuotaStateStore(tmp_path / "quota.json")
    store.update(
        "the_odds_api",
        quota=QuotaSnapshot(remaining=10, rate_remaining=5),
        observed_at=NOW,
        state=ProviderState.AVAILABLE.value,
        source="test_fresh",
    )
    result = release_day_preflight(
        config,
        authorization=NetworkAuthorizationContract("run", ("the_odds_api",)),
        quota_state_store=store,
        credentials={"the_odds_api": True},
        identity_readiness={"the_odds_api": "NOT_REQUIRED"},
        now=NOW,
        **_readiness_inputs(),
    )
    assert result.status == "READY_FOR_PROVIDER_RUN"
    assert result.blockers == ()
    assert result.network_called is False


@pytest.mark.parametrize("credential", [False, None])
def test_e_missing_credential_makes_zero_calls_and_blocks(credential: bool | None) -> None:
    adapter = FakeAdapter(_result())
    result = _route(
        _config(credentials_required=True, credential_available=credential), adapter
    )
    assert result.observation is None
    assert result.trace.attempts[0].state is ProviderState.CREDENTIAL_MISSING
    assert adapter.calls == 0


@pytest.mark.parametrize(
    ("state", "status"),
    [
        (ProviderState.AUTH_FAILED, 401),
        (ProviderState.RATE_LIMITED, 429),
        (ProviderState.TEMPORARILY_UNAVAILABLE, 503),
    ],
)
def test_f_to_h_provider_failure_blocks_without_alternate_provider(
    state: ProviderState, status: int
) -> None:
    adapter = FakeAdapter(_result(state, status_code=status))
    result = _route(_config(), adapter)
    assert result.observation is None
    assert result.trace.fail_closed is True
    assert [attempt.provider for attempt in result.trace.attempts] == ["the_odds_api"]
    assert adapter.calls == 1


def test_i_invalid_stale_observation_is_rejected_without_alternate_provider() -> None:
    stale = _observation(
        captured_at=NOW - timedelta(seconds=TIMING.maximum_odds_age_seconds + 1)
    )
    adapter = FakeAdapter(_result(observation=stale))
    result = _route(_config(), adapter)
    assert result.observation is None
    assert result.trace.attempts[0].state is ProviderState.STALE
    assert adapter.calls == 1


def test_j_quota_state_persists_across_process_restart(tmp_path: Path) -> None:
    store = QuotaStateStore(tmp_path / "top5-quota.json")
    config = _config()
    budget = RequestBudgetManager(config, quota_state_store=store, now=NOW)
    budget.record_attempt("the_odds_api", at=NOW)
    budget.record_result(
        "the_odds_api",
        state=ProviderState.TEMPORARILY_UNAVAILABLE,
        at=NOW,
        quota_after=QuotaSnapshot(
            used=8,
            remaining=12,
            reset_at=NOW + timedelta(days=14),
            rate_limit=20,
            rate_remaining=19,
        ),
    )
    restored = RequestBudgetManager(config, quota_state_store=store, now=NOW)
    assert restored.quota_before("the_odds_api").remaining == 12
    assert restored.quota_before("the_odds_api").rate_remaining == 19
    payload = store.path.read_text()
    assert "ODDS_API_KEY" not in payload
    assert "api_key" not in payload.lower()


def test_k_removed_football_providers_are_absent_from_every_active_surface() -> None:
    assert DEFAULT_PROVIDER_ORDER == ("the_odds_api",)
    assert tuple(DEFAULT_ADAPTERS) == ("the_odds_api",)
    assert supported_candidate_adapters() == ("the_odds_api",)
    for provider in DECOMMISSIONED_FOOTBALL_PROVIDERS:
        with pytest.raises(ProductionContractError):
            NetworkAuthorizationContract("run", (provider,)).validate()
        with pytest.raises(ProductionContractError):
            ProviderCascadeConfig.from_mapping({"provider_order": [provider]})


def test_request_and_quota_caps_are_separate_and_zero_cost_preflight_is_closed() -> None:
    request_capped = replace(_config(), global_request_budget=0, per_run_cap=0)
    adapter = FakeAdapter(_result())
    result = _route(request_capped, adapter)
    assert result.observation is None
    assert adapter.calls == 0

    cost_capped = replace(
        _config(remaining=1, request_cost=2), global_request_budget=1, per_run_cap=1
    )
    cost_budget = RequestBudgetManager(cost_capped, now=NOW)
    decision = cost_budget.preflight("the_odds_api", now=NOW)
    assert decision.allowed is False
    assert decision.reason == "quota_exhausted_or_reserve"


def test_preparation_rejects_removed_provider_and_never_exposes_fallback_state() -> None:
    removed_provider = next(iter(DECOMMISSIONED_FOOTBALL_PROVIDERS))
    with pytest.raises(ProductionContractError):
        preparation_from_input_payload(
            {
                "fixture": {
                    "fixture_key": "EPL:fixture",
                    "league_code": "EPL",
                    "home_team": "Home FC",
                    "away_team": "Away FC",
                    "kickoff": FIXTURE.kickoff.isoformat(),
                },
                "timing_policy": {
                    "maximum_odds_age_seconds": 900,
                    "kickoff_tolerance_seconds": 60,
                },
                "provider_order": ["the_odds_api", removed_provider],
                "credential_presence": {"the_odds_api": True},
                "provider_readiness": {},
                "maximum_total_network_requests": 1,
                "maximum_total_quota_cost_units": 1.0,
            }
        )
