from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.football.odds import isports as isports_module
from src.football.odds.isports import (
    ISPORTS_COMPETITIONS,
    ISPORTS_ENDPOINT_MIN_INTERVAL_SECONDS,
    ISPORTS_ENDPOINTS,
    ISPORTS_PROVIDER_IDENTITY,
    ISportsBookmakerQuote,
    ISportsClient,
    ISportsContractError,
    aggregate_1x2,
    normalize_schedule,
    normalized_observation,
    parse_european_odds,
    parse_main_odds,
    resolve_competitions,
    run_capability_diagnostic,
)
from src.football.provider_cascade.contracts import (
    CANDIDATE_ONLY_PROVIDER_IDENTITIES,
    DEFAULT_PROVIDER_ORDER,
    FOOTBALL_PROVIDER_REPERTOIRE,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
NAMES = {
    "EPL": ("101", "Premier League", "ENG PR", 1),
    "BL1": ("102", "Bundesliga", "GER D1", 1),
    "LL": ("103", "La Liga", "SPA D1", 1),
    "SA": ("104", "Serie A", "ITA D1", 1),
    "L1": ("105", "Ligue 1", "FRA D1", 1),
    "UCL": ("106", "UEFA Champions League", "UEFA CL", 2),
}


def _catalog():
    return {
        "code": 200,
        "data": [
            {
                "leagueId": value[0],
                "name": value[1],
                "shortName": value[2],
                "type": value[3],
            }
            for value in NAMES.values()
        ],
    }


def _competition(code="EPL"):
    return resolve_competitions(_catalog())[code]


def _schedule_row(*, match_id="match-1", status=0, kickoff=None):
    comp = NAMES["EPL"]
    return {
        "leagueId": comp[0],
        "leagueName": comp[1],
        "matchId": match_id,
        "matchTime": int((kickoff or NOW + timedelta(hours=24)).timestamp()),
        "status": status,
        "homeId": "home-1",
        "homeName": "Arsenal",
        "awayId": "away-1",
        "awayName": "Chelsea",
        "neutral": False,
    }


def _main_row(
    match_id="match-1", company_id="8", *, change_time=None, odds_type=1, close=False
):
    return {
        "matchId": match_id,
        "companyId": company_id,
        "initialHome": "1.35",
        "initialDraw": "1.70",
        "initialAway": "1.55",
        "instantHome": "1.40",
        "instantDraw": "1.65",
        "instantAway": "1.50",
        "changeTime": int((change_time or NOW - timedelta(seconds=30)).timestamp()),
        "close": close,
        "oddsType": odds_type,
    }


def _european_payload(
    match_id="match-1", *, change_time=None, company_id="81", name="Book A"
):
    return {
        "code": 200,
        "data": [
            {
                "matchId": match_id,
                "matchTime": int((NOW + timedelta(hours=24)).timestamp()),
                "leagueName": "Premier League",
                "homeName": "Arsenal",
                "awayName": "Chelsea",
                "odds": [
                    {
                        "oddsId": "odds-1",
                        "changeTime": int(
                            (change_time or NOW - timedelta(seconds=45)).timestamp()
                        ),
                        "oddsDetail": [
                            f"{company_id},{name},1.35,1.70,1.55,1.40,1.65,1.50"
                        ],
                    }
                ],
            }
        ],
    }


def _expected_fixture(match_id="match-1"):
    fixture = normalize_schedule(
        {"data": [_schedule_row(match_id=match_id)]}, _competition()
    )[0]
    return {fixture.provider_match_id: fixture}


def test_exact_six_competitions_resolve_without_guessing_ids():
    resolved = resolve_competitions(_catalog())
    assert tuple(resolved) == ISPORTS_COMPETITIONS
    assert {key: item.provider_league_id for key, item in resolved.items()} == {
        "EPL": "101",
        "BL1": "102",
        "LL": "103",
        "SA": "104",
        "L1": "105",
        "UCL": "106",
    }
    assert resolved["UCL"].competition_type == 2


def test_official_short_names_resolve_but_full_name_mismatch_is_rejected():
    payload = _catalog()
    for row in payload["data"]:
        row["name"] = row["shortName"]
    # Names, not the ID values, are used to verify each explicit catalog identity.
    aliases = {
        "EPL": "England Premier League",
        "BL1": "Germany Bundesliga",
        "LL": "Spain La Liga",
        "SA": "Italy Serie A",
        "L1": "France Ligue 1",
        "UCL": "UEFA Champions League",
    }
    for row, (league, (provider_id, _, short_name, competition_type)) in zip(
        payload["data"], NAMES.items(), strict=True
    ):
        row.update(
            leagueId=provider_id,
            name=aliases[league],
            shortName=short_name,
            type=competition_type,
        )
    assert tuple(resolve_competitions(payload)) == ISPORTS_COMPETITIONS


def test_wrong_or_ambiguous_catalog_identity_fails_closed():
    payload = _catalog()
    payload["data"].append(dict(payload["data"][0], leagueId="another"))
    with pytest.raises(ISportsContractError, match="duplicate"):
        resolve_competitions(payload)
    payload = _catalog()
    payload["data"][-1] = dict(payload["data"][-1], name="UEFA Europa League")
    with pytest.raises(ISportsContractError, match="mismatch"):
        resolve_competitions(payload)


def test_schedule_normalizes_provider_identity_utc_neutral_and_status():
    items = normalize_schedule({"code": 200, "data": [_schedule_row()]}, _competition())
    item = items[0]
    assert item.provider_match_id == "match-1"
    assert item.fixture.fixture_key.startswith("EPL|arsenal|chelsea|")
    assert item.kickoff_utc.tzinfo == timezone.utc
    assert item.neutral is False
    assert item.status == 0
    # Keep the fixture test independent of the wall clock: this fixture's
    # fixed kickoff date eventually passes in real time.
    assert item.prematch_eligible_at(NOW) is True


def test_schedule_rejects_duplicate_match_id_and_wrong_competition():
    with pytest.raises(ISportsContractError, match="duplicate"):
        normalize_schedule({"data": [_schedule_row(), _schedule_row()]}, _competition())
    row = _schedule_row()
    row["leagueId"] = "999"
    with pytest.raises(ISportsContractError, match="league ID"):
        normalize_schedule({"data": [row]}, _competition())
    row = _schedule_row()
    row["neutral"] = None
    with pytest.raises(ISportsContractError, match="neutral"):
        normalize_schedule({"data": [row]}, _competition())


def test_main_odds_converts_hong_kong_to_decimal_and_uses_current_early_prices():
    payload = {"data": [{"matchId": "match-1", "europeOdds": [_main_row()]}]}
    parsed, malformed = parse_main_odds(
        payload, captured_at=NOW, expected_match_ids=("match-1",)
    )
    quote = parsed["match-1"][0]
    assert malformed == 0
    assert quote.home_decimal == pytest.approx(2.40)
    assert quote.draw_decimal == pytest.approx(2.65)
    assert quote.away_decimal == pytest.approx(2.50)
    assert quote.opening_home_decimal == pytest.approx(2.35)
    assert quote.bookmaker_identity == "isports_api:main:8:Bet365"


def test_main_odds_documented_csv_field_order_and_all_quote_provenance():
    change_time = int((NOW - timedelta(seconds=25)).timestamp())
    csv_row = f"match-1,8,1.35,1.70,1.55,1.40,1.65,1.50,{change_time},false,1"
    parsed, malformed = parse_main_odds(
        {"data": [{"matchId": "match-1", "europeOdds": [csv_row]}]},
        captured_at=NOW,
        expected_match_ids=("match-1",),
    )
    quote = parsed["match-1"][0]
    assert malformed == 0
    assert quote.match_id == "match-1"
    assert quote.company_id == "8"
    assert quote.company_name == "Bet365"
    assert (
        quote.opening_home_decimal,
        quote.opening_draw_decimal,
        quote.opening_away_decimal,
    ) == pytest.approx((2.35, 2.70, 2.55))
    assert (
        quote.home_decimal,
        quote.draw_decimal,
        quote.away_decimal,
    ) == pytest.approx((2.40, 2.65, 2.50))
    assert quote.change_time == datetime.fromtimestamp(change_time, tz=timezone.utc)


def test_main_odds_csv_requires_exact_documented_field_count():
    valid_prefix = "match-1,8,1.35,1.70,1.55,1.40,1.65,1.50"
    for suffix in (",1,false", ",1,false,1,unexpected"):
        with pytest.raises(ISportsContractError, match="malformed requested-target"):
            parse_main_odds(
                {
                    "data": [
                        {
                            "matchId": "match-1",
                            "europeOdds": [valid_prefix + suffix],
                        }
                    ]
                },
                captured_at=NOW,
                expected_match_ids=("match-1",),
            )


def test_main_odds_rejects_unrequested_ids_and_outer_inner_identity_mismatch():
    with pytest.raises(ISportsContractError, match="unrequested match ID"):
        parse_main_odds(
            {"data": [{"europeOdds": [_main_row("outside")]}]},
            captured_at=NOW,
            expected_match_ids=("match-1",),
        )
    with pytest.raises(ISportsContractError, match="unrequested match ID"):
        parse_main_odds(
            {"data": [{"matchId": "outside", "europeOdds": []}]},
            captured_at=NOW,
            expected_match_ids=("match-1",),
        )
    with pytest.raises(ISportsContractError, match="container/row match ID mismatch"):
        parse_main_odds(
            {"data": [{"matchId": "match-1", "europeOdds": [_main_row("other")]}]},
            captured_at=NOW,
            expected_match_ids=("match-1",),
        )


def test_main_odds_valid_plus_malformed_target_rows_fail_closed():
    malformed = dict(_main_row(), instantDraw="not-a-price")
    payload = {"data": [{"matchId": "match-1", "europeOdds": [_main_row(), malformed]}]}
    with pytest.raises(ISportsContractError, match="malformed requested-target"):
        parse_main_odds(payload, captured_at=NOW, expected_match_ids=("match-1",))


@pytest.mark.parametrize(
    "row",
    [
        dict(_main_row(), close="unknown"),
        dict(_main_row(), oddsType=99),
    ],
)
def test_main_odds_malformed_target_flags_or_unknown_stage_fail_closed(row):
    with pytest.raises(ISportsContractError, match="malformed requested-target"):
        parse_main_odds(
            {"data": [{"matchId": "match-1", "europeOdds": [row]}]},
            captured_at=NOW,
            expected_match_ids=("match-1",),
        )


def test_main_odds_missing_market_is_not_malformed_coverage():
    parsed, malformed = parse_main_odds(
        {"data": [{"matchId": "match-1", "europeOdds": []}]},
        captured_at=NOW,
        expected_match_ids=("match-1",),
    )
    assert parsed == {}
    assert malformed == 0


def test_main_odds_duplicate_bookmaker_evidence_fails_closed():
    with pytest.raises(ISportsContractError, match="duplicate bookmaker evidence"):
        parse_main_odds(
            {
                "data": [
                    {
                        "matchId": "match-1",
                        "europeOdds": [_main_row(), _main_row()],
                    }
                ]
            },
            captured_at=NOW,
            expected_match_ids=("match-1",),
        )


def test_main_client_binds_parser_to_exact_outbound_match_id_set(monkeypatch):
    seen_expected = []
    original = isports_module.parse_main_odds

    def spy(payload, *, captured_at, expected_match_ids):
        seen_expected.append(tuple(expected_match_ids))
        return original(
            payload,
            captured_at=captured_at,
            expected_match_ids=expected_match_ids,
        )

    monkeypatch.setattr(isports_module, "parse_main_odds", spy)

    def transport(path, params, api_key, timeout):
        assert path == ISPORTS_ENDPOINTS["main_odds"]
        assert params == {"matchId": "match-1,match-2"}
        return (
            200,
            {
                "data": [
                    {"matchId": match_id, "europeOdds": [_main_row(match_id)]}
                    for match_id in ("match-1", "match-2")
                ]
            },
            {},
            NOW,
            NOW,
            None,
        )

    client = ISportsClient(
        api_key="offline",
        transport=transport,
        clock=lambda: NOW,
        enforce_pacing=False,
    )
    result, malformed, _ = client.main_odds(("match-1", "match-2"))
    assert tuple(result) == ("match-1", "match-2")
    assert malformed == 0
    assert seen_expected == [("match-1", "match-2")]


def test_main_client_fails_closed_on_mixed_valid_and_malformed_target_evidence():
    def transport(path, params, api_key, timeout):
        return (
            200,
            {
                "data": [
                    {
                        "matchId": "match-1",
                        "europeOdds": [
                            _main_row(),
                            dict(_main_row(company_id="9"), instantAway="bad"),
                        ],
                    }
                ]
            },
            {},
            NOW,
            NOW,
            None,
        )

    client = ISportsClient(
        api_key="offline",
        transport=transport,
        clock=lambda: NOW,
        enforce_pacing=False,
    )
    with pytest.raises(ISportsContractError, match="malformed requested-target"):
        client.main_odds(("match-1",))


def test_main_odds_rejects_closing_closed_inplay_malformed_and_incomplete_rows():
    rows = [
        _main_row(company_id="8", odds_type=2),
        _main_row(company_id="3", close=True),
        dict(_main_row(company_id="4"), inPlay=True),
        dict(_main_row(company_id="7"), instantDraw="nan"),
        dict(_main_row(company_id="9"), instantAway="0.0"),
    ]
    with pytest.raises(ISportsContractError, match="malformed requested-target"):
        parse_main_odds(
            {"data": [{"matchId": "match-1", "europeOdds": rows}]},
            captured_at=NOW,
            expected_match_ids=("match-1",),
        )


def test_european_odds_preserve_separate_bookmaker_namespace_and_hk_conversion():
    payload = _european_payload()
    payload["data"][0]["odds"][0]["oddsDetail"].append(
        "82,Book B,1.35,1.70,1.55,1.40,1.65,1.50"
    )
    parsed, malformed = parse_european_odds(
        payload, captured_at=NOW, expected_fixtures=_expected_fixture()
    )
    rows = parsed["match-1"]
    assert malformed == 0
    assert len(rows) == 2
    assert rows[0].home_decimal == pytest.approx(2.40)
    assert rows[0].bookmaker_identity.startswith("isports_api:european:81:")
    assert all(row.source == "european" for row in rows)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("leagueName", "UEFA Champions League"),
        ("homeName", "Different Home"),
        ("awayName", "Different Away"),
        ("matchTime", int((NOW + timedelta(hours=25)).timestamp())),
        ("leagueId", "999"),
    ],
)
def test_european_odds_rejects_each_fixture_identity_mismatch(field, value):
    payload = _european_payload()
    payload["data"][0][field] = value
    if field == "leagueId":
        payload["data"][0]["leagueName"] = "Premier League"
    with pytest.raises(ISportsContractError, match="fixture identity mismatch"):
        parse_european_odds(
            payload, captured_at=NOW, expected_fixtures=_expected_fixture()
        )


