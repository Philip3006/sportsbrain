from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

import pytest

from src.football.odds.therundown import (
    THERUNDOWN_BASE_URL,
    THERUNDOWN_PROVIDER_NAME,
    THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS,
    TheRundownExperimentalAdapter,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_event_discovery import (
    B4_QUOTA_PROOF_AFFILIATE_IDS,
    EventDiscoveryContractError,
    EventDiscoveryExecutionBlocked,
    TheRundownB4QuotaProofV1,
)
from src.football.top5_therundown_network_shadow import (
    TheRundownNetworkHttpResponseV1,
)
from src.football.top5_therundown_provider_native_discovery import (
    DISCOVERY_LEAGUE_ORDER,
    PROVIDER_NATIVE_BILLING_MODE_CUMULATIVE,
    PROVIDER_NATIVE_BILLING_MODE_PROVIDER,
    PROVIDER_NATIVE_DISCOVERY_AUTHORIZATION_SCHEMA_VERSION,
    PROVIDER_NATIVE_DISCOVERY_STRUCTURAL_AUTHORIZATION_SCHEMA_VERSION,
    PROVIDER_NATIVE_DISCOVERY_TARGET_SOURCE,
    PROVIDER_NATIVE_INDEPENDENT_QUALIFICATION,
    PROVIDER_NATIVE_MAX_DATAPOINTS,
    PROVIDER_NATIVE_MAX_DATAPOINTS_PER_REQUEST,
    PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE,
    PROVIDER_NATIVE_MAX_REQUEST_COUNT,
    PROVIDER_NATIVE_MAXIMUM_RETRIES,
    PROVIDER_NATIVE_MINIMUM_HEADROOM,
    PROVIDER_NATIVE_MINIMUM_INTERVAL_SECONDS,
    TheRundownProviderNativeDiscoveryAuthorizationV1,
    ProviderNativeDiscoverySelectionPurpose,
    TheRundownProviderNativeDiscoveryRequestV1,
    _real_provider_execution_lock,
    _request_shape_payload,
    _select_candidate,
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
    remaining_datapoints: int | None = None,
    minimum_lead_seconds: int = 0,
    maximum_lead_seconds: int = 22 * 24 * 60 * 60,
    issued_at: datetime = NOW - timedelta(minutes=1),
    expires_at: datetime = NOW + timedelta(hours=1),
    proof: TheRundownB4QuotaProofV1 | None = None,
) -> TheRundownProviderNativeDiscoveryAuthorizationV1:
    proof = proof or _proof()
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
        quota_proof_remaining_datapoints=(
            proof.remaining_datapoints
            if remaining_datapoints is None
            else remaining_datapoints
        ),
        quota_proof_sport_id=proof.sport_id,
        quota_proof_snapshot_date=proof.snapshot_date,
        quota_proof_observed_at=proof.response_started_at,
        quota_proof_finished_at=proof.response_finished_at,
        quota_proof_reset_at=proof.quota_reset_at,
        issued_at=issued_at,
        expires_at=expires_at,
        minimum_lead_seconds=minimum_lead_seconds,
        maximum_lead_seconds=maximum_lead_seconds,
    )


def _event(
    league: str,
    *,
    event_id: str | None = None,
    day: int = 0,
    kickoff: datetime | None = None,
) -> dict[str, object]:
    kickoff = kickoff or NOW + timedelta(days=day, hours=2)
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


def _proof_with_cumulative_baseline(
    *, used: int = 100, remaining: int = 19900
) -> TheRundownB4QuotaProofV1:
    return TheRundownB4QuotaProofV1(
        **{
            **_proof().__dict__,
            "remaining_datapoints": remaining,
            "quota_used_datapoints": used,
            "quota_limit_datapoints": used + remaining,
            "quota_period": "daily",
            "quota_tier": "free",
        }
    )


