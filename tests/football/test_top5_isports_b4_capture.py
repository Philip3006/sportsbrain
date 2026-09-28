from __future__ import annotations

import inspect
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.football.odds.isports import (
    ISPORTS_ENDPOINTS,
    ISPORTS_PROVIDER_IDENTITY,
    ISPORTS_TOP5_LEAGUES,
    ISportsClient,
)
from src.football.production_contracts import Fixture
from src.football.provider_cascade.contracts import (
    CANDIDATE_ONLY_PROVIDER_IDENTITIES,
    DEFAULT_PROVIDER_ORDER,
)
from src.football.top5_b4_provider_neutral_evidence import (
    AUTHORIZATION_PROVENANCE_SCHEMA_VERSION,
    TOP5_LEAGUE_ORDER,
    ProviderNeutralB4EvidenceError,
    Top5B4ProviderNeutralEvidenceDossierV1,
    canonical_evidence_digest,
)
from src.football.top5_isports_b4_capture import (
    MAX_RUN_REQUESTS,
    Top5ISportsB4CaptureError,
    main,
)
from src.football.top5_isports_b4_capture import (
    _run_one_shot as _run_one_shot_internal,
)
from src.football.top5_isports_b4_capture import (
    run_one_shot as production_run_one_shot,
)
from src.football.top5_signal_lifecycle import (
    DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
    LifecyclePlanStatus,
    SignalLifecycleStage,
    plan_signal_lifecycle,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
SOURCE_SHA = "1" * 40
ADAPTER_SHA = "2" * 40
TEST_KEY = "injected-test-credential-never-serialize"
LEAGUES = {
    "BL1": ("102", "Bundesliga", "GER D1"),
    "EPL": ("101", "Premier League", "ENG PR"),
    "LL": ("103", "La Liga", "SPA D1"),
    "SA": ("104", "Serie A", "ITA D1"),
    "L1": ("105", "Ligue 1", "FRA D1"),
    "UCL": ("106", "UEFA Champions League", "UEFA CL"),
}


def run_one_shot(
    *,
    authorization_payload,
    output_path,
    credential_loader,
    now,
    marker_directory,
    client_factory=lambda _key: None,
    execute_network=False,
    source_main_sha=SOURCE_SHA,
    adapter_source_sha=ADAPTER_SHA,
    repository_root=None,
):
    """Offline-only adapter to the module-private injectable test seam."""

    return _run_one_shot_internal(
        authorization_payload=authorization_payload,
        output_path=output_path,
        execute_network=execute_network,
        source_main_sha=source_main_sha,
        adapter_source_sha=adapter_source_sha,
        repository_root=repository_root,
        credential_loader=credential_loader,
        client_factory=client_factory,
        now=now,
        marker_directory=marker_directory,
    )


def _authorization(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": AUTHORIZATION_PROVENANCE_SCHEMA_VERSION,
        "provider_identity": ISPORTS_PROVIDER_IDENTITY,
        "controlled_shadow_run_id": "isports-top5-run-20260928-a",
        "qualification_session_id": "isports-top5-session-20260928-a",
        "authorization_id": "ceo-isports-top5-auth-20260928-a",
        "league_scope": list(TOP5_LEAGUE_ORDER),
        "no_bet": True,
        "publication": False,
        "production_activation": False,
        "betting": False,
        "monetary_spend_authorized": False,
    }
    body.update(overrides)
    return {**body, "authorization_digest": canonical_evidence_digest(body)}


def _fixture_row(
    league: str,
    provider_match_id: str,
    kickoff: datetime,
    *,
    alternate_participants: bool = False,
) -> dict[str, object]:
    provider_id, name, _short = LEAGUES[league]
    return {
        "leagueId": provider_id,
        "leagueName": name,
        "matchId": provider_match_id,
        "matchTime": int(kickoff.timestamp()),
        "status": 0,
        "homeId": f"{league}-home-id",
        "homeName": (
            f"Z Home {league}" if alternate_participants else f"Home {league}"
        ),
        "awayId": f"{league}-away-id",
        "awayName": (
            f"Z Away {league}" if alternate_participants else f"Away {league}"
        ),
        "neutral": False,
    }


class ReplayTransport:
    def __init__(
        self,
        *,
        missing_fixture: str | None = None,
        missing_initial_fixture: str | None = None,
        duplicate_selected_ids: bool = False,
        missing_market: str | None = None,
        malformed_market: str | None = None,
        stale_market: str | None = None,
        identity_mismatch: str | None = None,
        duplicate_bookmaker: str | None = None,
        market_age_seconds: int = 60,
        initial_lead_seconds: int = 24 * 60 * 60,
        bulk_completion_delay_seconds: float = 0.0,
        response_headers: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.keys: list[str] = []
        self.missing_fixture = missing_fixture
        self.missing_initial_fixture = missing_initial_fixture
        self.duplicate_selected_ids = duplicate_selected_ids
        self.missing_market = missing_market
        self.malformed_market = malformed_market
        self.stale_market = stale_market
        self.identity_mismatch = identity_mismatch
        self.duplicate_bookmaker = duplicate_bookmaker
        self.market_age_seconds = market_age_seconds
        self.initial_lead_seconds = initial_lead_seconds
        self.bulk_completion_delay_seconds = bulk_completion_delay_seconds
        self.response_headers = response_headers or {}
        self.fixture_rows: dict[str, dict[str, object]] = {}

    def __call__(self, endpoint, params, api_key, timeout):
        ordinal = len(self.calls) + 1
        safe_params = dict(params)
        self.calls.append((endpoint, safe_params))
        self.keys.append(api_key)
        start = NOW + timedelta(seconds=2 * ordinal)
        completed = start + timedelta(milliseconds=500)
        if endpoint == ISPORTS_ENDPOINTS["european_odds"]:
            completed += timedelta(seconds=self.bulk_completion_delay_seconds)
        if endpoint == ISPORTS_ENDPOINTS["catalog"]:
            payload = {
                "code": 200,
                "data": [
                    {
                        "leagueId": item[0],
                        "name": item[1],
                        "shortName": item[2],
                        "type": 2 if code == "UCL" else 1,
                    }
                    for code, item in LEAGUES.items()
                ],
            }
        elif endpoint == ISPORTS_ENDPOINTS["schedule"]:
            league = next(
                code for code, item in LEAGUES.items() if item[0] == params["leagueId"]
            )
            if league == self.missing_fixture:
                payload = {"code": 200, "data": []}
            else:
                candidate_lead_seconds = (
                    30 * 60 * 60
                    if league == self.missing_initial_fixture
                    else self.initial_lead_seconds
                )
                selected_id = (
                    "BL1-shared-id"
                    if self.duplicate_selected_ids and league == "BL1"
                    else f"{league}-a-match"
                )
                if self.duplicate_selected_ids and league == "EPL":
                    selected_id = "BL1-shared-id"
                generic_future = _fixture_row(
                    league,
                    f"{league}-early-generic-match",
                    NOW + timedelta(hours=3),
                )
                lexical_later = _fixture_row(
                    league,
                    f"{league}-z-match",
                    NOW + timedelta(seconds=candidate_lead_seconds),
                    alternate_participants=True,
                )
                selected = _fixture_row(
                    league,
                    selected_id,
                    NOW + timedelta(seconds=candidate_lead_seconds),
                )
                for row in (generic_future, lexical_later, selected):
                    self.fixture_rows[str(row["matchId"])] = row
                payload = {
                    "code": 200,
                    "data": [generic_future, lexical_later, selected],
                }
        elif endpoint == ISPORTS_ENDPOINTS["european_odds"]:
            target_ids = params["matchId"].split(",")
            reverse_league = {item[0]: code for code, item in LEAGUES.items()}
            rows = []
            for league in ISPORTS_TOP5_LEAGUES:
                match_id = target_ids[ISPORTS_TOP5_LEAGUES.index(league)]
                if league == self.missing_market:
                    continue
                fixture = self.fixture_rows[match_id]
                provider_id, league_name, _short = LEAGUES[league]
                row = {
                    "matchId": match_id,
                    "leagueId": provider_id,
                    "leagueName": league_name,
                    "homeName": fixture["homeName"],
                    "awayName": fixture["awayName"],
                    "matchTime": fixture["matchTime"],
                    "odds": [
                        {
                            "changeTime": int(
                                (
                                    NOW
                                    - timedelta(
                                        seconds=(
                                            301
                                            if league == self.stale_market
                                            else self.market_age_seconds
                                        )
                                    )
                                ).timestamp()
                            ),
                            "oddsDetail": [
                                {
                                    "companyId": "81",
                                    "companyName": "Replay Bookmaker",
                                    "changeTime": int(
                                        (
                                            NOW
                                            - timedelta(
                                                seconds=(
                                                    301
                                                    if league == self.stale_market
                                                    else self.market_age_seconds
                                                )
                                            )
                                        ).timestamp()
                                    ),
                                    "initialHome": "1.00",
                                    "initialDraw": "2.10",
                                    "initialAway": "2.60",
                                    "instantHome": "1.10",
                                    "instantDraw": "2.20",
                                    "instantAway": "2.70",
                                }
                            ],
                        }
                    ],
                }
                if league == self.identity_mismatch:
                    row["homeName"] = "Wrong Home"
                if league == self.malformed_market:
                    row["odds"][0]["oddsDetail"] = ["malformed"]
                if league == self.duplicate_bookmaker:
                    row["odds"][0]["oddsDetail"].append(
                        dict(row["odds"][0]["oddsDetail"][0])
                    )
                rows.append(row)
            # Preserve all five schedule identities in the replay and make the
            # specific provider ID the only odds-target binding.
            del reverse_league
            payload = {"code": 200, "data": rows}
        else:
            raise AssertionError(f"unexpected iSports endpoint {endpoint}")
        return 200, payload, self.response_headers, start, completed, None


def _client(transport: ReplayTransport) -> ISportsClient:
    return ISportsClient(
        api_key=TEST_KEY,
        transport=transport,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
        enforce_pacing=False,
    )


def _execute(
    tmp_path: Path,
    *,
    authorization: dict[str, object] | None = None,
    transport: ReplayTransport | None = None,
):
    transport = transport or ReplayTransport()
    loads: list[str] = []
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls <= 2:
            return NOW
        if clock_calls == 3:
            return NOW + timedelta(seconds=13)
        return NOW + timedelta(seconds=15)

    def load_key() -> str:
        loads.append("read")
        return TEST_KEY

    result = run_one_shot(
        authorization_payload=authorization or _authorization(),
        output_path=tmp_path / "top5-b4-dossier.json",
        execute_network=True,
        credential_loader=load_key,
        client_factory=lambda key: _client(transport),
        now=clock,
        source_main_sha=SOURCE_SHA,
        adapter_source_sha=ADAPTER_SHA,
        marker_directory=tmp_path / "markers",
    )
    return result, transport, loads


def test_exact_top5_path_builds_dossier_and_b1_ten_key_handoff(tmp_path):
    result, transport, loads = _execute(tmp_path)
    assert result.status == "COMPLETED"
    assert result.request_count == MAX_RUN_REQUESTS == 7
    assert result.credential_access_count == 1
    assert loads == ["read"]
    assert len(transport.calls) == 7
    assert transport.calls[0][0] == ISPORTS_ENDPOINTS["catalog"]
    assert [call[1]["leagueId"] for call in transport.calls[1:6]] == [
        LEAGUES[league][0] for league in ISPORTS_TOP5_LEAGUES
    ]
    assert [call[0] for call in transport.calls[1:6]] == [
        ISPORTS_ENDPOINTS["schedule"]
    ] * 5
    assert transport.calls[6][0] == ISPORTS_ENDPOINTS["european_odds"]
    expected_ids = [f"{league}-a-match" for league in ISPORTS_TOP5_LEAGUES]
    assert len(set(expected_ids)) == 5
    assert transport.calls[6][1] == {"matchId": ",".join(expected_ids)}
    assert all(
        endpoint != ISPORTS_ENDPOINTS["main_odds"]
        for endpoint, _params in transport.calls
    )
    assert all(
        params.get("leagueId") != LEAGUES["UCL"][0]
        for endpoint, params in transport.calls
        if endpoint == ISPORTS_ENDPOINTS["schedule"]
    )
    assert all(key == TEST_KEY for key in transport.keys)

    payload = json.loads(result.output_path.read_text())
    dossier = Top5B4ProviderNeutralEvidenceDossierV1.from_payload(
        payload, now=NOW + timedelta(seconds=100)
    )
    assert dossier.controlled_shadow.provider_identity == "isports_api"
    assert (
        tuple(item.league for item in dossier.controlled_shadow.discovery_evidence)
        == TOP5_LEAGUE_ORDER
    )
    assert (
        tuple(item.league for item in dossier.controlled_shadow.market_evidence)
        == TOP5_LEAGUE_ORDER
    )
    assert (
        tuple(item.league for item in dossier.controlled_shadow.captures)
        == TOP5_LEAGUE_ORDER
    )
    first_market = dossier.controlled_shadow.market_evidence[0]
    assert first_market.league == "BL1"
    assert first_market.provider_identity == "isports_api"
    assert first_market.source_identity == "isports_api:european"
    assert first_market.source_provenance == "isports_api:european:prematch_1x2"
    assert first_market.market_type == "PRE_MATCH_1X2_REGULATION"
    assert all(
        price > 1.0
        for price in (
            first_market.home_odds,
            first_market.draw_odds,
            first_market.away_odds,
        )
    )
    assert first_market.odds_timestamp <= first_market.captured_at
    assert len(first_market.raw_response_digest) == 64
    assert len(first_market.provider_record_digest) == 64
    assert len(first_market.normalized_observation_digest) == 64
    assert dossier.controlled_shadow.request_count == 7
    assert dossier.controlled_shadow.readiness.retry_count == 0
    assert dossier.controlled_shadow.readiness.usage.quota_status == "not_exposed"
    assert [item.operation_kind for item in dossier.controlled_shadow.operations] == [
        "competition_catalog",
        "competition_schedule",
        "competition_schedule",
        "competition_schedule",
        "competition_schedule",
        "competition_schedule",
        "bulk_odds",
    ]
    assert result.b1_logical_input_keys == (
        "source_main_sha",
        "b4_quota_proof_package",
        "b4_quota_headroom",
        "discovery_evidence",
        "provider_native_discovery_provenance",
        "controlled_shadow",
        "b4_reconciliation",
        "b4_qualification",
        "b4_native_authorization",
        "b4_dossier_digest",
    )
    handoff = dossier.b1_evidence_inputs(now=NOW + timedelta(seconds=100))
    assert tuple(handoff) == result.b1_logical_input_keys
    assert result.output_path.stat().st_mode & 0o777 == 0o400
    assert result.marker_path.stat().st_mode & 0o777 == 0o400
    assert TEST_KEY.encode() not in result.output_path.read_bytes()
    assert CANDIDATE_ONLY_PROVIDER_IDENTITIES >= {"isports_api"}
    assert "isports_api" not in DEFAULT_PROVIDER_ORDER
    assert handoff["controlled_shadow"]["publication"] is False
    assert handoff["controlled_shadow"]["production_activation"] is False
    assert handoff["controlled_shadow"]["betting"] is False
    assert handoff["controlled_shadow"]["ledger_mutated"] is False


def test_production_entrypoint_does_not_allow_caller_injected_identity_or_transport():
    parameters = inspect.signature(production_run_one_shot).parameters
    assert "source_main_sha" not in parameters
    assert "adapter_source_sha" not in parameters
    assert "credential_loader" not in parameters
    assert "client_factory" not in parameters
    assert "marker_directory" not in parameters
    assert "repository_root" not in parameters


def test_selection_ignores_earlier_outside_window_then_uses_kickoff_and_match_id(
    tmp_path,
):
    result, _transport, _loads = _execute(tmp_path)
    payload = json.loads(result.output_path.read_text())
    selected = payload["controlled_shadow"]["discovery_evidence"]
    assert [item["provider_fixture_id"] for item in selected] == [
        f"{league}-a-match" for league in TOP5_LEAGUE_ORDER
    ]
    selection_time = NOW + timedelta(seconds=13)
    market_reference_time = NOW + timedelta(seconds=14.5)
    for item in selected:
        kickoff = datetime.fromisoformat(item["kickoff"].replace("Z", "+00:00"))
        fixture = Fixture(
            fixture_key=item["fixture_key"],
            league_code=item["league"],
            home_team=item["home_team"],
            away_team=item["away_team"],
            kickoff=kickoff,
        )
        for reference_time in (selection_time, market_reference_time):
            plan = plan_signal_lifecycle(
                fixture,
                reference_time,
                lifecycle=None,
                contract=DEFAULT_SIGNAL_LIFECYCLE_CONTRACT,
            )
            assert plan.status is LifecyclePlanStatus.INITIAL_DUE
            assert plan.due_stage is SignalLifecycleStage.INITIAL


def test_default_mode_is_preflight_only_and_does_not_read_key_or_consume_marker(
    tmp_path,
):
    loads: list[bool] = []
    authorization = _authorization()
    result = run_one_shot(
        authorization_payload=authorization,
        output_path=tmp_path / "preflight.json",
        execute_network=False,
        credential_loader=lambda: loads.append(True) or TEST_KEY,
        now=lambda: NOW,
        source_main_sha=SOURCE_SHA,
        adapter_source_sha=ADAPTER_SHA,
        marker_directory=tmp_path / "markers",
    )
    assert result.status == "PREFLIGHT_READY"
    assert result.request_count == 0
    assert result.credential_access_count == 0
    assert loads == []
    assert not result.marker_path.exists()
    assert not result.output_path.exists()


def test_bad_authorization_fails_before_credential_or_transport(tmp_path):
    authorization = _authorization(provider_identity="the_odds_api")
    loads: list[bool] = []
    transport = ReplayTransport()
    with pytest.raises(Top5ISportsB4CaptureError, match="authorization_provider"):
        run_one_shot(
            authorization_payload=authorization,
            output_path=tmp_path / "bad-auth.json",
            execute_network=True,
            credential_loader=lambda: loads.append(True) or TEST_KEY,
            client_factory=lambda key: _client(transport),
            now=lambda: NOW,
            source_main_sha=SOURCE_SHA,
            adapter_source_sha=ADAPTER_SHA,
            marker_directory=tmp_path / "markers",
        )
    assert loads == []
    assert transport.calls == []


def test_replay_fails_before_second_credential_access_or_provider_request(tmp_path):
    result, transport, loads = _execute(tmp_path)
    before = len(transport.calls)
    alternate_output = tmp_path / "different-private-directory"
    alternate_output.mkdir(mode=0o700)
    with pytest.raises(Top5ISportsB4CaptureError, match="already_consumed"):
        run_one_shot(
            authorization_payload=_authorization(),
            output_path=alternate_output / "another-output.json",
            execute_network=True,
            credential_loader=lambda: loads.append("second") or TEST_KEY,
            client_factory=lambda key: _client(transport),
            now=lambda: NOW + timedelta(seconds=101),
            source_main_sha=SOURCE_SHA,
            adapter_source_sha=ADAPTER_SHA,
            marker_directory=tmp_path / "markers",
        )
    assert len(transport.calls) == before == 7
    assert loads == ["read"]
    assert result.marker_path.exists()


def test_missing_initial_window_fixture_stops_after_five_schedules_before_bulk(
    tmp_path,
):
    transport = ReplayTransport(missing_fixture="L1")
    with pytest.raises(Top5ISportsB4CaptureError) as error:
        _execute(tmp_path, transport=transport)
    assert error.value.code == "initial_window_fixture_missing"
    assert error.value.missing_leagues == ("L1",)
    assert error.value.request_count == 6
    assert len(transport.calls) == 6
    assert all(
        path != ISPORTS_ENDPOINTS["european_odds"] for path, _ in transport.calls
    )


def test_initial_window_missing_for_one_league_stops_after_six_calls(tmp_path):
    transport = ReplayTransport(missing_initial_fixture="LL")
    with pytest.raises(Top5ISportsB4CaptureError) as error:
        _execute(tmp_path, transport=transport)
    assert error.value.code == "initial_window_fixture_missing"
    assert error.value.missing_leagues == ("LL",)
    assert error.value.request_count == 6
    assert len(transport.calls) == 6
    assert [path for path, _params in transport.calls].count(
        ISPORTS_ENDPOINTS["european_odds"]
    ) == 0


def test_duplicate_selected_native_ids_stop_before_bulk(tmp_path):
    transport = ReplayTransport(duplicate_selected_ids=True)
    with pytest.raises(
        Top5ISportsB4CaptureError, match="duplicate_selected_fixture"
    ) as error:
        _execute(tmp_path, transport=transport)
    assert error.value.request_count == 6
    assert len(transport.calls) == 6


def test_post_bulk_timing_drift_fails_closed_without_reselection(tmp_path):
    # At the shared selection sample (+13s), kickoff is exactly 22h away.
    # The bulk response completes at +14.5s, outside INITIAL; B4 must stop.
    transport = ReplayTransport(initial_lead_seconds=22 * 60 * 60 + 13)
    with pytest.raises(Top5ISportsB4CaptureError) as error:
        _execute(tmp_path, transport=transport)
    assert error.value.code == "initial_window_expired_after_bulk"
    assert error.value.missing_leagues == TOP5_LEAGUE_ORDER
    assert error.value.request_count == 7
    assert len(transport.calls) == 7
    assert transport.calls[-1][0] == ISPORTS_ENDPOINTS["european_odds"]
    assert not (tmp_path / "top5-b4-dossier.json").exists()


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"missing_market": "LL"}, "european_market_missing"),
        ({"malformed_market": "LL"}, "european_odds_failed"),
        ({"identity_mismatch": "LL"}, "european_odds_failed"),
        ({"stale_market": "LL"}, "european_market_normalization_failed"),
        ({"duplicate_bookmaker": "LL"}, "european_odds_failed"),
    ],
)
def test_invalid_or_incomplete_european_bulk_fails_without_dossier(
    tmp_path, kwargs, code
):
    transport = ReplayTransport(**kwargs)
    with pytest.raises(Top5ISportsB4CaptureError) as error:
        _execute(tmp_path, transport=transport)
    assert error.value.code == code
    assert error.value.request_count == 7
    assert len(transport.calls) == 7
    assert not (tmp_path / "top5-b4-dossier.json").exists()


