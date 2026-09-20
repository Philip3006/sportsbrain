"""Offline proof for the five-league shadow authorization boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from src.football.top5_controlled_shadow_authorization_package import (
    CANONICAL_CANDIDATE_PROVIDER,
    FUTURE_EXECUTION_COMMAND,
    TOP5_LEAGUE_ORDER,
    ControlledShadowAuthorizationPackageError,
    prepare_authorization_package,
    reconcile_controlled_shadow_run,
    reconcile_controlled_shadow_run_with_b1_ll_artifact,
)
from src.football.top5_controlled_shadow_provider_qualification import (
    ObservationEvidenceKind,
)
from src.football.top5_shadow_provider_redundancy import make_fixture_key
from src.football.top5_therundown_network_shadow import (
    NetworkShadowRunStatus,
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
            "fixture_key": target.fixture_key,
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
        == (b1_artifact["b1_bridge_inputs"]["fixture_key"])
    )
    assert reconciliation.artifacts.candidate_eligibilities[2]["league_code"] == "LL"
    assert reconciliation.artifacts.builder2_receipt_inputs[2]["eligible"] is False
    assert reconciliation.artifacts.receipt_issuer_present is False
    assert reconciliation.artifacts.publication is False
    assert reconciliation.artifacts.production_activation is False


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