def _cumulative_response(
    league: str,
    *,
    used: int,
    remaining: int,
    include_provider_datapoints: bool = False,
    datapoints: int = 55,
    period: str = "daily",
    reset: datetime = NOW + timedelta(hours=12),
    tier: str = "free",
    events: list[dict[str, object]] | None = None,
) -> TheRundownNetworkHttpResponseV1:
    response = _response(league, events=events or [_event(league)])
    headers = {
        "x-datapoints-used": str(used),
        "x-datapoints-remaining": str(remaining),
        "x-datapoints-limit": str(used + remaining),
        "x-datapoints-period": period,
        "x-datapoints-reset": reset.isoformat(),
        "x-tier": tier,
    }
    if include_provider_datapoints:
        headers["x-datapoints"] = str(datapoints)
    return TheRundownNetworkHttpResponseV1(
        status_code=200,
        payload=response.payload,
        headers=headers,
        started_at=response.started_at,
        finished_at=response.finished_at,
        body_digest=response.body_digest,
    )


def _run_with_cumulative_responses(
    responses: list[TheRundownNetworkHttpResponseV1],
    *,
    proof: TheRundownB4QuotaProofV1 | None = None,
    authorization: TheRundownProviderNativeDiscoveryAuthorizationV1 | None = None,
):
    proof = proof or _proof_with_cumulative_baseline()
    authorization = authorization or _authorization(proof=proof)
    return discover_five_league_events_provider_native(
        authorization,
        proof=proof,
        api_key="injected-test-only",
        transport=FakeNativeTransport(responses),
        now=NOW,
        pacer=lambda _: None,
    )


class FakeNativeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        response = self.responses.pop(0)
        return response(request) if callable(response) else response


@pytest.mark.parametrize(
    ("lead_seconds", "eligible"),
    [
        (599, False),
        (600, True),
        (1800, True),
        (3600, True),
        (3601, False),
    ],
)
def test_native_candidate_must_be_within_explicit_inclusive_lead_window(
    lead_seconds: int, eligible: bool
):
    candidate = _select_candidate(
        TheRundownExperimentalAdapter(),
        {"events": [_event("EPL", kickoff=NOW + timedelta(seconds=lead_seconds))]},
        league="EPL",
        now=NOW,
        selection_purpose=ProviderNativeDiscoverySelectionPurpose.SIGNAL_TIME,
        minimum_lead_seconds=600,
        maximum_lead_seconds=3600,
    )

    assert (candidate is not None) is eligible


def test_native_candidate_chooses_earliest_eligible_not_earlier_out_of_window():
    selected = _select_candidate(
        TheRundownExperimentalAdapter(),
        {
            "events": [
                _event(
                    "EPL",
                    event_id="eligible-later",
                    kickoff=NOW + timedelta(seconds=900),
                ),
                _event(
                    "EPL",
                    event_id="too-early",
                    kickoff=NOW + timedelta(seconds=599),
                ),
                _event(
                    "EPL",
                    event_id="eligible-earliest",
                    kickoff=NOW + timedelta(seconds=600),
                ),
            ]
        },
        league="EPL",
        now=NOW,
        selection_purpose=ProviderNativeDiscoverySelectionPurpose.SIGNAL_TIME,
        minimum_lead_seconds=600,
        maximum_lead_seconds=3600,
    )

    assert selected is not None
    assert selected.provider_event_id == "eligible-earliest"


def test_native_candidate_keeps_deterministic_team_tie_breaking():
    alpha = _event("EPL", event_id="tie-alpha", kickoff=NOW + timedelta(seconds=1200))
    alpha["teams"][0]["name"] = "Alpha Home"
    zulu = _event("EPL", event_id="tie-zulu", kickoff=NOW + timedelta(seconds=1200))
    zulu["teams"][0]["name"] = "Zulu Home"

    selected = _select_candidate(
        TheRundownExperimentalAdapter(),
        {"events": [zulu, alpha]},
        league="EPL",
        now=NOW,
        selection_purpose=ProviderNativeDiscoverySelectionPurpose.SIGNAL_TIME,
        minimum_lead_seconds=600,
        maximum_lead_seconds=3600,
    )

    assert selected is not None
    assert selected.home_team == "Alpha Home"