def test_market_freshness_accepts_exact_300_second_b4_boundary(tmp_path):
    result, _transport, _loads = _execute(
        tmp_path, transport=ReplayTransport(market_age_seconds=285)
    )
    assert result.status == "COMPLETED"


def test_market_older_than_300_seconds_fails_closed(tmp_path):
    transport = ReplayTransport(market_age_seconds=286)
    with pytest.raises(Top5ISportsB4CaptureError) as error:
        _execute(tmp_path, transport=transport)
    assert error.value.code == "european_market_normalization_failed"
    assert error.value.missing_leagues == ("BL1",)
    assert not (tmp_path / "top5-b4-dossier.json").exists()


def test_operation_usage_records_observed_headers_without_inventing_quota(tmp_path):
    transport = ReplayTransport(
        response_headers={"x-rate-limit": "60", "x-rate-limit-remaining": "53"}
    )
    result, _transport, _loads = _execute(tmp_path, transport=transport)
    payload = json.loads(result.output_path.read_text())
    operations = payload["controlled_shadow"]["operations"]
    assert all(item["usage"]["quota_status"] == "not_exposed" for item in operations)
    assert all(item["usage"]["rate_status"] == "available" for item in operations)
    assert all(item["usage"]["rate_limit_requests"] == 60 for item in operations)
    assert (
        payload["controlled_shadow"]["readiness"]["usage"]["quota_status"]
        == "not_exposed"
    )


