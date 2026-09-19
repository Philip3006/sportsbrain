from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.football.odds.therundown import THERUNDOWN_ADAPTER_VERSION
from src.football.odds.therundown_la_liga_capture import (
    LA_LIGA_CODE,
    LA_LIGA_MAX_DATAPOINTS,
    LA_LIGA_MAX_REQUESTS,
    LaLigaCaptureAuthorization,
    LaLigaCaptureStatus,
    capture_la_liga,
    preflight_la_liga,
)
from src.football.provider_cascade.adapters import RawProviderResponse

_ROOT = Path(__file__).parents[3]
_NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
_RESPONSE_NOW = datetime(2026, 9, 22, 19, tzinfo=timezone.utc)
_AUTH = LaLigaCaptureAuthorization(
    provider="therundown_experimental",
    league=LA_LIGA_CODE,
    maximum_request_count=LA_LIGA_MAX_REQUESTS,
    maximum_datapoint_budget=LA_LIGA_MAX_DATAPOINTS,
    quota_before_used=100,
    quota_before_remaining=19900,
    expires_at=_NOW + timedelta(hours=1),
    controlled_shadow_run_id="ll-run-001",
    ceo_authorization_id="ceo-ll-001",
    qualification_session_id="ll-session-001",
)


def _event() -> dict[str, object]:
    with (
        _ROOT / "tests/fixtures/therundown/champions_league_events.json"
    ).open() as handle:
        event = json.load(handle)["events"][0]
    event["sport_id"] = 14
    event["schedule"]["league_name"] = "La Liga"
    event["event_date"] = "2026-09-22T19:00:00Z"
    event["event_id"] = "ll-event-001"
    return event


def _response(
    payload: object,
    *,
    used: int,
    remaining: int,
    cost: int | None,
    extra_headers: dict[str, str] | None = None,
    omit_headers: set[str] | None = None,
) -> RawProviderResponse:
    headers = {
        "x-datapoints-used": str(used),
        "x-datapoints-remaining": str(remaining),
        "x-datapoints-limit": "20000",
        "x-rate-limit": "1",
        "x-rate-limit-remaining": "1",
        "x-tier": "free",
        "x-data-delay-seconds": "300",
    }
    if cost is not None:
        headers["x-datapoints"] = str(cost)
    if extra_headers:
        headers.update(extra_headers)
    for name in omit_headers or set():
        headers.pop(name, None)
    return RawProviderResponse(
        200,
        payload,
        headers,
        _RESPONSE_NOW,
        _RESPONSE_NOW + timedelta(seconds=30),
        30_000,
    )


def _transport(responses: list[RawProviderResponse], calls: list[object]):
    def send(request: object, timeout: float) -> RawProviderResponse:
        del timeout
        calls.append(request)
        return responses.pop(0)

    return send


def _run(
    *,
    authorization: LaLigaCaptureAuthorization | None = _AUTH,
    event: dict[str, object] | None = None,
    dates: list[str] | None = None,
    date_cost: int | None = 1,
    event_cost: int | None = 11,
    date_used: int = 101,
    date_remaining: int = 19899,
    event_used: int = 112,
    event_remaining: int = 19888,
    date_headers: dict[str, str] | None = None,
    event_headers: dict[str, str] | None = None,
    date_omit_headers: set[str] | None = None,
    event_omit_headers: set[str] | None = None,
    budget: int = LA_LIGA_MAX_DATAPOINTS,
):
    calls: list[object] = []
    auth = authorization
    if auth is not None and budget != auth.maximum_datapoint_budget:
        auth = LaLigaCaptureAuthorization(
            **{**auth.__dict__, "maximum_datapoint_budget": budget}
        )
    responses = [
        _response(
            {"dates": dates or ["2026-09-22"]},
            used=date_used,
            remaining=date_remaining,
            cost=date_cost,
            extra_headers=date_headers,
            omit_headers=date_omit_headers,
        ),
        _response(
            {"events": [event or _event()]},
            used=event_used,
            remaining=event_remaining,
            cost=event_cost,
            extra_headers=event_headers,
            omit_headers=event_omit_headers,
        ),
    ]
    result = capture_la_liga(
        auth,
        now=_NOW,
        today=date(2026, 9, 18),
        transport=_transport(responses, calls),
    )
    return result, calls


