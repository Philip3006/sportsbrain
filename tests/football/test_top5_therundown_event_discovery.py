from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    TheRundownB4QuotaProofV1,
    TheRundownEventDiscoveryAuthorizationV1,
    TheRundownEventDiscoveryResponseV1,
    TheRundownEventDiscoveryTargetV1,
    _validated_datapoint_total,
    b4_quota_proof_state_path,
    discover_five_league_events,
    discovery_authorization_consumption_state_path,
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


@pytest.fixture(autouse=True)
def _canonical_test_state(monkeypatch, tmp_path: Path):
    proof_path = tmp_path / "b4-quota-proof.json"
    consumption_path = tmp_path / "discovery-consumption.json"
    monkeypatch.setattr(
        "src.football.top5_therundown_event_discovery.b4_quota_proof_state_path",
        lambda: proof_path,
    )
    monkeypatch.setattr(
        sys.modules[__name__], "b4_quota_proof_state_path", lambda: proof_path
    )
    monkeypatch.setattr(
        "src.football.top5_therundown_event_discovery.discovery_authorization_consumption_state_path",
        lambda: consumption_path,
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "discovery_authorization_consumption_state_path",
        lambda: consumption_path,
    )
    return proof_path, consumption_path


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


def _install_proof(
    *,
    remaining_datapoints: int = 550,
    finished_at: datetime = NOW - timedelta(minutes=1),
    quota_reset_at: datetime = NOW + timedelta(hours=1),
    response_digest: str = "b" * 64,
) -> TheRundownB4QuotaProofV1:
    proof = TheRundownB4QuotaProofV1(
        quota_proof_id="b4-quota-proof-20260921",
        quota_proof_authorization_id="ceo-quota-proof-20260921",
        provider=THERUNDOWN_PROVIDER_NAME,
        account_scope="therundown-account-test",
        remaining_datapoints=remaining_datapoints,
        observed_at=finished_at - timedelta(seconds=5),
        finished_at=finished_at,
        quota_reset_at=quota_reset_at,
        response_digest=response_digest,
        provenance_source="b4-quota-proof-test",
        evidence_digest="0" * 64,
    )
    payload = {
        **proof._payload_without_digest(),
        "evidence_digest": proof.computed_evidence_digest,
    }
    b4_quota_proof_state_path().parent.mkdir(parents=True, exist_ok=True)
    b4_quota_proof_state_path().write_text(json.dumps(payload))
    return replace(proof, evidence_digest=proof.computed_evidence_digest)


def _authorization() -> TheRundownEventDiscoveryAuthorizationV1:
    proof = _install_proof()
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
        quota_proof_id=proof.quota_proof_id,
        quota_proof_authorization_id=proof.quota_proof_authorization_id,
        quota_proof_evidence_digest=proof.evidence_digest,
        quota_proof_response_digest=proof.response_digest,
        quota_proof_account_scope=proof.account_scope,
        quota_proof_remaining_datapoints=proof.remaining_datapoints,
        quota_proof_observed_at=proof.observed_at,
        quota_proof_finished_at=proof.finished_at,
        quota_proof_reset_at=proof.quota_reset_at,
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


@pytest.mark.parametrize("remaining", [549, 275])
def test_insufficient_canonical_quota_proof_blocks_before_request(remaining):
    authorization = _authorization()
    _install_proof(remaining_datapoints=remaining)
    transport = FakeDiscoveryTransport([])

    with pytest.raises(
        EventDiscoveryExecutionBlocked,
        match="INSUFFICIENT_COMBINED_HEADROOM",
    ):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert transport.calls == []


def test_missing_canonical_quota_proof_blocks_before_request():
    authorization = _authorization()
    b4_quota_proof_state_path().unlink()
    transport = FakeDiscoveryTransport([])

    with pytest.raises(EventDiscoveryExecutionBlocked, match="unavailable"):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert transport.calls == []


def test_stale_or_reset_expired_quota_proof_blocks_before_request():
    authorization = _authorization()
    _install_proof(finished_at=NOW - timedelta(seconds=301))
    transport = FakeDiscoveryTransport([])
    with pytest.raises(EventDiscoveryExecutionBlocked, match="stale"):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert transport.calls == []

    authorization = _authorization()
    _install_proof(quota_reset_at=NOW)
    transport = FakeDiscoveryTransport([])
    with pytest.raises(EventDiscoveryExecutionBlocked, match="reset period"):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert transport.calls == []


