"""Contract tests for the non-financial local aggregate-health carrier."""
from __future__ import annotations

import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "aggregate_health_cron.sh"
PLIST = ROOT / "launchd" / "com.sportsbrain.aggregate-health.plist"


def test_aggregate_health_carrier_is_non_financial_and_truthful() -> None:
    source = SCRIPT.read_text()

    assert "python3 -m src.monitoring.aggregate_health --quiet --no-write --no-upload" in source
    assert 'health_start "aggregate_health"' in source
    assert 'health_finish "aggregate_health" "$EXIT_CODE"' in source
    assert 'exit "$EXIT_CODE"' in source

    forbidden = ("consume_pending_bets", "ledger", "pending-bet", "_kv_delete")
    assert not any(token in source for token in forbidden)


def test_local_carrier_never_publishes_from_the_active_checkout() -> None:
    source = SCRIPT.read_text()

    assert "--no-write" in source
    assert "--no-upload" in source


def test_aggregate_health_plist_matches_canonical_local_cadence() -> None:
    with PLIST.open("rb") as f:
        plist = plistlib.load(f)

    assert plist["Label"] == "com.sportsbrain.aggregate-health"
    assert plist["ProgramArguments"] == [
        "/bin/bash",
        "/Users/philiprassillier/sportsbrain/scripts/aggregate_health_cron.sh",
    ]
    assert plist["WorkingDirectory"] == "/Users/philiprassillier/sportsbrain"
    assert plist["StartInterval"] == 120
    assert plist["RunAtLoad"] is False
    assert set(plist["EnvironmentVariables"]) == {"PATH"}
