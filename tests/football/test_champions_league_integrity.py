from datetime import datetime, timedelta, timezone

import pytest

from src.football.champions_league_contracts import (
    ChampionsLeagueIntegrityError,
    ChampionsLeagueIntegrityPolicy,
    IntegrityCode,
)
from src.football.champions_league_integrity import (
    assert_champions_league_integrity,
    audit_champions_league_inputs,
)
from src.football.champions_league_replay import (
    build_champions_league_observability,
    build_champions_league_replay_coverage,
)


UTC = timezone.utc
AS_OF = datetime(2026, 9, 18, 12, tzinfo=UTC)
KICKOFF = AS_OF + timedelta(hours=2)
FIXTURE_KEY = "ucl:2026:fixture-001"


def _policy(**overrides: object) -> ChampionsLeagueIntegrityPolicy:
    values = {
        "as_of": AS_OF,
        "max_odds_age_seconds": 3600,
        "max_cache_age_seconds": 7200,
        "require_results": True,
    }
    values.update(overrides)
    return ChampionsLeagueIntegrityPolicy(**values)


def _fixture(**overrides: object) -> dict[str, object]:
    row = {
        "fixture_key": FIXTURE_KEY,
        "league_code": "soccer_uefa_champs_league",
        "home_team": "Paris Saint-Germain",
        "away_team": "Bayern Munich",
        "kickoff": KICKOFF.isoformat(),
    }
    row.update(overrides)
    return row


def _result(**overrides: object) -> dict[str, object]:
    row = {
        "fixture_key": FIXTURE_KEY,
        "league": "ucl",
        "home_team": "Paris Saint-Germain",
        "away_team": "Bayern Munich",
        "status": "final",
        "result_timestamp": (KICKOFF + timedelta(hours=2)).isoformat(),
        "home_score": 2,
        "away_score": 1,
    }
    row.update(overrides)
    return row


def _odds(**overrides: object) -> dict[str, object]:
    row = {
        "fixture_key": FIXTURE_KEY,
        "league": "ucl",
        "home_team": "Paris Saint-Germain",
        "away_team": "Bayern Munich",
        "kickoff": KICKOFF.isoformat(),
        "bookmaker_identity": "bookmaker-a",
        "provider": "synthetic-ucl-feed",
        "source_timestamp": (AS_OF - timedelta(minutes=5)).isoformat(),
        "captured_at": AS_OF.isoformat(),
        "market_phase": "pre_match",
        "odds": {"home": 2.2, "draw": 3.4, "away": 3.1},
    }
    row.update(overrides)
    return row


def _features(**overrides: object) -> dict[str, object]:
    row = {
        "fixture_key": FIXTURE_KEY,
        "league": "ucl",
        "home_team": "Paris Saint-Germain",
        "away_team": "Bayern Munich",
        "generated_at": (AS_OF - timedelta(minutes=10)).isoformat(),
        "features": {"home_form": 0.5, "away_form": 0.4},
    }
    row.update(overrides)
    return row


def _serializer(**overrides: object) -> dict[str, object]:
    row = {
        "prediction_id": "ucl-prediction-001",
        "fixture_key": FIXTURE_KEY,
        "league": "ucl",
        "home_team": "Paris Saint-Germain",
        "away_team": "Bayern Munich",
        "kickoff": KICKOFF.isoformat(),
        "snapshot_id": "ucl-snapshot-001",
        "generated_at": (AS_OF - timedelta(minutes=8)).isoformat(),
        "probabilities": {"home": 0.45, "draw": 0.25, "away": 0.30},
        "no_bet": True,
        "publication_enabled": False,
    }
    row.update(overrides)
    return row


def _cache(**overrides: object) -> dict[str, object]:
    row = {
        "fixture_key": FIXTURE_KEY,
        "league": "ucl",
        "cache_updated_at": (AS_OF - timedelta(minutes=20)).isoformat(),
    }
    row.update(overrides)
    return row


