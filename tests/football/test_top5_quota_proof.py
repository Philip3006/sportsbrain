"""Offline tests for the one-request TheRundown quota-proof boundary."""

from __future__ import annotations

import json
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError

import pytest

import src.football.top5_controlled_shadow_authorization_package as b4_package
import src.football.top5_therundown_network_shadow as network_shadow
from src.football.odds.therundown import THERUNDOWN_PROVIDER_NAME
from src.football.top5_controlled_shadow_authorization_package import (
    ControlledShadowAuthorizationPackageError,
    _load_spend_control_evidence,
    _quota_proof_consumption_marker_path,
    _write_quota_proof,
    run_guarded_quota_proof,
)
from src.football.top5_therundown_network_shadow import (
    NetworkShadowContractError,
    NetworkShadowExecutionBlocked,
    TheRundownNetworkHttpResponseV1,
    TheRundownQuotaProofAuthorizationV1,
    TheRundownQuotaProofEvidenceV1,
    TheRundownQuotaProofRequestV1,
    TheRundownRequestsHttpClientV1,
    TheRundownUrlLibHttpClientV1,
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


class _FakeRequestsResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        content: bytes = b'{"ok":true}',
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"Content-Type": "application/json"}


def _request(**changes: object) -> TheRundownQuotaProofRequestV1:
    request = TheRundownQuotaProofRequestV1(
        proof_id="quota-proof:test-run-001",
        provider=THERUNDOWN_PROVIDER_NAME,
        sport_id=3,
        snapshot_date=NOW.date(),
        authorization_package_digest="a" * 64,
        configuration_digest="b" * 64,
        authorization_id="CEO-TOP5-TEST-001",
        controlled_shadow_run_id="shadow-test-001",
        qualification_session_id="qualification-test-001",
        ceo_authorization_identity="ceo:test",
        adapter_version="therundown-adapter-v1",
        adapter_source_sha="c" * 64,
        endpoint=f"https://therundown.io/api/v2/sports/3/events/{NOW.date().isoformat()}",
        query={
            "affiliate_ids": "19",
            "hide_closed": "true",
            "main_line": "true",
            "market_ids": "1",
        },
        request_shape_digest="0" * 64,
    )
    request = replace(request, **changes)
    return replace(request, request_shape_digest=request.computed_request_shape_digest)


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
        "payload": {"events": [{"event_id": "snapshot-event-001"}]},
        "headers": headers,
        "started_at": NOW - timedelta(seconds=1),
        "finished_at": NOW,
    }
    values.update(changes)
    return TheRundownNetworkHttpResponseV1(**values)


def _proof_authorization(
    *,
    snapshot_date=None,
    **changes: object,
) -> TheRundownQuotaProofAuthorizationV1:
    if snapshot_date is None:
        snapshot_date = NOW.date()
    request = _request(snapshot_date=snapshot_date)
    authorization = TheRundownQuotaProofAuthorizationV1(
        proof_authorization_id="CEO-TOP5-QUOTA-PROOF-001",
        ceo_proof_authorization_identity="ceo:quota-proof",
        proof_id="quota-proof:authorized-001",
        provider=THERUNDOWN_PROVIDER_NAME,
        sport_id=3,
        snapshot_date=snapshot_date,
        request_shape_digest=request.request_shape_digest,
        adapter_version="therundown-adapter-v1",
        adapter_source_sha="c" * 64,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
        authorization_digest="0" * 64,
    )
    authorization = replace(authorization, **changes)
    return replace(
        authorization,
        authorization_digest=authorization.computed_authorization_digest,
    )


def _guarded_inputs(tmp_path: Path, monkeypatch):
    """Build only dated-snapshot authorization and spend-control inputs."""

    authorization = _proof_authorization()
    authorization_path = tmp_path / "proof-authorization.json"
    authorization_path.write_text(
        json.dumps({"authorization": authorization.as_payload()})
    )
    spend_path = tmp_path / "spend.json"
    spend_path.write_text(
        json.dumps(
            {
                "provider": THERUNDOWN_PROVIDER_NAME,
                "observed_at": (NOW - timedelta(minutes=1)).isoformat(),
                "headers": {
                    "x-tier": "free",
                    "x-datapoints-period": "daily",
                    "x-datapoints-limit": "20000",
                    "x-rate-limit": "1",
                },
            }
        )
    )
    credential_path = tmp_path / "therundown.env"
    credential_path.write_text("THERUNDOWN_API_KEY=test-secret\n")
    credential_path.chmod(0o600)
    monkeypatch.setattr(
        b4_package,
        "quota_proof_consumption_state_path",
        lambda: tmp_path / "canonical-consumption-store",
    )
    return {
        "authorization": authorization,
        "authorization_path": authorization_path,
        "spend_path": spend_path,
        "credential_path": credential_path,
    }


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
    assert client.calls[0].endpoint.endswith(
        f"/sports/3/events/{NOW.date().isoformat()}"
    )
    assert client.calls[0].query == dict(request.query)
    assert client.calls[0].headers["X-TheRundown-Key"] == "test-secret-never-written"
    assert evidence.execution_phase == "quota_proof"
    assert evidence.billed_datapoints == 55
    assert evidence.remaining_datapoints == 275
    assert evidence.request_count == 1
    assert evidence.retry_count == 0
    assert evidence.no_retry is True
    assert evidence.as_payload()["execution_phase"] == "quota_proof"


