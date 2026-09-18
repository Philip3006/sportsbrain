import json
from pathlib import Path

import pytest

from src.football.champions_league_research import (
    PartitionSpec,
    ResearchReadinessError,
    audit_feature_timestamps,
    build_candidate_artifact_manifest,
    canonical_season,
    evaluate_predictions,
    partition_matches,
)


def test_default_partitions_are_disjoint_and_chronological():
    spec = PartitionSpec.default()
    groups = [spec.development, spec.calibration, spec.final, spec.shadow]
    assert len({season for group in groups for season in group}) == sum(map(len, groups))
    assert max(spec.development) < min(spec.calibration)
    assert max(spec.calibration) < min(spec.final)
    assert max(spec.final) < min(spec.shadow)


def test_season_canonicalization_and_unknown_rows_fail_closed():
    assert canonical_season("2022/2023") == "2022-23"
    with pytest.raises(ResearchReadinessError, match="outside the sealed"):
        partition_matches([{"season": "2026-27"}])


def test_readiness_report_requires_kickoff_safe_match_schema():
    from src.football.champions_league_research import build_readiness_report

    with pytest.raises(ResearchReadinessError, match="missing required fields"):
        build_readiness_report([{"season": "2024-25"}])
    with pytest.raises(ResearchReadinessError, match="timezone-aware kickoff"):
        build_readiness_report([{
            "season": "2024-25",
            "kickoff_at": "2025-01-01",
            "home_team": "A",
            "away_team": "B",
            "home_score": 1,
            "away_score": 0,
        }])


def test_partition_assignment_is_deterministic():
    rows = [
        {"match_id": "b", "season": "2024-25", "kickoff_at": "2025-01-02T18:00:00Z"},
        {"match_id": "a", "season": "2024-25", "kickoff_at": "2025-01-01T18:00:00Z"},
    ]
    result = partition_matches(rows)
    assert [row["match_id"] for row in result["final"]] == ["a", "b"]
    assert all(row["partition"] == "final" for row in result["final"])


def test_timestamp_audit_blocks_future_equal_and_date_only_sources():
    findings = audit_feature_timestamps(
        [
            {
                "feature_id": "future",
                "source_id": "source-a",
                "source_timestamp": "2025-01-01T12:00:00Z",
                "cutoff_timestamp": "2025-01-01T12:00:00Z",
            },
            {
                "feature_id": "date-only",
                "source_id": "source-b",
                "source_timestamp": "2025-01-01",
                "cutoff_timestamp": "2025-01-01T12:00:00Z",
            },
            {
                "feature_id": "safe",
                "source_id": "source-c",
                "source_timestamp": "2025-01-01T11:59:00Z",
                "cutoff_timestamp": "2025-01-01T12:00:00Z",
            },
        ]
    )
    assert findings["status"] == "BLOCKED"
    assert findings["blocked_count"] == 2
    assert [item["status"] for item in findings["findings"]] == ["BLOCKED", "BLOCKED", "PASS"]


def test_timestamp_audit_can_expose_date_only_as_conditional_in_non_strict_mode():
    result = audit_feature_timestamps(
        [{
            "feature_id": "date-only",
            "source_timestamp": "2025-01-01",
            "cutoff_timestamp": "2025-01-01T12:00:00Z",
        }],
        strict=False,
    )
    assert result["status"] == "BLOCKED"
    assert result["conditional_count"] == 1
    assert result["findings"][0]["status"] == "CONDITIONAL"


def test_metrics_report_log_loss_and_calibration_by_season():
    report = evaluate_predictions([
        {
            "candidate_id": "uniform_prior_v1",
            "season": "2022-23",
            "outcome": "home",
            "p_away": 1 / 3,
            "p_draw": 1 / 3,
            "p_home": 1 / 3,
        },
        {
            "candidate_id": "uniform_prior_v1",
            "season": "2022-23",
            "outcome": "draw",
            "p_away": 1 / 3,
            "p_draw": 1 / 3,
            "p_home": 1 / 3,
        },
    ])
    row = report["metrics_by_season"][0]
    assert report["status"] == "OK"
    assert row["n"] == 2
    assert row["log_loss"] == pytest.approx(1.0986122886681098)
    assert row["expected_calibration_error"] == pytest.approx(1 / 3)
    assert row["selection_eligible"] is True


def test_manifest_is_deterministic_and_matches_committed_artifact():
    root = Path(__file__).resolve().parents[2]
    first = build_candidate_artifact_manifest(root)
    second = build_candidate_artifact_manifest(root)
    assert first == second
    committed = json.loads((root / "docs/champions_league_candidate_manifest.json").read_text())
    assert first == committed
    assert first["safety"] == {
        "financial_data_accessed": False,
        "network_accessed": False,
        "production_mutated": False,
    }
    assert all(
        not any(token in item["path"].lower() for token in (".env", "private", "production"))
        for candidate in first["candidates"]
        for item in candidate["paths"]
    )
