from __future__ import annotations

import json

from src.learning.migration import preview_jsonl


def test_migration_preview_reports_unconvertible_duplicates_and_conflicts(tmp_path):
    path = tmp_path / "history.jsonl"
    valid = {
        "source_record_id": "signal-1",
        "fixture_id": "fixture-1",
        "sport": "football",
        "competition": "EPL",
        "model_family": "model",
        "model_release_id": "release",
        "prediction_timestamp": "2026-10-01T12:00:00Z",
        "feature_cutoff": "2026-10-01T11:55:00Z",
        "market": "1X2",
        "selection": "HOME",
    }
    path.write_text(
        "\n".join(
            [
                json.dumps(valid),
                json.dumps(valid),
                json.dumps({**valid, "selection": "AWAY"}),
                json.dumps({"fixture_id": "legacy", "sport": "football"}),
                "{bad json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    preview = preview_jsonl(path, source_system="generic_football")
    assert preview.records_seen == 5
    assert preview.convertible_records == 1
    assert preview.duplicates == 1
    assert preview.conflicts == 1
    assert preview.malformed_or_unconvertible == 2
    assert path.read_bytes() == before


def test_already_represented_ids_are_measurement_only(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(
        json.dumps(
            {
                "source_record_id": "signal-1",
                "fixture_id": "fixture-1",
                "sport": "football",
                "competition": "EPL",
                "model_family": "model",
                "model_release_id": "release",
                "prediction_timestamp": "2026-10-01T12:00:00Z",
                "feature_cutoff": "2026-10-01T11:55:00Z",
                "market": "1X2",
                "selection": "HOME",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    preview = preview_jsonl(
        path,
        source_system="generic_football",
        already_represented_ids=("not-the-prediction-id",),
    )
    assert preview.convertible_records == 1
    assert preview.already_represented == 0
