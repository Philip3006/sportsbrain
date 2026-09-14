from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.football.production_contracts import ProductionContractError
from src.football.top5_real_shadow import (
    FROZEN_RESEARCH_SHA,
    RealShadowExecutionError,
    RealShadowQuotaError,
    RealTop5Provider,
    ShadowExperiment,
    run_controlled_shadow_cycle,
    write_shadow_archive,
)
from src.football.top5_real_shadow_provider import ProviderResponse

BASE = datetime(2026, 9, 14, 18, 30, tzinfo=timezone.utc)
SHA = "0123456789abcdef" * 2 + "01234567"
TIMING = ShadowExperiment("shadow-experiment:60-180-120", 60, 180, 120)


def _event(sport_key: str, event_id: str = "event-1", *, kickoff: datetime | None = None) -> dict:
    return {
        "id": event_id,
        "sport_key": sport_key,
        "commence_time": (kickoff or (BASE + timedelta(minutes=120))).isoformat(),
        "home_team": "Home FC",
        "away_team": "Away FC",
        "bookmakers": [{
            "key": "testbook",
            "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": "Home FC", "price": 2.0},
                    {"name": "Draw", "price": 3.5},
                    {"name": "Away FC", "price": 4.0},
                ],
            }],
        }],
    }


def _response(payload, *, status=200, headers=None, completed=BASE):
    return ProviderResponse(status, payload, headers or {}, completed, 12)


def test_timing_requires_explicit_shadow_namespace_and_rejects_ambiguous_identity():
    TIMING.validate()
    with pytest.raises(ProductionContractError, match="shadow-experiment"):
        ShadowExperiment("production", 60, 180, 120).validate()
    with pytest.raises(ProductionContractError, match="ambiguous"):
        ShadowExperiment("shadow-experiment:latest", 60, 180, 120).validate()


def test_quota_gate_fails_before_any_provider_request():
    calls = []

    def transport(*args):
        calls.append(args)
        raise AssertionError("provider must not be called")

    with pytest.raises(RealShadowQuotaError):
        run_controlled_shadow_cycle(
            api_key="secret-not-for-output",
            timing=TIMING,
            quota_remaining=14,
            safety_reserve=10,
            integration_sha=SHA,
            now=BASE,
            transport=transport,
        )
    assert calls == []


def test_provider_success_is_bulk_only_and_keeps_full_provenance():
    calls = []

    def transport(sport_key, markets, regions, api_key, timeout):
        calls.append((sport_key, markets, regions, api_key, timeout))
        return _response([_event(sport_key)], headers={"X-Requests-Remaining": "40"})

    observation = RealTop5Provider("secret-not-for-output", transport=transport).fetch_league(
        "BL1", TIMING, requested_at=BASE
    )
    assert len(calls) == 1
    assert calls[0][1:3] == (("h2h",), ("eu",))
    assert calls[0][3] == "secret-not-for-output"
    assert observation.status == "success"
    assert observation.request_id.startswith("provider-request:")
    assert observation.fallback_used is False
    assert observation.retry_count == 0
    assert observation.event_requests == 0
    assert observation.fixtures[0].fixture_key == "the_odds_api:soccer_germany_bundesliga:event-1"
    assert observation.snapshots[0].kind.value == "signal_time"
    assert observation.as_payload()["quota_headers"] == {"x-requests-remaining": "40"}


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "authentication"), (403, "forbidden"), (429, "rate_limited")],
)
def test_auth_and_rate_limit_outcomes_are_recorded_without_fallback(status, expected):
    response = _response(None, status=status, headers={"X-Requests-Remaining": "39"})
    observation = RealTop5Provider(
        "secret-not-for-output", transport=lambda *args: response
    ).fetch_league("BL1", TIMING, requested_at=BASE)
    payload = observation.as_payload()
    assert payload["status_code"] == status
    assert payload["failure_reason"] == expected
    assert payload["fallback_used"] is False
    assert payload["event_requests"] == 0
    assert "secret-not-for-output" not in str(payload)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(None, "malformed"), ([], "empty")],
)
def test_empty_or_malformed_payload_is_not_a_signal(payload, expected):
    response = _response(payload)
    observation = RealTop5Provider(
        "secret-not-for-output", transport=lambda *args: response
    ).fetch_league("BL1", TIMING, requested_at=BASE)
    assert observation.status == expected
    assert observation.snapshots == ()


def test_wrong_league_and_unsupported_market_are_counted():
    wrong = _event("soccer_epl", "wrong")
    unsupported = _event("soccer_germany_bundesliga", "unsupported")
    unsupported["bookmakers"][0]["markets"] = [{"key": "totals", "outcomes": []}]
    response = _response([wrong, unsupported])
    observation = RealTop5Provider(
        "secret-not-for-output", transport=lambda *args: response
    ).fetch_league("BL1", TIMING, requested_at=BASE)
    assert observation.wrong_league_count == 1
    assert observation.unsupported_market_count == 1
    assert observation.snapshots == ()


def test_full_cycle_uses_five_requests_and_only_m5_no_bet_artifacts():
    calls = []

    def transport(sport_key, markets, regions, api_key, timeout):
        calls.append((sport_key, markets, regions))
        return _response([_event(sport_key)], headers={"X-Requests-Used": "460", "X-Requests-Remaining": "40"})

    result = run_controlled_shadow_cycle(
        api_key="secret-not-for-output",
        timing=TIMING,
        quota_remaining=41,
        safety_reserve=10,
        integration_sha=SHA,
        now=BASE,
        transport=transport,
    )
    result.validate()
    assert len(calls) == 5
    assert len(result.observations) == 5
    assert {observation.league_code for observation in result.observations} == {"BL1", "EPL", "LL", "SA", "L1"}
    assert len(result.integrations) == 5
    assert all(signal.no_bet for integration in result.integrations for signal in integration.signals)
    assert all(not signal.publication for integration in result.integrations for signal in integration.signals)
    payload = result.as_payload()
    assert payload["research_sha"] == FROZEN_RESEARCH_SHA
    assert payload["timing_experiment"]["production_approved"] is False
    assert "secret-not-for-output" not in str(payload)


def test_archive_is_external_and_redacted(monkeypatch, tmp_path):
    calls = []

    def transport(sport_key, markets, regions, api_key, timeout):
        calls.append(sport_key)
        return _response([])

    result = run_controlled_shadow_cycle(
        api_key="secret-not-for-output",
        timing=TIMING,
        quota_remaining=41,
        safety_reserve=10,
        integration_sha=SHA,
        now=BASE,
        transport=transport,
    )
    monkeypatch.setattr(
        "src.football.top5_real_shadow.runtime_state_path",
        lambda relative_path, require_external: tmp_path / relative_path,
    )
    path = write_shadow_archive(result)
    assert path.is_file()
    assert Path("docs/data").exists()
    assert "secret-not-for-output" not in path.read_text()
    assert len(calls) == 5


def test_provider_auth_boundary_raises_before_next_league():
    calls = []

    def transport(sport_key, markets, regions, api_key, timeout):
        calls.append(sport_key)
        return _response(None, status=401)

    with pytest.raises(RealShadowExecutionError, match="provider_401"):
        run_controlled_shadow_cycle(
            api_key="secret-not-for-output",
            timing=TIMING,
            quota_remaining=41,
            safety_reserve=10,
            integration_sha=SHA,
            now=BASE,
            transport=transport,
        )
    assert len(calls) == 1