def test_native_discovery_authorization_requires_explicit_window_and_binds_it():
    authorization = _authorization(
        minimum_lead_seconds=600,
        maximum_lead_seconds=3600,
    )
    payload = authorization.as_payload()

    assert (
        payload["schema_version"]
        == PROVIDER_NATIVE_DISCOVERY_AUTHORIZATION_SCHEMA_VERSION
    )
    assert payload["minimum_lead_seconds"] == 600
    assert payload["maximum_lead_seconds"] == 3600
    restored = type(authorization).from_payload(payload)
    assert restored == authorization
    assert restored.authorization_digest == authorization.authorization_digest
    different_window = _authorization(
        minimum_lead_seconds=600,
        maximum_lead_seconds=3601,
    )
    assert different_window.authorization_digest != authorization.authorization_digest
    assert different_window.request_shape_digest == authorization.request_shape_digest

    payload.pop("minimum_lead_seconds")
    payload.pop("authorization_digest")
    with pytest.raises(EventDiscoveryContractError, match="payload shape is invalid"):
        type(authorization).from_payload(payload)

    with pytest.raises(EventDiscoveryContractError, match="cannot exceed"):
        _authorization(minimum_lead_seconds=3601, maximum_lead_seconds=3600).validate(
            now=NOW
        )


def test_structural_discovery_authorization_is_typed_and_contains_no_lead_defaults():
    authorization = replace(
        _authorization(),
        minimum_lead_seconds=None,
        maximum_lead_seconds=None,
        selection_purpose=ProviderNativeDiscoverySelectionPurpose.STRUCTURAL_PROVIDER,
    )
    payload = authorization.as_payload()

    assert (
        payload["schema_version"]
        == PROVIDER_NATIVE_DISCOVERY_STRUCTURAL_AUTHORIZATION_SCHEMA_VERSION
    )
    assert payload["selection_purpose"] == "STRUCTURAL_PROVIDER"
    assert "minimum_lead_seconds" not in payload
    assert "maximum_lead_seconds" not in payload
    restored = type(authorization).from_payload(payload)
    assert restored == authorization
    assert restored.authorization_digest == authorization.authorization_digest

    malformed = dict(payload)
    malformed["selection_purpose"] = "SIGNAL_TIME"
    with pytest.raises(EventDiscoveryContractError, match="selection purpose"):
        type(authorization).from_payload(malformed)


def test_structural_discovery_selects_earliest_future_without_signal_window():
    selected = _select_candidate(
        TheRundownExperimentalAdapter(),
        {
            "events": [
                _event("EPL", event_id="past", kickoff=NOW - timedelta(seconds=1)),
                _event(
                    "EPL",
                    event_id="later",
                    kickoff=NOW + timedelta(days=16),
                ),
                _event(
                    "EPL",
                    event_id="earliest-future",
                    kickoff=NOW + timedelta(days=14),
                ),
            ]
        },
        league="EPL",
        now=NOW,
        selection_purpose=ProviderNativeDiscoverySelectionPurpose.STRUCTURAL_PROVIDER,
        minimum_lead_seconds=None,
        maximum_lead_seconds=None,
    )

    assert selected is not None
    assert selected.provider_event_id == "earliest-future"


def test_structural_discovery_does_not_select_past_or_in_play_events():
    selected = _select_candidate(
        TheRundownExperimentalAdapter(),
        {
            "events": [
                _event("EPL", event_id="past", kickoff=NOW - timedelta(seconds=1)),
                _event("EPL", event_id="now", kickoff=NOW),
            ]
        },
        league="EPL",
        now=NOW,
        selection_purpose=ProviderNativeDiscoverySelectionPurpose.STRUCTURAL_PROVIDER,
        minimum_lead_seconds=None,
        maximum_lead_seconds=None,
    )

    assert selected is None