def test_digest_tampering_and_provider_substitution_are_rejected(tmp_path):
    result, _transport, _loads = _execute(tmp_path)
    payload = json.loads(result.output_path.read_text())
    bad_sha = json.loads(json.dumps(payload))
    bad_sha["source_main_sha"] = "f" * 40
    with pytest.raises(ProviderNeutralB4EvidenceError):
        Top5B4ProviderNeutralEvidenceDossierV1.from_payload(
            bad_sha, now=NOW + timedelta(seconds=100)
        )
    bad_adapter_sha = json.loads(json.dumps(payload))
    bad_adapter_sha["controlled_shadow"]["adapter_source_sha"] = "f" * 40
    with pytest.raises(ProviderNeutralB4EvidenceError):
        Top5B4ProviderNeutralEvidenceDossierV1.from_payload(
            bad_adapter_sha, now=NOW + timedelta(seconds=100)
        )
    bad_operation = json.loads(json.dumps(payload))
    bad_operation["controlled_shadow"]["operations"][0]["response_digest"] = "f" * 64
    with pytest.raises(ProviderNeutralB4EvidenceError):
        Top5B4ProviderNeutralEvidenceDossierV1.from_payload(
            bad_operation, now=NOW + timedelta(seconds=100)
        )
    bad_provider = json.loads(json.dumps(payload))
    bad_provider["provider_identity"] = "the_odds_api"
    with pytest.raises(ProviderNeutralB4EvidenceError):
        Top5B4ProviderNeutralEvidenceDossierV1.from_payload(
            bad_provider, now=NOW + timedelta(seconds=100)
        )