def _all_inputs() -> dict[str, object]:
    return {
        "results": [_result()],
        "odds": [_odds()],
        "features": [_features()],
        "serializer_inputs": [_serializer()],
        "cache_records": [_cache()],
    }


def test_valid_ucl_inventory_is_ready_and_deterministic():
    inputs = _all_inputs()
    first = audit_champions_league_inputs([_fixture()], policy=_policy(), **inputs)
    second = audit_champions_league_inputs([_fixture()], policy=_policy(), **inputs)

    assert first.valid
    assert first.as_payload() == second.as_payload()
    assert first.inventory == {
        "fixture_inputs": 1,
        "result_inputs": 1,
        "odds_inputs": 1,
        "feature_inputs": 1,
        "serializer_inputs": 1,
        "cache_inputs": 1,
        "timestamp_inputs": 7,
        "team_identity_inputs": 2,
    }
    assert first.as_payload()["activation_state"] == "disabled"


def test_duplicate_fixture_and_missing_attachments_are_counted():
    report = audit_champions_league_inputs(
        [_fixture(), _fixture()],
        policy=_policy(),
    )

    assert report.issue_counts[IntegrityCode.DUPLICATE_FIXTURE.value] == 1
    assert report.issue_counts[IntegrityCode.MISSING_RESULT.value] == 1
    assert report.issue_counts[IntegrityCode.MISSING_ODDS.value] == 1
    assert report.issue_counts[IntegrityCode.MISSING_FEATURE.value] == 1
    assert report.issue_counts[IntegrityCode.SERIALIZER_INPUT_MISMATCH.value] == 1


@pytest.mark.parametrize(
    ("source", "row", "code"),
    [
        ("results", _result(home_team="Real Madrid"), IntegrityCode.ENTITY_MISMATCH),
        ("odds", _odds(kickoff=(KICKOFF + timedelta(minutes=5)).isoformat()), IntegrityCode.KICKOFF_MISMATCH),
        ("features", _features(generated_at="2026-09-18T10:00:00"), IntegrityCode.TIMEZONE_MISMATCH),
    ],
)
def test_entity_kickoff_and_timezone_mismatches_fail_closed(source, row, code):
    inputs = _all_inputs()
    inputs[source] = [row]
    report = audit_champions_league_inputs([_fixture()], policy=_policy(), **inputs)
    assert report.issue_counts[code.value] >= 1
    assert not report.observability_ready


def test_result_status_normalization_is_case_insensitive():
    report = audit_champions_league_inputs(
        [_fixture()],
        results=[
            _result(
                status="FINAL",
                result_timestamp=(KICKOFF - timedelta(minutes=1)).isoformat(),
            )
        ],
        policy=_policy(require_odds=False, require_features=False, require_serializer_inputs=False),
    )

    assert report.issue_counts[IntegrityCode.KICKOFF_MISMATCH.value] == 1


def test_stale_odds_missing_bookmaker_stale_cache_and_post_kickoff_are_visible():
    report = audit_champions_league_inputs(
        [_fixture()],
        results=[_result()],
        odds=[
            _odds(
                bookmaker_identity="",
                source_timestamp=(AS_OF - timedelta(hours=2)).isoformat(),
                captured_at=(KICKOFF + timedelta(minutes=1)).isoformat(),
                market_phase="closing",
            )
        ],
        features=[_features()],
        serializer_inputs=[_serializer()],
        cache_records=[_cache(cache_updated_at=(AS_OF - timedelta(hours=3)).isoformat())],
        policy=_policy(max_odds_age_seconds=1800, max_cache_age_seconds=1800),
    )

    assert report.issue_counts[IntegrityCode.STALE_ODDS.value] == 1
    assert report.issue_counts[IntegrityCode.MISSING_BOOKMAKER_PROVENANCE.value] == 1
    assert report.issue_counts[IntegrityCode.STALE_CACHE.value] == 1
    assert report.issue_counts[IntegrityCode.POST_KICKOFF_CONTAMINATION.value] >= 1