def test_european_odds_rejects_unrequested_and_duplicate_target_rows():
    with pytest.raises(ISportsContractError, match="unrequested match ID"):
        parse_european_odds(
            _european_payload(match_id="outside"),
            captured_at=NOW,
            expected_fixtures=_expected_fixture(),
        )
    payload = _european_payload()
    payload["data"].append(dict(payload["data"][0]))
    with pytest.raises(ISportsContractError, match="duplicated a target match row"):
        parse_european_odds(
            payload, captured_at=NOW, expected_fixtures=_expected_fixture()
        )


def test_european_odds_does_not_turn_valid_plus_malformed_into_coverage():
    payload = _european_payload()
    payload["data"][0]["odds"].append(
        {"changeTime": int(NOW.timestamp()), "oddsDetail": ["81,Book A,broken"]}
    )
    with pytest.raises(ISportsContractError, match="malformed requested-target"):
        parse_european_odds(
            payload, captured_at=NOW, expected_fixtures=_expected_fixture()
        )


def test_european_duplicate_bookmaker_evidence_fails_closed():
    payload = _european_payload()
    payload["data"][0]["odds"][0]["oddsDetail"].append(
        "81,Book A,1.35,1.70,1.55,1.40,1.65,1.50"
    )
    with pytest.raises(ISportsContractError, match="ambiguous bookmaker evidence"):
        parse_european_odds(
            payload, captured_at=NOW, expected_fixtures=_expected_fixture()
        )