def test_native_discovery_defers_without_falling_back_outside_the_window():
    transport = FakeNativeTransport(
        [
            _response(
                "EPL",
                day=offset,
                events=[
                    _event(
                        "EPL",
                        event_id=f"too-soon-{offset}",
                        kickoff=NOW + timedelta(seconds=100),
                    )
                ],
            )
            for offset in range(PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE)
        ]
    )

    with pytest.raises(
        EventDiscoveryExecutionBlocked,
        match="no eligible target within the authorized search for EPL",
    ):
        discover_five_league_events_provider_native(
            _authorization(minimum_lead_seconds=600, maximum_lead_seconds=3600),
            proof=_proof(),
            api_key="injected-test-only",
            transport=transport,
            now=NOW,
            pacer=lambda _: None,
        )

    assert len(transport.calls) == PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE


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
    assert set(result.billing_modes) == {PROVIDER_NATIVE_BILLING_MODE_PROVIDER}


def test_native_request_shape_binds_each_league_to_its_verified_sport_endpoint():
    authorization = _authorization()
    planned = _request_shape_payload(NOW.date())
    assert len(planned) == 105
    expected_sport_ids = {
        "EPL": 11,
        "BL1": 13,
        "LL": 14,
        "SA": 15,
        "L1": 12,
    }
    for planned_index, planned_request in enumerate(planned):
        league_index, date_offset = divmod(planned_index, 21)
        league = DISCOVERY_LEAGUE_ORDER[league_index]
        request = TheRundownProviderNativeDiscoveryRequestV1(
            authorization=authorization,
            league=league,
            snapshot_date=NOW.date() + timedelta(days=date_offset),
            sequence=planned_index,
            date_offset=date_offset,
            league_index=league_index,
        )
        actual = request.as_http_request(api_key="injected-test-only")
        assert actual.endpoint == (
            f"{THERUNDOWN_BASE_URL}/sports/{expected_sport_ids[league]}"
            f"/events/{(NOW.date() + timedelta(days=date_offset)).isoformat()}"
        )
        assert dict(actual.query) == planned_request["query"]
        assert actual.query["affiliate_ids"] == ",".join(B4_QUOTA_PROOF_AFFILIATE_IDS)
    assert (
        authorization.request_shape_digest
        == provider_native_discovery_request_shape_digest(NOW.date())
    )


def test_native_discovery_has_exact_21_day_request_and_datapoint_bounds():
    auth = _authorization()
    assert PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE == 21
    assert PROVIDER_NATIVE_MAX_REQUEST_COUNT == 105
    assert PROVIDER_NATIVE_MAX_DATAPOINTS_PER_REQUEST == 55
    assert PROVIDER_NATIVE_MAX_DATAPOINTS == 5775
    assert PROVIDER_NATIVE_MINIMUM_HEADROOM == 11550
    assert PROVIDER_NATIVE_MINIMUM_INTERVAL_SECONDS == 1.1
    assert PROVIDER_NATIVE_MAXIMUM_RETRIES == 0
    assert auth.maximum_dates_per_league == 21
    assert auth.maximum_request_count == 105
    assert auth.maximum_datapoints == 5775
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


def test_provider_x_datapoints_over_cap_is_rejected_with_safe_diagnostic():
    transport = FakeNativeTransport([_response("EPL", datapoints=56)])
    with pytest.raises(
        EventDiscoveryExecutionBlocked,
        match="discovery request datapoint cap exceeded",
    ) as caught:
        discover_five_league_events_provider_native(
            _authorization(),
            proof=_proof(),
            api_key="injected-test-only",
            transport=transport,
            now=NOW,
            pacer=lambda _: None,
        )

    assert caught.value.diagnostic == {
        "diagnostic_kind": "rejected_observed_x_datapoints",
        "evidence_status": "rejected_observed",
        "header": "x-datapoints",
        "raw_provider_value": "56",
        "authorized_cap": 55,
        "accepted_as_billing": False,
    }
    assert len(transport.calls) == 1


