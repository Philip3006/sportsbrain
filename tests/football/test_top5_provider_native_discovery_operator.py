from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

from src.football.top5_therundown_provider_native_discovery import (
    PROVIDER_NATIVE_DISCOVERY_SCHEMA_VERSION,
    PROVIDER_NATIVE_DISCOVERY_STRUCTURAL_AUTHORIZATION_SCHEMA_VERSION,
    EventDiscoveryExecutionBlocked,
    TheRundownProviderNativeDiscoveryRunResultV1,
    main,
    run_structural_provider_native_discovery_operator,
)
from tests.football import test_top5_final_acceptance as builder1_tests
from tests.football import (
    test_top5_therundown_provider_native_discovery as native_tests,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _quota_package(monkeypatch, now=NOW) -> dict[str, object]:
    monkeypatch.setattr(builder1_tests, "NOW", now)
    package = builder1_tests._b4_package()
    proof = package["proof"]
    proof.update(
        {
            "remaining_datapoints": 19944,
            "quota_used_datapoints": 56,
            "quota_limit_datapoints": 20000,
        }
    )
    proof["raw_header_evidence"].update(
        {
            "x-datapoints-used": "56",
            "x-datapoints-remaining": "19944",
            "x-datapoints-limit": "20000",
        }
    )
    proof["evidence_digest"] = builder1_tests.canonical_digest(
        {key: value for key, value in proof.items() if key != "evidence_digest"}
    )
    return package


def _write_package(path, package):
    path.write_text(json.dumps(package), encoding="utf-8")
    return path


def _common_inputs(tmp_path, package_path):
    return {
        "quota_proof_package_path": package_path,
        "discovery_authorization_id": "native-operator-auth-test",
        "ceo_discovery_authorization_identity": "ceo:offline:native-operator-test",
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(hours=1),
        "authorization_output_path": tmp_path / "authorization.json",
        "now": NOW,
    }


def test_operator_dry_run_is_network_and_credential_free(monkeypatch, tmp_path):
    package_path = _write_package(tmp_path / "proof.json", _quota_package(monkeypatch))
    monkeypatch.setattr(
        "src.football.top5_therundown_provider_native_discovery._current_repository_source_sha",
        lambda: "d" * 40,
    )
    monkeypatch.setattr(
        "src.football.top5_therundown_provider_native_discovery.discover_five_league_events_provider_native",
        lambda *_args, **_kwargs: pytest.fail("dry-run attempted Discovery"),
    )

    args = _common_inputs(tmp_path, package_path)
    args["authorization_output_path"] = None
    result = run_structural_provider_native_discovery_operator(**args)

    assert result["status"] == "DRY_RUN_READY_NO_NETWORK"
    assert result["provider_requests"] == 0
    assert result["credential_accesses"] == 0
    assert result["authorization_schema"] == (
        PROVIDER_NATIVE_DISCOVERY_STRUCTURAL_AUTHORIZATION_SCHEMA_VERSION
    )
    assert result["league_order"] == ["EPL", "BL1", "LL", "SA", "L1"]
    assert result["maximum_retries"] == 0
    assert result["minimum_interval_seconds"] >= 1.1
    assert not (tmp_path / "authorization.json").exists()


def test_documented_module_command_builds_current_v3_without_network(
    monkeypatch, tmp_path, capsys
):
    now = datetime.now(timezone.utc)
    package_path = _write_package(
        tmp_path / "proof.json", _quota_package(monkeypatch, now)
    )
    monkeypatch.setattr(
        "src.football.top5_therundown_provider_native_discovery._current_repository_source_sha",
        lambda: "d" * 40,
    )
    monkeypatch.setattr(
        "src.football.top5_therundown_provider_native_discovery.discover_five_league_events_provider_native",
        lambda *_args, **_kwargs: pytest.fail("documented dry-run attempted network"),
    )

    status = main(
        [
            "run",
            "--quota-proof-package",
            str(package_path),
            "--discovery-authorization-id",
            "native-cli-auth-test",
            "--ceo-discovery-authorization-identity",
            "ceo:offline:native-cli-test",
            "--issued-at",
            (now - timedelta(minutes=1)).isoformat(),
            "--expires-at",
            (now + timedelta(hours=1)).isoformat(),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert status == 0
    assert output["status"] == "DRY_RUN_READY_NO_NETWORK"
    assert output["authorization_schema"] == (
        PROVIDER_NATIVE_DISCOVERY_STRUCTURAL_AUTHORIZATION_SCHEMA_VERSION
    )
    assert output["provider_requests"] == 0
    assert output["credential_accesses"] == 0


def test_operator_executes_only_current_v3_flow_with_injected_transport(
    monkeypatch, tmp_path
):
    package_path = _write_package(tmp_path / "proof.json", _quota_package(monkeypatch))
    monkeypatch.setattr(
        "src.football.top5_therundown_provider_native_discovery._current_repository_source_sha",
        lambda: "d" * 40,
    )
    monkeypatch.setattr(
        "src.football.top5_therundown_event_discovery.discovery_authorization_consumption_state_path",
        lambda: tmp_path / "consumption.json",
    )
    monkeypatch.setattr(native_tests, "NOW", NOW)
    transport = native_tests.FakeNativeTransport(
        [
            native_tests._response(league)
            for league in native_tests.DISCOVERY_LEAGUE_ORDER
        ]
    )
    args = _common_inputs(tmp_path, package_path)
    args.update(
        {
            "execute_network": True,
            "result_output_path": tmp_path / "native-result.json",
            "transport": transport,
        }
    )

    result = run_structural_provider_native_discovery_operator(**args)

    authorization = json.loads((tmp_path / "authorization.json").read_text())
    native_result_payload = json.loads((tmp_path / "native-result.json").read_text())
    native_result = TheRundownProviderNativeDiscoveryRunResultV1.from_payload(
        native_result_payload
    )
    assert result["status"] == "COMPLETED_NETWORK"
    assert result["request_count"] == 5
    assert result["credential_accesses"] == 0
    assert len(transport.calls) == 5
    assert authorization["schema_version"] == (
        PROVIDER_NATIVE_DISCOVERY_STRUCTURAL_AUTHORIZATION_SCHEMA_VERSION
    )
    assert authorization["selection_purpose"] == "STRUCTURAL_PROVIDER"
    assert "minimum_lead_seconds" not in authorization
    assert "maximum_lead_seconds" not in authorization
    assert native_result.as_payload() == native_result_payload
    assert (
        native_result_payload["schema_version"]
        == PROVIDER_NATIVE_DISCOVERY_SCHEMA_VERSION
    )
    assert [capture.league for capture in native_result.captures] == [
        "EPL",
        "BL1",
        "LL",
        "SA",
        "L1",
    ]
    assert stat.S_IMODE((tmp_path / "authorization.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "native-result.json").stat().st_mode) == 0o600
    assert json.loads((tmp_path / "consumption.json").read_text())


def test_operator_expired_authorization_fails_before_marker_or_transport(
    monkeypatch, tmp_path
):
    package_path = _write_package(tmp_path / "proof.json", _quota_package(monkeypatch))
    monkeypatch.setattr(
        "src.football.top5_therundown_provider_native_discovery._current_repository_source_sha",
        lambda: "d" * 40,
    )
    monkeypatch.setattr(
        "src.football.top5_therundown_event_discovery.discovery_authorization_consumption_state_path",
        lambda: tmp_path / "consumption.json",
    )
    transport = native_tests.FakeNativeTransport([])
    args = _common_inputs(tmp_path, package_path)
    args.update(
        {
            "expires_at": NOW - timedelta(seconds=1),
            "execute_network": True,
            "result_output_path": tmp_path / "native-result.json",
            "transport": transport,
        }
    )

    with pytest.raises(EventDiscoveryExecutionBlocked, match="expired"):
        run_structural_provider_native_discovery_operator(**args)

    assert transport.calls == []
    assert not (tmp_path / "consumption.json").exists()
    assert not (tmp_path / "authorization.json").exists()