def test_guarded_proof_validates_against_post_response_clock(
    tmp_path: Path, monkeypatch
):
    inputs = _guarded_inputs(tmp_path, monkeypatch)
    preflight_now = NOW
    response_started = preflight_now + timedelta(seconds=1)
    response_finished = preflight_now + timedelta(seconds=2)
    post_response_validation_now = preflight_now + timedelta(seconds=3)
    clock_values = iter((preflight_now, post_response_validation_now))
    response = _response(
        started_at=response_started,
        finished_at=response_finished,
    )

    summary = run_guarded_quota_proof(
        inputs["authorization_path"],
        spend_control_evidence_path=inputs["spend_path"],
        credential_file=inputs["credential_path"],
        output_path=tmp_path / "clocked-proof.json",
        clock=lambda: next(clock_values),
        http_client=_FakeProofClient(response),
    )

    assert summary["status"] == "TOP5_B4_QUOTA_PROOF — QUOTA_CONFIRMED"
    assert response_started <= response_finished <= post_response_validation_now


def test_response_after_post_response_validation_clock_fails_closed():
    preflight_now = NOW
    post_response_validation_now = preflight_now + timedelta(seconds=3)
    response = _response(
        started_at=preflight_now + timedelta(seconds=1),
        finished_at=post_response_validation_now + timedelta(seconds=1),
    )
    client = _FakeProofClient(response)

    with pytest.raises(NetworkShadowExecutionBlocked, match="timestamps"):
        execute_therundown_quota_proof(
            _request(),
            api_key="test-secret",
            http_client=client,
            now=preflight_now,
            clock=lambda: post_response_validation_now,
        )

    assert len(client.calls) == 1


def test_stale_response_fails_closed_against_post_response_clock():
    response = _response(
        started_at=NOW - timedelta(seconds=302),
        finished_at=NOW - timedelta(seconds=301),
    )

    with pytest.raises(NetworkShadowExecutionBlocked, match="stale"):
        execute_therundown_quota_proof(
            _request(),
            api_key="test-secret",
            http_client=_FakeProofClient(response),
            now=NOW,
            clock=lambda: NOW,
        )


def test_b4_accepts_observed_response_without_optional_delay_header():
    headers = {
        **_response().headers,
        "X-Datapoints": "56",
        "X-Datapoints-Used": "112",
        "X-Datapoints-Remaining": "19888",
        "X-Datapoints-Limit": "20000",
    }
    headers.pop("X-Data-Delay-Seconds")
    evidence = execute_therundown_quota_proof(
        _request(),
        api_key="test-secret",
        http_client=_FakeProofClient(_response(headers=headers)),
        now=NOW,
        clock=lambda: NOW,
    )

    assert evidence.billed_datapoints == 56
    assert evidence.quota_used_datapoints == 112
    assert evidence.remaining_datapoints == 19888
    assert "x-data-delay-seconds" not in evidence.raw_header_evidence
    with pytest.raises(NetworkShadowExecutionBlocked, match="incomplete"):
        evidence.validate(
            request=_request(),
            now=NOW,
            require_provider_delay=True,
        )


def test_zero_delay_header_is_evidence_only_not_authority():
    headers = {**_response().headers, "X-Data-Delay-Seconds": "0"}
    authorization = _proof_authorization()
    evidence = execute_therundown_quota_proof(
        _request(),
        api_key="test-secret",
        http_client=_FakeProofClient(_response(headers=headers)),
        now=NOW,
        clock=lambda: NOW,
    )

    authorization.validate(now=NOW)
    evidence.validate(request=_request(), now=NOW, require_provider_delay=True)
    assert authorization.five_league_execution_authorized is False
    assert authorization.provider_authority_granted is False
    assert authorization.activation_authorized is False
    assert authorization.publication_authorized is False
    assert authorization.betting_authorized is False


