"""Regression coverage for the read-only evidence audit CLI."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.top5_shadow_audit import main
from tests.football.test_top5_real_shadow_audit import _complete_payload


def test_session_cli_emits_json_and_markdown_without_writing(
    tmp_path: Path, capsys
) -> None:
    payload, evidence = _complete_payload()
    session_path = tmp_path / "session.json"
    evidence_path = tmp_path / "evidence.json"
    session_path.write_text(json.dumps(payload))
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
    assert (
        "READ ONLY | NO NETWORK | NO BET | NO PUBLICATION | NO PRODUCTION ACTIVATION"
        in output
    )
    assert '"overall_state": "COMPLETE"' in output
    assert session_path.read_text() == json.dumps(payload)


def test_prediction_cli_refuses_to_infer_lifecycle(tmp_path: Path, capsys) -> None:
    payload, _ = _complete_payload()
    path = tmp_path / "prediction.json"
    path.write_text(json.dumps(payload["predictions"][0]))

    exit_code = main(["prediction", str(path), "--format", "json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["overall_state"] == "INCOMPLETE"
    assert output["predictions"][0]["completeness"]["AUDIT_COMPLETE"] is False


def test_directory_cli_has_stable_order_and_summary(tmp_path: Path, capsys) -> None:
    payload, evidence = _complete_payload()
    (tmp_path / "session.json").write_text(json.dumps(payload))
    (tmp_path / "evidence.json").write_text(json.dumps(evidence, default=dict))

    exit_code = main(["directory", str(tmp_path), "--format", "json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["summary"]["total_predictions"] == 1
    assert output["deterministic_audit_digest"]
