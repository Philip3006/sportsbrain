from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.champions_league_dataset_audit import main
from src.football.champions_league_dataset_audit import (
    DatasetAuditError,
    audit_rows,
    build_manifest,
    canonical_season,
)


def _row(season: str, *, kickoff: str | None = None, **overrides: object) -> dict[str, object]:
    year = int(season[:4])
    kickoff = kickoff or f"{year}-09-19T19:00:00Z"
    row: dict[str, object] = {
        "season": season,
        "kickoff_at": kickoff,
        "source_timestamp": f"{year}-09-19T12:00:00Z",
        "fixture_id": f"fixture-{year}",
        "result_id": f"result-{year}",
        "home_team": "Alpha FC",
        "away_team": "Beta FC",
        "home_team_id": "team-alpha",
        "away_team_id": "team-beta",
        "home_score": 2,
        "away_score": 1,
    }
    row.update(overrides)
    return row


def _complete_partition_rows() -> list[dict[str, object]]:
    seasons = [f"{year:04d}-{(year + 1) % 100:02d}" for year in range(2015, 2026)]
    return [_row(season, fixture_id=f"fixture-{season}", result_id=f"result-{season}") for season in seasons]


def test_season_canonicalization_is_strict() -> None:
    assert canonical_season("2022/2023") == "2022-23"
    with pytest.raises(DatasetAuditError):
        canonical_season("2022-25")


def test_complete_identity_mapping_and_sealed_partitions() -> None:
    report = audit_rows(_complete_partition_rows())

    assert report["fixture_result_identity"]["status"] == "PASS"
    assert report["fixture_result_identity"]["explicit_fixture_id_coverage"] == 1.0
    assert report["team_mapping"]["status"] == "PASS"
    assert report["timestamp_provenance"]["status"] == "PASS"
    partitions = report["season_partition_feasibility"]["partitions"]
    assert partitions["final"]["selection_eligible"] is False
    assert partitions["final"]["feature_fit_allowed"] is False
    assert partitions["final"]["read_only"] is True
    assert report["season_partition_feasibility"]["final_evaluation_guard"] == {
        "season": "2024-25",
        "locked_for_selection": True,
        "feature_fit_allowed": False,
        "mapping_fit_allowed": False,
        "mutated_by_audit": False,
        "scoring_requires_frozen_candidate": True,
    }


def test_competition_identity_is_explicit_and_rejects_non_cl_rows() -> None:
    missing = audit_rows([_row("2024-25")])
    assert missing["competition_identity"]["status"] == "CONDITIONAL"

    foreign = audit_rows([_row("2024-25", competition="Premier League")])
    assert foreign["competition_identity"]["status"] == "BLOCKED"
    assert foreign["competition_identity"]["unexpected_values"] == ["Premier League"]


def test_fixture_and_result_identity_fail_closed_on_conflict() -> None:
    rows = [_row("2024-25"), _row("2024-25", home_score=0, away_score=0)]
    report = audit_rows(rows)
    identity = report["fixture_result_identity"]
    assert identity["status"] == "BLOCKED"
    assert identity["duplicate_fixture_ids"] == ["fixture-2024"]
    assert identity["duplicate_fixture_keys"]


def test_timestamp_provenance_blocks_date_only_and_future_sources() -> None:
    report = audit_rows([
        _row("2024-25", source_timestamp="2023-09-19"),
        _row("2024-25", fixture_id="fixture-future", source_timestamp="2024-09-19T20:00:00Z"),
    ])
    timestamps = report["timestamp_provenance"]
    assert timestamps["status"] == "BLOCKED"
    assert timestamps["invalid_or_unzoned_source_rows"] == 1
    assert timestamps["same_or_after_kickoff_rows"] == 1


def test_feature_availability_reports_partial_and_missing_point_in_time_features() -> None:
    row = _row("2024-25", home_odds=2.0, draw_odds=3.0, away_odds=4.0, odds_captured_at="2023-09-19T12:00:00Z")
    report = audit_rows([row])
    odds = next(item for item in report["feature_availability"] if item["feature_id"] == "odds_1x2")
    form = next(item for item in report["feature_availability"] if item["feature_id"] == "rolling_form")
    assert odds["status"] == "PASS"
    assert odds["point_in_time_ready"] is True
    assert form["status"] == "UNAVAILABLE"


def test_audit_does_not_mutate_final_rows() -> None:
    rows = _complete_partition_rows()
    before = copy.deepcopy(rows)
    audit_rows(rows)
    assert rows == before


def test_manifest_is_deterministic_and_contains_no_absolute_dataset_path(tmp_path: Path) -> None:
    dataset = tmp_path / "data" / "champions_league" / "matches.json"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(json.dumps({"matches": _complete_partition_rows()}, sort_keys=True), encoding="utf-8")

    first = build_manifest(tmp_path)
    second = build_manifest(tmp_path)

    assert first == second
    assert first["overall_status"] == "CONDITIONAL"
    assert first["datasets"][0]["identity"]["path"] == "data/champions_league/matches.json"
    assert str(tmp_path) not in json.dumps(first)
    assert first["safety"]["final_partition_mutated"] is False


def test_manifest_empty_repository_is_explicitly_blocked(tmp_path: Path) -> None:
    manifest = build_manifest(tmp_path)
    assert manifest["overall_status"] == "BLOCKED_NO_CL_DATA"
    assert manifest["dataset_count"] == 0
    assert manifest["row_count"] == 0
    assert manifest["combined_audit"]["season_partition_feasibility"]["status"] == "BLOCKED"


def test_committed_manifest_is_generated_by_the_local_tool() -> None:
    root = Path(__file__).resolve().parents[2]
    committed = json.loads((root / "docs/champions_league_dataset_readiness_manifest.json").read_text(encoding="utf-8"))
    assert build_manifest(root) == committed


def test_cli_is_local_and_writes_only_requested_output(tmp_path: Path) -> None:
    dataset = tmp_path / "data" / "champions_league" / "matches.json"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(json.dumps([_row("2024-25")]), encoding="utf-8")
    output = tmp_path / "manifest.json"

    assert main(["audit", "--root", str(tmp_path), "--dataset", str(dataset), "--output", str(output)]) == 0
    first = output.read_text(encoding="utf-8")
    assert main(["audit", "--root", str(tmp_path), "--dataset", str(dataset), "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == first
    assert dataset.read_text(encoding="utf-8") == json.dumps([_row("2024-25")])


def test_private_or_external_dataset_paths_are_rejected(tmp_path: Path) -> None:
    private = tmp_path / "private" / "matches.json"
    private.parent.mkdir()
    private.write_text("[]", encoding="utf-8")
    with pytest.raises(DatasetAuditError):
        build_manifest(tmp_path, [private])