def test_no_authorization_is_disabled_without_a_transport_call():
    result, calls = _run(authorization=None)

    assert result.status is LaLigaCaptureStatus.DISABLED
    assert result.reason == "explicit_capture_authorization_required"
    assert calls == []


def test_offline_preflight_validates_environment_without_network():
    result = preflight_la_liga(
        _AUTH,
        now=_NOW,
        environment={"THERUNDOWN_API_KEY": "synthetic-only"},
    )

    assert result.status is LaLigaCaptureStatus.READY
    assert result.requests_used == 0
    assert "network_not_called" in result.reason


def test_offline_preflight_rejects_missing_credential_without_network():
    result = preflight_la_liga(_AUTH, now=_NOW, environment={})

    assert result.status is LaLigaCaptureStatus.REJECTED
    assert result.reason == "THERUNDOWN_API_KEY is missing"
    assert result.requests_used == 0


def test_authorization_schema_and_template_are_present_and_safe():
    schema = json.loads(
        (
            _ROOT / "docs/top5_therundown_ll_capture_authorization.schema.json"
        ).read_text()
    )
    template = json.loads(
        (
            _ROOT / "docs/top5_therundown_ll_capture_authorization.template.json"
        ).read_text()
    )

    assert set(schema["required"]) == set(template)
    assert schema["properties"]["maximum_request_count"]["const"] == 2
    assert schema["properties"]["maximum_datapoint_budget"]["maximum"] == 100
    assert template["no_bet"] is True
    assert template["no_publication"] is True
    assert template["no_activation"] is True
    assert template["no_spend"] is True


@pytest.mark.parametrize(
    "mutator",
    [
        lambda auth: {**auth.__dict__, "league": "EPL"},
        lambda auth: {
            **auth.__dict__,
            "expires_at": _NOW - timedelta(seconds=1),
        },
        lambda auth: {**auth.__dict__, "no_bet": False},
    ],
)
def test_wrong_or_unsafe_authorization_fails_before_network(mutator):
    result, calls = _run(authorization=LaLigaCaptureAuthorization(**mutator(_AUTH)))

    assert result.status is LaLigaCaptureStatus.REJECTED
    assert calls == []


def test_upcoming_date_selection_and_full_bookmaker_capture():
    result, calls = _run()

    assert result.status is LaLigaCaptureStatus.CAPTURED
    assert len(calls) == 2
    assert result.selected_date == "2026-09-22"
    assert result.provider_event_id == "ll-event-001"
    assert result.provider_request_id == "therundown-ll:ll-run-001:2026-09-22"
    assert len(result.observations) == 2
    assert result.observations[0].candidate_only is True
    assert result.observations[0].metadata["participant_ids"] == {
        "away": "101",
        "draw": "0",
        "home": "202",
    }
    assert result.raw_response_digest
    assert len(result.normalized_record_digests) == 2
    assert result.quota_before.used == 100
    assert result.quota_after.used == 112
    assert result.quota_evidence["x-tier"] == "free"
    assert result.quota_evidence["discovery"]["provider_emitted_cost"] == 1
    assert result.quota_evidence["discovery"]["cost_inference"] == "provider_header"
    assert result.adapter_version == THERUNDOWN_ADAPTER_VERSION
    assert len(result.adapter_source_sha) == 64
    assert result.b1_bridge_fields["ceo_authorization_id"] == "ceo-ll-001"
    assert result.b1_bridge_fields["no_bet"] is True


def test_evidence_bundle_is_deterministic_and_b1_ready_without_fabricated_authority():
    result, _ = _run()

    bundle = result.as_evidence_bundle()
    assert bundle["schema_version"] == "top5-therundown-ll-evidence-bundle-v1"
    assert bundle["capture_status"] == "CAPTURED"
    assert len(bundle["bookmaker_observations"]) == 2
    assert [item["bookmaker_name"] for item in bundle["bookmaker_observations"]] == [
        "affiliate:19",
        "affiliate:3",
    ]
    bridge = bundle["b1_bridge_inputs"]
    assert bridge["evidence_kind"] == "REAL_OBSERVED"
    assert bridge["market_type"] == "football:pre_match:1x2"
    assert bridge["provider_event_id"] == "ll-event-001"
    assert bridge["cascade_evidence_digest"] is None
    assert bridge["cascade_evidence_required_from_b4"] is True
    assert bundle["safety"]["candidate_only"] is True
    assert bundle["safety"]["no_activation"] is True


