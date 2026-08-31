"""Contract tests for the non-financial local aggregate-health carrier."""
from __future__ import annotations

import plistlib
import sys
from pathlib import Path

from src.monitoring import aggregate_health
from src.monitoring.health_authority import jobs_for_authority

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "aggregate_health_cron.sh"
PLIST = ROOT / "launchd" / "com.sportsbrain.aggregate-health.plist"


def test_aggregate_health_carrier_is_non_financial_and_truthful() -> None:
    source = SCRIPT.read_text()

    assert "python3 -m src.monitoring.aggregate_health --quiet --no-write --authority local" in source
    assert 'health_start "aggregate_health"' in source
    assert 'health_finish "aggregate_health" "$EXIT_CODE"' in source
    assert 'exit "$EXIT_CODE"' in source

    forbidden = ("consume_pending_bets", "ledger", "pending-bet", "_kv_delete")
    assert not any(token in source for token in forbidden)


def test_local_carrier_never_writes_from_the_active_checkout() -> None:
    source = SCRIPT.read_text()

    assert "--no-write" in source
    assert "--no-upload" not in source
    assert "--authority local" in source


def test_local_authority_filter_excludes_cloud_jobs() -> None:
    jobs = [
        {"job": "daily_scan"},
        {"job": "tennis_scan"},
        {"job": "unknown_job"},
    ]

    assert jobs_for_authority(jobs, "local") == [{"job": "daily_scan"}]


def test_local_upload_failure_returns_nonzero(monkeypatch) -> None:
    payload = {"overall": "ok", "jobs": []}
    calls: dict[str, object] = {}

    monkeypatch.setattr(aggregate_health, "aggregate", lambda **_: payload)

    def fail_upload(uploaded: dict, *, authority: str) -> bool:
        calls["payload"] = uploaded
        calls["authority"] = authority
        return False

    monkeypatch.setattr(aggregate_health, "_push_to_cloud", fail_upload)
    monkeypatch.setattr(
        sys,
        "argv",
        ["aggregate_health", "--quiet", "--no-write", "--authority", "local"],
    )

    assert aggregate_health._cli() == 1
    assert calls == {"payload": payload, "authority": "local"}


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