def test_european_missing_market_is_distinct_from_malformed_market_evidence():
    payload = _european_payload()
    payload["data"][0]["odds"] = []
    parsed, malformed = parse_european_odds(
        payload, captured_at=NOW, expected_fixtures=_expected_fixture()
    )
    assert parsed == {}
    assert malformed == 0


def test_european_client_derives_request_ids_and_requires_fixture_bindings():
    fixture = _expected_fixture()["match-1"]
    captured = []

    def transport(path, params, api_key, timeout):
        captured.append((path, dict(params)))
        return 200, _european_payload(), {}, NOW, NOW, None

    client = ISportsClient(
        api_key="offline",
        transport=transport,
        clock=lambda: NOW,
        enforce_pacing=False,
    )
    result, malformed, _ = client.european_odds({"match-1": fixture})
    assert captured == [(ISPORTS_ENDPOINTS["european_odds"], {"matchId": "match-1"})]
    assert tuple(result) == ("match-1",)
    assert malformed == 0

    with pytest.raises(
        ISportsContractError, match="requires scheduled fixture bindings"
    ):
        client.european_odds({})


def test_european_incomplete_and_malformed_rows_are_rejected():
    payload = _european_payload()
    payload["data"][0]["odds"][0]["oddsDetail"] = [
        "81,Book A,0.8,0.9",
        "82,Book B,nan,1.7,1.5,1.3,1.6,1.5",
    ]
    with pytest.raises(ISportsContractError, match="malformed requested-target"):
        parse_european_odds(
            payload, captured_at=NOW, expected_fixtures=_expected_fixture()
        )


