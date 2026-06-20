"""Tests für src/data/fbref_ppda.py."""
from __future__ import annotations

import json
from pathlib import Path

from src.data import fbref_ppda


def test_get_team_season_ppda_returns_none_without_snapshot(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fbref_ppda, "_SNAPSHOT_PATH", tmp_path / "nope.json")
    assert fbref_ppda.get_team_season_ppda("Germany") is None


def test_write_and_read_snapshot(tmp_path: Path, monkeypatch):
    snap = tmp_path / "fbref_ppda.json"
    monkeypatch.setattr(fbref_ppda, "_SNAPSHOT_PATH", snap)
    fbref_ppda.write_snapshot(
        {"Germany": {"ppda": 9.2, "season": "2025-26", "league": "Bundesliga"}},
        as_of="2026-06-20",
    )
    data = json.loads(snap.read_text())
    assert data["as_of"] == "2026-06-20"
    assert data["teams"]["Germany"]["ppda"] == 9.2
    assert fbref_ppda.get_team_season_ppda("Germany") == 9.2


def test_get_team_season_ppda_handles_invalid_entries(tmp_path: Path, monkeypatch):
    snap = tmp_path / "fbref_ppda.json"
    snap.write_text(json.dumps({
        "as_of": "2026-06-20",
        "teams": {
            "Germany": {"ppda": "not-a-number"},   # invalid → skipped
            "France":  {"ppda": 10.1},
        },
    }))
    monkeypatch.setattr(fbref_ppda, "_SNAPSHOT_PATH", snap)
    assert fbref_ppda.get_team_season_ppda("Germany") is None
    assert fbref_ppda.get_team_season_ppda("France") == 10.1


def test_get_team_season_ppda_handles_corrupt_json(tmp_path: Path, monkeypatch):
    snap = tmp_path / "fbref_ppda.json"
    snap.write_text("{not valid json")
    monkeypatch.setattr(fbref_ppda, "_SNAPSHOT_PATH", snap)
    assert fbref_ppda.get_team_season_ppda("Germany") is None
