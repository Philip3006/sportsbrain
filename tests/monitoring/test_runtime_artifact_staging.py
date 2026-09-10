"""Runtime artifact staging must not write tracked files in the active checkout."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_stages_public_snapshot_and_keeps_cloud_payload(monkeypatch, tmp_path):
    import src.notifications.web_dashboard as dashboard

    active = tmp_path / "active"
    stage = tmp_path / "stage"
    source = active / "docs" / "data" / "signals_philip.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"football": [], "tennis": [], "schedule": []}))
    monkeypatch.setattr(dashboard, "ROOT", active)
    monkeypatch.setattr(dashboard, "_ledger_path_for", lambda _user: tmp_path / "ledger.csv")
    uploaded: dict[str, object] = {}
    monkeypatch.setattr(
        dashboard,
        "upload_signals_to_cloud",
        lambda **kwargs: uploaded.update(kwargs) or True,
    )
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR", str(stage))

    assert dashboard.write_signals_json(football=[], tennis=[], user="philip") is True

    assert json.loads(source.read_text()) == {"football": [], "tennis": [], "schedule": []}
    staged_user = stage / "docs" / "data" / "signals_philip.json"
    staged_legacy = stage / "docs" / "data" / "signals.json"
    assert staged_user.exists()
    assert staged_legacy.exists()
    assert uploaded["user"] == "philip"
    assert isinstance(uploaded["payload"], dict)


def test_runtime_artifact_stage_rejects_the_active_checkout(monkeypatch, tmp_path):
    from src.runtime import paths

    monkeypatch.setattr(paths, "ROOT", tmp_path / "active")
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR", str(tmp_path / "active" / "stage"))

    with pytest.raises(RuntimeError, match="active checkout"):
        paths.runtime_artifact_path("docs/data/signals.json")


def test_runtime_state_seeds_external_copy_without_touching_active(monkeypatch, tmp_path):
    from src.runtime import paths

    active = tmp_path / "active"
    source = active / "data" / "cache" / "history.json"
    source.parent.mkdir(parents=True)
    source.write_text("preserved")
    monkeypatch.setattr(paths, "ROOT", active)
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_STATE_DIR", str(tmp_path / "state"))

    state_path = paths.runtime_state_path("data/cache/history.json")

    assert state_path.read_text() == "preserved"
    assert source.read_text() == "preserved"


def test_provider_budget_migrates_to_external_state_without_mutating_active(monkeypatch, tmp_path):
    from src.runtime import paths
    from src.signals import provider_budget

    active = tmp_path / "active"
    source = active / "data" / "cache" / "provider_budget.json"
    source.parent.mkdir(parents=True)
    original = b'{"the_odds_api":{"circuit_open":true}}\n'
    source.write_bytes(original)
    runtime_state = tmp_path / "runtime-state"

    monkeypatch.setattr(paths, "ROOT", active)
    monkeypatch.setattr(paths, "DEFAULT_RUNTIME_STATE_DIR", runtime_state)
    monkeypatch.delenv("SPORTSBRAIN_RUNTIME_STATE_DIR", raising=False)
    monkeypatch.setattr(provider_budget, "_BUDGET_PATH", None)

    target = provider_budget._budget_path()
    assert target == runtime_state / "data" / "cache" / "provider_budget.json"
    assert target.read_bytes() == original

    provider_budget.record_error("the_odds_api", 429)

    assert source.read_bytes() == original
    assert json.loads(target.read_text())["the_odds_api"]["last_error_code"] == 429


def test_runtime_wrapper_logs_use_stable_external_paths():
    expected = {
        "live_score_trigger.sh": "sportsbrain_live_score_push.log",
        "closing_odds_cron.sh": "sportsbrain_closing_odds.log",
        "prematch_scan_cron.sh": "sportsbrain_prematch_scan.log",
        "scan_cron.sh": "sportsbrain_daily_scan.log",
        "auto_retrain_cron.sh": "sportsbrain_auto_retrain.log",
        "settle_cron.sh": "sportsbrain_settle.log",
    }
    for wrapper, name in expected.items():
        text = (ROOT / "scripts" / wrapper).read_text()
        assert f'/Users/philiprassillier/Library/Logs/{name}' in text

    for plist, name in {
        "com.sportsbrain.live-score-push.plist": "sportsbrain_live_score_push.log",
        "com.sportsbrain.auto-retrain.plist": "sportsbrain_auto_retrain.log",
    }.items():
        text = (ROOT / "launchd" / plist).read_text()
        assert f"/Users/philiprassillier/Library/Logs/{name}" in text
