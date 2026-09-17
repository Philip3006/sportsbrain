"""Safety and determinism tests for the no-network controlled-run planner."""

from __future__ import annotations

import inspect
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.top5_controlled_shadow_preparation import main as preparation_cli
from src.football.provider_cascade.contracts import MARKET_PREMATCH_1X2
from src.football.provider_cascade.preparation import (
    PREPARATION_BLOCKED,
    PREPARATION_READY,
    SUPPORTED_PREPARATION_PROVIDERS,
    ControlledShadowRunPreparationV1,
    PreparationContractError,
    load_preparation,
    preparation_from_input_payload,
    write_preparation,
)

BASE = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)
ORDER = SUPPORTED_PREPARATION_PROVIDERS


def _fixture(league: str = "EPL") -> dict[str, object]:
    return {
        "fixture_key": f"{league}:fixture-001",
        "league_code": league,
        "home_team": "Home FC",
        "away_team": "Away FC",
        "kickoff": BASE.isoformat(),
    }


def _state(
    provider: str,
    *,
    identity: str = "RESOLVED",
    quota_remaining: int | None = 10,
    readiness: str = "LIVE_PATH_READY_FOR_OBSERVATION",
    provider_fixture_id: str | None = None,
    cost: float = 1.0,
    pagination_risk: str | None = None,
    body_error_taxonomy: tuple[str, ...] = (),
) -> dict[str, object]:
    discovery = identity == "DISCOVERY_REQUIRED"
    known = identity == "RESOLVED"
    return {
        "readiness_state": readiness,
        "identity_state": identity,
        "fixture_id_known": known,
        "provider_fixture_id": provider_fixture_id
        or (f"{provider}-event-001" if known else None),
        "discovery_required": discovery,
        "quota": {"used": 0, "remaining": quota_remaining},
        "quota_cost_units_per_request": cost,
        "pagination_risk": pagination_risk,
        "body_error_taxonomy": list(body_error_taxonomy),
    }


def _input(
    *,
    order: tuple[str, ...] = ORDER,
    credentials: dict[str, bool | None] | None = None,
    states: dict[str, dict[str, object]] | None = None,
    maximum_requests: int = 4,
    maximum_cost: float = 4.0,
    fixture: dict[str, object] | None = None,
) -> dict[str, object]:
    credentials = credentials or {provider: True for provider in ORDER}
    states = states or {provider: _state(provider) for provider in ORDER}
    return {
        "fixture": fixture or _fixture(),
        "timing_policy": {
            "maximum_odds_age_seconds": 900,
            "kickoff_tolerance_seconds": 60,
        },
        "provider_order": list(order),
        "credential_presence": credentials,
        "provider_readiness": states,
        "maximum_total_network_requests": maximum_requests,
        "maximum_total_quota_cost_units": maximum_cost,
    }


def _prepare(**kwargs):
    return preparation_from_input_payload(_input(**kwargs))


def test_valid_preparation_has_only_the_odds_api_and_safety_flags() -> None:
    preparation = _prepare()
    assert preparation.preparation_status == PREPARATION_READY
    assert tuple(item.provider for item in preparation.providers) == ORDER
    assert [item.expected_network_request_count for item in preparation.providers] == [1]
    assert [
        item.provider for item in preparation.expected_sequential_execution_plan
    ] == list(ORDER)
    assert preparation.league == "EPL"
    assert preparation.timing_policy["maximum_odds_age_seconds"] == 900
    assert preparation.no_network_guarantee is True
    assert preparation.authorization_required is True
    assert preparation.authorization_status == "NOT_AUTHORIZED"
    assert preparation.execution_allowed is False
    assert preparation.builder2_receipt_required_before_capture is False
    assert preparation.builder2_receipt_issuer_exposed is False
    assert preparation.no_bet is True
    assert preparation.betting is False
    assert preparation.publication is False
    assert preparation.production_activation is False
    assert preparation.sealed_data_access is False
    assert preparation.monetary_spend == 0.0


def test_preparation_digest_and_id_are_deterministic() -> None:
    first = _prepare()
    second = _prepare()
    assert first.preparation_id == second.preparation_id
    assert first.preparation_digest == second.preparation_digest
    assert first.as_payload() == second.as_payload()


def test_configured_order_is_the_odds_api_only() -> None:
    order = ("the_odds_api",)
    preparation = _prepare(order=order)
    assert preparation.configured_provider_order == order
    assert [
        item.provider for item in preparation.expected_sequential_execution_plan
    ] == list(order)
    positions = {
        item.provider: item.configured_cascade_position
        for item in preparation.providers
    }
    assert positions == {provider: order.index(provider) for provider in order}


def test_the_odds_api_exhausted_500_0_plans_zero_requests() -> None:
    states = {"the_odds_api": _state("the_odds_api", quota_remaining=0)}
    preparation = _prepare(
        order=("the_odds_api",),
        credentials={"the_odds_api": True},
        states=states,
        maximum_requests=1,
        maximum_cost=1.0,
    )
    provider = preparation.providers[0]
    assert preparation.preparation_status == PREPARATION_BLOCKED
    assert provider.expected_network_request_count == 0
    assert provider.expected_quota_cost_units == 0.0
    assert provider.current_known_quota["used"] == 0
    assert provider.current_known_quota["remaining"] == 0
    assert (
        "500/remaining=0" in provider.reason_executable_or_blocked
        or "insufficient" in provider.reason_executable_or_blocked
    )
    assert preparation.expected_sequential_execution_plan[0].network_request_count == 0