def test_european_rows_must_match_the_exact_schedule_identity():
    fixture = normalize_schedule({"data": [_schedule_row()]}, _competition())[0]
    payload = _european_payload()
    payload["data"][0]["awayName"] = "Different Team"
    with pytest.raises(ISportsContractError, match="fixture identity mismatch"):
        parse_european_odds(
            payload,
            captured_at=NOW,
            expected_fixtures={fixture.provider_match_id: fixture},
        )


def test_european_name_aliases_use_canonical_sportsbrain_mapping():
    row = _schedule_row()
    row.update(homeName="Manchester United", awayName="Bayern Munich")
    fixture = normalize_schedule({"data": [row]}, _competition())[0]
    payload = _european_payload()
    payload["data"][0].update(homeName="Man Utd", awayName="Bayern München")
    parsed, malformed = parse_european_odds(
        payload,
        captured_at=NOW,
        expected_fixtures={fixture.provider_match_id: fixture},
    )
    assert malformed == 0
    assert len(parsed[fixture.provider_match_id]) == 1


def test_aggregation_uses_european_when_valid_and_shin_fair_odds_deterministically():
    fixture = normalize_schedule({"data": [_schedule_row()]}, _competition())[0]
    expected = _expected_fixture()
    european, bad_euro = parse_european_odds(
        _european_payload(), captured_at=NOW, expected_fixtures=expected
    )
    main, bad_main = parse_main_odds(
        {"data": [{"matchId": "match-1", "europeOdds": [_main_row()]}]},
        captured_at=NOW,
        expected_match_ids=("match-1",),
    )
    result = aggregate_1x2(
        fixture,
        main_quotes=main["match-1"],
        european_quotes=european["match-1"],
        captured_at=NOW,
        main_response_digest="a" * 64,
        european_response_digest="b" * 64,
        malformed_row_count=bad_euro + bad_main,
    )
    assert result.selected_source == "european"
    assert len(result.bookmaker_quotes) == 1
    assert result.snapshot.kind.value == "signal_time"
    assert sum(1.0 / value for value in result.snapshot.odds.values()) == pytest.approx(
        1.0
    )
    assert result.snapshot.source == ISPORTS_PROVIDER_IDENTITY


