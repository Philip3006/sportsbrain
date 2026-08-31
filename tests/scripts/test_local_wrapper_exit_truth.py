"""Static shell contracts for truthful local scheduler exit codes."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE_TRIGGER = ROOT / "scripts" / "live_score_trigger.sh"
DAILY_SCAN = ROOT / "scripts" / "scan_cron.sh"
PREMATCH_SCAN = ROOT / "scripts" / "prematch_scan_cron.sh"


def test_live_score_producer_failure_is_not_masked_by_output_filter() -> None:
    source = LIVE_TRIGGER.read_text()

    assert 'LIVE_EXIT=${PIPESTATUS[0]}' in source
    assert 'if [ "$LIVE_EXIT" -ne 0 ]; then' in source
    assert 'JOB_EXIT=$LIVE_EXIT' in source
    assert 'grep -v "^$" || true' not in source


def test_live_score_health_uses_the_final_combined_exit_code() -> None:
    source = LIVE_TRIGGER.read_text()

    assert 'health_finish "live_score_push" "$EXIT_CODE"' in source
    assert 'exit "$EXIT_CODE"' in source


def test_daily_and_prematch_scan_cannot_hide_settlement_failure() -> None:
    for path, job in ((DAILY_SCAN, "daily_scan"), (PREMATCH_SCAN, "prematch_scan")):
        source = path.read_text()
        assert 'SETTLE_EXIT=$?' in source, path.name
        assert 'if [ "$SETTLE_EXIT" -ne 0 ]; then' in source, path.name
        assert f'health_finish "{job}" "$SETTLE_EXIT"' in source, path.name
        assert 'exit "$SETTLE_EXIT"' in source, path.name
