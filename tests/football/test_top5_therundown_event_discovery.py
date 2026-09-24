from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.football.odds.therundown import (
    THERUNDOWN_BASE_URL,
    THERUNDOWN_PROVIDER_NAME,
    THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS,
)
from src.football.production_contracts import Fixture
from src.football.top5_builder2_qualification_receipt import (
    Builder2QualificationReceiptV1,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_event_discovery import (
    B4_QUOTA_PROOF_AFFILIATE_IDS,
    B4_QUOTA_PROOF_EXECUTION_PHASE,
    B4_QUOTA_PROOF_MAX_DATAPOINTS,
    B4_QUOTA_PROOF_PACKAGE_SCHEMA_VERSION,
    B4_QUOTA_PROOF_SCHEMA_VERSION,
    B4_QUOTA_PROOF_SPORT_ID,
    DISCOVERY_LEAGUE_ORDER,
    EventDiscoveryContractError,
    EventDiscoveryExecutionBlocked,
    TheRundownB4QuotaProofV1,
    TheRundownEventDiscoveryAuthorizationV1,
    TheRundownEventDiscoveryResponseV1,
    TheRundownEventDiscoveryTargetV1,
    Top5CurrentDiscoveryTargetManifestV1,
    _validated_datapoint_total,
    b4_quota_proof_state_path,
    discover_five_league_events,
    discovery_authorization_consumption_state_path,
    discovery_request_shape_digest,
    materialize_prebound_network_configuration,
    select_current_top5_discovery_targets,
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


def _payload_digest(value: object) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _install_proof(
    *,
    remaining_datapoints: int = 550,
    snapshot_date: date | None = None,
    finished_at: datetime = NOW - timedelta(minutes=1),
    quota_reset_at: datetime = NOW + timedelta(hours=1),
    response_digest: str = "b" * 64,
) -> TheRundownB4QuotaProofV1 | None:
    proof_id = "b4-quota-proof-20260921"
    authorization_id = "ceo-quota-proof-20260921"
    if snapshot_date is None:
        snapshot_date = NOW.date()
    authorization_package_digest = "a" * 64
    configuration_digest = "c" * 64
    response_started_at = finished_at - timedelta(seconds=1)
    quota_used_datapoints = 1000 - remaining_datapoints
    request_shape_digest = _payload_digest(
        {
            "method": "GET",
            "endpoint": (
                f"{THERUNDOWN_BASE_URL}/sports/{B4_QUOTA_PROOF_SPORT_ID}/events/"
                f"{snapshot_date.isoformat()}"
            ),
            "query": {
                "affiliate_ids": ",".join(B4_QUOTA_PROOF_AFFILIATE_IDS),
                "hide_closed": "true",
                "main_line": "true",
                "market_ids": "1",
            },
        }
    )
    proof_payload = {
        "schema_version": B4_QUOTA_PROOF_SCHEMA_VERSION,
        "execution_phase": B4_QUOTA_PROOF_EXECUTION_PHASE,
        "proof_id": proof_id,
        "provider": THERUNDOWN_PROVIDER_NAME,
        "sport_id": B4_QUOTA_PROOF_SPORT_ID,
        "snapshot_date": snapshot_date.isoformat(),
        "account_scope": "therundown-account-test",
        "authorization_package_digest": authorization_package_digest,
        "configuration_digest": configuration_digest,
        "authorization_id": authorization_id,
        "controlled_shadow_run_id": "quota-proof-only",
        "qualification_session_id": "quota-proof-only",
        "ceo_authorization_identity": "ceo-proof-20260921",
        "request_shape_digest": request_shape_digest,
        "credential_binding_digest": "e" * 64,
        "request_started_at": response_started_at.isoformat(),
        "response_finished_at": finished_at.isoformat(),
        "billed_datapoints": B4_QUOTA_PROOF_MAX_DATAPOINTS,
        "remaining_datapoints": remaining_datapoints,
        "quota_used_datapoints": quota_used_datapoints,
        "quota_limit_datapoints": 1000,
        "quota_period": "daily",
        "quota_reset_at": quota_reset_at.isoformat(),
        "raw_header_evidence": {
            "x-datapoints": str(B4_QUOTA_PROOF_MAX_DATAPOINTS),
            "x-datapoints-used": str(quota_used_datapoints),
            "x-datapoints-remaining": str(remaining_datapoints),
            "x-datapoints-limit": "1000",
            "x-datapoints-period": "daily",
            "x-datapoints-reset": quota_reset_at.isoformat(),
            "x-tier": "free",
            "x-rate-limit": "1",
            "x-data-delay-seconds": "0",
        },
        "response_digest": response_digest,
        "status_code": 200,
        "request_count": 1,
        "retry_count": 0,
        "no_retry": True,
    }
    proof_payload["evidence_digest"] = _payload_digest(proof_payload)
    package = {
        "schema_version": B4_QUOTA_PROOF_PACKAGE_SCHEMA_VERSION,
        "execution_phase": B4_QUOTA_PROOF_EXECUTION_PHASE,
        "proof": proof_payload,
        "request": {
            "proof_id": proof_id,
            "provider": THERUNDOWN_PROVIDER_NAME,
            "sport_id": B4_QUOTA_PROOF_SPORT_ID,
            "snapshot_date": snapshot_date.isoformat(),
            "authorization_package_digest": authorization_package_digest,
            "configuration_digest": configuration_digest,
            "authorization_id": authorization_id,
            "controlled_shadow_run_id": "quota-proof-only",
            "qualification_session_id": "quota-proof-only",
            "ceo_authorization_identity": "ceo-proof-20260921",
            "adapter_version": "therundown-v2-experimental:2",
            "adapter_source_sha": "a" * 40,
            "endpoint": (
                f"{THERUNDOWN_BASE_URL}/sports/{B4_QUOTA_PROOF_SPORT_ID}/events/"
                f"{snapshot_date.isoformat()}"
            ),
            "query": {
                "affiliate_ids": ",".join(B4_QUOTA_PROOF_AFFILIATE_IDS),
                "hide_closed": "true",
                "main_line": "true",
                "market_ids": "1",
            },
            "request_shape_digest": request_shape_digest,
            "maximum_datapoints": B4_QUOTA_PROOF_MAX_DATAPOINTS,
            "request_count": 1,
            "retry_count": 0,
        },
        "spend_control": {
            "provider": THERUNDOWN_PROVIDER_NAME,
            "evidence_kind": "provider_response_headers",
            "account_tier": "free",
            "overage_exposure": "none",
            "observed_at": finished_at.isoformat(),
            "digest": "d" * 64,
        },
        "safety": {
            "five_league_requests": 0,
            "receipt_issued": False,
            "authority_changed": False,
            "activation": False,
            "publication": False,
            "betting": False,
            "monetary_spend_authorized": False,
        },
    }
    b4_quota_proof_state_path().parent.mkdir(parents=True, exist_ok=True)
    b4_quota_proof_state_path().write_text(json.dumps(package))
    try:
        return TheRundownB4QuotaProofV1.from_package(package, now=NOW)
    except EventDiscoveryContractError:
        return None


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
        quota_proof_id=proof.proof_id,
        quota_proof_authorization_id=proof.authorization_id,
        quota_proof_evidence_digest=proof.evidence_digest,
        quota_proof_response_digest=proof.response_digest,
        quota_proof_account_scope=proof.account_scope,
        quota_proof_remaining_datapoints=proof.remaining_datapoints,
        quota_proof_sport_id=proof.sport_id,
        quota_proof_snapshot_date=proof.snapshot_date,
        quota_proof_observed_at=proof.response_started_at,
        quota_proof_finished_at=proof.response_finished_at,
        quota_proof_reset_at=proof.quota_reset_at,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
    )


def test_discovery_request_cost_remains_55_when_b4_proof_cap_is_56():
    authorization = _authorization()
    assert B4_QUOTA_PROOF_MAX_DATAPOINTS == 56
    assert authorization.maximum_datapoints_per_request == 55


def _event(
    target: TheRundownEventDiscoveryTargetV1, *, event_id: str | None = None
) -> dict[str, object]:
    home_participant_id = target.home_participant_id or f"provider-home-{target.league}"
    away_participant_id = target.away_participant_id or f"provider-away-{target.league}"
    return {
        "event_id": event_id or f"event-{target.league}",
        "sport_id": THERUNDOWN_VERIFIED_LEAGUE_SPORT_IDS[target.league],
        "event_date": target.kickoff.isoformat(),
        "schedule": {"league_name": LEAGUE_NAMES[target.league]},
        "score": {"event_status": "STATUS_SCHEDULED"},
        "teams": [
            {
                "team_id": home_participant_id,
                "name": target.home_team,
                "is_home": True,
            },
            {
                "team_id": away_participant_id,
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


def _scope_authorization() -> TheRundownEventDiscoveryAuthorizationV1:
    authorization = _authorization()
    targets = tuple(
        replace(target, home_participant_id=None, away_participant_id=None)
        for target in authorization.targets
    )
    return replace(
        authorization,
        targets=targets,
        request_shape_digest=discovery_request_shape_digest(targets),
    )


def test_discovery_target_scope_does_not_require_provider_participant_ids():
    target = _scope_authorization().targets[0]
    target.validate()
    payload = target.as_payload()
    assert "home_participant_id" not in payload
    assert "away_participant_id" not in payload


def test_provider_participant_ids_are_bound_from_matched_response():
    authorization = _scope_authorization()
    evidence = discover_five_league_events(
        authorization,
        transport=FakeDiscoveryTransport(
            [_response(target) for target in authorization.targets]
        ),
        now=NOW,
    )
    assert evidence[0].home_participant_id == "provider-home-EPL"
    assert evidence[0].away_participant_id == "provider-away-EPL"
    configuration = materialize_prebound_network_configuration(authorization, evidence)
    assert configuration.participant_scope[0].home_participant_id == "provider-home-EPL"
    assert configuration.participant_scope[0].away_participant_id == "provider-away-EPL"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda event: {
            **event,
            "teams": [{**event["teams"][0], "team_id": ""}, event["teams"][1]],
        },
        lambda event: {
            **event,
            "teams": [
                {**event["teams"][0], "team_id": "same"},
                {**event["teams"][1], "team_id": "same"},
            ],
        },
    ],
    ids=["missing-provider-id", "duplicate-provider-id"],
)
def test_response_side_provider_identity_is_fail_closed(mutator):
    authorization = _scope_authorization()
    target = authorization.targets[0]
    broken = mutator(_event(target))
    with pytest.raises(EventDiscoveryExecutionBlocked):
        discover_five_league_events(
            authorization,
            transport=FakeDiscoveryTransport([_response(target, events=[broken])]),
            now=NOW,
        )


def _current_fixture_source(*, now: datetime = NOW) -> list[Fixture]:
    return [
        Fixture(
            make_fixture_key(
                league,
                f"Home {league}",
                f"Away {league}",
                now + timedelta(hours=2, minutes=index),
            ),
            league,
            f"Home {league}",
            f"Away {league}",
            now + timedelta(hours=2, minutes=index),
        )
        for index, league in enumerate(DISCOVERY_LEAGUE_ORDER)
    ]


def test_current_target_manifest_selection_is_deterministic_and_round_trips():
    fixtures = _current_fixture_source()
    manifest = select_current_top5_discovery_targets(
        list(reversed(fixtures)),
        now=NOW,
        observed_at=NOW - timedelta(minutes=2),
        source_provenance="the_odds_api:current_fixture_source",
        source_release_sha="a" * 40,
        runtime_data_sha="b" * 64,
    )
    manifest.validate(now=NOW)
    assert tuple(target.league for target in manifest.targets) == DISCOVERY_LEAGUE_ORDER
    assert all(target.home_participant_id is None for target in manifest.targets)
    assert all(target.away_participant_id is None for target in manifest.targets)
    loaded = Top5CurrentDiscoveryTargetManifestV1.from_payload(
        manifest.as_payload(), now=NOW
    )
    assert loaded.manifest_digest == manifest.manifest_digest
    assert loaded.targets == manifest.targets


def test_current_target_manifest_rejects_stale_or_synthetic_source():
    with pytest.raises(EventDiscoveryExecutionBlocked, match="stale"):
        select_current_top5_discovery_targets(
            _current_fixture_source(),
            now=NOW,
            observed_at=NOW - timedelta(hours=2),
            source_provenance="the_odds_api:current_fixture_source",
            source_release_sha="a" * 40,
        )
    with pytest.raises(EventDiscoveryExecutionBlocked, match="synthetic"):
        select_current_top5_discovery_targets(
            _current_fixture_source(),
            now=NOW,
            observed_at=NOW - timedelta(minutes=1),
            source_provenance="synthetic://top5/current",
            source_release_sha="a" * 40,
        )


def test_current_target_manifest_rejects_missing_league_and_wrong_order():
    fixtures = _current_fixture_source()
    with pytest.raises(EventDiscoveryExecutionBlocked, match="no eligible L1"):
        select_current_top5_discovery_targets(
            fixtures[:-1],
            now=NOW,
            observed_at=NOW - timedelta(minutes=1),
            source_provenance="the_odds_api:current_fixture_source",
            source_release_sha="a" * 40,
        )
    manifest = select_current_top5_discovery_targets(
        fixtures,
        now=NOW,
        observed_at=NOW - timedelta(minutes=1),
        source_provenance="the_odds_api:current_fixture_source",
        source_release_sha="a" * 40,
    )
    broken = replace(
        manifest,
        targets=(manifest.targets[1], manifest.targets[0], *manifest.targets[2:]),
    )
    with pytest.raises(
        EventDiscoveryContractError, match="digest mismatch|league order"
    ):
        broken.validate(now=NOW)


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


def test_unmodified_pr144_package_shape_loads_directly():
    _authorization()
    proof = TheRundownB4QuotaProofV1.load_canonical(now=NOW)
    assert proof.proof_id == "b4-quota-proof-20260921"
    assert proof.authorization_id == "ceo-quota-proof-20260921"
    assert proof.sport_id == B4_QUOTA_PROOF_SPORT_ID
    assert proof.snapshot_date == NOW.date()
    assert proof.remaining_datapoints == 550
    assert proof.response_digest == "b" * 64
    assert len(proof.evidence_digest) == 64


@pytest.mark.parametrize(
    "mutator, message",
    [
        (
            lambda package: package.update(
                schema_version="top5-therundown-b4-quota-proof-v1"
            ),
            "package schema",
        ),
        (lambda package: package.pop("proof"), "proof is missing"),
        (
            lambda package: package["proof"].update(
                schema_version="top5-therundown-b4-quota-proof-v0"
            ),
            "nested B4 quota proof schema",
        ),
    ],
    ids=["outer-wrong-schema", "missing-proof", "nested-wrong-schema"],
)
def test_pr144_package_shape_mismatches_fail_before_transport(mutator, message):
    authorization = _authorization()
    package = json.loads(b4_quota_proof_state_path().read_text())
    mutator(package)
    b4_quota_proof_state_path().write_text(json.dumps(package))
    transport = FakeDiscoveryTransport([])

    with pytest.raises(EventDiscoveryContractError, match=message):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert transport.calls == []


def test_pr144_response_digest_tamper_fails_against_direct_nested_binding():
    authorization = _authorization()
    _install_proof(response_digest="c" * 64)
    transport = FakeDiscoveryTransport([])

    with pytest.raises(EventDiscoveryExecutionBlocked, match="binding mismatch"):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert transport.calls == []


def test_dated_snapshot_binding_rejects_historical_proof_before_transport():
    authorization = _authorization()
    _install_proof(snapshot_date=NOW.date() - timedelta(days=1))
    transport = FakeDiscoveryTransport([])

    with pytest.raises(EventDiscoveryExecutionBlocked, match="historical"):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert transport.calls == []


def test_retired_event_bound_proof_shape_is_not_accepted():
    authorization = _authorization()
    package = json.loads(b4_quota_proof_state_path().read_text())
    package["proof"]["provider_event_id"] = "legacy-event-id"
    b4_quota_proof_state_path().write_text(json.dumps(package))
    transport = FakeDiscoveryTransport([])

    with pytest.raises(EventDiscoveryContractError, match="B4 nested proof shape"):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert transport.calls == []


def test_remaining_550_is_accepted_from_nested_proof():
    authorization = _authorization()
    transport = FakeDiscoveryTransport(
        [_response(target) for target in authorization.targets]
    )
    evidence = discover_five_league_events(authorization, transport=transport, now=NOW)
    assert len(evidence) == 5
    assert len(transport.calls) == 5


@pytest.mark.parametrize(
    "field, value",
    [
        ("account_scope", "attacker-account"),
        ("authorization_id", "attacker-proof-authorization"),
    ],
    ids=["account-scope", "proof-authorization-id"],
)
def test_nested_identity_alteration_fails_against_authorization_binding(field, value):
    authorization = _authorization()
    package = json.loads(b4_quota_proof_state_path().read_text())
    package["proof"][field] = value
    proof_without_digest = {
        key: item for key, item in package["proof"].items() if key != "evidence_digest"
    }
    package["proof"]["evidence_digest"] = _payload_digest(proof_without_digest)
    b4_quota_proof_state_path().write_text(json.dumps(package))
    transport = FakeDiscoveryTransport([])

    with pytest.raises(EventDiscoveryExecutionBlocked, match="binding mismatch"):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert transport.calls == []


def test_package_safety_metadata_cannot_grant_discovery_authority():
    authorization = _authorization()
    package = json.loads(b4_quota_proof_state_path().read_text())
    package["safety"]["authority_changed"] = True
    b4_quota_proof_state_path().write_text(json.dumps(package))
    transport = FakeDiscoveryTransport([])

    with pytest.raises(EventDiscoveryExecutionBlocked, match="safety metadata"):
        discover_five_league_events(authorization, transport=transport, now=NOW)
    assert transport.calls == []
    assert authorization.production_authority is False
    assert authorization.activation is False


def test_flat_proof_alias_is_not_an_accepted_canonical_artifact():
    authorization = _authorization()
    b4_quota_proof_state_path().write_text(
        json.dumps(
            {
                "schema_version": B4_QUOTA_PROOF_SCHEMA_VERSION,
                "proof_id": "flat-proof",
                "remaining_datapoints": 550,
            }
        )
    )
    transport = FakeDiscoveryTransport([])

    with pytest.raises(EventDiscoveryContractError, match="unsupported.*schema"):
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
    package = json.loads(b4_quota_proof_state_path().read_text())
    package["proof"]["evidence_digest"] = "f" * 64
    b4_quota_proof_state_path().write_text(json.dumps(package))
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