def test_aggregate_is_deterministic_for_bookmaker_input_order_and_exact_age_limit():
    fixture = normalize_schedule({"data": [_schedule_row()]}, _competition())[0]
    first = ISportsBookmakerQuote(
        source="european",
        company_id="82",
        company_name="Book B",
        match_id="match-1",
        home_decimal=2.4,
        draw_decimal=2.65,
        away_decimal=2.5,
        change_time=NOW - timedelta(seconds=900),
    )
    second = ISportsBookmakerQuote(
        source="european",
        company_id="81",
        company_name="Book A",
        match_id="match-1",
        home_decimal=2.5,
        draw_decimal=2.7,
        away_decimal=2.45,
        change_time=NOW - timedelta(seconds=899),
    )
    left = aggregate_1x2(
        fixture,
        european_quotes=(first, second),
        captured_at=NOW,
        european_response_digest="d" * 64,
    )
    right = aggregate_1x2(
        fixture,
        european_quotes=(second, first),
        captured_at=NOW,
        european_response_digest="d" * 64,
    )
    assert left.snapshot.snapshot_id == right.snapshot.snapshot_id
    assert dict(left.snapshot.odds) == dict(right.snapshot.odds)
    assert [quote.company_id for quote in left.bookmaker_quotes] == ["81", "82"]


def test_european_namespace_is_not_mixed_when_fallback_to_main():
    fixture = normalize_schedule({"data": [_schedule_row()]}, _competition())[0]
    main, _ = parse_main_odds(
        {"data": [{"matchId": "match-1", "europeOdds": [_main_row()]}]},
        captured_at=NOW,
        expected_match_ids=("match-1",),
    )
    stale, _ = parse_european_odds(
        _european_payload(change_time=NOW - timedelta(seconds=901)),
        captured_at=NOW,
        expected_fixtures=_expected_fixture(),
    )
    result = aggregate_1x2(
        fixture,
        main_quotes=main["match-1"],
        european_quotes=stale.get("match-1", ()),
        captured_at=NOW,
    )
    assert result.selected_source == "main"
    assert {row.source for row in result.bookmaker_quotes} == {"main"}


def test_incomplete_or_stale_market_fails_closed():
    fixture = normalize_schedule({"data": [_schedule_row()]}, _competition())[0]
    with pytest.raises(ISportsContractError, match="no fresh"):
        aggregate_1x2(fixture, captured_at=NOW)
    stale, _ = parse_main_odds(
        {
            "data": [
                {
                    "matchId": "match-1",
                    "europeOdds": [_main_row(change_time=NOW - timedelta(seconds=901))],
                }
            ]
        },
        captured_at=NOW,
        expected_match_ids=("match-1",),
    )
    with pytest.raises(ISportsContractError, match="no fresh"):
        aggregate_1x2(fixture, main_quotes=stale.get("match-1", ()), captured_at=NOW)


