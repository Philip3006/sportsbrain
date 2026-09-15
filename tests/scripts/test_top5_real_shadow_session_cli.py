"""Regression coverage for the provider-neutral future CLI seam."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.top5_real_shadow_session import main
from tests.football.test_top5_real_shadow_session import (
    BASE,
    INTEGRATION_SHA,
    observation,
)


def test_cli_dry_run_builds_without_network_or_storage(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "normalized.json"
    input_path.write_text(json.dumps({"league_scope": ["BL1"], "observations": [observation().as_payload()]}))
    exit_code = main([
        "--observations", str(input_path),
        "--session-key", "shadow-session:cli",
        "--integration-sha", INTEGRATION_SHA,
        "--experiment-id", "shadow-experiment:cli-v1",
        "--created-at", BASE.isoformat(),
        "--min-lead-minutes", "30",
        "--max-lead-minutes", "180",
        "--max-odds-age-seconds", "300",
        "--kickoff-tolerance-seconds", "0",
        "--fixture-mode",
        "--dry-run",
    ])
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["stored"] is None
    assert report["manifest"]["status"] == "AWAITING_RESULTS"
    assert report["manifest"]["fixture_mode"] is True
