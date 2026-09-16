"""Regression coverage for the experiment-matrix CLI."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.top5_shadow_experiment_matrix import main
from tests.football.test_top5_shadow_experiment_matrix import _measurement


def test_matrix_cli_emits_json_and_markdown_without_writing(
    tmp_path: Path, capsys
) -> None:
    measurement = _measurement()
    path = tmp_path / "measurement.json"
    original = json.dumps(measurement)
    path.write_text(original)

    exit_code = main(["directory", str(tmp_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Top-5 Shadow Experiment Evidence Matrix V1" in output
    assert '"matrix_schema": "top5-shadow-experiment-evidence-matrix-v1"' in output
    assert "NO PRODUCTION ACTIVATION" in output
    assert path.read_text() == original


def test_matrix_cli_rejects_malformed_digest(tmp_path: Path, capsys) -> None:
    measurement = _measurement()
    measurement["measurement_digest"] = "0" * 64
    (tmp_path / "measurement.json").write_text(json.dumps(measurement))

    exit_code = main(["directory", str(tmp_path), "--format", "json"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["overall_state"] == "FAILED_CLOSED"
    assert report["cohort_integrity"]["state"] == "FAILED_CLOSED"