def test_malformed_odds_and_feature_values_report_without_throwing():
    report = audit_champions_league_inputs(
        [_fixture()],
        results=[_result()],
        odds=[_odds(odds={"home": "not-a-number", "draw": 3.4})],
        features=[_features(features={"home_form": float("nan")})],
        serializer_inputs=[_serializer()],
        cache_records=[_cache()],
        policy=_policy(),
    )
    assert report.issue_counts[IntegrityCode.MISSING_ODDS.value] == 1
    assert report.issue_counts[IntegrityCode.MISSING_FEATURE.value] == 1


def test_issue_samples_are_bounded_but_counts_are_complete():
    fixtures = [_fixture(fixture_key=f"ucl:2026:{index}") for index in range(6)]
    report = audit_champions_league_inputs(
        fixtures,
        policy=_policy(max_issue_samples=2),
    )
    assert len(report.issues) == 2
    assert report.issue_counts[IntegrityCode.MISSING_RESULT.value] == 6
    assert sum(report.issue_counts.values()) > len(report.issues)


def test_bounded_issue_sample_is_independent_of_input_order():
    fixtures = [_fixture(fixture_key=f"ucl:2026:{index}") for index in range(6)]
    policy = _policy(max_issue_samples=2)
    first = audit_champions_league_inputs(fixtures, policy=policy)
    second = audit_champions_league_inputs(list(reversed(fixtures)), policy=policy)

    assert first.as_payload() == second.as_payload()


def test_coverage_uses_complete_invalid_fixture_accounting_when_samples_are_bounded():
    valid = _fixture(fixture_key="ucl:2026:valid")
    invalid = _fixture(fixture_key="ucl:2026:invalid")
    inputs = _all_inputs()
    inputs = {
        source: [dict(row, fixture_key="ucl:2026:valid") for row in rows]
        for source, rows in inputs.items()
    }
    coverage, report = build_champions_league_replay_coverage(
        [valid, invalid],
        **inputs,
        policy=_policy(max_issue_samples=1),
    )

    assert coverage.legitimate_replay_inputs == 1
    assert coverage.rejected_replay_inputs == 1
    assert "ucl:2026:invalid" in report.invalid_fixture_keys


def test_replay_coverage_and_observability_remain_offline_no_bet():
    coverage, report = build_champions_league_replay_coverage(
        [_fixture()],
        **_all_inputs(),
        policy=_policy(),
    )
    payload = build_champions_league_observability(report, coverage)

    assert coverage.integrity_status == "ready"
    assert coverage.candidate_fixtures == 1
    assert coverage.legitimate_replay_inputs == 1
    assert coverage.performance_eligible_observations == 1
    assert payload["offline_replay"] is True
    assert payload["counts_as_real"] is False
    assert payload["no_bet"] is True
    assert payload["publication_enabled"] is False


def test_replay_input_coverage_is_separate_from_result_coverage():
    inputs = _all_inputs()
    inputs.pop("results")
    coverage, report = build_champions_league_replay_coverage(
        [_fixture()], policy=_policy(), **inputs
    )

    assert coverage.legitimate_replay_inputs == 1
    assert coverage.results_attached == 0
    assert coverage.performance_eligible_observations == 0
    assert report.issue_counts[IntegrityCode.MISSING_RESULT.value] == 1


def test_nested_serializer_envelope_counts_as_attached_serializer_input():
    inputs = _all_inputs()
    inputs["serializer_inputs"] = [
        {"prediction_artifact": inputs["serializer_inputs"][0]}
    ]

    coverage, report = build_champions_league_replay_coverage(
        [_fixture()], policy=_policy(), **inputs
    )

    assert report.valid
    assert coverage.serializer_inputs_attached == 1
    assert coverage.legitimate_replay_inputs == 1


def test_assertion_helper_raises_on_integrity_failure():
    with pytest.raises(ChampionsLeagueIntegrityError, match="missing_"):
        assert_champions_league_integrity(
            [_fixture()],
            policy=_policy(),
        )
