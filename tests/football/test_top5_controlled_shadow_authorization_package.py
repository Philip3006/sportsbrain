"""Offline proof for the five-league shadow authorization boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.football.odds.therundown import THERUNDOWN_ADAPTER_VERSION
from src.football.provider_cascade.contracts import QuotaSnapshot
from src.football.top5_controlled_shadow_authorization_package import (
    CANONICAL_CANDIDATE_PROVIDER,
    FUTURE_EXECUTION_COMMAND,
    TOP5_LEAGUE_ORDER,
    ControlledShadowAuthorizationPackageError,
    prepare_authorization_package,
    reconcile_controlled_shadow_run,
    reconcile_controlled_shadow_run_with_b1_ll_artifact,
    run_guarded_network_execution,
    run_guarded_network_preflight,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    ObservationEvidenceKind,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_network_shadow import (
    NetworkShadowRunStatus,
    TheRundownCanonicalPayloadAdapterV1,
    TheRundownNetworkHttpResponseV1,
    TheRundownNetworkParticipantScopeV1,
    TheRundownNetworkRequestScopeV1,
    TheRundownNetworkShadowExecutorV1,
    TheRundownReplayTransportV1,
)
from tests.football.test_top5_therundown_network_shadow import (
    NOW,
    _authorization,
    _configuration,
    _NetworkStubTransport,
    _response,
    _targets,
)


def _network_run():
    targets = tuple(
        replace(target, provider=CANONICAL_CANDIDATE_PROVIDER) for target in _targets()
    )
    configuration = _configuration(targets=targets, enabled=True)
    authorization = _authorization(configuration, provider=CANONICAL_CANDIDATE_PROVIDER)
    transport = _NetworkStubTransport(
        lambda request: _response(
            request,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            network_execution=True,
        )
    )
    result = TheRundownNetworkShadowExecutorV1(
        clock=lambda: NOW,
        pacer=lambda _seconds: None,
        allow_live_network=True,
    ).run(configuration, authorization, transport=transport)
    return result, configuration, authorization


def _capture(result, league: str):
    return next(item for item in result.captures if item.target.league == league)


def _replace_capture(result, original, **changes):
    captures = list(result.captures)
    index = captures.index(original)
    captures[index] = replace(original, **changes)
    return replace(result, captures=tuple(captures))


def _b1_ll_artifact(result, authorization):
    capture = _capture(result, "LL")
    target = capture.target
    request = capture.request
    response = capture.response
    return {
        "schema_version": "top5-therundown-ll-evidence-bundle-v1",
        "capture_status": "CAPTURED",
        "capture_reason": "captured",
        "authorization": {
            "provider": CANONICAL_CANDIDATE_PROVIDER,
            "league": "LL",
            "maximum_request_count": 2,
            "maximum_datapoint_budget": 100,
            "quota_before_used": response.quota_before,
            "quota_before_remaining": response.rate_limit_remaining,
            "expires_at": authorization.expires_at.isoformat(),
            "controlled_shadow_run_id": authorization.controlled_shadow_run_id,
            "ceo_authorization_id": authorization.authorization_id,
            "qualification_session_id": authorization.qualification_session_id,
            "no_bet": True,
            "no_publication": True,
            "no_activation": True,
            "no_spend": True,
        },
        "b1_bridge_inputs": {
            "controlled_shadow_run_id": authorization.controlled_shadow_run_id,
            "ceo_authorization_id": authorization.authorization_id,
            "qualification_session_id": authorization.qualification_session_id,
            "provider_identity": CANONICAL_CANDIDATE_PROVIDER,
            "league": "LL",
            "fixture_key": f"therundown:LL:{response.provider_event_id}",
            "provider_event_id": response.provider_event_id,
            "provider_request_id": request.request_identity,
            "home_team": target.home_team,
            "away_team": target.away_team,
            "kickoff": target.kickoff.isoformat(),
            "adapter_version": response.adapter_version,
            "adapter_source_sha": response.adapter_source_sha,
            "raw_response_digest": response.raw_response_digest,
            "provider_record_digests": [response.provider_record_digest],
            "normalized_record_digests": [response.normalized_record_digest],
            "source_timestamps": [response.source_timestamp.isoformat()],
            "captured_at": response.captured_at.isoformat(),
            "participant_ids": {
                "home": request.home_participant_id,
                "away": request.away_participant_id,
                "draw": "draw-id-LL",
            },
            "participant_names": {
                "home": target.home_team,
                "away": target.away_team,
                "draw": "Draw",
            },
            "quota_before": {"used": response.quota_before},
            "quota_after": {"used": response.quota_after},
            "rate_limit_state": {"remaining": response.rate_limit_remaining},
            "quota_evidence": {"source": "B1 synthetic contract fixture"},
            "network_execution": True,
            "no_bet": True,
            "publication": False,
            "production_activation": False,
            "monetary_spend_authorized": False,
            "evidence_kind": "REAL_OBSERVED",
            "market_phase": "PRE_MATCH",
            "market_type": "football:pre_match:1x2",
            "cascade_evidence_digest": None,
            "cascade_evidence_required_from_b4": True,
            "capture_attestation_required_from_b4": True,
        },
        "bookmaker_observations": [
            {
                "bookmaker_id": "bookmaker-a-id",
                "bookmaker_name": response.bookmaker_identity,
                "odds": {
                    "home": response.home_odds,
                    "draw": response.draw_odds,
                    "away": response.away_odds,
                },
                "source_timestamp": response.source_timestamp.isoformat(),
                "captured_at": response.captured_at.isoformat(),
                "provider_record_digest": response.provider_record_digest,
                "normalized_record_digest": response.normalized_record_digest,
                "quota_before": {"used": response.quota_before},
                "quota_after": {"used": response.quota_after},
                "rate_limit_state": {"remaining": response.rate_limit_remaining},
                "metadata": {},
            }
        ],
        "raw_response_digest": response.raw_response_digest,
        "adapter_version": response.adapter_version,
        "adapter_source_sha": response.adapter_source_sha,
        "safety": {
            "candidate_only": True,
            "quality_eligible": False,
            "no_bet": True,
            "no_publication": True,
            "no_activation": True,
            "no_spend": True,
        },
    }


def _run006_network_run():
    body_path = Path(
        "/private/tmp/top5-laliga-nextdate-20260920-006.request-2.body.json"
    )
    headers_path = body_path.with_name(
        "top5-laliga-nextdate-20260920-006.request-2.headers.json"
    )
    metadata_path = body_path.with_name(
        "top5-laliga-nextdate-20260920-006.request-2.meta.json"
    )
    if not all(path.exists() for path in (body_path, headers_path, metadata_path)):
        pytest.skip("Run-006 offline artifacts are not available")
    payload = json.loads(body_path.read_text())
    headers = json.loads(headers_path.read_text())
    metadata = json.loads(metadata_path.read_text())
    run_now = datetime.fromisoformat(metadata["completed_at"])
    adapter_source_sha = (
        "67f67ff97cb072bfca03bae688acbf87074359be9af31a36d890483ecd4fe152"
    )
    kickoff = datetime.fromisoformat("2026-09-20T12:00:00+00:00")
    ll_target = replace(
        _targets()[2],
        provider=CANONICAL_CANDIDATE_PROVIDER,
        fixture_key=make_fixture_key("LL", "Getafe", "Málaga", kickoff),
        provider_event_id="48e87c231045c73e2318f6b4d5327405",
        home_team="Getafe",
        away_team="Málaga",
        kickoff=kickoff,
    )
    targets = tuple(
        ll_target
        if target.league == "LL"
        else replace(
            target,
            provider=CANONICAL_CANDIDATE_PROVIDER,
            kickoff=run_now + timedelta(hours=2),
            fixture_key=make_fixture_key(
                target.league,
                target.home_team,
                target.away_team,
                run_now + timedelta(hours=2),
            ),
        )
        for target in _targets()
    )
    participant_scope = tuple(
        TheRundownNetworkParticipantScopeV1(
            target.fixture_key,
            "3952" if target.league == "LL" else f"home-id-{target.league}",
            "133109" if target.league == "LL" else f"away-id-{target.league}",
        )
        for target in targets
    )
    request_scope = tuple(
        TheRundownNetworkRequestScopeV1(
            target.fixture_key,
            (
                "therundown-ll:top5-laliga-real-capture-20260920-006:2026-09-20"
                if target.league == "LL"
                else f"request-{target.league}"
            ),
        )
        for target in targets
    )
    configuration = _configuration(
        targets=targets,
        participant_scope=participant_scope,
        request_scope=request_scope,
        adapter_version=THERUNDOWN_ADAPTER_VERSION,
        adapter_source_sha=adapter_source_sha,
        maximum_source_age_seconds=900,
        enabled=True,
    )
    authorization = _authorization(
        configuration,
        provider=CANONICAL_CANDIDATE_PROVIDER,
        authorization_id="CEO-TOP5-LALIGA-NEXTDATE-CAPTURE-20260920-006",
        controlled_shadow_run_id="top5-laliga-real-capture-20260920-006",
        qualification_session_id="top5-laliga-qualification-20260920-006",
        issued_at=run_now - timedelta(minutes=1),
        expires_at=datetime.fromisoformat("2026-09-20T01:00:00+00:00"),
    )
    request = authorization.request_for(ll_target, configuration)
    ll_response = TheRundownCanonicalPayloadAdapterV1(
        adapter_source_sha=adapter_source_sha,
        maximum_source_age_seconds=configuration.maximum_source_age_seconds,
        initial_quota=QuotaSnapshot(used=0, remaining=None),
    ).decode_response(
        request,
        TheRundownNetworkHttpResponseV1(
            status_code=200,
            payload=payload,
            headers=headers,
            started_at=datetime.fromisoformat(metadata["started_at"]),
            finished_at=run_now,
        ),
    )

    def response_factory(network_request):
        if network_request.target.league == "LL":
            return ll_response
        return _response(
            network_request,
            provider=CANONICAL_CANDIDATE_PROVIDER,
            adapter_version=THERUNDOWN_ADAPTER_VERSION,
            adapter_source_sha=adapter_source_sha,
            source_timestamp=run_now - timedelta(seconds=5),
            captured_at=run_now,
            request_started_at=run_now - timedelta(seconds=1),
            request_finished_at=run_now,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            network_execution=True,
        )

    result = TheRundownNetworkShadowExecutorV1(
        clock=lambda: run_now,
        pacer=lambda _seconds: None,
        allow_live_network=True,
    ).run(
        configuration,
        authorization,
        transport=_NetworkStubTransport(response_factory),
    )
    artifact = json.loads(
        Path("/private/tmp/top5-b1-laliga-final-evidence.json").read_text()
    )["canonical_b1_evidence_bundle"]
    return result, configuration, authorization, artifact, run_now


def test_run006_payload_reconciles_through_b4_with_b1_artifact():
    result, configuration, authorization, artifact, run_now = _run006_network_run()

    reconciliation = reconcile_controlled_shadow_run_with_b1_ll_artifact(
        result,
        configuration,
        authorization,
        artifact,
        now=run_now,
    )

    assert result.status is NetworkShadowRunStatus.COMPLETED_NETWORK
    assert result.request_count == 5
    assert result.datapoint_count == 275
    assert result.quota_cost_units == 275.0
    assert reconciliation.provider == CANONICAL_CANDIDATE_PROVIDER
    assert reconciliation.receipt_eligible is False
    assert reconciliation.authority_changed is False
    assert reconciliation.artifacts.b1_ll_artifact is not None
    assert len(reconciliation.artifacts.candidate_eligibilities) == 5
    ll_capture = result.captures[2]
    assert len(ll_capture.response.raw_metadata["normalized_observations"]) == 3
    assert (
        ll_capture.response.raw_response_digest
        == "6df5aa61479bbeaa85f46b4a1f66c7c3a40d88736b9b7f08555f9eb0e0f276c4"
    )


def test_disabled_package_preserves_exact_reviewed_budgets_and_scope():
    targets = tuple(
        replace(target, provider=CANONICAL_CANDIDATE_PROVIDER) for target in _targets()
    )
    configuration = replace(_configuration(targets=targets), enabled=False)

    package = prepare_authorization_package(configuration)
    payload = package.as_payload()

    assert package.network_execution_enabled is False
    assert package.future_execution_command == FUTURE_EXECUTION_COMMAND
    assert payload["configuration"]["provider"] == CANONICAL_CANDIDATE_PROVIDER
    assert payload["configuration"]["enabled"] is False
    assert payload["configuration"]["maximum_request_count"] == (
        configuration.maximum_request_count
    )
    assert payload["configuration"]["maximum_datapoints"] == (
        configuration.maximum_datapoints
    )
    assert payload["configuration"]["maximum_quota_cost_units"] == (
        configuration.maximum_quota_cost_units
    )
    template = payload["authorization_template"]
    assert template["ceo_authorization_identity"] is None
    assert template["controlled_shadow_run_id"] is None
    assert template["qualification_session_id"] is None
    assert template["issued_at"] is None
    assert template["expires_at"] is None
    assert template["maximum_retries"] == 0
    assert template["no_bet"] is True
    assert template["publication"] is False
    assert template["production_activation"] is False
    assert template["monetary_spend_authorized"] is False
    assert payload["receipt_issuer_present"] is False
    assert payload["active_provider_authority"] is False


def test_completed_network_run_reconciles_all_five_leagues_to_downstream_inputs():
    result, configuration, authorization = _network_run()

    reconciliation = reconcile_controlled_shadow_run(
        result, configuration, authorization, now=NOW
    )
    payload = reconciliation.as_payload()

    assert reconciliation.leagues == TOP5_LEAGUE_ORDER
    assert reconciliation.provider == CANONICAL_CANDIDATE_PROVIDER
    assert reconciliation.request_count == configuration.maximum_request_count
    assert reconciliation.datapoint_count == configuration.maximum_datapoints
    assert reconciliation.quota_cost_units == configuration.maximum_quota_cost_units
    assert len(reconciliation.artifacts.capture_attestations) == 5
    assert len(reconciliation.artifacts.candidate_eligibilities) == 5
    assert len(reconciliation.artifacts.qualification_inputs) == 5
    assert len(reconciliation.artifacts.builder2_receipt_inputs) == 5
    assert payload["reconciliation_digest"] == reconciliation.reconciliation_digest
    for attestation in reconciliation.artifacts.capture_attestations:
        assert attestation["provider_identity"] == CANONICAL_CANDIDATE_PROVIDER
        assert attestation["network_execution"] is True
        assert attestation["no_bet"] is True
        assert attestation["publication"] is False
    for eligibility in reconciliation.artifacts.candidate_eligibilities:
        assert eligibility["evidence_kind"] == "REAL_OBSERVED"
        assert eligibility["active_production_capability"] is False
        assert eligibility["publication_capability"] is False
        assert eligibility["scheduler_capability"] is False
        assert eligibility["betting_capability"] is False
        assert eligibility["raw_response_digest"]
        assert eligibility["provider_record_digest"]
        assert eligibility["normalized_record_digest"]
        assert eligibility["cascade_evidence_digest"]
    for receipt_input in reconciliation.artifacts.builder2_receipt_inputs:
        assert receipt_input["eligible"] is False
        assert receipt_input["issuer_present"] is False


def test_repaired_b1_ll_artifact_is_injected_into_the_exact_ll_slot():
    result, configuration, authorization = _network_run()
    b1_artifact = _b1_ll_artifact(result, authorization)

    reconciliation = reconcile_controlled_shadow_run_with_b1_ll_artifact(
        result,
        configuration,
        authorization,
        b1_artifact,
        now=NOW,
    )

    assert reconciliation.leagues == ("EPL", "BL1", "LL", "SA", "L1")
    assert reconciliation.artifacts.b1_ll_artifact == b1_artifact
    assert (
        reconciliation.artifacts.capture_attestations[2]["fixture_key"]
        == configuration.targets[2].fixture_key
    )
    assert b1_artifact["b1_bridge_inputs"]["fixture_key"] == ("therundown:LL:event-LL")
    assert reconciliation.artifacts.candidate_eligibilities[2]["league_code"] == "LL"
    assert reconciliation.artifacts.builder2_receipt_inputs[2]["eligible"] is False
    assert reconciliation.artifacts.receipt_issuer_present is False
    assert reconciliation.artifacts.publication is False
    assert reconciliation.artifacts.production_activation is False


@pytest.mark.parametrize(
    "field, value, pattern",
    [
        ("fixture_key", "therundown:LL:other-event", "provider fixture identity"),
        ("home_team", "Other Home", "canonical fixture identity"),
    ],
)
def test_b1_provider_and_canonical_fixture_bindings_fail_closed(field, value, pattern):
    result, configuration, authorization = _network_run()
    b1_artifact = _b1_ll_artifact(result, authorization)
    b1_artifact["b1_bridge_inputs"][field] = value

    with pytest.raises(
        ControlledShadowAuthorizationPackageError,
        match=pattern,
    ):
        reconcile_controlled_shadow_run_with_b1_ll_artifact(
            result,
            configuration,
            authorization,
            b1_artifact,
            now=NOW,
        )


def test_b1_ll_artifact_cannot_self_supply_real_authority():
    result, configuration, authorization = _network_run()
    b1_artifact = _b1_ll_artifact(result, authorization)
    b1_artifact["b1_bridge_inputs"]["evidence_kind"] = "TEST_FIXTURE"

    with pytest.raises(
        ControlledShadowAuthorizationPackageError, match="synthetic or replay"
    ):
        reconcile_controlled_shadow_run_with_b1_ll_artifact(
            result, configuration, authorization, b1_artifact, now=NOW
        )


def test_b1_ll_artifact_must_match_exact_request_and_bookmaker_prices():
    result, configuration, authorization = _network_run()
    b1_artifact = _b1_ll_artifact(result, authorization)
    b1_artifact["b1_bridge_inputs"]["provider_request_id"] = "other-request"

    with pytest.raises(ControlledShadowAuthorizationPackageError, match="request"):
        reconcile_controlled_shadow_run_with_b1_ll_artifact(
            result, configuration, authorization, b1_artifact, now=NOW
        )

    b1_artifact = _b1_ll_artifact(result, authorization)
    b1_artifact["bookmaker_observations"][0]["odds"]["draw"] = 9.9
    with pytest.raises(ControlledShadowAuthorizationPackageError, match="bookmaker"):
        reconcile_controlled_shadow_run_with_b1_ll_artifact(
            result, configuration, authorization, b1_artifact, now=NOW
        )

    b1_artifact = _b1_ll_artifact(result, authorization)
    b1_artifact["raw_response_digest"] = "f" * 64
    with pytest.raises(ControlledShadowAuthorizationPackageError, match="top-level"):
        reconcile_controlled_shadow_run_with_b1_ll_artifact(
            result, configuration, authorization, b1_artifact, now=NOW
        )


def test_reconciliation_preserves_identity_and_evidence_for_each_league():
    result, configuration, authorization = _network_run()
    reconciliation = reconcile_controlled_shadow_run(
        result, configuration, authorization, now=NOW
    )

    for league, fixture, event, request in zip(
        reconciliation.leagues,
        reconciliation.fixture_keys,
        reconciliation.provider_event_ids,
        reconciliation.provider_request_ids,
    ):
        eligibility = next(
            item
            for item in reconciliation.artifacts.candidate_eligibilities
            if item["league_code"] == league
        )
        assert eligibility["fixture_key"] == fixture
        assert eligibility["provider_event_id"] == event
        assert eligibility["request_identity"] == request
        assert eligibility["home_participant_id"]
        assert eligibility["away_participant_id"]
        assert eligibility["bookmaker_identity"] == "bookmaker-a"
        assert eligibility["home_odds"] == 2.1
        assert eligibility["draw_odds"] == 3.4
        assert eligibility["away_odds"] == 3.2
        assert eligibility["source_timestamp"]
        assert eligibility["captured_at"]
        assert eligibility["adapter_source_sha"]
        assert eligibility["configuration_digest"] == configuration.configuration_digest


def test_replay_or_test_fixture_cannot_reconcile_as_real():
    targets = tuple(
        replace(target, provider=CANONICAL_CANDIDATE_PROVIDER) for target in _targets()
    )
    configuration = _configuration(targets=targets, enabled=True)
    authorization = _authorization(configuration, provider=CANONICAL_CANDIDATE_PROVIDER)
    replay = TheRundownReplayTransportV1(
        lambda request: _response(
            request,
            evidence_kind=ObservationEvidenceKind.TEST_FIXTURE,
            network_execution=False,
        ),
        clock=lambda: NOW,
    )
    result = TheRundownNetworkShadowExecutorV1(clock=lambda: NOW).run(
        configuration, authorization, transport=replay
    )

    assert result.status is NetworkShadowRunStatus.COMPLETED_REPLAY
    with pytest.raises(
        ControlledShadowAuthorizationPackageError, match="completed network"
    ):
        reconcile_controlled_shadow_run(result, configuration, authorization, now=NOW)


def test_reconciliation_rejects_expired_authorization_before_evidence_use():
    result, configuration, authorization = _network_run()
    expired = replace(
        authorization,
        expires_at=NOW - timedelta(seconds=1),
    )
    with pytest.raises(ControlledShadowAuthorizationPackageError, match="expired"):
        reconcile_controlled_shadow_run(result, configuration, expired, now=NOW)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("bookmaker_identity", "candidate eligibility rejected"),
        ("source_identity", "candidate eligibility rejected"),
        ("home_odds", "candidate eligibility rejected"),
    ],
)
def test_reconciliation_rejects_missing_bookmaker_provenance_or_incomplete_1x2(
    field, expected
):
    result, configuration, authorization = _network_run()
    original = _capture(result, "EPL")
    response = replace(
        original.response, **{field: "" if field != "home_odds" else None}
    )
    tampered = _replace_capture(result, original, response=response)

    with pytest.raises(ControlledShadowAuthorizationPackageError, match=expected):
        reconcile_controlled_shadow_run(tampered, configuration, authorization, now=NOW)


def test_reconciliation_rejects_stale_observation():
    result, configuration, authorization = _network_run()
    original = _capture(result, "BL1")
    response = replace(original.response, source_timestamp=NOW - timedelta(seconds=301))
    tampered = _replace_capture(result, original, response=response)

    with pytest.raises(ControlledShadowAuthorizationPackageError, match="stale"):
        reconcile_controlled_shadow_run(tampered, configuration, authorization, now=NOW)


def test_reconciliation_rejects_wrong_provider_and_post_kickoff_capture():
    result, configuration, authorization = _network_run()
    original = _capture(result, "EPL")
    wrong_provider = _replace_capture(
        result,
        original,
        target=replace(original.target, provider="the_odds_api"),
    )
    with pytest.raises(
        ControlledShadowAuthorizationPackageError, match="allowed identity"
    ):
        reconcile_controlled_shadow_run(
            wrong_provider, configuration, authorization, now=NOW
        )

    kickoff = NOW - timedelta(seconds=1)
    fixture_key = make_fixture_key(
        "EPL", original.target.home_team, original.target.away_team, kickoff
    )
    target = replace(original.target, fixture_key=fixture_key, kickoff=kickoff)
    request = replace(original.request, target=target)
    response = replace(original.response, fixture_key=fixture_key)
    attestation = dict(original.canonical_capture_attestation)
    attestation["fixture_key"] = fixture_key
    post_kickoff = _replace_capture(
        result,
        original,
        target=target,
        request=request,
        response=response,
        capture_attestation_input=attestation,
    )
    with pytest.raises(ControlledShadowAuthorizationPackageError, match="post-kickoff"):
        reconcile_controlled_shadow_run(
            post_kickoff, configuration, authorization, now=NOW
        )


@pytest.mark.parametrize("field", ["provider_event_id", "provider_request_id"])
def test_reconciliation_rejects_event_or_request_rebinding(field):
    result, configuration, authorization = _network_run()
    original = _capture(result, "LL")
    response = replace(original.response, **{field: "rebound-value"})
    tampered = _replace_capture(result, original, response=response)

    with pytest.raises(ControlledShadowAuthorizationPackageError, match="mismatch"):
        reconcile_controlled_shadow_run(tampered, configuration, authorization, now=NOW)


def test_reconciliation_rejects_participant_rebinding_and_digest_mismatch():
    result, configuration, authorization = _network_run()
    original = _capture(result, "SA")
    participant_tampered = _replace_capture(
        result,
        original,
        response=replace(original.response, home_participant_id="other-participant"),
    )
    with pytest.raises(ControlledShadowAuthorizationPackageError, match="participant"):
        reconcile_controlled_shadow_run(
            participant_tampered, configuration, authorization, now=NOW
        )

    digest_tampered = _replace_capture(
        result,
        original,
        response=replace(original.response, raw_response_digest="f" * 64),
    )
    with pytest.raises(ControlledShadowAuthorizationPackageError, match="attestation"):
        reconcile_controlled_shadow_run(
            digest_tampered, configuration, authorization, now=NOW
        )


def test_reconciliation_rejects_duplicate_or_missing_league_capture():
    result, configuration, authorization = _network_run()
    first = _capture(result, "EPL")
    duplicate = replace(
        result, captures=result.captures[:1] + (first,) + result.captures[2:]
    )
    with pytest.raises(
        ControlledShadowAuthorizationPackageError, match="duplicate league"
    ):
        reconcile_controlled_shadow_run(
            duplicate, configuration, authorization, now=NOW
        )

    missing = replace(result, captures=result.captures[:-1])
    with pytest.raises(ControlledShadowAuthorizationPackageError, match="incomplete"):
        reconcile_controlled_shadow_run(missing, configuration, authorization, now=NOW)


def test_package_and_reconciliation_never_issue_receipt_or_change_authority():
    result, configuration, authorization = _network_run()
    reconciliation = reconcile_controlled_shadow_run(
        result, configuration, authorization, now=NOW
    )
    assert reconciliation.receipt_eligible is False
    assert reconciliation.authority_changed is False
    assert reconciliation.publication is False
    assert reconciliation.production_activation is False
    assert reconciliation.monetary_spend_authorized is False
    assert reconciliation.artifacts.cascade_evidence_available is False
    assert (
        reconciliation.artifacts.qualification_status == "PENDING_BUILDER2_VALIDATION"
    )


def _cli_input_files(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    result, configuration, authorization = _network_run()
    disabled = replace(configuration, enabled=False, configuration_digest="")
    disabled = replace(
        disabled, configuration_digest=disabled.computed_configuration_digest
    )
    package = prepare_authorization_package(disabled)
    package_path = tmp_path / "authorization-package.json"
    package_path.write_text(json.dumps(package.as_payload()), encoding="utf-8")

    authorization_payload = authorization.as_payload()
    authorization_path = tmp_path / "ceo-authorization.json"
    authorization_path.write_text(
        json.dumps(
            {
                "package_digest": package.package_digest,
                "authorization": authorization_payload,
            }
        ),
        encoding="utf-8",
    )
    b1_path = tmp_path / "b1-ll-evidence.json"
    b1_path.write_text(
        json.dumps(
            {"canonical_b1_evidence_bundle": _b1_ll_artifact(result, authorization)}
        ),
        encoding="utf-8",
    )
    credential_path = tmp_path / "therundown.env"
    credential_path.write_text(
        "THERUNDOWN_API_KEY=offline-test-secret\n", encoding="utf-8"
    )
    credential_path.chmod(0o600)
    return (
        package_path,
        authorization_path,
        b1_path,
        credential_path,
        configuration,
        authorization,
        package,
    )


def _guarded_cli_run(tmp_path: Path, response_factory, *, output_name="result.json"):
    package_path, authorization_path, b1_path, credential_path, _, _, _ = (
        _cli_input_files(tmp_path)
    )
    transport = _NetworkStubTransport(response_factory)
    with pytest.raises(ControlledShadowAuthorizationPackageError):
        run_guarded_network_execution(
            package_path,
            authorization_path,
            b1_path,
            credential_file=credential_path,
            output_path=tmp_path / output_name,
            clock=lambda: NOW,
            pacer=lambda _seconds: None,
            transport=transport,
        )
    return transport


def test_guarded_cli_default_preflight_is_zero_network(tmp_path):
    package_path, authorization_path, b1_path, credential_path, _, _, _ = (
        _cli_input_files(tmp_path)
    )
    output = run_guarded_network_preflight(
        package_path,
        authorization_path,
        b1_path,
        credential_file=credential_path,
        clock=lambda: NOW,
    )
    assert output["status"] == "DRY_RUN_READY"
    assert output["network_calls"] == 0
    assert output["provider_requests"] == 0


@pytest.mark.parametrize(
    "mutation", ["missing", "expired", "wrong-package", "wrong-config", "wrong-b1"]
)
def test_guarded_cli_bindings_fail_before_first_request(tmp_path, mutation):
    package_path, authorization_path, b1_path, credential_path, _, _, _ = (
        _cli_input_files(tmp_path)
    )
    if mutation == "missing":
        authorization_path.unlink()
    elif mutation == "expired":
        payload = json.loads(authorization_path.read_text())
        payload["authorization"]["issued_at"] = (NOW - timedelta(minutes=2)).isoformat()
        payload["authorization"]["expires_at"] = (
            NOW - timedelta(seconds=1)
        ).isoformat()
        authorization_path.write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "wrong-package":
        payload = json.loads(authorization_path.read_text())
        payload["package_digest"] = "f" * 64
        authorization_path.write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "wrong-config":
        payload = json.loads(authorization_path.read_text())
        payload["authorization"]["configuration_digest"] = "f" * 64
        authorization_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        payload = json.loads(b1_path.read_text())
        payload["canonical_b1_evidence_bundle"]["b1_bridge_inputs"][
            "provider_event_id"
        ] = "wrong-event"
        b1_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ControlledShadowAuthorizationPackageError):
        run_guarded_network_execution(
            package_path,
            authorization_path,
            b1_path,
            credential_file=credential_path,
            output_path=tmp_path / "blocked.json",
            clock=lambda: NOW,
            pacer=lambda _seconds: None,
            transport=_NetworkStubTransport(
                lambda request: pytest.fail("network called")
            ),
        )


def test_guarded_cli_credential_missing_or_unsafe_fails_before_request(tmp_path):
    package_path, authorization_path, b1_path, _, _, _, _ = _cli_input_files(tmp_path)
    missing = tmp_path / "missing.env"
    with pytest.raises(
        ControlledShadowAuthorizationPackageError, match="THERUNDOWN_API_KEY"
    ):
        run_guarded_network_preflight(
            package_path,
            authorization_path,
            b1_path,
            credential_file=missing,
            clock=lambda: NOW,
        )

    unsafe = tmp_path / "unsafe.env"
    unsafe.write_text("THERUNDOWN_API_KEY=offline-test-secret\n", encoding="utf-8")
    unsafe.chmod(0o644)
    with pytest.raises(ControlledShadowAuthorizationPackageError, match="permissions"):
        run_guarded_network_preflight(
            package_path,
            authorization_path,
            b1_path,
            credential_file=unsafe,
            clock=lambda: NOW,
        )


def test_guarded_cli_first_request_and_cumulative_billing_overrun_fail_closed(tmp_path):
    first = _guarded_cli_run(
        tmp_path,
        lambda request: _response(
            request,
            datapoint_count=56,
            quota_cost_units=56.0,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            network_execution=True,
        ),
        output_name="first-overrun.json",
    )
    assert len(first.calls) == 1

    counter = {"value": 0}

    def fifth_overrun(request):
        counter["value"] += 1
        if counter["value"] == 5:
            return _response(
                request,
                datapoint_count=56,
                quota_cost_units=56.0,
                evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
                network_execution=True,
            )
        return _response(
            request,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            network_execution=True,
        )

    fifth = _guarded_cli_run(
        tmp_path / "fifth", fifth_overrun, output_name="fifth-overrun.json"
    )
    assert len(fifth.calls) == 5


def test_guarded_cli_retry_attempt_fails_closed_without_retry(tmp_path):
    transport = _guarded_cli_run(
        tmp_path,
        lambda request: _response(
            request,
            retry_count=1,
            evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
            network_execution=True,
        ),
        output_name="retry.json",
    )
    assert len(transport.calls) == 1


def test_guarded_cli_five_of_five_writes_b2_compatible_output(tmp_path):
    package_path, authorization_path, b1_path, credential_path, _, _, _ = (
        _cli_input_files(tmp_path)
    )
    output_path = tmp_path / "completed.json"
    summary = run_guarded_network_execution(
        package_path,
        authorization_path,
        b1_path,
        credential_file=credential_path,
        output_path=output_path,
        clock=lambda: NOW,
        pacer=lambda _seconds: None,
        transport=_NetworkStubTransport(
            lambda request: _response(
                request,
                evidence_kind=ObservationEvidenceKind.REAL_OBSERVED,
                network_execution=True,
            )
        ),
    )
    payload = json.loads(output_path.read_text())
    assert summary["status"] == "COMPLETED_NETWORK"
    assert payload["status"] == "COMPLETED_NETWORK"
    assert payload["request_count"] == 5
    assert payload["datapoint_count"] == 275
    assert len(payload["captures"]) == 5
    assert len(payload["capture_attestations"]) == 5
    assert len(payload["candidate_eligibilities"]) == 5
    assert len(payload["builder2_receipt_inputs"]) == 5
    assert (
        payload["reconciliation"]["reconciliation_digest"]
        == payload["reconciliation_digest"]
    )
    assert all(item["eligible"] is False for item in payload["builder2_receipt_inputs"])
    assert payload["safety"]["receipt_issued"] is False
    assert payload["safety"]["authority_changed"] is False
    assert output_path.stat().st_mode & 0o077 == 0