def test_observed_dated_snapshot_cost_of_56_is_accepted():
    response = _response(
        headers={
            **_response().headers,
            "X-Datapoints": "56",
            "X-Datapoints-Used": "56",
            "X-Datapoints-Remaining": "944",
            "X-Datapoints-Limit": "1000",
        }
    )
    client = _FakeProofClient(response)

    evidence = execute_therundown_quota_proof(
        _request(), api_key="test-secret", http_client=client, now=NOW
    )

    assert evidence.billed_datapoints == 56
    assert evidence.remaining_datapoints == 944
    assert evidence.request_count == 1
    assert evidence.retry_count == 0


def test_proof_only_authorization_does_not_require_five_league_scope():
    authorization = _proof_authorization()
    authorization.validate(now=NOW)
    request = authorization.request_for_proof(
        proof_configuration_digest="e" * 64, now=NOW
    )
    client = _FakeProofClient(_response())
    evidence = execute_therundown_quota_proof(
        request,
        api_key="test-secret",
        http_client=client,
        now=NOW,
    )
    assert evidence.authorization_id == authorization.proof_authorization_id
    assert evidence.snapshot_date == NOW.date()
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"five_league_execution_authorized": True}, "forbidden authority"),
        ({"provider_authority_granted": True}, "forbidden authority"),
        ({"activation_authorized": True}, "forbidden authority"),
        ({"publication_authorized": True}, "forbidden authority"),
        ({"betting_authorized": True}, "forbidden authority"),
        ({"maximum_request_count": 5}, "request count"),
        ({"retry_count": 1}, "retries"),
    ],
)
def test_proof_authorization_cannot_grant_later_authority(changes, match):
    authorization = _proof_authorization(**changes)
    with pytest.raises(NetworkShadowExecutionBlocked, match=match):
        authorization.validate(now=NOW)


def test_proof_authorization_digest_and_expiry_fail_closed():
    authorization = _proof_authorization()
    with pytest.raises(NetworkShadowContractError, match="digest"):
        replace(authorization, authorization_digest="f" * 64).validate(now=NOW)
    with pytest.raises(NetworkShadowExecutionBlocked, match="outside"):
        replace(
            authorization,
            issued_at=NOW - timedelta(hours=2),
            expires_at=NOW - timedelta(seconds=1),
            authorization_digest="0" * 64,
        ).validate(now=NOW)


def test_guarded_proof_uses_proof_authorization_without_five_league_package(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        b4_package,
        "quota_proof_consumption_state_path",
        lambda: tmp_path / "canonical-consumption-store",
    )
    authorization = _proof_authorization()
    authorization_path = tmp_path / "proof-authorization.json"
    authorization_path.write_text(
        json.dumps({"authorization": authorization.as_payload()})
    )
    spend_path = tmp_path / "spend.json"
    spend_path.write_text(
        json.dumps(
            {
                "provider": THERUNDOWN_PROVIDER_NAME,
                "observed_at": (NOW - timedelta(minutes=1)).isoformat(),
                "headers": {
                    "x-tier": "free",
                    "x-datapoints-period": "daily",
                    "x-datapoints-limit": "20000",
                    "x-rate-limit": "1",
                },
            }
        )
    )
    credential_path = tmp_path / "therundown.env"
    credential_path.write_text("THERUNDOWN_API_KEY=test-secret\n")
    credential_path.chmod(0o600)
    output_path = tmp_path / "proof-output.json"
    client = _FakeProofClient(_response())

    summary = run_guarded_quota_proof(
        authorization_path,
        spend_control_evidence_path=spend_path,
        credential_file=credential_path,
        output_path=output_path,
        clock=lambda: NOW,
        http_client=client,
    )

    assert summary["status"] == "TOP5_B4_QUOTA_PROOF — QUOTA_CONFIRMED"
    assert summary["proof_authorization_id"] == authorization.proof_authorization_id
    assert summary["sport_id"] == authorization.sport_id
    assert summary["snapshot_date"] == authorization.snapshot_date.isoformat()
    assert summary["five_league_requests"] == 0
    assert len(client.calls) == 1
    output = json.loads(output_path.read_text())
    assert output["execution_phase"] == "quota_proof"
    assert output["safety"]["authority_changed"] is False


def _run_guarded(
    inputs: dict[str, object],
    output_path: Path,
    client: _FakeProofClient,
):
    return run_guarded_quota_proof(
        inputs["authorization_path"],
        spend_control_evidence_path=inputs["spend_path"],
        credential_file=inputs["credential_path"],
        output_path=output_path,
        clock=lambda: NOW,
        http_client=client,
    )


