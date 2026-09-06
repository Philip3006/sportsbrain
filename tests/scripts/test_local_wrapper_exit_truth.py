"""Static shell contracts for truthful local scheduler exit codes."""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIVE_TRIGGER = ROOT / "scripts" / "live_score_trigger.sh"
LIVE_PLIST = ROOT / "launchd" / "com.sportsbrain.live-score-push.plist"
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


def test_live_score_publishes_suspended_cache_only_when_staged() -> None:
    source = LIVE_TRIGGER.read_text()

    assert 'if [ -f "$RUNTIME_STAGE_DIR/data/cache/tennis_suspended.json" ]; then' in source
    assert "PUBLISH_PATHS+=(data/cache/tennis_suspended.json)" in source
    assert '"${PUBLISH_PATHS[@]}"' in source


def test_live_score_launchd_path_loads_the_protected_runtime_environment() -> None:
    with LIVE_PLIST.open("rb") as file:
        plist = plistlib.load(file)

    assert plist["ProgramArguments"] == [
        "/bin/bash",
        "/Users/philiprassillier/sportsbrain/scripts/live_score_trigger.sh",
    ]
    assert plist["RunAtLoad"] is False
    assert set(plist["EnvironmentVariables"]) == {"PATH"}

    source = LIVE_TRIGGER.read_text()
    assert 'set -a\n    . "$SPORTSBRAIN_DIR/.env"\n    set +a' in source
    assert "SPORTSBRAIN_LEDGER_DIR" not in source
    assert "set -x" not in source
    assert "printenv" not in source


def test_missing_ledger_environment_remains_fail_closed(monkeypatch) -> None:
    from src.config import _resolve_ledger_dir

    monkeypatch.delenv("SPORTSBRAIN_LEDGER_DIR", raising=False)

    with pytest.raises(OSError, match="SPORTSBRAIN_LEDGER_DIR is not set"):
        _resolve_ledger_dir()


def test_daily_and_prematch_scan_cannot_hide_settlement_failure() -> None:
    for path, job in ((DAILY_SCAN, "daily_scan"), (PREMATCH_SCAN, "prematch_scan")):
        source = path.read_text()
        assert 'SETTLE_EXIT=$?' in source, path.name
        assert 'if [ "$SETTLE_EXIT" -ne 0 ]; then' in source, path.name
        assert f'health_finish "{job}" "$SETTLE_EXIT"' in source, path.name
        assert 'exit "$SETTLE_EXIT"' in source, path.name
