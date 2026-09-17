"""Acceptance matrix for the credit-aware Top-5 provider release seam."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.top5_provider_release_preflight import main as release_preflight_main
from src.football.production_contracts import Fixture, ProductionContractError
from src.football.provider_cascade import (
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
from src.football.top5_shadow_provider_redundancy import ProviderReadinessState

NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
FIXTURE = Fixture(
    "EPL:credit-aware-fixture",
    "EPL",
    "Home FC",
    "Away FC",
    NOW + timedelta(hours=2),
)
TIMING = CascadeTimingPolicy(900, 90)


def _provider(
    name: str,
    *,
    remaining: int | None = 10,
    request_budget: int = 1,
    request_cost: int = 1,
    credentials_required: bool = False,
    credential_available: bool | None = True,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        bookmakers=("test-book",),
        credentials_required=credentials_required,
        credential_available=credential_available,
        initial_quota=QuotaSnapshot(remaining=remaining),
        request_budget=request_budget,
        request_cost=request_cost,
        adapter_version=f"{name}:test",
    )


def _config(
    *names: str,
    global_request_budget: int | None = None,
    per_run_cap: int | None = None,
    quotas: dict[str, int | None] | None = None,
    request_costs: dict[str, int] | None = None,
    request_budgets: dict[str, int] | None = None,
) -> ProviderCascadeConfig:
    quotas = quotas or {}
    request_costs = request_costs or {}
    request_budgets = request_budgets or {}
    providers = {
        name: _provider(
            name,
            remaining=quotas.get(name, 10),
            request_cost=request_costs.get(name, 1),
            request_budget=request_budgets.get(name, 1),
        )
        for name in names
    }
    return ProviderCascadeConfig(
        provider_order=names,
        providers=providers,
        global_request_budget=(
            len(names) if global_request_budget is None else global_request_budget
        ),
        per_run_cap=len(names) if per_run_cap is None else per_run_cap,
    )


def _observation(
    provider: str, *, captured_at: datetime = NOW
) -> NormalizedOddsObservation:
    return NormalizedOddsObservation(
        league_code=FIXTURE.league_code,
        fixture_key=FIXTURE.fixture_key,
        provider_fixture_id=f"{provider}-event",
        home_team=FIXTURE.home_team,
        away_team=FIXTURE.away_team,
        kickoff_utc=FIXTURE.kickoff,
        market_type=MARKET_PREMATCH_1X2,
        home_odds=2.1,
        draw_odds=3.4,
        away_odds=3.8,
        provider_identity=provider,
        bookmaker_identity="test-book",
        source_timestamp=captured_at,
        captured_at=captured_at,
        request_identity=f"request-{provider}",
        request_started_at=NOW,
        request_completed_at=NOW,
        latency_ms=3,
        provider_priority=0,
        fallback_depth=0,
        source_provenance=f"injected:{provider}",
        raw_record_digest="a" * 64,
        adapter_version=f"{provider}:test",
    )


def _result(
    provider: str,
    state: ProviderState = ProviderState.AVAILABLE,
    *,
    observation: NormalizedOddsObservation | None = None,
    reason: str | None = None,
    status_code: int | None = None,
    quota_after: QuotaSnapshot | None = None,
) -> AdapterResult:
    return AdapterResult(
        state=state,
        reason=reason or state.value,
        observation=observation
        or (_observation(provider) if state is ProviderState.AVAILABLE else None),
        status_code=status_code or (200 if state is ProviderState.AVAILABLE else 503),
        network_called=True,
        latency_ms=3,
        quota_after=quota_after or QuotaSnapshot(remaining=9),
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
    adapters: dict[str, FakeAdapter],
    *,
    provider_fixture_ids: dict[str, str] | None = None,
    budget: RequestBudgetManager | None = None,
):
    return ProviderCascadeRouter(
        config,
        adapters=adapters,
        budget=budget,
        now=NOW,
        timing_policy=TIMING,
    ).route(FIXTURE, now=NOW, provider_fixture_ids=provider_fixture_ids)


def test_matrix_a_zero_primary_quota_skips_primary_and_falls_back() -> None:
    config = _config("the_odds_api", "odds_api_io", quotas={"the_odds_api": 0})
    primary = FakeAdapter(_result("the_odds_api"))
    fallback = FakeAdapter(_result("odds_api_io"))
    result = _route(
        config,
        {"the_odds_api": primary, "odds_api_io": fallback},
        provider_fixture_ids={"odds_api_io": "odds-event"},
    )
    assert result.accepted
    assert result.observation.provider_identity == "odds_api_io"
    assert primary.calls == 0
    assert fallback.calls == 1
    assert result.trace.attempts[0].state is ProviderState.QUOTA_EXHAUSTED
    assert result.trace.attempts[0].network_called is False


@pytest.mark.parametrize(
    "failure",
    [
        (ProviderState.RATE_LIMITED, 429),
        (ProviderState.AUTH_FAILED, 401),
        (ProviderState.TEMPORARILY_UNAVAILABLE, 504),
        (ProviderState.MALFORMED, 502),
    ],
)
def test_matrix_b_to_d_provider_failures_fall_back_without_retry(
    failure: tuple[ProviderState, int],
) -> None:
    state, status = failure
    config = _config("the_odds_api", "odds_api_io")
    primary = FakeAdapter(_result("the_odds_api", state, status_code=status))
    fallback = FakeAdapter(_result("odds_api_io"))
    result = _route(
        config,
        {"the_odds_api": primary, "odds_api_io": fallback},
        provider_fixture_ids={"odds_api_io": "odds-event"},
    )
    assert result.accepted
    assert result.observation.provider_identity == "odds_api_io"
    assert primary.calls == fallback.calls == 1
    assert [attempt.provider for attempt in result.trace.attempts] == [
        "the_odds_api",
        "odds_api_io",
    ]


def test_matrix_e_stale_primary_falls_back() -> None:
    config = _config("the_odds_api", "odds_api_io")
    stale = _observation(
        "the_odds_api",
        captured_at=NOW - timedelta(seconds=TIMING.maximum_odds_age_seconds + 1),
    )
    primary = FakeAdapter(_result("the_odds_api", observation=stale))
    fallback = FakeAdapter(_result("odds_api_io"))
    result = _route(
        config,
        {"the_odds_api": primary, "odds_api_io": fallback},
        provider_fixture_ids={"odds_api_io": "odds-event"},
    )
    assert result.accepted
    assert result.trace.attempts[0].state is ProviderState.STALE
    assert fallback.calls == 1


def test_matrix_f_first_success_is_the_only_call() -> None:
    config = _config("the_odds_api", "odds_api_io")
    primary = FakeAdapter(_result("the_odds_api"))
    fallback = FakeAdapter(_result("odds_api_io"))
    result = _route(config, {"the_odds_api": primary, "odds_api_io": fallback})
    assert result.accepted
    assert result.trace.fallback_depth == 0
    assert primary.calls == 1
    assert fallback.calls == 0


def test_matrix_g_all_fail_is_fail_closed() -> None:
    config = _config("the_odds_api", "odds_api_io")
    adapters = {
        name: FakeAdapter(
            _result(name, ProviderState.TEMPORARILY_UNAVAILABLE, status_code=503)
        )
        for name in config.provider_order
    }
    result = _route(
        config,
        adapters,
        provider_fixture_ids={"odds_api_io": "odds-event"},
    )
    assert result.observation is None
    assert result.trace.fail_closed
    assert all(adapter.calls == 1 for adapter in adapters.values())


def test_matrix_h_zero_quota_without_fallback_is_fail_closed_without_calls() -> None:
    config = _config("the_odds_api", quotas={"the_odds_api": 0})
    adapter = FakeAdapter(_result("the_odds_api"))
    result = _route(config, {"the_odds_api": adapter})
    assert result.observation is None
    assert result.trace.fail_closed
    assert adapter.calls == 0
    assert result.trace.attempts[0].network_called is False


def test_matrix_i_reset_boundary_allows_one_explicit_budget_revalidation(
    tmp_path: Path,
) -> None:
    reset_at = NOW - timedelta(minutes=1)
    config = replace(
        _config("the_odds_api", quotas={"the_odds_api": 0}),
        providers={
            "the_odds_api": replace(
                _provider("the_odds_api", remaining=0),
                initial_quota=QuotaSnapshot(remaining=0, reset_at=reset_at),
            )
        },
    )
    store = QuotaStateStore(tmp_path / "quota.json")
    store.update(
        "the_odds_api",
        quota=QuotaSnapshot(remaining=0, reset_at=reset_at),
        observed_at=NOW - timedelta(days=1),
        state=ProviderState.QUOTA_EXHAUSTED.value,
        source="test_reset_boundary",
    )
    budget = RequestBudgetManager(config, quota_state_store=store, now=NOW)
    decision = budget.preflight("the_odds_api", now=NOW)
    assert decision.allowed
    assert decision.reason == "quota_reset_revalidation_allowed"


def test_matrix_j_quota_state_survives_a_new_budget_manager(tmp_path: Path) -> None:
    store = QuotaStateStore(tmp_path / "top5-quota.json")
    config = _config("the_odds_api")
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
    payload = (tmp_path / "top5-quota.json").read_text()
    assert "ODDS_API_KEY" not in payload
    assert "api_key" not in payload.lower()


def test_injected_transport_cannot_persist_fake_quota_evidence(tmp_path: Path) -> None:
    store = QuotaStateStore(tmp_path / "quota.json")
    config = _config("the_odds_api")
    budget = RequestBudgetManager(config, quota_state_store=store, now=NOW)
    result = _route(
        config,
        {"the_odds_api": FakeAdapter(_result("the_odds_api"))},
        budget=budget,
    )
    assert result.accepted
    assert not store.path.exists()


def test_request_and_quota_cost_caps_stop_before_next_provider() -> None:
    config = _config(
        "the_odds_api",
        "odds_api_io",
        global_request_budget=1,
        per_run_cap=1,
    )
    first = FakeAdapter(_result("the_odds_api", ProviderState.TEMPORARILY_UNAVAILABLE))
    second = FakeAdapter(_result("odds_api_io"))
    result = _route(
        config,
        {"the_odds_api": first, "odds_api_io": second},
        provider_fixture_ids={"odds_api_io": "odds-event"},
    )
    assert result.observation is None
    assert first.calls == 1
    assert second.calls == 0
    assert result.trace.attempts[-1].reason.endswith("global_request_budget_exhausted")


def test_network_capable_adapter_is_rejected_without_exact_live_authorization() -> None:
    config = _config("the_odds_api")

    class NetworkMarkedAdapter(FakeAdapter):
        transport_capability = TransportCapability.NETWORK_CAPABLE

    adapter = NetworkMarkedAdapter(_result("the_odds_api"))
    result = _route(config, {"the_odds_api": adapter})
    assert result.observation is None
    assert result.trace.attempts[0].reason == "live_calls_not_authorized"
    assert adapter.calls == 0


def test_readiness_view_is_secret_free_and_zero_network(tmp_path: Path) -> None:
    config = _config("the_odds_api", "odds_api_io")
    store = QuotaStateStore(tmp_path / "quota.json")
    for provider in config.provider_order:
        store.update(
            provider,
            quota=QuotaSnapshot(remaining=10, rate_remaining=5),
            observed_at=NOW,
            state="AVAILABLE",
            source="test_readiness",
        )
    authorization = NetworkAuthorizationContract(
        "controlled-shadow-test",
        config.provider_order,
    )
    view = build_provider_readiness_view(
        config,
        authorization=authorization,
        quota_state_store=store,
        identity_readiness={"odds_api_io": "RESOLVED"},
        readiness_states={
            provider: ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION.value
            for provider in config.provider_order
        },
        now=NOW,
    )
    payload = view.as_payload()
    assert view.status == "READY_FOR_PROVIDER_RUN"
    assert payload["network_called"] is False
    assert all(item.fallback_eligibility for item in view.providers)
    assert "ODDS_API_KEY" not in json.dumps(payload)
    assert "secret" not in json.dumps(payload).lower()


def test_release_preflight_requires_fresh_quota_and_reports_blockers(
    tmp_path: Path,
) -> None:
    config = _config("odds_api_io")
    store = QuotaStateStore(tmp_path / "quota.json")
    store.update(
        "odds_api_io",
        quota=QuotaSnapshot(remaining=10),
        observed_at=NOW - timedelta(days=40),
        state="AVAILABLE",
        source="test_stale",
    )
    result = release_day_preflight(
        config,
        authorization=NetworkAuthorizationContract("run", ("odds_api_io",)),
        quota_state_store=store,
        identity_readiness={"odds_api_io": "RESOLVED"},
        readiness_states={
            "odds_api_io": ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION.value
        },
        now=NOW,
    )
    assert result.status == "BLOCKED"
    assert "NO_ELIGIBLE_PROVIDER" in result.blockers
    assert "QUOTA_STATE_STALE" in result.provider_blockers["odds_api_io"]
    assert result.network_called is False


def test_release_preflight_blocks_secondary_without_source_timestamp_contract(
    tmp_path: Path,
) -> None:
    config = _config("api_football")
    store = QuotaStateStore(tmp_path / "quota.json")
    store.update(
        "api_football",
        quota=QuotaSnapshot(remaining=10),
        observed_at=NOW,
        state="AVAILABLE",
        source="test_secondary",
    )
    result = release_day_preflight(
        config,
        authorization=NetworkAuthorizationContract("run", ("api_football",)),
        quota_state_store=store,
        identity_readiness={"api_football": "RESOLVED"},
        readiness_states={
            "api_football": ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION.value
        },
        now=NOW,
    )
    assert result.status == "BLOCKED"
    assert "SOURCE_TIMESTAMP_UNSUPPORTED" in result.provider_blockers["api_football"]


def test_malformed_persisted_quota_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "quota.json"
    path.write_text('{"schema_version":"wrong","providers":{}}')
    with pytest.raises(ProductionContractError):
        QuotaStateStore(path).load()


def test_quota_state_store_rejects_active_checkout_paths() -> None:
    active_checkout = Path(__file__).resolve().parents[2]
    with pytest.raises(ProductionContractError, match="outside the active checkout"):
        QuotaStateStore(active_checkout / "data" / "cache" / "quota.json")


def test_release_preflight_cli_is_zero_network_and_reports_ready(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    now = datetime.now(timezone.utc)
    state_path = tmp_path / "quota.json"
    QuotaStateStore(state_path).update(
        "the_odds_api",
        quota=QuotaSnapshot(remaining=10, rate_remaining=5),
        observed_at=now,
        state="AVAILABLE",
        source="cli_test",
    )
    input_path = tmp_path / "preflight.json"
    input_path.write_text(
        json.dumps(
            {
                "cascade_config": {
                    "provider_order": ["the_odds_api"],
                    "providers": {
                        "the_odds_api": {
                            "credentials_required": False,
                            "credential_available": True,
                            "initial_quota": {"remaining": 10},
                        }
                    },
                    "global_request_budget": 1,
                    "per_run_cap": 1,
                },
                "network_authorization": {
                    "controlled_shadow_run_ref": "cli-run",
                    "authorized_providers": ["the_odds_api"],
                },
                "credentials_present": {"the_odds_api": True},
                "readiness_states": {
                    "the_odds_api": ProviderReadinessState.LIVE_PATH_READY_FOR_OBSERVATION.value
                },
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "top5_provider_release_preflight.py",
            "--input",
            str(input_path),
            "--state-path",
            str(state_path),
        ],
    )

    assert release_preflight_main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "READY_FOR_PROVIDER_RUN"
    assert output["network_called"] is False
