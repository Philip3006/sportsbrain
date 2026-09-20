from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.football.odds.therundown import (
    THERUNDOWN_PROVIDER_NAME,
    THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS,
)
from src.football.top5_builder2_qualification_receipt import (
    Builder2QualificationReceiptV1,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_event_discovery import (
    DISCOVERY_LEAGUE_ORDER,
    EventDiscoveryContractError,
    EventDiscoveryExecutionBlocked,
    TheRundownEventDiscoveryAuthorizationV1,
    TheRundownEventDiscoveryResponseV1,
    TheRundownEventDiscoveryTargetV1,
    _validated_datapoint_total,
    discover_five_league_events,
    discovery_request_shape_digest,
    materialize_prebound_network_configuration,
)
from src.football.top5_therundown_network_shadow import (
    TheRundownNetworkAuthorizationV1,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
LEAGUE_NAMES = {
    "EPL": "Premier League",
    "BL1": "Bundesliga",
    "LL": "La Liga",
    "SA": "Serie A",
    "L1": "Ligue 1",
}


def _target(league: str, index: int) -> TheRundownEventDiscoveryTargetV1:
    kickoff = NOW + timedelta(hours=1, minutes=index)
    home = f"Home {league}"
    away = f"Away {league}"
    return TheRundownEventDiscoveryTargetV1(
        provider=THERUNDOWN_PROVIDER_NAME,
        league=league,
        fixture_key=make_fixture_key(league, home, away, kickoff),
        home_team=home,
        away_team=away,
        kickoff=kickoff,
        home_participant_id=f"home-{league}",
        away_participant_id=f"away-{league}",
        request_identity=f"discovery-request-{league}-{index}",
    )


def _authorization() -> TheRundownEventDiscoveryAuthorizationV1:
    targets = tuple(
        _target(league, index) for index, league in enumerate(DISCOVERY_LEAGUE_ORDER)
    )
    return TheRundownEventDiscoveryAuthorizationV1(
        discovery_authorization_id="discovery-auth-20260921",
        ceo_discovery_authorization_identity="ceo-discovery-20260921",
        provider=THERUNDOWN_PROVIDER_NAME,
        targets=targets,
        adapter_version="therundown-v2-experimental:2",
        adapter_source_sha="a" * 40,
        request_shape_digest=discovery_request_shape_digest(targets),
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
    )


def _event(
    target: TheRundownEventDiscoveryTargetV1, *, event_id: str | None = None
) -> dict[str, object]:
    return {
        "event_id": event_id or f"event-{target.league}",
        "sport_id": THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS[target.league],
        "event_date": target.kickoff.isoformat(),
        "schedule": {"league_name": LEAGUE_NAMES[target.league]},
        "score": {"event_status": "STATUS_SCHEDULED"},
        "teams": [
            {
                "team_id": target.home_participant_id,
                "name": target.home_team,
                "is_home": True,
            },
            {
                "team_id": target.away_participant_id,
                "name": target.away_team,
                "is_away": True,
            },
        ],
    }


def _response(
    target: TheRundownEventDiscoveryTargetV1,
    *,
    datapoints: int = 55,
    events: list[dict[str, object]] | None = None,
    headers: dict[str, str] | None = None,
    finished_at: datetime = NOW - timedelta(minutes=1),
) -> TheRundownEventDiscoveryResponseV1:
    used = datapoints
    response_headers = {
        "x-datapoints": str(datapoints),
        "x-datapoints-used": str(used),
        "x-datapoints-remaining": str(1000 - used),
        "x-datapoints-limit": "1000",
    }
    if headers is not None:
        response_headers = headers
    return TheRundownEventDiscoveryResponseV1(
        status_code=200,
        payload={"events": events or [_event(target)]},
        headers=response_headers,
        started_at=finished_at - timedelta(seconds=1),
        finished_at=finished_at,
        retry_count=0,
        network_execution=False,
    )


class FakeDiscoveryTransport:
    def __init__(self, responses: list[TheRundownEventDiscoveryResponseV1 | Exception]):
        self.responses = responses
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def test_fake_five_league_discovery_is_ordered_non_authorizing_and_materializes_prebound_ids():
    authorization = _authorization()
    transport = FakeDiscoveryTransport(
        [_response(target) for target in authorization.targets]
    )
    waits: list[float] = []

    evidence = discover_five_league_events(
        authorization, transport=transport, now=NOW, pacer=waits.append
    )

    assert [request.target.league for request in transport.calls] == list(
        DISCOVERY_LEAGUE_ORDER
    )
    assert waits == [authorization.minimum_interval_seconds] * 4
    assert [item.league for item in evidence] == list(DISCOVERY_LEAGUE_ORDER)
    assert [item.provider_event_id for item in evidence] == [
        f"event-{league}" for league in DISCOVERY_LEAGUE_ORDER
    ]
    assert all(item.network_execution is False for item in evidence)
    assert all(item.qualification_eligible is False for item in evidence)
    assert all(item.receipt_eligible is False for item in evidence)
    assert all(item.provider_authority is False for item in evidence)
    assert all(item.activation_authorized is False for item in evidence)
    assert all(item.publication_authorized is False for item in evidence)
    assert all(item.monetary_spend_authorized is False for item in evidence)

    configuration = materialize_prebound_network_configuration(authorization, evidence)
    assert configuration.enabled is False
    assert [target.league for target in configuration.targets] == list(
        DISCOVERY_LEAGUE_ORDER
    )
    assert [target.provider_event_id for target in configuration.targets] == [
        f"event-{league}" for league in DISCOVERY_LEAGUE_ORDER
    ]
    assert configuration.production_activation is False
    assert configuration.publication is False
    assert configuration.monetary_spend_authorized is False


def test_discovery_is_not_a_real_run_authorization():
    authorization = _authorization()
    transport = FakeDiscoveryTransport(
        [_response(target) for target in authorization.targets]
    )
    evidence = discover_five_league_events(authorization, transport=transport, now=NOW)
    configuration = materialize_prebound_network_configuration(authorization, evidence)

    assert not hasattr(authorization, "controlled_shadow_run_id")
    assert not hasattr(authorization.targets[0], "provider_event_id")
    assert not isinstance(authorization, TheRundownNetworkAuthorizationV1)
    assert all(
        not isinstance(item, Builder2QualificationReceiptV1) for item in evidence
    )
    assert not any(item.provider_authority for item in evidence)
    assert configuration.enabled is False
    assert all(item.activation_authorized is False for item in evidence)
    assert all(item.publication_authorized is False for item in evidence)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda event: {**event, "sport_id": 999},
        lambda event: {**event, "score": {"event_status": "STATUS_IN_PROGRESS"}},
        lambda event: {**event, "event_id": ""},
        lambda event: {
            **event,
            "teams": [
                {**event["teams"][0], "name": "Wrong Home"},
                event["teams"][1],
            ],
        },
    ],
    ids=["wrong-league", "live-event", "malformed-event-id", "wrong-participant"],
)
def test_identity_and_state_mismatch_fail_closed(mutator):
    authorization = _authorization()
    first = _event(authorization.targets[0])
    broken = mutator(first)
    transport = FakeDiscoveryTransport(
        [_response(authorization.targets[0], events=[broken])]
    )

    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert len(transport.calls) == 1