def test_same_authorization_cannot_bypass_consumption_with_output_paths(
    tmp_path: Path, monkeypatch
):
    inputs = _guarded_inputs(tmp_path, monkeypatch)
    authorization = inputs["authorization"]
    first_client = _FakeProofClient(_response())
    first_output = tmp_path / "caller-a" / "proof.json"
    _run_guarded(inputs, first_output, first_client)
    marker_path = _quota_proof_consumption_marker_path(authorization)
    marker_before = marker_path.read_bytes()

    for output_path in (
        tmp_path / "caller-a" / "different-output.json",
        tmp_path / "caller-b" / "arbitrary-output.json",
    ):
        second_client = _FakeProofClient(_response())
        with pytest.raises(
            ControlledShadowAuthorizationPackageError,
            match="already been consumed",
        ):
            _run_guarded(inputs, output_path, second_client)
        assert second_client.calls == []

    assert marker_path.read_bytes() == marker_before
    assert len(first_client.calls) == 1


def test_transport_failure_consumes_authorization_without_retry(
    tmp_path: Path, monkeypatch
):
    inputs = _guarded_inputs(tmp_path, monkeypatch)
    failing_client = _FakeProofClient(error=TimeoutError("offline transport"))
    with pytest.raises(NetworkShadowExecutionBlocked, match="transport failed"):
        _run_guarded(inputs, tmp_path / "first.json", failing_client)
    assert len(failing_client.calls) == 1

    retry_client = _FakeProofClient(_response())
    with pytest.raises(
        ControlledShadowAuthorizationPackageError,
        match="already been consumed",
    ):
        _run_guarded(inputs, tmp_path / "retry.json", retry_client)
    assert retry_client.calls == []


def test_same_proof_authorization_id_with_modified_digest_is_rejected(
    tmp_path: Path, monkeypatch
):
    inputs = _guarded_inputs(tmp_path, monkeypatch)
    _run_guarded(inputs, tmp_path / "first.json", _FakeProofClient(_response()))

    modified = _proof_authorization(
        proof_authorization_id=inputs["authorization"].proof_authorization_id,
        proof_id="quota-proof:modified-material",
    )
    inputs["authorization_path"].write_text(
        json.dumps({"authorization": modified.as_payload()})
    )
    client = _FakeProofClient(_response())
    with pytest.raises(
        ControlledShadowAuthorizationPackageError,
        match="identity conflicts",
    ):
        _run_guarded(inputs, tmp_path / "modified.json", client)
    assert client.calls == []


def test_same_digest_with_inconsistent_authorization_id_fails_before_consumption(
    tmp_path: Path, monkeypatch
):
    inputs = _guarded_inputs(tmp_path, monkeypatch)
    payload = inputs["authorization"].as_payload()
    payload["proof_authorization_id"] = "CEO-TOP5-QUOTA-PROOF-INCONSISTENT"
    inputs["authorization_path"].write_text(json.dumps({"authorization": payload}))
    client = _FakeProofClient(_response())
    with pytest.raises(NetworkShadowContractError, match="digest"):
        _run_guarded(inputs, tmp_path / "inconsistent.json", client)
    assert client.calls == []
    assert not (tmp_path / "canonical-consumption-store").exists()


def test_historical_snapshot_is_rejected_before_credential_read(tmp_path: Path):
    authorization = _proof_authorization(snapshot_date=(NOW - timedelta(days=1)).date())
    with pytest.raises(NetworkShadowExecutionBlocked, match="current or next"):
        authorization.validate(now=NOW)
    assert not (tmp_path / "credential.env").exists()


def test_snapshot_becoming_historical_before_execution_is_rejected():
    authorization = _proof_authorization(snapshot_date=NOW.date())
    with pytest.raises(NetworkShadowExecutionBlocked, match="outside"):
        authorization.validate(now=NOW + timedelta(days=1))


def test_snapshot_authorization_and_request_date_are_exactly_bound():
    authorization = _proof_authorization()
    request = authorization.request_for_proof(
        proof_configuration_digest="e" * 64, now=NOW
    )
    assert request.sport_id == 3
    assert request.snapshot_date == NOW.date()
    assert request.endpoint.endswith(f"/sports/3/events/{NOW.date().isoformat()}")
    with pytest.raises(NetworkShadowExecutionBlocked, match="endpoint"):
        replace(request, snapshot_date=NOW.date() + timedelta(days=1)).validate(now=NOW)


def test_historical_event_artifact_and_legacy_authorization_cannot_satisfy_new_contract():
    for suffix in ("004", "005", "006", "007"):
        legacy = _proof_authorization(
            proof_authorization_id=f"CEO-TOP5-B4-QUOTA-PROOF-20260921-{suffix}"
        ).as_payload()
        legacy["schema_version"] = "top5-therundown-quota-proof-authorization-v1"
        with pytest.raises(NetworkShadowContractError, match="unsupported"):
            TheRundownQuotaProofAuthorizationV1.from_payload(legacy).validate(now=NOW)


