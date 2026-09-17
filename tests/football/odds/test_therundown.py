from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.football.odds.therundown import (
    THERUNDOWN_ADAPTER_VERSION,
    THERUNDOWN_CHAMPIONS_LEAGUE_CODE,
    TheRundownExperimentalAdapter,
)
from src.football.production_contracts import Fixture
from src.football.provider_cascade.adapters import RawProviderResponse
from src.football.provider_cascade.contracts import (
    FOOTBALL_PROVIDER_REPERTOIRE,
    MARKET_PREMATCH_1X2,
    CascadeTimingPolicy,
    NetworkAuthorizationContract,
    ProviderConfig,
    ProviderState,
)

_ROOT = Path(__file__).parents[3]
_STARTED_AT = datetime(2026, 9, 22, 19, 0, tzinfo=timezone.utc)
_COMPLETED_AT = datetime(2026, 9, 22, 19, 0, 30, tzinfo=timezone.utc)
_FIXTURE = Fixture(
    "cl-fixture-001",
    THERUNDOWN_CHAMPIONS_LEAGUE_CODE,
    "Home City",
    "Away United",
    _STARTED_AT,
)
_TIMING = CascadeTimingPolicy(
    maximum_odds_age_seconds=600, kickoff_tolerance_seconds=60
)
_AUTHORIZATION = NetworkAuthorizationContract(
    controlled_shadow_run_ref="synthetic-run",
    authorized_providers=("therundown_experimental",),
)


def _payload() -> dict[str, object]:
    with (
        _ROOT / "tests/fixtures/therundown/champions_league_events.json"
    ).open() as handle:
        return json.load(handle)


def _config(**overrides: object) -> ProviderConfig:
    values: dict[str, object] = {
        "name": "therundown_experimental",
        "league_allowlist": frozenset({THERUNDOWN_CHAMPIONS_LEAGUE_CODE}),
        "market_allowlist": (MARKET_PREMATCH_1X2,),
        "bookmakers": ("draftkings", "fanduel"),
        "credentials_required": True,
        "credential_env": ("THERUNDOWN_API_KEY",),
        "candidate_only": True,
        "quality_eligible": False,
        "shadow_only": True,
        "adapter_version": THERUNDOWN_ADAPTER_VERSION,
    }
    values.update(overrides)
    return ProviderConfig(**values)


def _response(payload: object, **headers: str) -> RawProviderResponse:
    return RawProviderResponse(
        status_code=200,
        payload=payload,
        headers={
            "X-Datapoints-Used": "11",
            "X-Datapoints-Remaining": "19989",
            "X-Datapoints-Limit": "20000",
            "X-Rate-Limit": "1",
            "X-Rate-Limit-Remaining": "1",
            **headers,
        },
        started_at=_STARTED_AT,
        completed_at=_COMPLETED_AT,
        latency_ms=30,
    )


def _adapter(
    response: RawProviderResponse, calls: list[object] | None = None
) -> TheRundownExperimentalAdapter:
    def transport(request: object, timeout: float) -> RawProviderResponse:
        del timeout
        if calls is not None:
            calls.append(request)
        return response

    return TheRundownExperimentalAdapter(
        transport=transport,
        affiliate_ids=("3", "19"),
        affiliate_names={"3": "DraftKings", "19": "FanDuel"},
    )


def _fetch(
    response: RawProviderResponse,
    *,
    config: ProviderConfig | None = None,
    authorization: NetworkAuthorizationContract | None = _AUTHORIZATION,
    calls: list[object] | None = None,
    **kwargs: object,
):
    return _adapter(response, calls).fetch(
        _FIXTURE,
        config or _config(),
        request_identity="synthetic-request-001",
        requested_at=_STARTED_AT,
        provider_priority=0,
        timing_policy=_TIMING,
        authorization=authorization,
        **kwargs,
    )


def _price_rows(payload: dict[str, object]):
    event = payload["events"][0]
    for participant in event["markets"][0]["participants"]:
        for line in participant["lines"]:
            yield from line["prices"].values()


def test_request_uses_official_endpoint_and_redacts_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "synthetic-secret-never-log"
    monkeypatch.setenv("THERUNDOWN_API_KEY", secret)
    request = TheRundownExperimentalAdapter(affiliate_ids=("3",)).request_for_fixture(
        _FIXTURE, _config()
    )

    assert request is not None
    assert request.endpoint.endswith("/sports/16/events/2026-09-22")
    assert request.params["market_ids"] == "1"
    assert request.safe_payload()["headers"]["X-TheRundown-Key"] == "[REDACTED]"
    assert secret not in repr(request.safe_payload())