def test_duplicate_matching_events_fail_closed():
    authorization = _authorization()
    target = authorization.targets[0]
    events = [_event(target, event_id="event-a"), _event(target, event_id="event-b")]
    transport = FakeDiscoveryTransport([_response(target, events=events)])

    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events(authorization, transport=transport, now=NOW)


def test_duplicate_provider_event_id_fails_closed_even_with_one_match():
    authorization = _authorization()
    target = authorization.targets[0]
    duplicate = _event(target, event_id="event-a")
    unrelated = {**_event(target, event_id="event-a"), "sport_id": 999}
    transport = FakeDiscoveryTransport(
        [_response(target, events=[duplicate, unrelated])]
    )

    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events(authorization, transport=transport, now=NOW)


def test_kickoff_mismatch_fails_closed():
    authorization = _authorization()
    target = authorization.targets[0]
    mismatched = {
        **_event(target),
        "event_date": (target.kickoff + timedelta(minutes=2)).isoformat(),
    }
    transport = FakeDiscoveryTransport([_response(target, events=[mismatched])])

    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events(authorization, transport=transport, now=NOW)


def test_stale_matching_event_fails_closed():
    authorization = _authorization()
    target = authorization.targets[0]
    transport = FakeDiscoveryTransport(
        [_response(target, finished_at=target.kickoff + timedelta(seconds=1))]
    )

    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events(authorization, transport=transport, now=NOW)