def test_no_provider_event_or_prior_capture_is_required_for_new_proof():
    request = _request()
    assert "provider_event_id" not in request.__dict__
    evidence = execute_therundown_quota_proof(
        request,
        api_key="test-secret",
        http_client=_FakeProofClient(_response()),
        now=NOW,
    )
    assert evidence.snapshot_date == NOW.date()


def test_empty_dated_snapshot_fails_closed_without_quota_claims():
    client = _FakeProofClient(_response(payload={"events": []}))
    with pytest.raises(NetworkShadowExecutionBlocked, match="no events"):
        execute_therundown_quota_proof(
            _request(), api_key="test-secret", http_client=client, now=NOW
        )
    assert len(client.calls) == 1


def test_spend_control_failure_does_not_consume_authorization(
    tmp_path: Path, monkeypatch
):
    inputs = _guarded_inputs(tmp_path, monkeypatch)
    paid_path = tmp_path / "paid-spend.json"
    paid_path.write_text(
        json.dumps(
            {
                "provider": THERUNDOWN_PROVIDER_NAME,
                "observed_at": NOW.isoformat(),
                "headers": {
                    "x-tier": "pro",
                    "x-datapoints-period": "daily",
                    "x-datapoints-limit": "20000",
                    "x-rate-limit": "1",
                },
            }
        )
    )
    inputs["spend_path"] = paid_path
    blocked_client = _FakeProofClient(_response())
    with pytest.raises(
        ControlledShadowAuthorizationPackageError,
        match="BLOCKED_SPEND_CONTROL",
    ):
        _run_guarded(inputs, tmp_path / "paid.json", blocked_client)
    assert blocked_client.calls == []

    inputs["spend_path"] = tmp_path / "spend.json"
    valid_client = _FakeProofClient(_response())
    _run_guarded(inputs, tmp_path / "valid.json", valid_client)
    assert len(valid_client.calls) == 1


def test_concurrent_invocations_can_execute_transport_only_once(
    tmp_path: Path, monkeypatch
):
    inputs = _guarded_inputs(tmp_path, monkeypatch)
    clients = [_FakeProofClient(_response()), _FakeProofClient(_response())]
    output_paths = (tmp_path / "race-a.json", tmp_path / "race-b.json")

    def invoke(index: int):
        try:
            return _run_guarded(inputs, output_paths[index], clients[index])
        except Exception as exc:  # noqa: BLE001 - assert one fail-closed race loser
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(invoke, (0, 1)))

    assert sum(isinstance(result, dict) for result in results) == 1
    assert (
        sum(
            isinstance(result, ControlledShadowAuthorizationPackageError)
            for result in results
        )
        == 1
    )
    assert sum(len(client.calls) for client in clients) == 1


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
            {"headers": {**_response().headers, "X-Datapoints": "57"}},
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


@pytest.mark.parametrize(
    "error,reason_class,category,errno_value",
    [
        (
            URLError(socket.gaierror(-2, "name resolution failed")),
            "gaierror",
            "dns",
            -2,
        ),
        (
            URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")),
            "SSLCertVerificationError",
            "tls_certificate",
            1,
        ),
        (
            URLError(ConnectionRefusedError(111, "connection refused")),
            "ConnectionRefusedError",
            "tcp_refused",
            111,
        ),
        (URLError(TimeoutError("timed out")), "TimeoutError", "timeout", None),
        (URLError("opaque provider failure"), "str", "generic_urllib", None),
        (URLError("proxy tunnel failed"), "str", "proxy", None),
    ],
)
def test_urllib_transport_reason_is_normalized_without_exception_text(
    error: Exception,
    reason_class: str,
    category: str,
    errno_value: int | None,
):
    client = _FakeProofClient(error=error)
    with pytest.raises(NetworkShadowExecutionBlocked) as raised:
        execute_therundown_quota_proof(
            _request(),
            api_key="test-secret-never-written",
            http_client=client,
            now=NOW,
        )

    diagnostic = raised.value.diagnostic
    assert diagnostic is not None
    assert diagnostic["transport_exception_class"] == "URLError"
    assert diagnostic["transport_reason_class"] == reason_class
    assert diagnostic["transport_reason_category"] == category
    assert diagnostic["transport_errno"] == errno_value
    assert "opaque provider failure" not in json.dumps(diagnostic)
    assert "test-secret-never-written" not in json.dumps(diagnostic)
    assert len(client.calls) == 1