def test_aggregate_never_accepts_malformed_row_count_as_valid_snapshot():
    fixture = normalize_schedule({"data": [_schedule_row()]}, _competition())[0]
    quote, _ = parse_main_odds(
        {"data": [{"matchId": "match-1", "europeOdds": [_main_row()]}]},
        captured_at=NOW,
        expected_match_ids=("match-1",),
    )
    with pytest.raises(ISportsContractError, match="malformed provider rows"):
        aggregate_1x2(
            fixture,
            main_quotes=quote["match-1"],
            captured_at=NOW,
            malformed_row_count=1,
        )


def test_post_kickoff_evidence_rejected_even_if_prices_are_present():
    fixture = normalize_schedule(
        {"data": [_schedule_row(kickoff=NOW - timedelta(minutes=1))]}, _competition()
    )[0]
    main, _ = parse_main_odds(
        {"data": [{"matchId": "match-1", "europeOdds": [_main_row()]}]},
        captured_at=NOW,
        expected_match_ids=("match-1",),
    )
    with pytest.raises(ISportsContractError, match="in-play"):
        aggregate_1x2(fixture, main_quotes=main["match-1"], captured_at=NOW)


def test_ucl_uses_its_own_provider_competition_and_event_key():
    ucl = _competition("UCL")
    row = dict(
        _schedule_row(),
        leagueId="106",
        leagueName="UEFA Champions League",
        matchId="ucl-event",
    )
    result = normalize_schedule({"data": [row]}, ucl)[0]
    assert result.fixture.fixture_key == "UCL:ucl-event"
    assert result.league_code == "UCL"


def test_normalized_observation_is_candidate_only_and_retains_bookmakers_and_digests():
    fixture = normalize_schedule({"data": [_schedule_row()]}, _competition())[0]
    european, _ = parse_european_odds(
        _european_payload(), captured_at=NOW, expected_fixtures=_expected_fixture()
    )
    result = aggregate_1x2(
        fixture,
        european_quotes=european["match-1"],
        captured_at=NOW,
        european_response_digest="a" * 64,
    )
    obs = normalized_observation(
        result,
        request_identity="isports:request:1",
        request_started_at=NOW - timedelta(seconds=1),
        request_completed_at=NOW,
        adapter_source_sha="a" * 40,
    )
    assert obs.provider_identity == ISPORTS_PROVIDER_IDENTITY
    assert obs.candidate_only is True
    assert obs.provider_fixture_id == "match-1"
    assert obs.metadata["bookmaker_count"] == 1
    assert obs.metadata["bookmakers"][0]["company_id"] == "81"
    assert obs.raw_record_digest == "a" * 64
    assert len(obs.metadata["provider_record_digest"]) == 64
    assert len(obs.metadata["normalized_record_digest"]) == 64
    assert obs.metadata["adapter_source_sha"] == "a" * 40
    assert ISPORTS_PROVIDER_IDENTITY in CANDIDATE_ONLY_PROVIDER_IDENTITIES
    assert DEFAULT_PROVIDER_ORDER == FOOTBALL_PROVIDER_REPERTOIRE == ("the_odds_api",)
    assert "therundown_experimental" not in ISPORTS_PROVIDER_IDENTITY
    assert "api_key" not in json_dumps(obs.as_payload()).casefold()


def json_dumps(value):
    import json

    return json.dumps(value, sort_keys=True)


def test_safe_http_evidence_never_serializes_secret_or_query_string():
    secret = "never-output-this"

    def transport(path, params, api_key, timeout):
        assert api_key == secret
        assert "api_key" not in params
        return (
            200,
            {"data": []},
            {"X-RateLimit-Limit": "200", "Set-Cookie": "secret"},
            NOW,
            NOW,
            None,
        )

    client = ISportsClient(
        api_key=secret, transport=transport, clock=lambda: NOW, enforce_pacing=False
    )
    response = client.request(ISPORTS_ENDPOINTS["catalog"])
    serialized = json_dumps(response.safe_payload())
    assert secret not in serialized
    assert "api_key" not in serialized
    assert "set-cookie" not in serialized
    assert response.safe_headers == {"x-ratelimit-limit": "200"}