def test_failure_on_request_n_stops_later_requests_and_emits_no_partial_set():
    authorization = _authorization()
    transport = FakeDiscoveryTransport(
        [
            _response(authorization.targets[0]),
            _response(authorization.targets[1]),
            EventDiscoveryExecutionBlocked("synthetic transport failure"),
            _response(authorization.targets[3]),
            _response(authorization.targets[4]),
        ]
    )

    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert [request.target.league for request in transport.calls] == [
        "EPL",
        "BL1",
        "LL",
    ]


def test_budget_and_quota_metadata_fail_closed_before_identity_resolution():
    authorization = _authorization()
    target = authorization.targets[0]
    over_cap = FakeDiscoveryTransport([_response(target, datapoints=56)])
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events(authorization, transport=over_cap, now=NOW)

    missing_header = FakeDiscoveryTransport(
        [_response(target, headers={"x-datapoints": "55"})]
    )
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events(authorization, transport=missing_header, now=NOW)

    with pytest.raises(EventDiscoveryExecutionBlocked):
        _validated_datapoint_total(275, 1)


def test_retry_metadata_is_never_accepted():
    authorization = _authorization()
    target = authorization.targets[0]
    retry_response = replace(_response(target), retry_count=1)
    transport = FakeDiscoveryTransport([retry_response])

    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events(authorization, transport=transport, now=NOW)


def test_authorization_requires_exact_canonical_five_league_order():
    authorization = _authorization()
    with pytest.raises(EventDiscoveryContractError):
        replace(authorization, targets=authorization.targets[:4]).validate(now=NOW)
    with pytest.raises(EventDiscoveryContractError):
        replace(
            authorization,
            targets=(
                authorization.targets[0],
                authorization.targets[0],
                *authorization.targets[2:],
            ),
        ).validate(now=NOW)


def test_tampered_discovery_event_id_cannot_materialize_strict_config():
    authorization = _authorization()
    transport = FakeDiscoveryTransport(
        [_response(target) for target in authorization.targets]
    )
    evidence = discover_five_league_events(authorization, transport=transport, now=NOW)
    tampered = (replace(evidence[0], provider_event_id="attacker-event"), *evidence[1:])

    with pytest.raises(EventDiscoveryContractError):
        materialize_prebound_network_configuration(authorization, tampered)


def test_altered_request_shape_or_response_digest_fails_closed():
    authorization = _authorization()
    with pytest.raises(EventDiscoveryContractError):
        replace(authorization, request_shape_digest="b" * 64).validate(now=NOW)

    transport = FakeDiscoveryTransport(
        [_response(target) for target in authorization.targets]
    )
    evidence = discover_five_league_events(authorization, transport=transport, now=NOW)
    tampered = replace(evidence[0], raw_response_digest="c" * 64)
    with pytest.raises(EventDiscoveryContractError):
        tampered.validate()


def test_provider_participant_id_mismatch_fails_closed():
    authorization = _authorization()
    target = authorization.targets[0]
    broken = _event(target)
    broken["teams"][0]["team_id"] = "attacker-home"
    transport = FakeDiscoveryTransport([_response(target, events=[broken])])

    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events(authorization, transport=transport, now=NOW)


def test_discovery_cannot_enable_the_strict_network_configuration():
    authorization = _authorization()
    transport = FakeDiscoveryTransport(
        [_response(target) for target in authorization.targets]
    )
    evidence = discover_five_league_events(authorization, transport=transport, now=NOW)

    with pytest.raises(EventDiscoveryExecutionBlocked):
        materialize_prebound_network_configuration(
            authorization, evidence, enabled=True
        )