def test_duplicate_request_or_retry_can_never_be_reported_as_success(tmp_path):
    result, transport, _loads = _execute(tmp_path)
    payload = json.loads(result.output_path.read_text())
    assert len(transport.calls) == MAX_RUN_REQUESTS
    assert payload["controlled_shadow"]["request_count"] == 7
    assert payload["controlled_shadow"]["retry_count"] == 0
    assert (
        payload["controlled_shadow"]["readiness"]["authorized_run_request_count"] == 7
    )
    assert all(
        item["request_count"] == 1
        for item in payload["controlled_shadow"]["operations"]
    )
    assert all(
        item["retry_count"] == 0 for item in payload["controlled_shadow"]["operations"]
    )
    assert all(
        item["operation_kind"] != "bulk_odds"
        or item["endpoint_path"] == ISPORTS_ENDPOINTS["european_odds"]
        for item in payload["controlled_shadow"]["operations"]
    )


def test_output_parent_must_be_private_and_credentials_are_not_read_on_failure(
    tmp_path,
):
    public_dir = tmp_path / "public"
    public_dir.mkdir(mode=0o755)
    public_dir.chmod(0o755)
    reads: list[bool] = []
    with pytest.raises(Top5ISportsB4CaptureError, match="permissions_invalid"):
        run_one_shot(
            authorization_payload=_authorization(),
            output_path=public_dir / "unsafe.json",
            execute_network=True,
            credential_loader=lambda: reads.append(True) or TEST_KEY,
            now=lambda: NOW,
            source_main_sha=SOURCE_SHA,
            adapter_source_sha=ADAPTER_SHA,
            marker_directory=tmp_path / "markers",
        )
    assert reads == []