def test_native_authorization_rejects_stale_headroom_and_unsafe_pacing():
    low_headroom = _authorization(
        remaining_datapoints=PROVIDER_NATIVE_MINIMUM_HEADROOM - 1
    )
    with pytest.raises(EventDiscoveryExecutionBlocked):
        low_headroom.validate(now=NOW)

    too_fast = _authorization()
    object.__setattr__(too_fast, "minimum_interval_seconds", 1.0)
    with pytest.raises(EventDiscoveryExecutionBlocked):
        too_fast.validate(now=NOW)


def test_native_discovery_rejects_insufficient_proof_headroom_before_consumption():
    auth = _authorization(remaining_datapoints=PROVIDER_NATIVE_MINIMUM_HEADROOM - 1)
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events_provider_native(
            auth,
            proof=_proof(),
            api_key="injected-test-only",
            transport=FakeNativeTransport([]),
            now=NOW,
            pacer=lambda _: None,
        )


def test_native_discovery_never_exceeds_105_requests_without_partial_result():
    transport = FakeNativeTransport(
        [
            _response(
                league,
                day=day,
                events=[] if day < PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE - 1 else None,
            )
            for league in DISCOVERY_LEAGUE_ORDER
            for day in range(PROVIDER_NATIVE_MAX_DATES_PER_LEAGUE)
        ]
    )
    result = discover_five_league_events_provider_native(
        _authorization(),
        proof=_proof(),
        api_key="injected-test-only",
        transport=transport,
        now=NOW,
        pacer=lambda _: None,
    )
    assert len(transport.calls) == PROVIDER_NATIVE_MAX_REQUEST_COUNT == 105
    assert result.datapoint_total == PROVIDER_NATIVE_MAX_DATAPOINTS == 5775


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


def test_missing_provider_datapoints_uses_cumulative_quota_delta():
    proof = _proof_with_cumulative_baseline()
    responses = [
        _cumulative_response(
            league,
            used=155 + index * 55,
            remaining=19845 - index * 55,
        )
        for index, league in enumerate(DISCOVERY_LEAGUE_ORDER)
    ]
    result = _run_with_cumulative_responses(responses, proof=proof)
    assert result.datapoint_total == 275
    assert result.billing_modes == (PROVIDER_NATIVE_BILLING_MODE_CUMULATIVE,) * 5
    assert result.billing_datapoints == (55,) * 5
    assert all(
        capture.billing_mode == PROVIDER_NATIVE_BILLING_MODE_CUMULATIVE
        for capture in result.captures
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda headers: headers.update(
            {
                "x-datapoints-used": "156",
                "x-datapoints-remaining": "19844",
            }
        ),
        lambda headers: headers.update(
            {
                "x-datapoints-used": "101",
                "x-datapoints-remaining": "19901",
            }
        ),
        lambda headers: headers.update(
            {
                "x-datapoints-used": "100",
                "x-datapoints-remaining": "19900",
                "x-datapoints-limit": "20001",
            }
        ),
        lambda headers: headers.update({"x-datapoints-period": "weekly"}),
        lambda headers: headers.update(
            {"x-datapoints-reset": (NOW + timedelta(hours=13)).isoformat()}
        ),
        lambda headers: headers.update({"x-tier": "pro"}),
    ],
)
def test_invalid_cumulative_quota_transitions_fail_closed(mutate):
    proof = _proof_with_cumulative_baseline()
    response = _cumulative_response("EPL", used=155, remaining=19845)
    headers = dict(response.headers)
    mutate(headers)
    invalid = TheRundownNetworkHttpResponseV1(
        status_code=response.status_code,
        payload=response.payload,
        headers=headers,
        started_at=response.started_at,
        finished_at=response.finished_at,
        body_digest=response.body_digest,
    )
    with pytest.raises(EventDiscoveryExecutionBlocked):
        _run_with_cumulative_responses([invalid], proof=proof)


