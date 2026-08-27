"""Static regressions for GitHub Actions health-writer exit-code truth."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
UNSAFE_EXIT_EXPRESSION = "job.status == 'success' && 0 || 1"
FIXED_WORKFLOWS = (
    "tennis_scan.yml",
    "tennis_settle.yml",
    "tennis_closing_odds.yml",
    "tennis_lgbm_retrain.yml",
    "tennis_elo_refresh.yml",
    "bundesliga2_settle.yml",
)


def test_active_workflows_do_not_use_falsy_zero_exit_expression():
    offenders = [
        path.name
        for path in WORKFLOWS.glob("*.yml")
        if UNSAFE_EXIT_EXPRESSION in path.read_text()
    ]
    assert offenders == []


def test_fixed_health_writers_map_success_to_zero_and_non_success_to_one():
    expected = (
        'EXIT_CODE=1\n'
        '          if [ "${{ job.status }}" = "success" ]; then\n'
        '            EXIT_CODE=0\n'
        "          fi"
    )
    for filename in FIXED_WORKFLOWS:
        text = (WORKFLOWS / filename).read_text()
        assert expected in text, filename
        assert '--exit-code "$EXIT_CODE"' in text, filename
