"""Offline tests for the one-request TheRundown quota-proof boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.football.odds.therundown import THERUNDOWN_PROVIDER_NAME
from src.football.top5_controlled_shadow_authorization_package import (
    ControlledShadowAuthorizationPackageError,
    _load_spend_control_evidence,
    _write_quota_proof,
)
from src.football.top5_therundown_network_shadow import (
    NetworkShadowExecutionBlocked,
    TheRundownNetworkHttpResponseV1,
    TheRundownQuotaProofEvidenceV1,
    TheRundownQuotaProofRequestV1,
    execute_therundown_quota_proof,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


class _FakeProofClient:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def _request(**changes: object) -> TheRundownQuotaProofRequestV1:
    request = TheRundownQuotaProofRequestV1(
        proof_id="quota-proof:test-run-001",
        provider=THERUNDOWN_PROVIDER_NAME,
        provider_event_id="event-ll-001",
        authorization_package_digest="a" * 64,
        configuration_digest="b" * 64,
        authorization_id="CEO-TOP5-TEST-001",
        controlled_shadow_run_id="shadow-test-001",
        qualification_session_id="qualification-test-001",
        ceo_authorization_identity="ceo:test",
        adapter_version="therundown-adapter-v1",
        adapter_source_sha="c" * 64,
        endpoint="https://therundown.io/api/v2/events/event-ll-001",
        query={
            "affiliate_ids": "19,22,23",
            "hide_closed": "true",
            "main_line": "true",
            "market_ids": "1",
        },
        request_shape_digest="0" * 64,
    )
    return replace(
        request, **changes, request_shape_digest=request.computed_request_shape_digest
    )


def _response(**changes: object) -> TheRundownNetworkHttpResponseV1:
    headers = {
        "X-Datapoints": "55",
        "X-Datapoints-Used": "55",
        "X-Datapoints-Remaining": "275",
        "X-Datapoints-Limit": "330",
        "X-Datapoints-Period": "daily",
        "X-Datapoints-Reset": "2026-09-21T00:00:00Z",
        "X-Tier": "free",
        "X-Rate-Limit": "1",
        "X-Data-Delay-Seconds": "300",
    }
    values: dict[str, object] = {
        "status_code": 200,
        "payload": {"event": "provider-response"},
        "headers": headers,
        "started_at": NOW - timedelta(seconds=1),
        "finished_at": NOW,
    }
    values.update(changes)
    return TheRundownNetworkHttpResponseV1(**values)


def test_valid_proof_is_one_bounded_request_and_not_a_league_capture():
    request = _request()
    client = _FakeProofClient(_response())
    evidence = execute_therundown_quota_proof(
        request,
        api_key="test-secret-never-written",
        http_client=client,
        now=NOW,
    )

    assert len(client.calls) == 1
    assert client.calls[0].endpoint.endswith("/events/event-ll-001")
    assert client.calls[0].query == dict(request.query)
    assert client.calls[0].headers["X-TheRundown-Key"] == "test-secret-never-written"
    assert evidence.execution_phase == "quota_proof"
    assert evidence.billed_datapoints == 55
    assert evidence.remaining_datapoints == 275
    assert evidence.request_count == 1
    assert evidence.retry_count == 0
    assert evidence.no_retry is True
    assert evidence.as_payload()["execution_phase"] == "quota_proof"


@pytest.mark.parametrize(
    "changes,match",
    [
        (
            {
                "headers": {
                    "X-Datapoints": "55",
                    "X-Datapoints-Used": "55",
                    "X-Datapoints-Limit": "330",
                    "X-Datapoints-Period": "daily",
                    "X-Datapoints-Reset": "2026-09-21T00:00:00Z",
                    "X-Tier": "free",
                    "X-Rate-Limit": "1",
                    "X-Data-Delay-Seconds": "300",
                }
            },
            "remaining",
        ),
        (
            {"headers": {**_response().headers, "X-Datapoints-Remaining": "274"}},
            "below the five-league budget",
        ),
        (
            {"headers": {**_response().headers, "X-Datapoints-Used": "54"}},
            "contradictory",
        ),
        (
            {"headers": {**_response().headers, "X-Datapoints": "56"}},
            "per-request cap",
        ),
    ],
)
def test_malformed_or_over_budget_provider_evidence_fails_closed(changes, match):
    client = _FakeProofClient(_response(**changes))
    with pytest.raises(NetworkShadowExecutionBlocked, match=match):
        execute_therundown_quota_proof(
            _request(), api_key="test-secret", http_client=client, now=NOW
        )
    assert len(client.calls) == 1


def test_missing_body_http_failure_and_transport_failure_never_retry():
    for response, error in (
        (_response(status_code=429), None),
        (_response(payload=None), None),
        (None, TimeoutError("offline test")),
    ):
        client = _FakeProofClient(response, error)
        with pytest.raises(NetworkShadowExecutionBlocked):
            execute_therundown_quota_proof(
                _request(), api_key="test-secret", http_client=client, now=NOW
            )
        assert len(client.calls) == 1


def test_stale_response_wrong_request_shape_and_provider_mismatch_fail_closed():
    stale = _response(
        started_at=NOW - timedelta(seconds=302),
        finished_at=NOW - timedelta(seconds=301),
    )
    with pytest.raises(NetworkShadowExecutionBlocked, match="stale"):
        execute_therundown_quota_proof(
            _request(),
            api_key="test-secret",
            http_client=_FakeProofClient(stale),
            now=NOW,
        )

    with pytest.raises(NetworkShadowExecutionBlocked, match="request-shape"):
        execute_therundown_quota_proof(
            replace(_request(), request_shape_digest="d" * 64),
            api_key="test-secret",
            http_client=_FakeProofClient(_response()),
            now=NOW,
        )

    with pytest.raises(NetworkShadowExecutionBlocked, match="provider"):
        execute_therundown_quota_proof(
            replace(_request(), provider="the_odds_api"),
            api_key="test-secret",
            http_client=_FakeProofClient(_response()),
            now=NOW,
        )


def test_proof_reload_keeps_digest_and_cannot_be_a_receipt():
    request = _request()
    evidence = execute_therundown_quota_proof(
        request,
        api_key="test-secret",
        http_client=_FakeProofClient(_response()),
        now=NOW,
    )
    reloaded = TheRundownQuotaProofEvidenceV1.from_payload(evidence.as_payload())
    reloaded.validate(request=request, now=NOW)
    assert reloaded.evidence_digest == evidence.evidence_digest
    assert "receipt" not in evidence.as_payload()


def test_payload_quota_claims_do_not_override_provider_headers():
    response = _response(
        payload={
            "caller_claimed_remaining": 999_999,
            "caller_claimed_billed": 1,
        }
    )
    evidence = execute_therundown_quota_proof(
        _request(),
        api_key="test-secret",
        http_client=_FakeProofClient(response),
        now=NOW,
    )
    assert evidence.remaining_datapoints == 275
    assert evidence.billed_datapoints == 55


def test_second_proof_output_is_rejected(tmp_path: Path):
    request = _request()
    evidence = execute_therundown_quota_proof(
        request,
        api_key="test-secret",
        http_client=_FakeProofClient(_response()),
        now=NOW,
    )
    output = tmp_path / "quota-proof.json"
    spend_control = {
        "provider": THERUNDOWN_PROVIDER_NAME,
        "account_tier": "free",
        "overage_exposure": "none",
        "observed_at": NOW,
        "digest": "f" * 64,
    }
    _write_quota_proof(
        output,
        request=request,
        evidence=evidence,
        spend_control=spend_control,
    )
    with pytest.raises(ControlledShadowAuthorizationPackageError, match="second"):
        _write_quota_proof(
            output,
            request=request,
            evidence=evidence,
            spend_control=spend_control,
        )


def test_spend_control_accepts_only_recent_free_hard_cap(tmp_path: Path):
    path = tmp_path / "tier-evidence.json"
    path.write_text(
        json.dumps(
            {
                "provider": THERUNDOWN_PROVIDER_NAME,
                "headers": {
                    "Date": "Sun, 20 Sep 2026 11:00:00 GMT",
                    "x-tier": "free",
                    "x-datapoints-period": "daily",
                    "x-datapoints-limit": "20000",
                    "x-rate-limit": "1",
                },
            }
        )
    )
    evidence = _load_spend_control_evidence(path, now=NOW)
    assert evidence["account_tier"] == "free"
    assert evidence["overage_exposure"] == "none"
    assert isinstance(evidence["digest"], str)


def test_spend_control_rejects_paid_or_stale_evidence(tmp_path: Path):
    paid = tmp_path / "paid.json"
    paid.write_text(
        json.dumps(
            {
                "headers": {
                    "Date": "Sun, 20 Sep 2026 11:00:00 GMT",
                    "x-tier": "pro",
                    "x-datapoints-period": "daily",
                    "x-datapoints-limit": "20000",
                    "x-rate-limit": "1",
                }
            }
        )
    )
    with pytest.raises(
        ControlledShadowAuthorizationPackageError, match="BLOCKED_SPEND_CONTROL"
    ):
        _load_spend_control_evidence(paid, now=NOW)

    stale = tmp_path / "stale.json"
    stale.write_text(
        json.dumps(
            {
                "headers": {
                    "Date": "Thu, 17 Sep 2026 11:00:00 GMT",
                    "x-tier": "free",
                    "x-datapoints-period": "daily",
                    "x-datapoints-limit": "20000",
                    "x-rate-limit": "1",
                }
            }
        )
    )
    with pytest.raises(ControlledShadowAuthorizationPackageError, match="stale"):
        _load_spend_control_evidence(stale, now=NOW)