@pytest.mark.parametrize("credential", [False, None])
def test_missing_or_unknown_credentials_fail_closed(credential: bool | None) -> None:
    preparation = _prepare(
        order=("the_odds_api",),
        credentials={"the_odds_api": credential},
        states={"the_odds_api": _state("the_odds_api")},
        maximum_requests=1,
        maximum_cost=1.0,
    )
    provider = next(
        item for item in preparation.providers if item.provider == "the_odds_api"
    )
    assert preparation.preparation_status == PREPARATION_BLOCKED
    assert provider.executable is False
    assert provider.expected_network_request_count == 0
    assert provider.credential_present is credential
    assert credential in (False, None)


def test_discovery_required_is_fail_closed_for_the_odds_api() -> None:
    provider = "the_odds_api"
    preparation = _prepare(
        order=(provider,),
        credentials={provider: True},
        states={provider: _state(provider, identity="DISCOVERY_REQUIRED")},
        maximum_requests=2,
        maximum_cost=2.0,
    )
    manifest = next(item for item in preparation.providers if item.provider == provider)
    actions = preparation.expected_sequential_execution_plan[:2]
    assert preparation.preparation_status == PREPARATION_READY
    assert manifest.fixture_discovery_required is True
    assert manifest.provider_fixture_id_known is False
    assert manifest.expected_network_request_count == 1
    assert actions[0].action_class == "ODDS_AND_EVENT_DISCOVERY"


@pytest.mark.parametrize("identity", ["UNRESOLVED", "AMBIGUOUS"])
def test_unresolved_or_ambiguous_identity_has_no_odds_plan(identity: str) -> None:
    preparation = _prepare(
        order=("the_odds_api",),
        credentials={"the_odds_api": True},
        states={"the_odds_api": _state("the_odds_api", identity=identity)},
        maximum_requests=2,
        maximum_cost=2.0,
    )
    manifest = next(
        item for item in preparation.providers if item.provider == "the_odds_api"
    )
    assert preparation.preparation_status == PREPARATION_BLOCKED
    assert manifest.expected_network_request_count == 0
    assert manifest.executable is False
    assert (
        preparation.expected_sequential_execution_plan[0].action_class
        == "PRECHECK_ONLY"
    )


def test_global_and_per_provider_request_and_quota_caps_fail_closed() -> None:
    preparation = _prepare(
        order=("the_odds_api",),
        credentials={"the_odds_api": True},
        states={"the_odds_api": _state("the_odds_api")},
        maximum_requests=1,
        maximum_cost=1.0,
        # One canonical provider receives one bounded request.
    )
    first = preparation.providers[0]
    assert first.expected_network_request_count == 1
    assert (
        sum(
            action.network_request_count
            for action in preparation.expected_sequential_execution_plan
        )
        == 1
    )
    assert preparation.preparation_status == PREPARATION_READY

    capped = _prepare(
        order=("the_odds_api",),
        credentials={"the_odds_api": True},
        states={"the_odds_api": _state("the_odds_api", cost=2.0)},
        maximum_requests=1,
        maximum_cost=2.0,
    )
    assert capped.preparation_status == PREPARATION_READY
    assert capped.providers[0].expected_quota_cost_units == 2.0

    provider_capped = _prepare(
        order=("the_odds_api",),
        credentials={"the_odds_api": True},
        states={"the_odds_api": _state("the_odds_api")},
        maximum_requests=1,
        maximum_cost=1.0,
    )
    # Caller-supplied provider maximums are exercised through the payload API below.
    payload = _input(
        order=("the_odds_api",),
        credentials={"the_odds_api": True},
        states={"the_odds_api": _state("the_odds_api")},
        maximum_requests=1,
        maximum_cost=1.0,
    )
    payload["per_provider_maximum_requests"] = {"the_odds_api": 0}
    limited = preparation_from_input_payload(payload)
    assert limited.preparation_status == PREPARATION_BLOCKED
    assert limited.providers[0].expected_network_request_count == 0
    assert (
        "provider request maximum" in limited.providers[0].reason_executable_or_blocked
    )
    assert provider_capped.preparation_status == PREPARATION_READY


def test_zero_quota_cost_cap_blocks_without_network() -> None:
    preparation = _prepare(
        order=("the_odds_api",),
        credentials={"the_odds_api": True},
        states={"the_odds_api": _state("the_odds_api")},
        maximum_requests=1,
        maximum_cost=0.0,
    )
    assert preparation.preparation_status == PREPARATION_BLOCKED
    assert preparation.providers[0].expected_network_request_count == 0
    assert (
        "global quota-cost budget"
        in preparation.providers[0].reason_executable_or_blocked
    )