def test_transport_failure_artifact_keeps_safe_reason_fields_only(
    tmp_path: Path, monkeypatch
):
    inputs = _guarded_inputs(tmp_path, monkeypatch)
    output_path = tmp_path / "proof.json"
    error = URLError("https://user:password@proxy.example:443/tunnel")
    with pytest.raises(NetworkShadowExecutionBlocked, match="transport failed"):
        _run_guarded(inputs, output_path, _FakeProofClient(error=error))

    failure = json.loads(Path(f"{output_path}.failure.json").read_text())
    assert failure["transport_exception_class"] == "URLError"
    assert failure["transport_reason_class"] == "str"
    assert failure["transport_reason_category"] == "proxy"
    assert failure["transport_errno"] is None
    serialized = json.dumps(failure)
    assert "user:password" not in serialized
    assert "proxy.example" not in serialized
    assert "Authorization" not in serialized
    assert "test-secret" not in serialized


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_http_status_failure_precedes_body_validation(status: int):
    response = _response(
        status_code=status,
        payload=None,
        error_detail="HTTPError",
        headers={
            "X-Datapoints": "55",
            "X-Datapoints-Remaining": "275",
            "X-Tier": "free",
        },
    )
    client = _FakeProofClient(response)
    with pytest.raises(
        NetworkShadowExecutionBlocked,
        match=f"HTTP {status}",
    ):
        execute_therundown_quota_proof(
            _request(),
            api_key="test-secret",
            http_client=client,
            now=NOW,
        )
    assert len(client.calls) == 1


def test_requests_http_error_preserves_safe_status_headers_and_json(monkeypatch):
    body = b'{"error":"rate limited"}'
    response_headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Datapoints": "55",
        "X-Datapoints-Remaining": "275",
        "X-Tier": "free",
        "Authorization": "Bearer test-secret",
    }
    monkeypatch.setattr(
        network_shadow.requests,
        "request",
        lambda **kwargs: _FakeRequestsResponse(
            status_code=429,
            content=body,
            headers=response_headers,
        ),
    )
    response = TheRundownRequestsHttpClientV1().execute(
        _request().as_http_request("test-secret", now=NOW)
    )

    assert response.status_code == 429
    assert response.payload == {"error": "rate limited"}
    assert response.headers == {
        "x-datapoints": "55",
        "x-datapoints-remaining": "275",
        "x-tier": "free",
    }
    assert response.content_type == "application/json; charset=utf-8"
    assert response.body_length == len(b'{"error":"rate limited"}')
    assert (
        response.body_digest
        == network_shadow.sha256(b'{"error":"rate limited"}').hexdigest()
    )
    assert response.error_detail == "HTTPError"