def test_client_no_retry_and_endpoint_allowlist():
    calls = []

    def transport(path, params, api_key, timeout):
        calls.append(path)
        return 503, {"data": []}, {}, NOW, NOW, None

    client = ISportsClient(
        api_key="offline", transport=transport, clock=lambda: NOW, enforce_pacing=False
    )
    response = client.request(ISPORTS_ENDPOINTS["catalog"])
    assert response.status_code == 503
    assert client.request_count == len(calls) == 1
    with pytest.raises(ISportsContractError, match="allowlist"):
        client.request("/sport/football/live/changes")
    with pytest.raises(ISportsContractError, match="parameters"):
        client.request(
            ISPORTS_ENDPOINTS["catalog"], {"api_key": "must-not-be-persisted"}
        )


def test_report_types_reject_closing_snapshot_as_prediction_input():
    fixture = normalize_schedule({"data": [_schedule_row()]}, _competition())[0]
    main, _ = parse_main_odds(
        {"data": [{"matchId": "match-1", "europeOdds": [_main_row()]}]},
        captured_at=NOW,
        expected_match_ids=("match-1",),
    )
    result = aggregate_1x2(fixture, main_quotes=main["match-1"], captured_at=NOW)
    assert result.snapshot.kind is not None
    assert result.snapshot.kind.value == "signal_time"


def test_ucl_schedule_and_market_project_without_claiming_model_authority():
    ucl = _competition("UCL")
    row = dict(
        _schedule_row(match_id="ucl-event"),
        leagueId="106",
        leagueName="UEFA Champions League",
        homeId="ucl-home",
        homeName="Real Madrid",
        awayId="ucl-away",
        awayName="Bayern Munich",
        neutral=True,
    )
    fixture = normalize_schedule({"data": [row]}, ucl)[0]
    cl_fixture = fixture.as_champions_league_fixture()
    assert cl_fixture.fixture_key == "UCL:ucl-event"
    assert cl_fixture.neutral_ground is True
    quotes, _ = parse_main_odds(
        {"data": [{"matchId": "ucl-event", "europeOdds": [_main_row("ucl-event")]}]},
        captured_at=NOW,
        expected_match_ids=("ucl-event",),
    )
    market = aggregate_1x2(
        fixture,
        main_quotes=quotes["ucl-event"],
        captured_at=NOW,
        main_response_digest="c" * 64,
    )
    assert market.snapshot.source == "isports_api"
    assert market.snapshot.fixture_key == cl_fixture.fixture_key


def test_capability_run_is_single_bounded_and_respects_endpoint_pacing():
    class TestClock:
        def __init__(self):
            self.current = NOW
            self.sleeps = []

        def __call__(self):
            return self.current

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.current += timedelta(seconds=seconds)

    clock = TestClock()
    calls = []
    match_ids = {code: f"event-{code}" for code in ISPORTS_COMPETITIONS}

    def transport(path, params, api_key, timeout):
        calls.append((path, dict(params), api_key))
        started = clock()
        if path == ISPORTS_ENDPOINTS["catalog"]:
            payload = _catalog()
        elif path == ISPORTS_ENDPOINTS["schedule"]:
            code = next(
                code for code, value in NAMES.items() if value[0] == params["leagueId"]
            )
            _, name, _, _ = NAMES[code]
            payload = {
                "data": [
                    {
                        "leagueId": NAMES[code][0],
                        "leagueName": name,
                        "matchId": match_ids[code],
                        "matchTime": int((NOW + timedelta(days=1)).timestamp()),
                        "status": 0,
                        "homeId": f"home-{code}",
                        "awayId": f"away-{code}",
                        "homeName": f"Home {code}",
                        "awayName": f"Away {code}",
                        "neutral": False,
                    }
                ]
            }
        elif path == ISPORTS_ENDPOINTS["main_odds"]:
            payload = {
                "data": [
                    {
                        "matchId": match_id,
                        "europeOdds": [
                            f"{match_id},8,1.35,1.70,1.55,1.40,1.65,1.50,{int(started.timestamp())},false,1"
                        ],
                    }
                    for match_id in params["matchId"].split(",")
                ]
            }
        elif path == ISPORTS_ENDPOINTS["european_odds"]:
            payload = {
                "data": [
                    {
                        "matchId": match_id,
                        "matchTime": int((NOW + timedelta(days=1)).timestamp()),
                        "leagueName": NAMES[code][1],
                        "homeName": f"Home {code}",
                        "awayName": f"Away {code}",
                        "odds": [
                            {
                                "changeTime": int(started.timestamp()),
                                "oddsDetail": [
                                    "81,Book A,1.35,1.70,1.55,1.40,1.65,1.50"
                                ],
                            }
                        ],
                    }
                    for code, match_id in match_ids.items()
                ]
            }
        else:
            raise AssertionError("unexpected provider endpoint")
        finished = clock()
        return 200, payload, {"X-RateLimit-Remaining": "190"}, started, finished, None

    client = ISportsClient(
        api_key="offline-secret",
        transport=transport,
        clock=clock,
        sleeper=clock.sleep,
        enforce_pacing=True,
    )
    report = run_capability_diagnostic(client, now=clock)
    result = report.as_payload()
    assert report.request_count == len(calls) == 9
    assert report.retries == report.fallbacks == report.polling == 0
    assert report.credential_access_count == 0
    assert [item[0] for item in calls] == [
        ISPORTS_ENDPOINTS["catalog"],
        *([ISPORTS_ENDPOINTS["schedule"]] * 6),
        ISPORTS_ENDPOINTS["main_odds"],
        ISPORTS_ENDPOINTS["european_odds"],
    ]
    assert (
        clock.sleeps
        == [ISPORTS_ENDPOINT_MIN_INTERVAL_SECONDS[ISPORTS_ENDPOINTS["schedule"]]] * 5
    )
    assert [item["league_code"] for item in result["competitions"]] == list(
        ISPORTS_COMPETITIONS
    )
    assert all(item["schedule_coverage"] for item in result["competitions"])
    assert all(item["market_coverage"] for item in result["competitions"])
    assert all(item["selected_bookmaker_count"] == 1 for item in result["competitions"])
    encoded = json_dumps(result)
    assert "offline-secret" not in encoded
    assert "api_key" not in encoded


