"""F7: Sport-getrennter Schedule-Merge in write_signals_json."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import src.notifications.web_dashboard as wd


def _write_existing(path: Path, schedule: list[dict]) -> None:
    path.write_text(json.dumps({"schedule": schedule, "football": [], "tennis": []}))


def test_tennis_scan_preserves_football_schedule(tmp_path, monkeypatch):
    """tennis_scan übergibt schedule=[] (leer) → football-Einträge bleiben erhalten."""
    sig_path = tmp_path / "signals_philip.json"
    _write_existing(sig_path, [
        {"sport": "football", "home": "Bayern", "away": "Dortmund", "kickoff": ""},
        {"sport": "football", "home": "Arsenal", "away": "Chelsea", "kickoff": ""},
    ])
    monkeypatch.setattr(wd, "_DEFAULT_USER", "philip")
    monkeypatch.setattr(wd, "ROOT", tmp_path.parent)
    # patch path so it writes to tmp_path
    (tmp_path.parent / "docs" / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path.parent / "docs" / "data" / "signals_philip.json").write_text(
        sig_path.read_text()
    )
    # Simulate tennis_scan: no football, tennis schedule only (empty list here)
    wd.write_signals_json(schedule=[], user="philip")

    out = json.loads((tmp_path.parent / "docs" / "data" / "signals_philip.json").read_text())
    sports = {g.get("sport", "football") for g in out.get("schedule", [])}
    assert "football" in sports, "Football schedule entries must be preserved"


def test_football_scan_preserves_tennis_schedule(tmp_path, monkeypatch):
    """daily_scan übergibt football schedule → tennis-Einträge bleiben erhalten."""
    (tmp_path.parent / "docs" / "data").mkdir(parents=True, exist_ok=True)
    sig_path = tmp_path.parent / "docs" / "data" / "signals_philip.json"
    _write_existing(sig_path, [
        {"sport": "tennis", "home": "Alcaraz", "away": "Sinner", "kickoff": ""},
    ])
    monkeypatch.setattr(wd, "_DEFAULT_USER", "philip")
    monkeypatch.setattr(wd, "ROOT", tmp_path.parent)

    football_schedule = [
        {"sport": "football", "home": "Bayern", "away": "Dortmund", "kickoff": ""},
    ]
    wd.write_signals_json(schedule=football_schedule, user="philip")

    out = json.loads(sig_path.read_text())
    sports = {g.get("sport", "football") for g in out.get("schedule", [])}
    assert "tennis" in sports, "Tennis schedule entries must be preserved"
    assert "football" in sports, "Football schedule entries must be present"


def test_tennis_schedule_replaces_old_tennis(tmp_path, monkeypatch):
    """Tennis-Einträge aus vorherigem Scan werden durch neue tennis-Einträge ersetzt."""
    (tmp_path.parent / "docs" / "data").mkdir(parents=True, exist_ok=True)
    sig_path = tmp_path.parent / "docs" / "data" / "signals_philip.json"
    _write_existing(sig_path, [
        {"sport": "tennis", "home": "Old", "away": "Player", "kickoff": ""},
    ])
    monkeypatch.setattr(wd, "_DEFAULT_USER", "philip")
    monkeypatch.setattr(wd, "ROOT", tmp_path.parent)

    new_tennis = [{"sport": "tennis", "home": "Alcaraz", "away": "Sinner", "kickoff": ""}]
    wd.write_signals_json(schedule=new_tennis, user="philip")

    out = json.loads(sig_path.read_text())
    tennis_entries = [g for g in out.get("schedule", []) if g.get("sport") == "tennis"]
    assert len(tennis_entries) == 1
    assert tennis_entries[0]["home"] == "Alcaraz"