def test_output_parent_symlink_is_rejected_before_credential_access(tmp_path):
    private_dir = tmp_path / "private-target"
    private_dir.mkdir(mode=0o700)
    alias = tmp_path / "private-alias"
    alias.symlink_to(private_dir, target_is_directory=True)
    reads: list[bool] = []
    with pytest.raises(Top5ISportsB4CaptureError, match="symlink"):
        run_one_shot(
            authorization_payload=_authorization(),
            output_path=alias / "dossier.json",
            execute_network=True,
            credential_loader=lambda: reads.append(True) or TEST_KEY,
            now=lambda: NOW,
            source_main_sha=SOURCE_SHA,
            adapter_source_sha=ADAPTER_SHA,
            marker_directory=tmp_path / "markers",
        )
    assert reads == []


def test_operator_cli_defaults_to_offline_preflight(monkeypatch, tmp_path, capsys):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    authorization_path = private / "authorization.json"
    fd = os.open(authorization_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(_authorization(), handle)
    monkeypatch.setattr(
        "src.football.top5_isports_b4_capture._checkout_identity",
        lambda _root: (SOURCE_SHA, ADAPTER_SHA),
    )
    monkeypatch.delenv("ISPORTS_API_KEY", raising=False)
    output = private / "cli-dossier.json"
    status = main(
        [
            "--authorization-file",
            str(authorization_path),
            "--output",
            str(output),
        ]
    )
    record = json.loads(capsys.readouterr().out)
    assert status == 0
    assert record["status"] == "PREFLIGHT_READY"
    assert record["request_count"] == 0
    assert record["credential_access_count"] == 0
    assert record["main_odds_requested"] is False
    assert record["ucl_schedule_requested"] is False
    assert not output.exists()
