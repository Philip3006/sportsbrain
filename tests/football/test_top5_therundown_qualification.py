"""Deterministic, no-network tests for the APP-B1 TheRundown gate."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.top5_therundown_qualification import main as qualification_main
from src.football.provider_cascade.contracts import (
    FOOTBALL_PROVIDER_REPERTOIRE,
    MARKET_PREMATCH_1X2,
)
from src.football.top5_therundown_qualification import (
    EVIDENCE_SCHEMA_VERSION,
    QUALIFICATION_CRITERIA,
    THERUNDOWN_PROVIDER_IDENTITY,
    TheRundownEvidenceSummaryStatus,
    TheRundownQualificationCode,
    TheRundownQualificationError,
    TheRundownQualificationStatus,
    evaluate_therundown_pr88_evidence_summary,
    evaluate_therundown_qualification,
)

UTC = timezone.utc
_ROOT = Path(__file__).parents[2]
CAPTURED_AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
KICKOFF = CAPTURED_AT + timedelta(hours=2)
SOURCE_TIMESTAMP = CAPTURED_AT - timedelta(minutes=2)


def _evidence(league: str = "EPL", **changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_id": f"evidence-{league}",
        "provider_identity": THERUNDOWN_PROVIDER_IDENTITY,
        "evidence_kind": "REAL_OBSERVED",
        "canonical_league": league,
        "provider_league_code": f"therundown-{league.lower()}",
        "provider_league_identity_verified": True,
        "fixture_observed": True,
        "fixture_key": f"{league}|Home {league}|Away {league}|2026-09-17T14:00:00+00:00",
        "provider_event_id": f"event-{league}",
        "home_team": f"Home {league}",
        "away_team": f"Away {league}",
        "home_away_identity_verified": True,
        "kickoff": KICKOFF.isoformat(),
        "market_type": MARKET_PREMATCH_1X2,
        "market_phase": "PRE_MATCH",
        "odds": {"home": 2.2, "draw": 3.4, "away": 3.1},
        "bookmaker_observed": True,
        "bookmaker_identity": "affiliate:bookmaker-1",
        "source_timestamp": SOURCE_TIMESTAMP.isoformat(),
        "source_timing_provenance": "SOURCE_TIMESTAMP",
        "captured_at": CAPTURED_AT.isoformat(),
        "provider_request_id": f"request-{league}",
        "observation_id": f"observation-{league}",
        "source_provenance": "therundown:v2:event;market=1;affiliate=bookmaker-1",
        "raw_record_digest": "a" * 64,
        "provider_record_digest": "c" * 64,
        "normalized_record_digest": "b" * 64,
        "adapter_version": "therundown-v2-experimental:1",
        "adapter_source_sha": "d" * 40,
        "authorization_metadata": {
            "controlled_shadow_run_id": "run-1",
            "qualification_session_id": "session-1",
            "ceo_authorization_id": "ceo-1",
            "provider_identity": THERUNDOWN_PROVIDER_IDENTITY,
            "canonical_league": league,
            "fixture_key": f"{league}|Home {league}|Away {league}|2026-09-17T14:00:00+00:00",
            "provider_event_id": f"event-{league}",
            "provider_request_id": f"request-{league}",
            "provider_scope": [THERUNDOWN_PROVIDER_IDENTITY],
            "league_scope": [league],
            "fixture_scope": [
                f"{league}|Home {league}|Away {league}|2026-09-17T14:00:00+00:00"
            ],
            "network_execution": True,
            "no_bet": True,
            "publication_enabled": False,
            "monetary_spend_authorized": False,
        },
        "quota_state_before": {"used": 10, "remaining": 490},
        "quota_state_after": {"used": 21, "remaining": 479},
        "rate_limit_state": {"rate_limit": 1, "rate_remaining": 0},
        "quota_cost_units": 11.0,
        "network_request_count": 1,
        "synthetic_reconstruction": False,
        "provider_status": "AVAILABLE",
        "failure_codes": [],
    }
    record.update(changes)
    return record


def _payload(records: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "provider_identity": THERUNDOWN_PROVIDER_IDENTITY,
        "evidence": records,
    }


def _pr88_summary() -> dict[str, object]:
    path = _ROOT / "tests/fixtures/therundown/pr88_top5_real_evidence_summary.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_pr88_real_evidence_summary_is_consumed_as_partial_only() -> None:
    report = evaluate_therundown_pr88_evidence_summary(_pr88_summary())

    assert report.status is TheRundownEvidenceSummaryStatus.PARTIAL_EVIDENCE
    by_league = {item.league: item for item in report.leagues}
    assert set(by_league) == {"BL1", "EPL", "LL", "SA", "L1"}
    for league in ("BL1", "EPL", "SA", "L1"):
        item = by_league[league]
        assert item.status is TheRundownEvidenceSummaryStatus.PARTIAL_EVIDENCE
        assert item.real_fixture is not None
        assert item.complete_1x2 is True
        assert item.complete_1x2_count == 3
        assert item.bookmakers == ("DraftKings", "BetMGM", "FanDuel")
        assert item.source_update_timestamp_range
        assert "CANONICAL_OBSERVATION_REQUIRED" in item.blockers
        assert "EXACT_PRICE_PROVENANCE_REQUIRED" in item.blockers
    assert by_league["LL"].status is TheRundownEvidenceSummaryStatus.UNOBSERVED
    assert by_league["LL"].real_fixture is None
    payload = report.as_payload()
    assert payload["source"]["source_pr"] == 88
    assert payload["source"]["source_head"] == (
        "c550e8037704eda58f536246e4563b47e3c2e8bb"
    )
    assert payload["canonical_gate_eligible"] is False
    assert payload["builder2_receipt_eligible"] is False
    assert payload["provider_registered"] is False
    assert payload["production_authority_changed"] is False
    assert payload["evaluator_network_accessed"] is False
    assert FOOTBALL_PROVIDER_REPERTOIRE == ("the_odds_api",)
    assert THERUNDOWN_PROVIDER_IDENTITY not in FOOTBALL_PROVIDER_REPERTOIRE
    assert payload["shadow_compatibility"]["head"] == (
        "a29030571940c73f51066856f589e29b06627523"
    )


@pytest.mark.parametrize(
    "field",
    ["source_head", "source_review_id", "source_document_blob"],
)
def test_pr88_summary_is_bound_to_the_reviewed_external_source(field: str) -> None:
    payload = _pr88_summary()
    payload["source"] = {**payload["source"], field: "changed"}

    with pytest.raises(TheRundownQualificationError):
        evaluate_therundown_pr88_evidence_summary(payload)


def test_pr88_summary_order_is_normalized_and_cannot_supply_canonical_prices() -> None:
    payload = _pr88_summary()
    payload["leagues"] = list(reversed(payload["leagues"]))
    report = evaluate_therundown_pr88_evidence_summary(payload)

    assert [item.league for item in report.leagues] == ["BL1", "EPL", "LL", "SA", "L1"]
    assert all(
        item.status is not TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
        for item in report.leagues
    )
    assert all(
        "EXACT_PRICE_PROVENANCE_REQUIRED" in item.blockers for item in report.leagues
    )


def test_all_five_leagues_require_and_can_reach_evidence_ready() -> None:
    report = evaluate_therundown_qualification(
        _payload([_evidence(league) for league in ("BL1", "EPL", "LL", "SA", "L1")]),
        maximum_odds_age_seconds=300,
    )

    assert report.status is TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
    assert [item.league for item in report.leagues] == ["BL1", "EPL", "LL", "SA", "L1"]
    assert all(
        item.status is TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
        for item in report.leagues
    )
    assert set(report.leagues[0].criteria_satisfied) == set(QUALIFICATION_CRITERIA)
    assert report.as_payload()["production_authority_changed"] is False
    assert report.as_payload()["provider_registered"] is False


def test_missing_league_is_unobserved_and_does_not_count_other_leagues() -> None:
    report = evaluate_therundown_qualification(
        _payload([_evidence("EPL")]), maximum_odds_age_seconds=300
    )

    assert report.status is TheRundownQualificationStatus.PARTIAL_EVIDENCE
    by_league = {item.league: item.status for item in report.leagues}
    assert by_league["EPL"] is TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
    assert by_league["BL1"] is TheRundownQualificationStatus.UNOBSERVED


def test_no_input_is_unobserved_for_each_top5_league() -> None:
    report = evaluate_therundown_qualification(
        _payload([]), maximum_odds_age_seconds=300
    )

    assert report.status is TheRundownQualificationStatus.UNOBSERVED
    assert all(
        item.status is TheRundownQualificationStatus.UNOBSERVED
        for item in report.leagues
    )


def test_missing_quality_criterion_is_partial_evidence() -> None:
    report = evaluate_therundown_qualification(
        _payload([_evidence("EPL", odds={"home": 2.2, "draw": None, "away": 3.1})]),
        maximum_odds_age_seconds=300,
    )

    item = report.leagues[1]
    assert item.status is TheRundownQualificationStatus.PARTIAL_EVIDENCE
    assert "complete_home_draw_away_prices" not in item.criteria_satisfied
    assert (
        TheRundownQualificationCode.COMPLETE_PRICES_REQUIRED.value in item.failure_codes
    )


def test_provider_failure_is_failed_not_partial() -> None:
    report = evaluate_therundown_qualification(
        _payload(
            [
                _evidence(
                    "EPL",
                    provider_status="RATE_LIMITED",
                    failure_codes=["HTTP_429"],
                )
            ]
        ),
        maximum_odds_age_seconds=300,
    )

    assert report.leagues[1].status is TheRundownQualificationStatus.FAILED
    assert (
        TheRundownQualificationCode.PROVIDER_FAILURE.value
        in report.leagues[1].failure_codes
    )


def test_synthetic_evidence_can_never_qualify() -> None:
    report = evaluate_therundown_qualification(
        _payload(
            [
                _evidence(
                    "EPL",
                    evidence_kind="TEST_FIXTURE",
                    network_request_count=0,
                    synthetic_reconstruction=True,
                )
            ]
        ),
        maximum_odds_age_seconds=300,
    )

    assert report.leagues[1].status is TheRundownQualificationStatus.FAILED
    assert not report.leagues[1].qualified_evidence_ids
    assert (
        TheRundownQualificationCode.EVIDENCE_NOT_REAL.value
        in report.leagues[1].failure_codes
    )
    assert (
        TheRundownQualificationCode.SYNTHETIC_EVIDENCE.value
        in report.leagues[1].failure_codes
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        (
            "source_timestamp",
            "2026-09-17T11:58:00",
            TheRundownQualificationCode.FRESHNESS_NOT_OBSERVABLE,
        ),
        (
            "source_timestamp",
            "2026-09-17T11:00:00+00:00",
            TheRundownQualificationCode.ODDS_STALE,
        ),
        (
            "market_phase",
            "CLOSING",
            TheRundownQualificationCode.PREMATCH_MARKET_REQUIRED,
        ),
    ],
)
def test_timestamp_and_market_boundaries_fail_closed(
    field: str, value: object, code: TheRundownQualificationCode
) -> None:
    report = evaluate_therundown_qualification(
        _payload([_evidence("EPL", **{field: value})]),
        maximum_odds_age_seconds=300,
    )

    assert report.leagues[1].status is TheRundownQualificationStatus.PARTIAL_EVIDENCE
    assert code.value in report.leagues[1].failure_codes


def test_provider_identity_and_out_of_scope_leagues_fail_closed() -> None:
    report = evaluate_therundown_qualification(
        _payload(
            [
                _evidence("EPL", provider_identity="other-provider"),
                _evidence("UCL"),
            ]
        ),
        maximum_odds_age_seconds=300,
    )

    assert report.status is TheRundownQualificationStatus.FAILED
    assert report.invalid_evidence_count == 1
    assert (
        TheRundownQualificationCode.PROVIDER_IDENTITY_MISMATCH.value
        in report.leagues[1].failure_codes
    )
    assert (
        TheRundownQualificationCode.LEAGUE_OUT_OF_SCOPE.value
        in report.global_failure_codes
    )


def test_missing_provenance_or_quota_is_partial_not_accepted() -> None:
    report = evaluate_therundown_qualification(
        _payload(
            [
                _evidence(
                    "EPL",
                    provider_request_id="",
                    quota_state_before={},
                    rate_limit_state={},
                )
            ]
        ),
        maximum_odds_age_seconds=300,
    )

    item = report.leagues[1]
    assert item.status is TheRundownQualificationStatus.PARTIAL_EVIDENCE
    assert (
        TheRundownQualificationCode.REQUEST_PROVENANCE_INCOMPLETE.value
        in item.failure_codes
    )
    assert (
        TheRundownQualificationCode.QUOTA_RATE_LIMIT_UNRECORDED.value
        in item.failure_codes
    )


def test_envelope_schema_and_unknown_fields_fail_closed() -> None:
    with pytest.raises(
        TheRundownQualificationError, match="unsupported evidence schema"
    ):
        evaluate_therundown_qualification(
            {
                "schema_version": "wrong",
                "provider_identity": THERUNDOWN_PROVIDER_IDENTITY,
                "evidence": [],
            },
            maximum_odds_age_seconds=300,
        )

    report = evaluate_therundown_qualification(
        {
            **_payload([_evidence("EPL")]),
            "unexpected": "must not be ignored",
        },
        maximum_odds_age_seconds=300,
    )
    assert report.status is TheRundownQualificationStatus.FAILED
    assert report.global_failure_codes == (
        TheRundownQualificationCode.INVALID_SCHEMA.value,
    )


def test_cli_is_offline_and_returns_machine_readable_gate_result(
    tmp_path, capsys
) -> None:
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(_payload([_evidence("EPL")])), encoding="utf-8")

    exit_code = qualification_main([str(path), "--maximum-odds-age-seconds", "300"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert output["status"] == TheRundownQualificationStatus.PARTIAL_EVIDENCE.value
    assert output["provider_registered"] is False