def test_discovery_without_billed_cost_uses_explicit_zero_cost_inference():
    result, calls = _run(
        date_cost=None,
        date_used=100,
        date_remaining=19900,
    )

    assert result.status is LaLigaCaptureStatus.CAPTURED
    assert len(calls) == 2
    assert result.datapoints_consumed == 11
    discovery = result.quota_evidence["discovery"]
    assert discovery["provider_emitted_cost"] is None
    assert discovery["cost_inference"] == "inferred_unchanged_quota_counters"
    assert "x-datapoints" not in discovery["quota_evidence"]


def test_discovery_without_billed_cost_rejects_changed_quota_counters():
    result, calls = _run(
        date_cost=None,
        date_used=101,
        date_remaining=19899,
    )

    assert result.status is LaLigaCaptureStatus.REJECTED
    assert result.reason == "discovery quota counters changed without billed cost"
    assert len(calls) == 1


def test_discovery_without_billed_cost_requires_both_quota_counters():
    result, calls = _run(
        date_cost=None,
        date_used=100,
        date_remaining=19900,
        date_omit_headers={"x-datapoints-remaining"},
    )

    assert result.status is LaLigaCaptureStatus.REJECTED
    assert (
        result.reason
        == "discovery quota counters are required when billed cost is absent"
    )
    assert len(calls) == 1


def test_discovery_without_billed_cost_rejects_contradictory_quota_evidence():
    result, calls = _run(
        date_cost=None,
        date_used=100,
        date_remaining=20000,
    )

    assert result.status is LaLigaCaptureStatus.REJECTED
    assert result.reason == "discovery quota evidence is contradictory"
    assert len(calls) == 1


def test_event_odds_without_billed_cost_rejects_and_preserves_raw_quota_evidence():
    result, calls = _run(event_cost=None)

    assert result.status is LaLigaCaptureStatus.REJECTED
    assert result.reason == "event/odds billed-cost provenance is missing"
    assert len(calls) == 2
    assert "x-datapoints" not in result.quota_evidence["event"]
    assert result.quota_evidence["event"]["x-datapoints-used"] == 112
    assert result.datapoints_consumed is None


def test_no_upcoming_fixture_stops_after_date_request():
    result, calls = _run(dates=["2026-09-17"])

    assert result.status is LaLigaCaptureStatus.REJECTED
    assert result.reason == "no_upcoming_fixture_date"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "event_status", ["STATUS_IN_PROGRESS", "STATUS_CLOSED", "STATUS_COMPLETED"]
)
def test_live_completed_and_closed_state_stops_before_observation_is_returned(
    event_status: str,
):
    event = _event()
    event["score"]["event_status"] = event_status

    result, calls = _run(event=event)

    assert result.status is LaLigaCaptureStatus.REJECTED
    assert "no_upcoming_prematch_fixture" in result.reason
    assert len(calls) == 2
    assert result.observations == ()


def test_datapoint_overrun_stops_before_event_request():
    result, calls = _run(date_cost=101, budget=100)

    assert result.status is LaLigaCaptureStatus.REJECTED
    assert result.reason == "datapoint_budget_exceeded_before_event_request"
    assert len(calls) == 1


def test_datapoint_overrun_after_event_is_rejected_without_evidence():
    result, calls = _run(event_cost=100, budget=100)

    assert result.status is LaLigaCaptureStatus.REJECTED
    assert result.reason == "datapoint budget exceeded"
    assert result.requests_used == 2
    assert result.datapoints_consumed == 101
    assert result.observations == ()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "case", ["incomplete", "participant_mismatch", "missing_provenance"]
)
def test_market_identity_and_provenance_fail_closed(case: str):
    event = _event()
    if case == "incomplete":
        for participant in event["markets"][0]["participants"]:
            participant["lines"][0]["prices"] = {}
    elif case == "participant_mismatch":
        event["markets"][0]["participants"][0]["name"] = "Wrong Club"
    else:
        del event["markets"][0]["participants"][0]["lines"][0]["prices"]["3"][
            "updated_at"
        ]

    result, calls = _run(event=event)

    assert result.status is LaLigaCaptureStatus.REJECTED
    assert len(calls) == 2
    assert result.observations == ()


def test_capture_config_cannot_become_provider_authority():
    result, _ = _run()

    assert result.status is LaLigaCaptureStatus.CAPTURED
    assert all(item.candidate_only for item in result.observations)
    assert all(item.metadata["experimental"] is True for item in result.observations)
