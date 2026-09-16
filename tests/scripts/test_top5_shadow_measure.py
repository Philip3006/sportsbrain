"""Regression coverage for the measurement-pack CLI."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.top5_shadow_measure import main
from tests.football.test_top5_real_shadow_measurement import _complete_session


def test_session_cli_emits_deterministic_json_and_markdown_without_writing(
    tmp_path: Path, capsys
) -> None:
    payload, evidence = _complete_session()
    session_path = tmp_path / "session.json"
    evidence_path = tmp_path / "evidence.json"
    original = json.dumps(payload)
    session_path.write_text(original)
    evidence_path.write_text(json.dumps(evidence, default=dict))

    exit_code = main(
        [
            "session",
            str(session_path),
            "--evidence",
            str(evidence_path),
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "READ ONLY | NO NETWORK | NO BET | NO PUBLICATION" in output
    assert '"eligible_count": 1' in output
    assert "BENCHMARK / CLV MEASUREMENT ONLY" in output
    assert session_path.read_text() == original


def test_directory_cli_emits_json_only(tmp_path: Path, capsys) -> None:
    payload, evidence = _complete_session()
    (tmp_path / "session.json").write_text(json.dumps(payload))
    (tmp_path / "evidence.json").write_text(json.dumps(evidence, default=dict))

    exit_code = main(["directory", str(tmp_path), "--format", "json"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["eligible_count"] == 1
    assert report["measurement_digest"]
    assert "_candidates" not in report
