from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

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
    PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE,
    PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION,
    PROVIDER_NATIVE_MAX_DATAPOINTS,
    PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE,
    PROVIDER_NATIVE_MAX_REQUEST_COUNT,
    TheRundownProviderNativeDiscoveryAuthorizationV1,
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
) -> TheRundownNetworkHttpResponseV1:
    finished = NOW + timedelta(seconds=day + 1)
    return TheRundownNetworkHttpResponseV1(
        status_code=200,
        payload={"events": events if events is not None else [_event(league, day=day)]},
        headers={
            "x-datapoints": str(datapoints),
            "x-datapoints-used": str(datapoints),
            "x-datapoints-remaining": str(20000 - datapoints),
            "x-datapoints-limit": "20000",
        },
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


def test_native_discovery_searches_canonical_order_and_stops_after_first_valid_day():
    responses = []
    for league in DISCOVERY_LEAGUE_ORDER:
        responses.append(_response(league, events=[]))
        responses.append(_response(league, day=1))
    transport = FakeNativeTransport(responses)

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
        [_response(league) for league in DISCOVERY_LEAGUE_ORDER]
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
        [_response(league) for league in DISCOVERY_LEAGUE_ORDER]
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
