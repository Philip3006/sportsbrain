from datetime import datetime, timezone

import pytest

from src.football.provider_cascade.shadow_diagnostic import (
    DEFAULT_REPLAY_AS_OF,
    DiagnosticCode,
    DiagnosticPolicy,
    QualityStatus,
    ShadowDiagnosticError,
    SourceFixture,
    compare_champions_league_sources,
    deterministic_champions_league_replay,
    run_deterministic_champions_league_shadow,
    run_offline_champions_league_shadow,
)


def test_deterministic_replay_reports_source_disagreement_and_data_quality() -> None:
    report = run_deterministic_champions_league_shadow()

    assert report.status == "diagnostic_findings"
    assert report.event_count == 2
    assert report.source_input_count == 4
    assert report.source_evaluated_count == 4
    assert report.eligible_source_count == 1
    assert {item.field for item in report.disagreements} >= {
        "participants",
        "regulation_1x2",
        "bookmaker_coverage",
        "freshness",
    }
    assert DiagnosticCode.REGULATION_1X2_INCOMPLETE.value in report.issue_counts
    assert DiagnosticCode.STALE_SOURCE.value in report.issue_counts
    assert DiagnosticCode.PROVENANCE_INVALID.value in report.issue_counts


def test_report_is_repeatable_and_declares_offline_safety_boundary() -> None:
    first = run_deterministic_champions_league_shadow().as_payload()
    second = run_deterministic_champions_league_shadow().as_payload()

    assert first == second
    assert first["offline_replay"] is True
    assert first["source_kind"] == "OFFLINE_REPLAY"
    assert first["network_called"] is False
    assert first["routing_authority"] == "none"
    assert first["selected_source"] is None
    assert first["counts_as_real"] is False
    assert first["no_bet"] is True
    assert first["publication_enabled"] is False
    assert first["activation_state"] == "disabled"


def test_source_mapping_accepts_provider_shaped_aliases_and_checks_complete_1x2() -> None:
    events, _ = deterministic_champions_league_replay()
    row = {
        "source": "mapped-source",
        "fixture_key": events[0].fixture_key,
        "provider_event_id": "mapped-event",
        "league": "soccer_uefa_champs_league",
        "home": {"id": "club-alpha", "name": "Club Alpha"},
        "away": {"id": "club-beta", "name": "Club Beta"},
        "kickoff_utc": "2026-09-19T20:00:00Z",
        "h2h": {"1": 2.0, "x": 3.5, "2": 4.0},
        "bookmakers": [{"key": "Bet365"}],
        "last_update": "2026-09-19T17:55:00Z",
        "captured_at": "2026-09-19T18:00:00Z",
        "adapter": "mapped-v1",
        "request_id": "mapped-request",
        "raw_response_digest": "a" * 64,
        "source_provenance": "local-replay:mapped",
    }
    report = run_offline_champions_league_shadow(
        events[:1],
        [row],
        as_of=DEFAULT_REPLAY_AS_OF,
    )

    quality = report.source_quality[0]
    assert quality.event_identity is QualityStatus.MATCH
    assert quality.participant_mapping is QualityStatus.MATCH
    assert quality.regulation_1x2_complete is True
    assert quality.bookmakers == ("bet365",)
    assert quality.fresh is True
    assert quality.provenance_complete is True
    assert quality.eligible is True
    assert report.issue_counts == {}


def test_missing_timestamp_and_provenance_fail_closed_without_raising() -> None:
    events, _ = deterministic_champions_league_replay()
    source = SourceFixture(
        source="incomplete",
        fixture_key=events[0].fixture_key,
        source_event_id="event",
        competition="ucl",
        home_participant_id="club-alpha",
        home_name="Club Alpha",
        away_participant_id="club-beta",
        away_name="Club Beta",
        kickoff=events[0].kickoff,
        regulation_1x2={"home": 2.0, "draw": 3.0, "away": 4.0},
        bookmakers=("bet365",),
    )
    report = run_offline_champions_league_shadow(
        events[:1],
        [source],
        as_of=DEFAULT_REPLAY_AS_OF,
    )

    quality = report.source_quality[0]
    assert quality.eligible is False
    assert DiagnosticCode.SOURCE_TIMESTAMP_MISSING.value in report.issue_counts
    assert DiagnosticCode.CAPTURE_TIMESTAMP_MISSING.value in report.issue_counts
    assert DiagnosticCode.PROVENANCE_MISSING.value in report.issue_counts
    assert DiagnosticCode.FRESHNESS_MISSING.value in report.issue_counts


def test_source_and_event_bounds_are_reported_deterministically() -> None:
    events, sources = deterministic_champions_league_replay()
    report = run_offline_champions_league_shadow(
        events,
        sources + tuple(sources[0] for _ in range(4)),
        as_of=DEFAULT_REPLAY_AS_OF,
        max_sources_per_event=2,
        max_issue_samples=1,
    )

    assert report.truncated is True
    assert report.source_input_count == 8
    assert report.source_evaluated_count == 3
    assert report.issue_counts[DiagnosticCode.SOURCE_LIMIT_EXCEEDED.value] == 1
    assert len(report.issue_samples) == 1


def test_policy_requires_explicit_timezone_aware_replay_time() -> None:
    with pytest.raises(ShadowDiagnosticError, match="timezone-aware"):
        DiagnosticPolicy(datetime(2026, 9, 19, 18))


def test_fixed_replay_inputs_are_not_mutated_by_comparison() -> None:
    events, sources = deterministic_champions_league_replay()
    before = tuple(source.as_payload() for source in sources)

    compare_champions_league_sources(
        events,
        sources,
        policy=DiagnosticPolicy(as_of=DEFAULT_REPLAY_AS_OF),
    )

    after = tuple(source.as_payload() for source in sources)
    assert before == after