def test_valid_ucl_response_returns_sorted_candidate_only_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    result = _fetch(_response(_payload()))

    assert result.state is ProviderState.AVAILABLE
    assert result.network_called is True
    assert result.observation is not None
    assert result.observation.provider_identity == "therundown_experimental"
    assert result.observation.bookmaker_identity == "DraftKings"
    assert result.observation.home_odds == 2.5
    assert result.observation.draw_odds == 3.5
    assert result.observation.away_odds == pytest.approx(3.1)
    assert result.observation.candidate_only is True
    assert result.observation.metadata["therundown_sport_id"] == 16
    assert result.quota_after.used == 11
    assert result.quota_after.remaining == 19989

    observations = _adapter(_response(_payload())).normalize_event(
        _payload()["events"][0],
        _FIXTURE,
        response=_response(_payload()),
        request_identity="synthetic-request-001",
        requested_at=_STARTED_AT,
        provider_priority=0,
        config=_config(),
        timing_policy=_TIMING,
    )
    assert [item.bookmaker_identity for item in observations] == [
        "DraftKings",
        "FanDuel",
    ]


def test_date_response_selects_exact_fixture_without_registering_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    payload = _payload()
    unrelated = copy.deepcopy(payload["events"][0])
    unrelated["event_id"] = "cl-event-unrelated"
    unrelated["teams"][1]["name"] = "Another City"
    payload["events"].append(unrelated)
    result = _fetch(_response(payload))

    assert result.state is ProviderState.AVAILABLE
    assert result.observation is not None
    assert result.observation.provider_fixture_id == "cl-event-001"
    assert "therundown_experimental" not in FOOTBALL_PROVIDER_REPERTOIRE


def test_missing_credential_is_fail_closed_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("THERUNDOWN_API_KEY", raising=False)
    calls: list[object] = []
    result = _fetch(_response(_payload()), calls=calls)

    assert result.state is ProviderState.CREDENTIAL_MISSING
    assert result.network_called is False
    assert calls == []


def test_network_authorization_is_required_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    calls: list[object] = []
    result = _fetch(_response(_payload()), authorization=None, calls=calls)

    assert result.state is ProviderState.HEALTH_UNKNOWN
    assert result.network_called is False
    assert calls == []


def test_missing_market_and_missing_draw_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    no_market = _payload()
    no_market["events"][0]["markets"] = []
    result = _fetch(_response(no_market))
    assert result.state is ProviderState.UNSUPPORTED_MARKET

    no_draw = _payload()
    no_draw["events"][0]["markets"][0]["participants"] = [
        participant
        for participant in no_draw["events"][0]["markets"][0]["participants"]
        if participant["name"] != "Draw"
    ]
    result = _fetch(_response(no_draw))
    assert result.state is ProviderState.QUALITY_REJECTED
    assert "missing_draw" in result.reason


def test_invalid_and_off_board_prices_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    malformed = _payload()
    for price in _price_rows(malformed):
        price["price"] = 0.0001
    result = _fetch(_response(malformed))

    assert result.state is ProviderState.QUALITY_REJECTED
    assert result.observation is None


def test_all_stale_bookmakers_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    stale = _payload()
    for price in _price_rows(stale):
        price["updated_at"] = "2026-09-22T17:00:00Z"
    result = _fetch(_response(stale))

    assert result.state is ProviderState.STALE
    assert result.observation is None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda event: event.update({"sport_id": 11}),
            ProviderState.UNSUPPORTED_LEAGUE,
        ),
        (
            lambda event: event["schedule"].update(
                {"league_name": "English Premier League"}
            ),
            ProviderState.UNSUPPORTED_LEAGUE,
        ),
        (
            lambda event: event["teams"][1].update({"name": "Other City"}),
            ProviderState.UNSUPPORTED_FIXTURE,
        ),
    ],
)
def test_competition_and_fixture_identity_are_strict(
    monkeypatch: pytest.MonkeyPatch, change, expected: ProviderState
) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    payload = _payload()
    change(payload["events"][0])
    result = _fetch(_response(payload))

    assert result.state is expected
    assert result.observation is None