def test_decreasing_used_and_increasing_remaining_counters_fail_closed():
    proof = _proof_with_cumulative_baseline()
    for used, remaining in ((99, 19901), (101, 19901)):
        response = _cumulative_response("EPL", used=used, remaining=remaining)
        with pytest.raises(EventDiscoveryExecutionBlocked):
            _run_with_cumulative_responses([response], proof=proof)


def test_inferred_cumulative_delta_above_request_cap_fails_closed():
    proof = _proof_with_cumulative_baseline()
    response = _cumulative_response("EPL", used=156, remaining=19844)
    with pytest.raises(EventDiscoveryExecutionBlocked):
        _run_with_cumulative_responses([response], proof=proof)


def test_missing_cumulative_header_fails_closed_when_provider_datapoints_absent():
    proof = _proof_with_cumulative_baseline()
    response = _cumulative_response("EPL", used=155, remaining=19845)
    headers = dict(response.headers)
    del headers["x-datapoints-period"]
    incomplete = TheRundownNetworkHttpResponseV1(
        status_code=response.status_code,
        payload=response.payload,
        headers=headers,
        started_at=response.started_at,
        finished_at=response.finished_at,
        body_digest=response.body_digest,
    )
    with pytest.raises(EventDiscoveryExecutionBlocked):
        _run_with_cumulative_responses([incomplete], proof=proof)


def test_provider_datapoints_must_agree_with_cumulative_delta():
    proof = _proof_with_cumulative_baseline()
    response = _cumulative_response(
        "EPL",
        used=155,
        remaining=19845,
        include_provider_datapoints=True,
        datapoints=54,
    )
    with pytest.raises(EventDiscoveryExecutionBlocked):
        _run_with_cumulative_responses([response], proof=proof)


def test_cumulative_fallback_requires_the_exclusive_real_provider_lock():
    proof = _proof_with_cumulative_baseline()
    response = _cumulative_response("EPL", used=155, remaining=19845)
    with (
        _real_provider_execution_lock(),
        pytest.raises(EventDiscoveryExecutionBlocked, match="lock"),
    ):
        _run_with_cumulative_responses([response], proof=proof)


def test_cumulative_fallback_failure_emits_no_partial_result():
    proof = _proof_with_cumulative_baseline()
    first = _cumulative_response("EPL", used=155, remaining=19845)
    second = _cumulative_response("BL1", used=211, remaining=19844)
    with pytest.raises(EventDiscoveryExecutionBlocked):
        _run_with_cumulative_responses([first, second], proof=proof)


def test_candidate_provider_remains_non_authoritative_on_cumulative_path():
    proof = _proof_with_cumulative_baseline()
    responses = [
        _cumulative_response(
            league,
            used=155 + index * 55,
            remaining=19845 - index * 55,
        )
        for index, league in enumerate(DISCOVERY_LEAGUE_ORDER)
    ]
    result = _run_with_cumulative_responses(responses, proof=proof)
    assert all(
        not capture.provider_authority
        and not capture.activation_authorized
        and not capture.publication_authorized
        and not capture.ledger_mutated
        for capture in result.captures
    )


def test_run_result_rejects_cumulative_total_above_bound():
    proof = _proof_with_cumulative_baseline()
    result = _run_with_cumulative_responses(
        [
            _cumulative_response(
                league,
                used=155 + index * 55,
                remaining=19845 - index * 55,
            )
            for index, league in enumerate(DISCOVERY_LEAGUE_ORDER)
        ],
        proof=proof,
    )
    object.__setattr__(result, "datapoint_total", PROVIDER_NATIVE_MAX_DATAPOINTS + 1)
    with pytest.raises(EventDiscoveryExecutionBlocked):
        result.validate()
