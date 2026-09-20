"""Offline tests for the one-request TheRundown quota-proof boundary."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.football.top5_controlled_shadow_authorization_package as b4_package
from src.football.odds.therundown import THERUNDOWN_PROVIDER_NAME
from src.football.top5_controlled_shadow_authorization_package import (
    ControlledShadowAuthorizationPackageError,
    _digest,
    _load_quota_proof_target_evidence,
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
        "payload": {"event": "provider-response"},
        "headers": headers,
        "started_at": NOW - timedelta(seconds=1),
        "finished_at": NOW,
    }
    values.update(changes)
    return TheRundownNetworkHttpResponseV1(**values)


def _proof_authorization(
    *,
    provider_event_id: str = "event-ll-001",
    target_source_digest: str = "d" * 64,
    **changes: object,
) -> TheRundownQuotaProofAuthorizationV1:
    request = _request(provider_event_id=provider_event_id)
    authorization = TheRundownQuotaProofAuthorizationV1(
        proof_authorization_id="CEO-TOP5-QUOTA-PROOF-001",
        ceo_proof_authorization_identity="ceo:quota-proof",
        proof_id="quota-proof:authorized-001",
        provider=THERUNDOWN_PROVIDER_NAME,
        provider_event_id=provider_event_id,
        proof_target_source_digest=target_source_digest,
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
    """Build a deterministic local target and redirect only canonical state in tests."""

    provider_event_id = "event-ll-001"
    body_path = tmp_path / "source-event.json"
    body_path.write_text(json.dumps({"events": [{"event_id": provider_event_id}]}))
    headers_path = tmp_path / "source-headers.json"
    headers_path.write_text(json.dumps({"X-Datapoints": "55"}))
    raw = {
        "schema_version": "top5-b1-laliga-final-evidence-v1",
        "evidence_kind": "REAL_OBSERVED",
        "canonical_b1_evidence_bundle": {
            "capture_status": "CAPTURED",
            "raw_response_digest": "a" * 64,
        },
        "original_network_capture": {
            "provider": "therundown_experimental",
            "league": "LL",
            "network_evidence_is_original": True,
            "raw_response_digest": "a" * 64,
        },
        "repaired_normalization": {
            "provider_event_id": provider_event_id,
            "captured_at": NOW.isoformat(),
            "raw_response_digest": "a" * 64,
        },
        "replay": {
            "source_event_body": str(body_path),
            "source_event_headers": str(headers_path),
        },
    }
    target_path = tmp_path / "target-evidence.json"
    target_path.write_text(json.dumps(raw))
    authorization = _proof_authorization(
        provider_event_id=provider_event_id,
        target_source_digest=_digest(raw),
    )
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
        "target_path": target_path,
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


def test_proof_only_authorization_does_not_require_five_league_scope():
    authorization = _proof_authorization()
    authorization.validate(now=NOW)
    request = authorization.request_for_proof(proof_configuration_digest="e" * 64)
    client = _FakeProofClient(_response())
    evidence = execute_therundown_quota_proof(
        request,
        api_key="test-secret",
        http_client=client,
        now=NOW,
    )
    assert evidence.authorization_id == authorization.proof_authorization_id
    assert evidence.proof_target_source_digest == "d" * 64
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


def test_known_local_real_event_is_deterministically_bound_without_discovery():
    path = Path("/private/tmp/top5-b1-laliga-final-evidence.json")
    if not path.exists():
        pytest.skip("trusted local B1 evidence is not available")
    raw = json.loads(path.read_text())
    target = raw["repaired_normalization"]["provider_event_id"]
    authorization = _proof_authorization(
        provider_event_id=target,
        target_source_digest=_digest(raw),
    )
    selected = _load_quota_proof_target_evidence(
        path,
        authorization=authorization,
    )
    assert selected["provider_event_id"] == target
    assert selected["league"] == "LL"
    assert selected["source_digest"] == _digest(raw)


def test_guarded_proof_uses_proof_authorization_without_five_league_package(
    tmp_path: Path,
    monkeypatch,
):
    target_path = Path("/private/tmp/top5-b1-laliga-final-evidence.json")
    if not target_path.exists():
        pytest.skip("trusted local B1 evidence is not available")
    monkeypatch.setattr(
        b4_package,
        "quota_proof_consumption_state_path",
        lambda: tmp_path / "canonical-consumption-store",
    )
    raw = json.loads(target_path.read_text())
    authorization = _proof_authorization(
        provider_event_id=raw["repaired_normalization"]["provider_event_id"],
        target_source_digest=_digest(raw),
    )
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
        target_path,
        spend_control_evidence_path=spend_path,
        credential_file=credential_path,
        output_path=output_path,
        clock=lambda: NOW,
        http_client=client,
    )

    assert summary["status"] == "TOP5_B4_QUOTA_PROOF — QUOTA_CONFIRMED"
    assert summary["proof_authorization_id"] == authorization.proof_authorization_id
    assert summary["selected_proof_target"] == authorization.provider_event_id
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
        inputs["target_path"],
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
        provider_event_id=inputs["authorization"].provider_event_id,
        target_source_digest=inputs["authorization"].proof_target_source_digest,
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


def test_invalid_target_evidence_does_not_consume_authorization(
    tmp_path: Path, monkeypatch
):
    inputs = _guarded_inputs(tmp_path, monkeypatch)
    valid_raw = json.loads(inputs["target_path"].read_text())
    invalid_raw = json.loads(inputs["target_path"].read_text())
    invalid_raw["repaired_normalization"]["provider_event_id"] = "wrong-event"
    inputs["target_path"].write_text(json.dumps(invalid_raw))
    invalid_client = _FakeProofClient(_response())
    with pytest.raises(
        ControlledShadowAuthorizationPackageError,
        match="target event binding mismatch",
    ):
        _run_guarded(inputs, tmp_path / "invalid.json", invalid_client)
    assert invalid_client.calls == []

    inputs["target_path"].write_text(json.dumps(valid_raw))
    valid_client = _FakeProofClient(_response())
    _run_guarded(inputs, tmp_path / "valid.json", valid_client)
    assert len(valid_client.calls) == 1


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