def test_capability_diagnostic_records_http_failure_without_retry_or_secret():
    calls = []

    def transport(path, params, api_key, timeout):
        calls.append((path, dict(params), api_key))
        return (
            403,
            {"code": 403, "message": "access denied"},
            {"X-RateLimit-Remaining": "189"},
            NOW,
            NOW + timedelta(milliseconds=50),
            None,
        )

    client = ISportsClient(
        api_key="never-serialize-this-key",
        transport=transport,
        clock=lambda: NOW + timedelta(seconds=1),
        enforce_pacing=True,
    )
    payload = run_capability_diagnostic(client, now=lambda: NOW).as_payload()
    assert payload["status"] == "FAILED_CLOSED"
    assert payload["failure_classification"] == "http_failure"
    assert payload["failure_operation"] == "catalog"
    assert payload["failure_http_status"] == 403
    assert payload["request_count"] == len(calls) == 1
    assert payload["retry_count"] == 0
    assert payload["competitions"] == []
    assert "never-serialize-this-key" not in json_dumps(payload)


def test_capability_diagnostic_stops_after_failed_main_odds_request():
    class TestClock:
        def __init__(self):
            self.current = NOW

        def __call__(self):
            return self.current

        def sleep(self, seconds):
            self.current += timedelta(seconds=seconds)

    clock = TestClock()
    calls = []

    def transport(path, params, api_key, timeout):
        calls.append(path)
        started = clock()
        if path == ISPORTS_ENDPOINTS["catalog"]:
            return 200, _catalog(), {}, started, started, None
        if path == ISPORTS_ENDPOINTS["schedule"]:
            code = next(
                code for code, value in NAMES.items() if value[0] == params["leagueId"]
            )
            league_id, name, _, _ = NAMES[code]
            payload = {
                "data": [
                    {
                        **_schedule_row(
                            match_id=f"event-{code}",
                            kickoff=NOW + timedelta(days=1),
                        ),
                        "leagueId": league_id,
                        "leagueName": name,
                        "homeId": f"home-{code}",
                        "awayId": f"away-{code}",
                        "homeName": f"Home {code}",
                        "awayName": f"Away {code}",
                    }
                ]
            }
            return 200, payload, {}, started, started, None
        if path == ISPORTS_ENDPOINTS["main_odds"]:
            return 403, {"code": 403}, {}, started, started, None
        raise AssertionError("diagnostic must stop before European Odds")

    client = ISportsClient(
        api_key="offline-secret",
        transport=transport,
        clock=clock,
        sleeper=clock.sleep,
        enforce_pacing=True,
    )
    payload = run_capability_diagnostic(client, now=clock).as_payload()
    assert payload["status"] == "FAILED_CLOSED"
    assert payload["failure_operation"] == "main_odds"
    assert payload["failure_http_status"] == 403
    assert payload["request_count"] == len(calls) == 8
    assert calls.count(ISPORTS_ENDPOINTS["european_odds"]) == 0
    assert [row["upcoming_fixture_count"] for row in payload["competitions"]] == [
        1,
        1,
        1,
        1,
        1,
        1,
    ]
    assert [row["schedule_http_status"] for row in payload["competitions"]] == [
        200,
        200,
        200,
        200,
        200,
        200,
    ]