def test_wrong_provider_fixture_id_is_not_substituted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    result = _fetch(_response(_payload()), provider_fixture_id="different-event")

    assert result.state is ProviderState.UNSUPPORTED_FIXTURE
    assert result.reason == "event_not_found"


def test_missing_event_and_malformed_payload_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    missing = _payload()
    missing["events"] = []
    result = _fetch(_response(missing))
    assert result.state is ProviderState.UNSUPPORTED_FIXTURE

    malformed = _fetch(_response([]))
    assert malformed.state is ProviderState.MALFORMED


def test_transport_and_provider_payload_errors_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")

    def failing_transport(request: object, timeout: float) -> RawProviderResponse:
        del request, timeout
        raise RuntimeError("synthetic transport failure")

    adapter = TheRundownExperimentalAdapter(transport=failing_transport)
    result = adapter.fetch(
        _FIXTURE,
        _config(),
        request_identity="synthetic-request-001",
        requested_at=_STARTED_AT,
        provider_priority=0,
        timing_policy=_TIMING,
        authorization=_AUTHORIZATION,
    )
    assert result.state is ProviderState.TEMPORARILY_UNAVAILABLE

    error_response = RawProviderResponse(
        status_code=200,
        payload=None,
        headers={},
        started_at=_STARTED_AT,
        completed_at=_COMPLETED_AT,
        latency_ms=30,
        error_code="malformed_json",
    )
    assert _fetch(error_response).state is ProviderState.MALFORMED


def test_duplicate_outcome_and_non_main_line_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    duplicate = _payload()
    home = duplicate["events"][0]["markets"][0]["participants"][0]
    home["lines"].append(copy.deepcopy(home["lines"][0]))
    result = _fetch(_response(duplicate))
    assert result.state is ProviderState.QUALITY_REJECTED
    assert result.reason == "duplicate_observation"

    non_main = _payload()
    for price in _price_rows(non_main):
        price["is_main_line"] = False
    result = _fetch(_response(non_main))
    assert result.state is ProviderState.QUALITY_REJECTED


def test_naive_source_timestamp_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    payload = _payload()
    next(_price_rows(payload))["updated_at"] = "2026-09-22T18:58:00"
    result = _fetch(_response(payload))

    assert result.state is ProviderState.MALFORMED


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (401, {}, ProviderState.AUTH_FAILED),
        (429, {"X-Datapoints-Remaining": "0"}, ProviderState.QUOTA_EXHAUSTED),
        (429, {"X-Datapoints-Remaining": "5"}, ProviderState.RATE_LIMITED),
    ],
)
def test_http_auth_and_quota_states_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str],
    expected: ProviderState,
) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    response = _response({}, **headers)
    response = RawProviderResponse(
        status_code=status,
        payload={},
        headers=response.headers,
        started_at=response.started_at,
        completed_at=response.completed_at,
        latency_ms=response.latency_ms,
    )

    assert _fetch(response).state is expected


def test_duplicate_event_identity_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    payload = _payload()
    payload["events"].append(copy.deepcopy(payload["events"][0]))
    result = _fetch(_response(payload))

    assert result.state is ProviderState.QUALITY_REJECTED
    assert result.reason == "duplicate_event_identity"


def test_ambiguous_team_markers_are_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    payload = _payload()
    payload["events"][0]["teams"][0]["is_home"] = True
    result = _fetch(_response(payload))

    assert result.state is ProviderState.MALFORMED


def test_alias_mapping_and_candidate_only_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THERUNDOWN_API_KEY", "synthetic-secret")
    payload = _payload()
    payload["events"][0]["teams"][1]["name"] = "Home City FC"
    payload["events"][0]["markets"][0]["participants"][0]["name"] = "Home City FC"
    adapter = TheRundownExperimentalAdapter(
        transport=lambda request, timeout: _response(payload),
        affiliate_ids=("3", "19"),
        aliases={"Home City FC": "Home City"},
    )
    result = adapter.fetch(
        _FIXTURE,
        _config(quality_eligible=True),
        request_identity="synthetic-request-001",
        requested_at=_STARTED_AT,
        provider_priority=0,
        timing_policy=_TIMING,
        authorization=_AUTHORIZATION,
    )

    assert result.state is ProviderState.AVAILABLE
    assert result.observation is not None
    assert result.observation.candidate_only is True
    assert result.observation.provider_identity == "therundown_experimental"
