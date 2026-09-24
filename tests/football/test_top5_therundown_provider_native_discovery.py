from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.football.top5_therundown_provider_native_discovery as native_discovery
from src.football.odds.therundown import (
    THERUNDOWN_BASE_URL,
    THERUNDOWN_PROVIDER_NAME,
    THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_event_discovery import (
    EventDiscoveryExecutionBlocked,
    TheRundownB4QuotaProofV1,
)
from src.football.top5_therundown_network_shadow import (
    TheRundownNetworkHttpResponseV1,
)
from src.football.top5_therundown_provider_native_discovery import (
    DISCOVERY_LEAGUE_ORDER,
    PROVIDER_NATIVE_BILLING_CUMULATIVE_DELTA,
    PROVIDER_NATIVE_BILLING_PROVIDER_HEADER,
    PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE,
    PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION,
    PROVIDER_NATIVE_MAX_DATAPOINTS,
    PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE,
    PROVIDER_NATIVE_MAX_REQUEST_COUNT,
    TheRundownProviderNativeDiscoveryAuthorizationV1,
    TheRundownProviderNativeDiscoveryRequestV1,
    _validated_datapoint_total,
    discover_five_league_events_provider_native,
    provider_native_discovery_request_shape_digest,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
LEAGUE_NAMES = {
    "EPL": "Premier League",
    "BL1": "Bundesliga",
    "LL": "LaLiga",
    "SA": "Serie A",
    "L1": "Ligue 1",
}


@pytest.fixture(autouse=True)
def _isolated_consumption(monkeypatch, tmp_path: Path):
    path = tmp_path / "native-discovery-consumption.json"
    monkeypatch.setattr(
        "src.football.top5_therundown_event_discovery.discovery_authorization_consumption_state_path",
        lambda: path,
    )
    lock_path = tmp_path / "TOP5_REAL_PROVIDER_EXECUTION.lock"
    monkeypatch.setattr(
        "src.football.top5_therundown_provider_native_discovery.top5_real_provider_execution_lock_path",
        lambda: lock_path,
    )
    return path


def _proof() -> TheRundownB4QuotaProofV1:
    return TheRundownB4QuotaProofV1(
        proof_id="proof-native-20260924",
        authorization_id="proof-auth-native-20260924",
        provider=THERUNDOWN_PROVIDER_NAME,
        sport_id=3,
        snapshot_date=NOW.date(),
        account_scope="therundown-free",
        remaining_datapoints=19952,
        response_started_at=NOW - timedelta(minutes=2),
        response_finished_at=NOW - timedelta(minutes=1),
        quota_reset_at=NOW + timedelta(hours=12),
        response_digest="a" * 64,
        evidence_digest="b" * 64,
        request_shape_digest="c" * 64,
        quota_used_datapoints=48,
        quota_limit_datapoints=20000,
        quota_period="daily",
        quota_tier="free",
    )


def _authorization(
    *,
    remaining_datapoints: int = 19952,
    issued_at: datetime = NOW - timedelta(minutes=1),
    expires_at: datetime = NOW + timedelta(hours=1),
) -> TheRundownProviderNativeDiscoveryAuthorizationV1:
    proof = _proof()
    return TheRundownProviderNativeDiscoveryAuthorizationV1(
        discovery_authorization_id="native-discovery-20260924",
        ceo_discovery_authorization_identity="ceo:top5:native-discovery:20260924",
        provider=THERUNDOWN_PROVIDER_NAME,
        search_start_date=NOW.date(),
        adapter_version="therundown-v2-experimental:2",
        adapter_source_sha="d" * 40,
        request_shape_digest=provider_native_discovery_request_shape_digest(NOW.date()),
        quota_proof_id=proof.proof_id,
        quota_proof_authorization_id=proof.authorization_id,
        quota_proof_evidence_digest=proof.evidence_digest,
        quota_proof_response_digest=proof.response_digest,
        quota_proof_account_scope=proof.account_scope,
        quota_proof_remaining_datapoints=remaining_datapoints,
        quota_proof_sport_id=proof.sport_id,
        quota_proof_snapshot_date=proof.snapshot_date,
        quota_proof_observed_at=proof.response_started_at,
        quota_proof_finished_at=proof.response_finished_at,
        quota_proof_reset_at=proof.quota_reset_at,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _event(
    league: str, *, event_id: str | None = None, day: int = 0
) -> dict[str, object]:
    kickoff = NOW + timedelta(days=day, hours=2)
    return {
        "event_id": event_id or f"native-{league}-{day}",
        "sport_id": THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS[league],
        "event_date": kickoff.isoformat(),
        "schedule": {"league_name": LEAGUE_NAMES[league]},
        "score": {"event_status": "STATUS_SCHEDULED"},
        "teams": [
            {
                "team_id": f"home-{league}-{day}",
                "name": f"Home {league}",
                "is_home": True,
            },
            {
                "team_id": f"away-{league}-{day}",
                "name": f"Away {league}",
                "is_away": True,
            },
        ],
    }


def _response(
    league: str,
    *,
    day: int = 0,
    datapoints: int = 55,
    events: list[dict[str, object]] | None = None,
    used: int | None = None,
    remaining: int | None = None,
    include_exact: bool = True,
    headers_override: dict[str, str] | None = None,
    remove_headers: tuple[str, ...] = (),
) -> TheRundownNetworkHttpResponseV1:
    finished = NOW + timedelta(seconds=day + 1)
    current_used = used if used is not None else 48 + datapoints
    current_remaining = remaining if remaining is not None else 20000 - current_used
    response_headers = {
        "x-datapoints-used": str(current_used),
        "x-datapoints-remaining": str(current_remaining),
        "x-datapoints-limit": "20000",
        "x-datapoints-period": "daily",
        "x-datapoints-reset": (NOW + timedelta(hours=12)).isoformat(),
        "x-tier": "free",
    }
    if include_exact:
        response_headers["x-datapoints"] = str(datapoints)
    if headers_override is not None:
        response_headers.update(headers_override)
    for name in remove_headers:
        response_headers.pop(name, None)
    return TheRundownNetworkHttpResponseV1(
        status_code=200,
        payload={"events": events if events is not None else [_event(league, day=day)]},
        headers=response_headers,
        started_at=finished - timedelta(milliseconds=100),
        finished_at=finished,
        body_digest=("e" * 64),
    )


class FakeNativeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        response = self.responses.pop(0)
        return response(request) if callable(response) else response


def _response_sequence(
    entries: list[tuple[str, int, list[dict[str, object]] | None]],
    *,
    include_exact: bool = True,
) -> list[TheRundownNetworkHttpResponseV1]:
    used = 48
    remaining = 19952
    responses = []
    for league, day, events in entries:
        used += 55
        remaining -= 55
        responses.append(
            _response(
                league,
                day=day,
                events=events,
                used=used,
                remaining=remaining,
                include_exact=include_exact,
            )
        )
    return responses


def test_native_discovery_searches_canonical_order_and_stops_after_first_valid_day():
    entries = [
        (league, day, events)
        for league in DISCOVERY_LEAGUE_ORDER
        for day, events in ((0, []), (1, [_event(league, day=1)]))
    ]
    transport = FakeNativeTransport(_response_sequence(entries))

    result = discover_five_league_events_provider_native(
        _authorization(),
        proof=_proof(),
        api_key="injected-test-only",
        transport=transport,
        now=NOW,
        pacer=lambda _: None,
    )

    assert [capture.league for capture in result.captures] == list(
        DISCOVERY_LEAGUE_ORDER
    )
    assert result.request_count == 10
    assert result.datapoint_total == 550
    assert [request.league for request in transport.calls] == [
        league for league in DISCOVERY_LEAGUE_ORDER for _ in (0, 1)
    ]
    assert all(
        capture.discovery_target_source == PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE
        for capture in result.captures
    )
    assert all(
        capture.independent_fixture_source_qualification
        == PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION
        for capture in result.captures
    )
    assert all(
        capture.provider_authority is False and capture.qualification_eligible is False
        for capture in result.captures
    )
    assert all(
        capture.billing_evidence_mode == PROVIDER_NATIVE_BILLING_PROVIDER_HEADER
        for capture in result.captures
    )


def test_native_discovery_has_exact_request_and_datapoint_bounds():
    auth = _authorization()
    assert auth.maximum_dates_per_league == PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE
    assert auth.maximum_request_count == PROVIDER_NATIVE_MAX_REQUEST_COUNT
    assert auth.maximum_datapoints == PROVIDER_NATIVE_MAX_DATAPOINTS
    assert auth.maximum_datapoints_per_request == 55

    too_expensive = FakeNativeTransport(
        [_response(league, datapoints=56) for league in DISCOVERY_LEAGUE_ORDER]
    )
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events_provider_native(
            auth,
            proof=_proof(),
            api_key="injected-test-only",
            transport=too_expensive,
            now=NOW,
            pacer=lambda _: None,
        )
    assert len(too_expensive.calls) == 1


def test_native_discovery_rejects_insufficient_proof_headroom_before_consumption():
    auth = _authorization(remaining_datapoints=3849)
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events_provider_native(
            auth,
            proof=_proof(),
            api_key="injected-test-only",
            transport=FakeNativeTransport([]),
            now=NOW,
            pacer=lambda _: None,
        )


def test_native_discovery_never_exceeds_thirty_five_requests_without_partial_result():
    transport = FakeNativeTransport(
        [
            _response(league, events=[])
            for league in DISCOVERY_LEAGUE_ORDER
            for _ in range(PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE)
        ]
    )
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events_provider_native(
            _authorization(),
            proof=_proof(),
            api_key="injected-test-only",
            transport=transport,
            now=NOW,
            pacer=lambda _: None,
        )
    assert 0 < len(transport.calls) <= PROVIDER_NATIVE_MAX_REQUEST_COUNT


def test_native_discovery_rejects_cross_league_and_incomplete_runs_without_partial_result():
    wrong = _event("EPL", event_id="wrong")
    transport = FakeNativeTransport(
        [
            _response("BL1", events=[wrong])
            for _ in range(PROVIDER_NATIVE_MAX_REQUEST_COUNT)
        ]
    )
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events_provider_native(
            _authorization(),
            proof=_proof(),
            api_key="injected-test-only",
            transport=transport,
            now=NOW,
            pacer=lambda _: None,
        )
    assert 0 < len(transport.calls) <= PROVIDER_NATIVE_MAX_REQUEST_COUNT


def test_native_discovery_consumes_before_loading_credential_and_rejects_replay():
    loader_calls: list[str] = []
    transport = FakeNativeTransport(
        _response_sequence(
            [(league, 0, [_event(league)]) for league in DISCOVERY_LEAGUE_ORDER]
        )
    )

    def load_credential() -> str:
        loader_calls.append("loaded")
        return "injected-test-only"

    auth = _authorization()
    discover_five_league_events_provider_native(
        auth,
        proof=_proof(),
        credential_loader=load_credential,
        transport=transport,
        now=NOW,
        pacer=lambda _: None,
    )
    assert loader_calls == ["loaded"]
    replay = FakeNativeTransport([])
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events_provider_native(
            auth,
            proof=_proof(),
            credential_loader=load_credential,
            transport=replay,
            now=NOW,
            pacer=lambda _: None,
        )
    assert loader_calls == ["loaded"]
    assert replay.calls == []


def test_native_discovery_rejects_duplicate_provider_event_identity():
    duplicate = _event("EPL", event_id="same")
    transport = FakeNativeTransport([_response("EPL", events=[duplicate, duplicate])])
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events_provider_native(
            _authorization(),
            proof=_proof(),
            api_key="injected-test-only",
            transport=transport,
            now=NOW,
            pacer=lambda _: None,
        )


def test_native_discovery_expired_authorization_and_unsafe_flags_fail_closed():
    expired = _authorization(expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(EventDiscoveryExecutionBlocked):
        expired.validate(now=NOW)
    unsafe = _authorization()
    object.__setattr__(unsafe, "production_authority", True)
    with pytest.raises(EventDiscoveryExecutionBlocked):
        unsafe.validate(now=NOW)


def test_native_fixture_identity_is_canonical_and_provider_identity_is_separate():
    transport = FakeNativeTransport(
        _response_sequence(
            [(league, 0, [_event(league)]) for league in DISCOVERY_LEAGUE_ORDER]
        )
    )
    result = discover_five_league_events_provider_native(
        _authorization(),
        proof=_proof(),
        api_key="injected-test-only",
        transport=transport,
        now=NOW,
        pacer=lambda _: None,
    )
    ll = result.captures[2]
    assert ll.provider_event_id == "native-LL-0"
    assert ll.fixture_key == make_fixture_key(
        "LL", "Home LL", "Away LL", NOW + timedelta(hours=2)
    )
    assert ll.request_identity
    assert ll.raw_response_digest == "e" * 64


def test_native_request_shape_is_the_reviewed_provider_shape():
    digest = provider_native_discovery_request_shape_digest(date(2026, 9, 24))
    assert len(digest) == 64
    assert THERUNDOWN_BASE_URL in "".join(
        f"{THERUNDOWN_BASE_URL}/sports/{THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS[league]}"
        for league in DISCOVERY_LEAGUE_ORDER
    )


def test_native_request_shape_binds_each_league_and_date_to_actual_endpoint():
    authorization = _authorization()
    for league_index, league in enumerate(DISCOVERY_LEAGUE_ORDER):
        for date_offset in range(PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE):
            request = TheRundownProviderNativeDiscoveryRequestV1(
                authorization=authorization,
                league=league,
                snapshot_date=NOW.date() + timedelta(days=date_offset),
                sequence=league_index * PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE
                + date_offset,
                date_offset=date_offset,
                league_index=league_index,
            )
            assert request.endpoint == (
                f"{THERUNDOWN_BASE_URL}/sports/"
                f"{THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS[league]}/events/"
                f"{request.snapshot_date.isoformat()}"
            )


def test_native_discovery_accepts_cumulative_quota_delta_without_exact_header():
    transport = FakeNativeTransport(
        _response_sequence(
            [(league, 0, [_event(league)]) for league in DISCOVERY_LEAGUE_ORDER],
            include_exact=False,
        )
    )
    result = discover_five_league_events_provider_native(
        _authorization(),
        proof=_proof(),
        api_key="injected-test-only",
        transport=transport,
        now=NOW,
        pacer=lambda _: None,
    )
    assert result.datapoint_total == 275
    assert all(
        capture.billing_evidence_mode == PROVIDER_NATIVE_BILLING_CUMULATIVE_DELTA
        for capture in result.captures
    )


@pytest.mark.parametrize(
    ("headers_override", "remove_headers"),
    [
        ({"x-datapoints-used": "102", "x-datapoints-remaining": "19898"}, ()),
        ({"x-datapoints-used": "47", "x-datapoints-remaining": "19953"}, ()),
        ({"x-datapoints-used": "49", "x-datapoints-remaining": "19953"}, ()),
        ({"x-datapoints-limit": "20001"}, ()),
        ({"x-datapoints-period": "monthly"}, ()),
        ({"x-datapoints-reset": (NOW + timedelta(hours=13)).isoformat()}, ()),
        ({"x-tier": "paid"}, ()),
        ({}, ("x-datapoints-used",)),
    ],
)
def test_native_discovery_rejects_invalid_cumulative_quota_evidence(
    headers_override: dict[str, str], remove_headers: tuple[str, ...]
):
    response = _response(
        "EPL",
        used=103,
        remaining=19897,
        headers_override=headers_override,
        remove_headers=remove_headers,
    )
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events_provider_native(
            _authorization(),
            proof=_proof(),
            api_key="injected-test-only",
            transport=FakeNativeTransport([response]),
            now=NOW,
            pacer=lambda _: None,
        )


def test_native_discovery_rejects_inferred_delta_above_per_request_cap():
    response = _response(
        "EPL",
        datapoints=56,
        used=104,
        remaining=19896,
        include_exact=False,
    )
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events_provider_native(
            _authorization(),
            proof=_proof(),
            api_key="injected-test-only",
            transport=FakeNativeTransport([response]),
            now=NOW,
            pacer=lambda _: None,
        )


def test_native_discovery_rejects_exact_header_when_delta_disagrees():
    response = _response("EPL", datapoints=55, used=102, remaining=19898)
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events_provider_native(
            _authorization(),
            proof=_proof(),
            api_key="injected-test-only",
            transport=FakeNativeTransport([response]),
            now=NOW,
            pacer=lambda _: None,
        )


def test_native_discovery_rejects_cumulative_total_above_governed_budget():
    with pytest.raises(EventDiscoveryExecutionBlocked):
        _validated_datapoint_total(
            PROVIDER_NATIVE_MAX_DATAPOINTS,
            1,
            maximum=PROVIDER_NATIVE_MAX_DATAPOINTS,
        )


def test_native_discovery_uses_exclusive_execution_resource_without_authority():
    observed = []

    class LockObservingTransport(FakeNativeTransport):
        def execute(self, request):
            observed.append(
                native_discovery.top5_real_provider_execution_lock_path().exists()
            )
            return super().execute(request)

    transport = LockObservingTransport(
        _response_sequence(
            [(league, 0, [_event(league)]) for league in DISCOVERY_LEAGUE_ORDER]
        )
    )
    result = discover_five_league_events_provider_native(
        _authorization(),
        proof=_proof(),
        api_key="injected-test-only",
        transport=transport,
        now=NOW,
        pacer=lambda _: None,
    )
    assert observed and all(observed)
    assert all(not capture.provider_authority for capture in result.captures)
