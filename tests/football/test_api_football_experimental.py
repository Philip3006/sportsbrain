from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from src.football.experimental.api_football import (
    API_FOOTBALL_COMPETITION_ID,
    API_FOOTBALL_COMPETITION_NAME,
    API_FOOTBALL_MARKET_ID,
    ApiFootballDiagnosticBudget,
    ApiFootballEvidenceKind,
    ApiFootballExperimentalAdapter,
    ApiFootballExperimentalError,
    ApiFootballExperimentPolicy,
    ApiFootballFailure,
    ApiFootballRequest,
    ApiFootballRequestKind,
    ApiFootballResponse,
    summarize_odds_payload,
)
from src.football.provider_cascade.contracts import (
    FOOTBALL_PROVIDER_REPERTOIRE,
    ProviderState,
    TimingProvenance,
)

UTC = timezone.utc
CAPTURE = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
SOURCE = CAPTURE - timedelta(seconds=120)
KICKOFF = CAPTURE + timedelta(hours=2)


def _fixture_entry(
    *,
    fixture_id: int = 123456,
    competition_id: int = API_FOOTBALL_COMPETITION_ID,
    competition_name: str = API_FOOTBALL_COMPETITION_NAME,
    home: str = "Paris Saint-Germain",
    away: str = "Bayern Munich",
    kickoff: datetime = KICKOFF,
) -> dict[str, object]:
    return {
        "fixture": {
            "id": fixture_id,
            "timezone": "UTC",
            "date": kickoff.isoformat(),
            "timestamp": int(kickoff.timestamp()),
        },
        "league": {
            "id": competition_id,
            "name": competition_name,
            "season": 2026,
        },
        "teams": {
            "home": {"id": 85, "name": home, "winner": None},
            "away": {"id": 157, "name": away, "winner": None},
        },
    }


def _fixtures_payload(**kwargs: object) -> dict[str, object]:
    return {
        "get": "fixtures",
        "parameters": {"league": "2", "season": "2026", "date": "2026-09-17"},
        "errors": {},
        "results": 1,
        "paging": {"current": 1, "total": 1},
        "response": [_fixture_entry(**kwargs)],
    }


def _winner_bet(
    *,
    home: str = "Home",
    away: str = "Away",
    home_odd: object = "2.10",
    draw_odd: object = "3.40",
    away_odd: object = "3.20",
) -> dict[str, object]:
    return {
        "id": API_FOOTBALL_MARKET_ID,
        "name": "Match Winner",
        "values": [
            {"value": home, "odd": home_odd},
            {"value": "Draw", "odd": draw_odd},
            {"value": away, "odd": away_odd},
        ],
    }