def test_requests_client_uses_verified_certifi_bundle_and_preserves_request(
    monkeypatch,
):
    captured: dict[str, object] = {}

    def fake_request(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return _FakeRequestsResponse()

    monkeypatch.setattr(network_shadow.requests, "request", fake_request)
    request = _request().as_http_request("test-secret-never-written", now=NOW)
    response = TheRundownRequestsHttpClientV1().execute(request)

    assert captured["method"] == "GET"
    assert captured["url"] == request.endpoint
    assert captured["params"] == dict(request.query)
    assert captured["headers"] == dict(request.headers)
    assert captured["timeout"] == request.timeout_seconds
    assert captured["verify"] == network_shadow.certifi.where()
    assert captured["allow_redirects"] is False
    assert response.status_code == 200
    assert "User-Agent" not in captured["headers"]
    assert isinstance(TheRundownUrlLibHttpClientV1(), TheRundownRequestsHttpClientV1)


def test_requests_client_does_not_follow_redirect_or_retry(monkeypatch):
    calls = []

    def fake_request(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return _FakeRequestsResponse(
            status_code=302,
            content=b"",
            headers={"Location": "https://other.example/"},
        )

    monkeypatch.setattr(network_shadow.requests, "request", fake_request)
    response = TheRundownRequestsHttpClientV1().execute(
        _request().as_http_request("test-secret", now=NOW)
    )

    assert response.status_code == 302
    assert response.error_detail == "HTTPError"
    assert len(calls) == 1
    assert calls[0]["allow_redirects"] is False


def test_requests_transport_failure_keeps_safe_proxy_diagnostic(monkeypatch):
    error = network_shadow.requests.exceptions.ProxyError(
        "https://user:password@proxy.example/tunnel"
    )
    monkeypatch.setattr(
        network_shadow.requests,
        "request",
        lambda **kwargs: (_ for _ in ()).throw(error),
    )

    response = TheRundownRequestsHttpClientV1().execute(
        _request().as_http_request("test-secret", now=NOW)
    )

    assert response.status_code is None
    assert response.error_detail == "ProxyError"
    assert response.transport_reason_class == "ProxyError"
    assert response.transport_reason_category == "proxy"
    serialized = json.dumps(response.safe_failure_diagnostic("transport_failure"))
    assert "user:password" not in serialized
    assert "proxy.example" not in serialized
    assert "test-secret" not in serialized


def test_requests_http_error_non_json_preserves_safe_digest_without_raw_body(
    monkeypatch,
):
    raw_body = b"upstream failure: test-secret"
    monkeypatch.setattr(
        network_shadow.requests,
        "request",
        lambda **kwargs: _FakeRequestsResponse(
            status_code=503,
            content=raw_body,
            headers={"Content-Type": "text/plain", "X-Rate-Limit": "1"},
        ),
    )
    response = TheRundownRequestsHttpClientV1().execute(
        _request().as_http_request("test-secret", now=NOW)
    )

    assert response.status_code == 503
    assert response.payload is None
    assert response.content_type == "text/plain"
    assert response.body_length == len(raw_body)
    assert response.body_digest == network_shadow.sha256(raw_body).hexdigest()
    assert raw_body.decode() not in json.dumps(
        response.safe_failure_diagnostic("http_status_failure")
    )


def test_http_200_malformed_body_reports_body_failure():
    client = _FakeProofClient(
        _response(payload=None, status_code=200, error_detail=None)
    )
    with pytest.raises(
        NetworkShadowExecutionBlocked,
        match="response body is missing or malformed",
    ):
        execute_therundown_quota_proof(
            _request(),
            api_key="test-secret",
            http_client=client,
            now=NOW,
        )


def test_http_200_valid_body_reaches_quota_header_validation():
    client = _FakeProofClient(
        _response(
            payload={"events": [{"event_id": "snapshot-event-001"}]},
            headers={"X-Tier": "free"},
        )
    )
    with pytest.raises(
        NetworkShadowExecutionBlocked,
        match="quota proof header missing",
    ):
        execute_therundown_quota_proof(
            _request(),
            api_key="test-secret",
            http_client=client,
            now=NOW,
        )


def test_consumed_http_failure_writes_safe_failure_artifact(
    tmp_path: Path, monkeypatch
):
    inputs = _guarded_inputs(tmp_path, monkeypatch)
    output_path = tmp_path / "proof.json"
    response = _response(
        status_code=429,
        payload={"error": "rate limited"},
        error_detail="HTTPError",
        content_type="application/json",
        body_length=25,
        body_digest="e" * 64,
    )
    with pytest.raises(
        NetworkShadowExecutionBlocked,
        match="HTTP 429",
    ):
        _run_guarded(inputs, output_path, _FakeProofClient(response))

    failure_path = Path(f"{output_path}.failure.json")
    failure = json.loads(failure_path.read_text())
    assert failure["schema_version"] == "top5-therundown-quota-proof-failure-v1"
    assert failure["http_status"] == 429
    assert failure["safe_response_headers"]["x-tier"] == "free"
    assert failure["content_type"] == "application/json"
    assert failure["response_body_length"] == 25
    assert failure["response_body_digest"] == "e" * 64
    assert failure["request_count"] == 1
    assert failure["retry_count"] == 0
    assert failure["safety"]["quota_confirmed"] is False
    serialized = json.dumps(failure)
    assert "Authorization" not in serialized
    assert "X-TheRundown-Key" not in serialized
    assert "test-secret" not in serialized
    assert "remaining_datapoints" not in failure
    assert not output_path.exists()
    marker_path = _quota_proof_consumption_marker_path(inputs["authorization"])
    assert marker_path.exists()


def test_failure_artifact_is_not_quota_proof_evidence(tmp_path: Path, monkeypatch):
    inputs = _guarded_inputs(tmp_path, monkeypatch)
    output_path = tmp_path / "proof.json"
    with pytest.raises(NetworkShadowExecutionBlocked):
        _run_guarded(
            inputs,
            output_path,
            _FakeProofClient(
                _response(status_code=401, payload=None, error_detail="HTTPError")
            ),
        )
    failure = json.loads(Path(f"{output_path}.failure.json").read_text())
    with pytest.raises(NetworkShadowContractError):
        TheRundownQuotaProofEvidenceV1.from_payload(failure)
    assert failure["execution_phase"] == "quota_proof_failure"
    assert failure["safety"]["discovery_authorized"] is False


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
            "events": [{"event_id": "snapshot-event-001"}],
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
        "evidence_kind": "provider_response_headers",
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


def _dashboard_spend_control(**changes: object) -> dict[str, object]:
    evidence = {
        "schema_version": "top5-spend-control-dashboard-attestation-v1",
        "evidence_kind": "operator_dashboard_attestation",
        "provider": "therundown_experimental",
        "observed_at": (NOW - timedelta(minutes=1)).isoformat(),
        "account_tier": "free",
        "plan_price_usd": 0,
        "paid_overage_enabled": False,
        "daily_datapoint_limit": 20_000,
        "monthly_datapoint_limit": 200_000,
        "rate_limit_requests_per_second": 1,
        "hard_cap_behavior": "http_429",
        "attestation_identity": "ceo:account-owner:therundown-free-tier:20260921",
    }
    evidence.update(changes)
    return evidence


def test_fresh_dashboard_free_attestation_is_spend_control_only(tmp_path: Path):
    path = tmp_path / "dashboard-attestation.json"
    path.write_text(json.dumps(_dashboard_spend_control()))
    evidence = _load_spend_control_evidence(path, now=NOW)
    assert evidence["provider"] == "therundown_experimental"
    assert evidence["account_tier"] == "free"
    assert evidence["overage_exposure"] == "none"
    assert evidence["evidence_kind"] == "operator_dashboard_attestation"
    assert (
        evidence["observed_at"].isoformat() == (NOW - timedelta(minutes=1)).isoformat()
    )
    assert evidence["daily_datapoint_limit"] == 20_000
    assert evidence["monthly_datapoint_limit"] == 200_000
    assert isinstance(evidence["digest"], str)
    assert "remaining_datapoints" not in evidence
    assert "quota_used_datapoints" not in evidence
    assert "safe_headers" not in evidence


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_kind": "provider_response_headers"},
        {"account_tier": "pro"},
        {"plan_price_usd": 1},
        {"paid_overage_enabled": True},
        {"provider": "the_odds_api"},
        {"daily_datapoint_limit": 19_999},
        {"monthly_datapoint_limit": 199_999},
        {"rate_limit_requests_per_second": 2},
        {"hard_cap_behavior": "paid_overage"},
        {"attestation_identity": ""},
        {"observed_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"observed_at": (NOW - timedelta(days=2)).isoformat()},
    ],
)
def test_dashboard_attestation_invalid_state_fails_closed(
    tmp_path: Path, changes: dict[str, object]
):
    path = tmp_path / "invalid-dashboard-attestation.json"
    path.write_text(json.dumps(_dashboard_spend_control(**changes)))
    with pytest.raises(
        ControlledShadowAuthorizationPackageError,
        match="BLOCKED_SPEND_CONTROL",
    ):
        _load_spend_control_evidence(path, now=NOW)


def test_dashboard_quota_claims_are_ignored_and_live_headers_remain_authoritative(
    tmp_path: Path,
):
    path = tmp_path / "dashboard-with-untrusted-quota.json"
    path.write_text(
        json.dumps(
            _dashboard_spend_control(
                remaining_datapoints=999_999,
                quota_used_datapoints=0,
                headers={"X-Datapoints-Remaining": "999999"},
            )
        )
    )
    spend_control = _load_spend_control_evidence(path, now=NOW)
    assert spend_control["overage_exposure"] == "none"
    assert "remaining_datapoints" not in spend_control
    assert "quota_used_datapoints" not in spend_control
    assert "headers" not in spend_control

    proof = execute_therundown_quota_proof(
        _request(),
        api_key="test-secret",
        http_client=_FakeProofClient(_response()),
        now=NOW,
    )
    assert proof.remaining_datapoints == 275
    assert proof.billed_datapoints == 55


def test_dashboard_attestation_only_gates_spend_control_in_guarded_proof(
    tmp_path: Path, monkeypatch
):
    inputs = _guarded_inputs(tmp_path, monkeypatch)
    dashboard_path = tmp_path / "dashboard-spend-control.json"
    dashboard_path.write_text(json.dumps(_dashboard_spend_control()))
    inputs["spend_path"] = dashboard_path
    output_path = tmp_path / "dashboard-proof.json"
    summary = _run_guarded(inputs, output_path, _FakeProofClient(_response()))

    assert summary["spend_control_evidence_kind"] == ("operator_dashboard_attestation")
    assert summary["remaining_datapoints"] == 275
    output = json.loads(output_path.read_text())
    spend_control = output["spend_control"]
    assert spend_control["evidence_kind"] == "operator_dashboard_attestation"
    assert spend_control["daily_datapoint_limit"] == 20_000
    assert spend_control["monthly_datapoint_limit"] == 200_000
    assert "remaining_datapoints" not in spend_control


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
