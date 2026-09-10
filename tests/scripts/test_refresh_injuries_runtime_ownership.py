"""Runtime ownership for daily injury and public squads refreshes."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "refresh_injuries.py"


def _load_refresh_module():
    spec = importlib.util.spec_from_file_location("refresh_injuries_runtime_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_refresh_stages_squads_and_keeps_active_state_clean(monkeypatch, tmp_path):
    from src.runtime import paths

    active = tmp_path / "active"
    source_suspensions = active / "data" / "suspensions.json"
    source_squads = active / "docs" / "data" / "squads.json"
    source_suspensions.parent.mkdir(parents=True)
    source_squads.parent.mkdir(parents=True)
    source_suspensions.write_text('{"Sweden": ["Eric Smith"]}\n')
    source_squads.write_text(json.dumps({"teams": {"Sweden": {"players": [
        {"name": "Alexander Isak", "status": "fit"},
    ]}}}))
    original_suspensions = source_suspensions.read_bytes()
    original_squads = source_squads.read_bytes()
    monkeypatch.setattr(paths, "ROOT", active)
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_ARTIFACT_STAGE_DIR", str(tmp_path / "stage"))

    refresh = _load_refresh_module()
    monkeypatch.setattr(refresh, "WM_TEAMS", [("Sweden", "query")])
    monkeypatch.setattr(
        refresh, "search_team_injuries", lambda *_args, **_kwargs: (["Alexander Isak"], [], []),
    )
    monkeypatch.setattr(refresh.time, "sleep", lambda _seconds: None)

    refresh.run()

    runtime_suspensions = tmp_path / "state" / "data" / "suspensions.json"
    staged_squads = tmp_path / "stage" / "docs" / "data" / "squads.json"
    assert source_suspensions.read_bytes() == original_suspensions
    assert source_squads.read_bytes() == original_squads
    assert "Alexander Isak" in json.loads(runtime_suspensions.read_text())["Sweden"]
    assert json.loads(staged_squads.read_text())["teams"]["Sweden"]["players"][0]["status"] == "injured"