def _odds_entry(
    *,
    fixture_id: int = 123456,
    competition_id: int = API_FOOTBALL_COMPETITION_ID,
    competition_name: str = API_FOOTBALL_COMPETITION_NAME,
    home: str = "Paris Saint-Germain",
    away: str = "Bayern Munich",
    kickoff: datetime = KICKOFF,
    source: datetime | None = SOURCE,
    bookmakers: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if bookmakers is None:
        bookmakers = [
            {
                "id": 4,
                "name": "Bet365",
                "bets": [_winner_bet(home=home, away=away)],
            },
        ]
    entry = _fixture_entry(
        fixture_id=fixture_id,
        competition_id=competition_id,
        competition_name=competition_name,
        home=home,
        away=away,
        kickoff=kickoff,
    )
    entry["bookmakers"] = bookmakers
    if source is not None:
        entry["update"] = source.isoformat()
    return entry


def _odds_payload(**kwargs: object) -> dict[str, object]:
    return {
        "get": "odds",
        "parameters": {"fixture": "123456", "bet": "1", "page": "1"},
        "errors": {},
        "results": 1,
        "paging": {"current": 1, "total": 1},
        "response": [_odds_entry(**kwargs)],
    }


def _candidate(adapter: ApiFootballExperimentalAdapter):
    candidates = adapter.normalize_injected_fixtures(
        _fixtures_payload(), capture_timestamp=CAPTURE
    )
    assert len(candidates) == 1
    return candidates[0]


def _response(
    payload: object,
    *,
    status: int = 200,
    error_code: str | None = None,
    started: datetime = CAPTURE - timedelta(seconds=2),
    completed: datetime = CAPTURE,
    headers: dict[str, str] | None = None,
) -> ApiFootballResponse:
    return ApiFootballResponse(
        status_code=status,
        payload=payload,
        headers=headers or {"x-ratelimit-requests-remaining": "99"},
        started_at=started,
        completed_at=completed,
        latency_ms=2000,
        error_code=error_code,
    )


def test_active_football_authority_remains_the_odds_api_only():
    assert FOOTBALL_PROVIDER_REPERTOIRE == ("the_odds_api",)
    assert "api_football" not in FOOTBALL_PROVIDER_REPERTOIRE


def test_champions_league_fixture_mapping_reuses_canonical_fixture_contract():
    adapter = ApiFootballExperimentalAdapter()
    candidate = _candidate(adapter)

    assert candidate.provider_fixture_id == "123456"
    assert candidate.competition_id == API_FOOTBALL_COMPETITION_ID
    assert candidate.fixture.league_code == "UCL"
    assert candidate.fixture.home_team == "Paris Saint-Germain"
    assert candidate.fixture.away_team == "Bayern Munich"
    assert candidate.fixture.kickoff == KICKOFF


def test_valid_1x2_response_and_multiple_bookmakers_are_canonical_and_injected():
    adapter = ApiFootballExperimentalAdapter()
    expected = _candidate(adapter)
    payload = _odds_payload(
        bookmakers=[
            {"id": 4, "name": "Bet365", "bets": [_winner_bet()]},
            {"id": 1, "name": "Bwin", "bets": [_winner_bet()]},
        ]
    )

    observations = adapter.normalize_injected_odds(
        payload, expected, capture_timestamp=CAPTURE
    )

    assert {item.canonical.bookmaker_identity for item in observations} == {
        "Bet365",
        "Bwin",
    }
    assert all(
        item.evidence_kind is ApiFootballEvidenceKind.INJECTED_FIXTURE
        for item in observations
    )
    assert all(item.is_real_network_capture is False for item in observations)
    assert all(item.canonical.candidate_only for item in observations)
    assert all(
        item.canonical.source_timing_provenance is TimingProvenance.SOURCE_TIMESTAMP
        for item in observations
    )
    assert all(item.canonical.home_odds == 2.1 for item in observations)


def test_missing_match_winner_fails_closed():
    adapter = ApiFootballExperimentalAdapter()
    expected = _candidate(adapter)
    payload = _odds_payload(
        bookmakers=[
            {
                "id": 4,
                "name": "Bet365",
                "bets": [{"id": 8, "name": "Both Teams Score", "values": []}],
            }
        ]
    )

    with pytest.raises(
        ApiFootballExperimentalError,
        match=ApiFootballFailure.MISSING_MATCH_WINNER.value,
    ):
        adapter.normalize_injected_odds(payload, expected, capture_timestamp=CAPTURE)


def test_malformed_odds_fail_closed():
    adapter = ApiFootballExperimentalAdapter()
    expected = _candidate(adapter)
    payload = _odds_payload(
        bookmakers=[
            {
                "id": 4,
                "name": "Bet365",
                "bets": [_winner_bet(home_odd="not-a-decimal")],
            }
        ]
    )

    with pytest.raises(
        ApiFootballExperimentalError,
        match=ApiFootballFailure.INVALID_DECIMAL_ODDS.value,
    ):
        adapter.normalize_injected_odds(payload, expected, capture_timestamp=CAPTURE)


def test_stale_odds_are_rejected():
    adapter = ApiFootballExperimentalAdapter(
        policy=ApiFootballExperimentPolicy(maximum_odds_age_seconds=900)
    )
    expected = _candidate(adapter)
    payload = _odds_payload(source=CAPTURE - timedelta(seconds=901))

    with pytest.raises(
        ApiFootballExperimentalError, match=ApiFootballFailure.STALE_DATA.value
    ):
        adapter.normalize_injected_odds(payload, expected, capture_timestamp=CAPTURE)


def test_wrong_competition_fails_closed():
    adapter = ApiFootballExperimentalAdapter()

    with pytest.raises(ApiFootballExperimentalError, match="another competition"):
        adapter.normalize_injected_fixtures(
            _fixtures_payload(competition_id=39, competition_name="Premier League"),
            capture_timestamp=CAPTURE,
        )


def test_fixture_mismatch_fails_closed():
    adapter = ApiFootballExperimentalAdapter()
    expected = _candidate(adapter)

    with pytest.raises(
        ApiFootballExperimentalError, match=ApiFootballFailure.FIXTURE_MISMATCH.value
    ):
        adapter.normalize_injected_odds(
            _odds_payload(fixture_id=999999),
            expected,
            capture_timestamp=CAPTURE,
        )


def test_missing_source_timestamp_is_not_promoted_to_qualified_observation():
    adapter = ApiFootballExperimentalAdapter()
    expected = _candidate(adapter)

    with pytest.raises(
        ApiFootballExperimentalError,
        match=ApiFootballFailure.SOURCE_TIMESTAMP_MISSING.value,
    ):
        adapter.normalize_injected_odds(
            _odds_payload(source=None), expected, capture_timestamp=CAPTURE
        )


def test_capture_only_policy_is_explicitly_non_source_time():
    adapter = ApiFootballExperimentalAdapter(
        policy=ApiFootballExperimentPolicy(require_source_timestamp=False)
    )
    expected = _candidate(adapter)
    observation = adapter.normalize_injected_odds(
        _odds_payload(source=None), expected, capture_timestamp=CAPTURE
    )[0]

    assert observation.canonical.source_timestamp is None
    assert (
        observation.canonical.source_timing_provenance
        is TimingProvenance.CAPTURE_TIME_ONLY
    )
    assert observation.is_real_network_capture is False


def test_duplicate_bookmaker_snapshot_is_suppressed():
    adapter = ApiFootballExperimentalAdapter()
    expected = _candidate(adapter)
    duplicate = {"id": 4, "name": "Bet365", "bets": [_winner_bet()]}

    observations = adapter.normalize_injected_odds(
        _odds_payload(bookmakers=[duplicate, duplicate]),
        expected,
        capture_timestamp=CAPTURE,
    )

    assert len(observations) == 1


@pytest.mark.parametrize(
    ("status", "failure"),
    [
        (401, ApiFootballFailure.AUTHENTICATION_ERROR),
        (403, ApiFootballFailure.AUTHENTICATION_ERROR),
        (429, ApiFootballFailure.RATE_LIMITED),
    ],
)
def test_auth_and_rate_limit_errors_are_redacted_and_fail_closed(status, failure):
    calls = []

    def transport(request, timeout):
        calls.append(request)
        return _response({"errors": {"message": "provider error"}}, status=status)

    adapter = ApiFootballExperimentalAdapter(transport=transport)
    expected = _candidate(adapter)
    result = adapter.fetch_real_odds(
        expected,
        api_key="test-secret-never-logged",
        request_identity="real-request-1",
        capture_timestamp=CAPTURE,
    )

    assert result.failure is failure
    assert result.network_called is True
    assert len(calls) == 1
    assert calls[0].safe_payload()["headers"]["x-apisports-key"] == "[REDACTED]"
    assert "test-secret-never-logged" not in repr(result.as_payload())


def test_body_quota_error_is_not_retried_or_reinterpreted():
    calls = []

    def transport(request, timeout):
        calls.append(request)
        return _response(
            {"errors": {"requests": "Daily request quota limit reached"}},
            headers={"x-ratelimit-requests-remaining": "0"},
        )

    adapter = ApiFootballExperimentalAdapter(transport=transport)
    expected = _candidate(adapter)
    result = adapter.fetch_real_odds(
        expected,
        api_key="key",
        request_identity="real-request-2",
        capture_timestamp=CAPTURE,
    )

    assert result.failure is ApiFootballFailure.QUOTA_EXHAUSTED
    assert result.state is ProviderState.QUOTA_EXHAUSTED
    assert result.quota.remaining == 0
    assert len(calls) == 1


def test_malformed_json_and_missing_credential_make_no_observation():
    calls = []

    def transport(request, timeout):
        calls.append(request)
        return _response(None, error_code="malformed_json")

    adapter = ApiFootballExperimentalAdapter(transport=transport)
    expected = _candidate(adapter)
    malformed = adapter.fetch_real_odds(
        expected,
        api_key="key",
        request_identity="real-request-3",
        capture_timestamp=CAPTURE,
    )
    missing = adapter.fetch_real_odds(
        expected,
        api_key=None,
        request_identity="real-request-4",
        capture_timestamp=CAPTURE,
    )

    assert malformed.failure is ApiFootballFailure.MALFORMED_JSON
    assert malformed.observations == ()
    assert missing.failure is ApiFootballFailure.CREDENTIAL_MISSING
    assert missing.network_called is False
    assert len(calls) == 1


def test_real_network_path_is_the_only_path_that_emits_real_network_capture():
    calls = []

    def transport(request, timeout):
        calls.append(request)
        return _response(_odds_payload())

    adapter = ApiFootballExperimentalAdapter(transport=transport)
    expected = _candidate(adapter)
    injected = adapter.normalize_injected_odds(
        _odds_payload(), expected, capture_timestamp=CAPTURE
    )[0]
    result = adapter.fetch_real_odds(
        expected,
        api_key="key",
        request_identity="real-request-5",
        capture_timestamp=CAPTURE,
    )

    assert injected.evidence_kind is ApiFootballEvidenceKind.INJECTED_FIXTURE
    assert (
        result.observations[0].evidence_kind
        is ApiFootballEvidenceKind.REAL_NETWORK_CAPTURE
    )
    assert result.observations[0].canonical.captured_at == CAPTURE
    assert result.observations[0].canonical.request_started_at == CAPTURE - timedelta(
        seconds=2
    )
    assert len(calls) == 1


def test_injected_wrapper_cannot_be_relabelled_as_real():
    adapter = ApiFootballExperimentalAdapter()
    expected = _candidate(adapter)
    injected = adapter.normalize_injected_odds(
        _odds_payload(), expected, capture_timestamp=CAPTURE
    )[0]
    forged = replace(
        injected, evidence_kind=ApiFootballEvidenceKind.REAL_NETWORK_CAPTURE
    )

    with pytest.raises(ApiFootballExperimentalError):
        forged.validate()


def test_injected_normalization_never_invokes_transport_and_has_no_b2_authority():
    calls = []

    def transport(request, timeout):
        calls.append(request)
        raise AssertionError("injected normalization must not call transport")

    adapter = ApiFootballExperimentalAdapter(transport=transport)
    expected = _candidate(adapter)
    observation = adapter.normalize_injected_odds(
        _odds_payload(), expected, capture_timestamp=CAPTURE
    )[0]

    assert calls == []
    assert observation.canonical.metadata["synthetic"] is True
    assert not hasattr(adapter, "issue_builder2_qualification_receipt")
    assert not hasattr(adapter, "create_prediction")


def test_api_football_fixture_call_is_explicit_and_candidate_only():
    calls = []

    def transport(request, timeout):
        calls.append(request)
        return _response(_fixtures_payload())

    adapter = ApiFootballExperimentalAdapter(transport=transport)
    result = adapter.fetch_real_fixtures(
        api_key="key",
        season=2026,
        match_date=date(2026, 9, 17),
        request_identity="real-fixtures-1",
        capture_timestamp=CAPTURE,
    )

    assert result.state is ProviderState.AVAILABLE
    assert result.network_called is True
    assert result.fixture_candidates[0].provider_fixture_id == "123456"
    assert calls[0].params["league"] == "2"
    assert calls[0].params["date"] == "2026-09-17"


def test_summary_reports_bookmakers_market_and_timestamp_without_raw_payload():
    summary = summarize_odds_payload(_odds_payload())

    assert summary.fixture_ids == ("123456",)
    assert summary.bookmaker_names == ("Bet365",)
    assert summary.match_winner_fixture_ids == ("123456",)
    assert summary.source_timestamps == (SOURCE,)
    assert summary.one_x_two_rows == 1


def test_diagnostic_budget_cannot_exceed_five_requests():
    with pytest.raises(ApiFootballExperimentalError):
        ApiFootballDiagnosticBudget(maximum_requests=6)

    budget = ApiFootballDiagnosticBudget()
    assert [budget.reserve() for _ in range(5)] == [True] * 5
    assert budget.reserve() is False
    assert budget.requests_used == 5


def test_request_safe_payload_redacts_api_key():
    request = ApiFootballRequest(
        kind=ApiFootballRequestKind.ODDS,
        endpoint="https://v3.football.api-sports.io/odds",
        params={"fixture": "123456", "bet": "1"},
        request_identity="request-1",
        headers={"x-apisports-key": "secret", "Accept": "application/json"},
    )

    safe = request.safe_payload()
    assert safe["headers"]["x-apisports-key"] == "[REDACTED]"
    assert safe["params"] == {"fixture": "123456", "bet": "1"}