@pytest.mark.parametrize(
    "payload_change",
    [
        {"fixture": _fixture("MLS")},
        {"provider_order": ["unknown_provider"]},
        {"provider_order": ["the_odds_api", "the_odds_api"]},
        {"maximum_total_network_requests": -1},
        {"maximum_total_quota_cost_units": -1.0},
        {"timing_policy": {"maximum_odds_age_seconds": 900}},
        {"provider_readiness": {"the_odds_api": {"readiness_state": "not-real"}}},
    ],
)
def test_invalid_preparation_input_fails_closed(
    payload_change: dict[str, object],
) -> None:
    payload = _input()
    payload.update(payload_change)
    with pytest.raises(PreparationContractError):
        preparation_from_input_payload(payload)


def test_unknown_readiness_never_becomes_ready() -> None:
    preparation = _prepare(
        order=("the_odds_api",),
        credentials={"the_odds_api": True},
        states={"the_odds_api": _state("the_odds_api", readiness="UNKNOWN")},
        maximum_requests=1,
        maximum_cost=1.0,
    )
    assert preparation.preparation_status == PREPARATION_BLOCKED
    assert preparation.providers[0].readiness_state == "UNKNOWN"
    assert preparation.providers[0].executable is False


def test_ready_status_is_not_authorization_and_needs_no_b2_receipt() -> None:
    payload = _input()
    preparation = preparation_from_input_payload(payload)
    assert preparation.preparation_status == PREPARATION_READY
    assert preparation.authorization_status == "NOT_AUTHORIZED"
    assert preparation.execution_allowed is False
    assert preparation.builder2_receipt_required_before_capture is False
    assert "builder2_receipt" not in payload
    assert "qualification_receipt" not in payload


def test_preparation_has_no_network_or_ledger_execution_path(monkeypatch) -> None:
    def forbidden_socket(*args, **kwargs):
        raise AssertionError("preparation opened a socket")

    monkeypatch.setattr(socket, "socket", forbidden_socket)
    preparation = _prepare()
    assert preparation.no_network_guarantee is True
    source = inspect.getsource(
        __import__("src.football.provider_cascade.preparation", fromlist=["x"])
    )
    assert "provider_cascade.adapters" not in source
    assert "src.betting" not in source
    assert "Builder2QualificationReceipt" not in source


def test_external_storage_round_trip_and_checkout_ledger_paths_are_blocked(
    tmp_path: Path,
) -> None:
    preparation = _prepare()
    artifact = tmp_path / "preparation.json"
    assert write_preparation(preparation, artifact) == artifact
    assert (
        load_preparation(artifact).preparation_digest == preparation.preparation_digest
    )
    with pytest.raises(PreparationContractError, match="active checkout"):
        write_preparation(
            preparation, Path(__file__).resolve().parents[2] / "preparation.json"
        )
    with pytest.raises(PreparationContractError, match="external preparation"):
        write_preparation(preparation, tmp_path / "ledger" / "preparation.json")


def test_payload_round_trip_and_market_contract_are_bound() -> None:
    preparation = _prepare()
    payload = preparation.as_payload()
    assert payload["market_type"] == MARKET_PREMATCH_1X2
    restored = ControlledShadowRunPreparationV1.from_payload(payload)
    assert restored.as_payload() == payload
    tampered = json.loads(json.dumps(payload))
    tampered["providers"][0]["expected_network_request_count"] = 99
    with pytest.raises(PreparationContractError):
        ControlledShadowRunPreparationV1.from_payload(tampered)
    for field in ("market_type", "storage_scope"):
        tampered = json.loads(json.dumps(payload))
        tampered[field] = "tampered"
        with pytest.raises(PreparationContractError):
            ControlledShadowRunPreparationV1.from_payload(tampered)


def test_b4_preparation_does_not_expose_a_builder2_issuer() -> None:
    module = __import__("src.football.provider_cascade.preparation", fromlist=["x"])
    assert not hasattr(module, "issue_builder2_qualification_receipt")


def test_cli_prepare_inspect_validate_is_prominent_and_no_network(
    tmp_path: Path, capsys
) -> None:
    input_path = tmp_path / "fixture-manifest.json"
    input_path.write_text(json.dumps(_input()))
    output_path = tmp_path / "preparation.json"

    assert (
        preparation_cli(["prepare", str(input_path), "--output", str(output_path)]) == 0
    )
    captured = capsys.readouterr()
    prepared_output = json.loads(captured.out)
    banner = captured.err
    assert prepared_output["preparation_status"] == PREPARATION_READY
    assert prepared_output["stored_path"] == str(output_path)
    assert "PREPARATION ONLY" in banner
    assert "NO NETWORK" in banner
    assert "CEO AUTHORIZATION REQUIRED FOR REAL EXECUTION" in banner

    assert preparation_cli(["inspect", str(output_path)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["preparation_digest"] == prepared_output["preparation_digest"]

    assert preparation_cli(["validate", str(output_path)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated == {
        "preparation_digest": prepared_output["preparation_digest"],
        "preparation_id": prepared_output["preparation_id"],
        "preparation_status": PREPARATION_READY,
        "valid": True,
    }