def test_altered_quota_proof_digests_and_binding_fail_before_request():
    authorization = _authorization()
    _install_proof(response_digest="c" * 64)
    transport = FakeDiscoveryTransport([])
    with pytest.raises(EventDiscoveryExecutionBlocked, match="binding mismatch"):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert transport.calls == []

    authorization = _authorization()
    proof = _install_proof()
    payload = proof.as_payload()
    payload["evidence_digest"] = "f" * 64
    b4_quota_proof_state_path().write_text(json.dumps(payload))
    transport = FakeDiscoveryTransport([])
    with pytest.raises(EventDiscoveryContractError, match="digest mismatch"):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert transport.calls == []


def test_caller_quota_remaining_cannot_override_canonical_proof():
    authorization = _authorization()
    caller_claim = replace(authorization, quota_proof_remaining_datapoints=275)
    transport = FakeDiscoveryTransport([])

    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events(caller_claim, transport=transport, now=NOW)
    assert transport.calls == []


def test_quota_proof_bindings_are_part_of_authorization_digest():
    authorization = _authorization()
    altered = replace(authorization, quota_proof_response_digest="c" * 64)
    assert altered.authorization_digest != authorization.authorization_digest


def test_authorization_is_consumed_before_request_and_replay_is_blocked():
    authorization = _authorization()
    transport = FakeDiscoveryTransport(
        [_response(target) for target in authorization.targets]
    )
    discover_five_league_events(authorization, transport=transport, now=NOW)
    assert len(transport.calls) == 5

    replay_transport = FakeDiscoveryTransport([])
    with pytest.raises(EventDiscoveryExecutionBlocked, match="already been consumed"):
        discover_five_league_events(authorization, transport=replay_transport, now=NOW)
    assert replay_transport.calls == []


def test_modified_authorization_cannot_reuse_consumed_identity():
    authorization = _authorization()
    transport = FakeDiscoveryTransport(
        [_response(target) for target in authorization.targets]
    )
    discover_five_league_events(authorization, transport=transport, now=NOW)

    proof = _install_proof(response_digest="c" * 64)
    modified = replace(
        authorization,
        quota_proof_response_digest=proof.response_digest,
        quota_proof_evidence_digest=proof.evidence_digest,
    )
    replay_transport = FakeDiscoveryTransport([])
    with pytest.raises(
        EventDiscoveryExecutionBlocked, match="modified after consumption"
    ):
        discover_five_league_events(modified, transport=replay_transport, now=NOW)
    assert replay_transport.calls == []


@pytest.mark.parametrize("failure_index", [1, 3])
def test_transport_failure_after_request_one_or_three_keeps_consumption_marker(
    failure_index,
):
    authorization = _authorization()
    responses: list[TheRundownEventDiscoveryResponseV1 | Exception] = [
        _response(target) for target in authorization.targets
    ]
    responses[failure_index] = EventDiscoveryExecutionBlocked("transport failure")
    transport = FakeDiscoveryTransport(responses)

    with pytest.raises(EventDiscoveryExecutionBlocked, match="transport failure"):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert len(transport.calls) == failure_index + 1

    replay_transport = FakeDiscoveryTransport([])
    with pytest.raises(EventDiscoveryExecutionBlocked, match="already been consumed"):
        discover_five_league_events(authorization, transport=replay_transport, now=NOW)
    assert replay_transport.calls == []


def test_concurrent_replay_has_at_most_one_five_request_batch():
    authorization = _authorization()
    transports = [
        FakeDiscoveryTransport([_response(target) for target in authorization.targets])
        for _ in range(2)
    ]

    def invoke(transport):
        try:
            discover_five_league_events(authorization, transport=transport, now=NOW)
        except EventDiscoveryExecutionBlocked:
            return "blocked"
        return "completed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, transports))
    assert sorted(results) == ["blocked", "completed"]
    assert sum(len(transport.calls) for transport in transports) == 5


def test_tampered_consumption_state_cannot_be_overwritten_or_replayed():
    authorization = _authorization()
    transport = FakeDiscoveryTransport(
        [_response(target) for target in authorization.targets]
    )
    discover_five_league_events(authorization, transport=transport, now=NOW)
    state = json.loads(discovery_authorization_consumption_state_path().read_text())
    state["records"] = []
    discovery_authorization_consumption_state_path().write_text(json.dumps(state))

    replay_transport = FakeDiscoveryTransport([])
    with pytest.raises(EventDiscoveryExecutionBlocked, match="digest mismatch"):
        discover_five_league_events(authorization, transport=replay_transport, now=NOW)
    assert replay_transport.calls == []


def test_discovery_has_no_caller_selected_output_or_authority_path():
    authorization = _authorization()
    transport = FakeDiscoveryTransport(
        [_response(target) for target in authorization.targets]
    )
    evidence = discover_five_league_events(authorization, transport=transport, now=NOW)
    assert discovery_authorization_consumption_state_path().is_file()
    assert all(item.provider_authority is False for item in evidence)
    assert all(item.qualification_eligible is False for item in evidence)
