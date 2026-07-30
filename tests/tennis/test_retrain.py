"""Tests fuer scripts/tennis_retrain.py (Roadmap TENNIS P1.6)."""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _mock_matches() -> pd.DataFrame:
    """Tiny synthetic match history for fast tests."""
    return pd.DataFrame([
        {
            "tourney_date": pd.Timestamp("2024-01-15"),
            "tourney_name": "Test", "tourney_level": "A",
            "surface": "Hard",
            "winner_name": "Alcaraz C.", "loser_name": "Zverev A.",
            "score": "6-3 6-4", "round": "F",
            "winner_rank": 1.0, "loser_rank": 5.0,
        },
        {
            "tourney_date": pd.Timestamp("2024-05-20"),
            "tourney_name": "Roland Garros", "tourney_level": "G",
            "surface": "Clay",
            "winner_name": "Alcaraz C.", "loser_name": "Sinner J.",
            "score": "6-4 3-6 6-2 7-5", "round": "F",
            "winner_rank": 1.0, "loser_rank": 2.0,
        },
    ])


def test_retrain_creates_snapshot_and_meta(tmp_path, monkeypatch):
    """Retrain-Script erzeugt elo_snapshot.pkl + elo_meta.json."""
    from scripts import tennis_retrain

    # Redirect model paths to tmp
    monkeypatch.setattr(tennis_retrain, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(tennis_retrain, "SNAPSHOT_PATH", tmp_path / "elo_snapshot.pkl")
    monkeypatch.setattr(tennis_retrain, "META_PATH", tmp_path / "elo_meta.json")

    # Mock load_match_history
    monkeypatch.setattr(
        tennis_retrain, "load_match_history",
        lambda: (_mock_matches(), "test-source"),
    )

    # sys.argv without dry-run
    monkeypatch.setattr(sys, "argv", ["tennis_retrain.py"])
    rc = tennis_retrain.main()
    assert rc == 0

    snap = tmp_path / "elo_snapshot.pkl"
    meta = tmp_path / "elo_meta.json"
    assert snap.exists()
    assert meta.exists()

    ratings = pickle.load(snap.open("rb"))
    assert "Alcaraz C." in ratings.overall
    assert ratings.overall["Alcaraz C."] > 1500  # increased from default after 2 wins

    m = json.loads(meta.read_text())
    assert m["source"] == "test-source"
    assert m["n_matches"] == 2
    assert m["n_players_overall"] >= 3
    assert len(m["top20_overall"]) >= 1
    assert m["top20_overall"][0]["name"] == "Alcaraz C."


def test_retrain_dry_run_no_files_written(tmp_path, monkeypatch):
    from scripts import tennis_retrain
    monkeypatch.setattr(tennis_retrain, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(tennis_retrain, "SNAPSHOT_PATH", tmp_path / "elo_snapshot.pkl")
    monkeypatch.setattr(tennis_retrain, "META_PATH", tmp_path / "elo_meta.json")
    monkeypatch.setattr(
        tennis_retrain, "load_match_history",
        lambda: (_mock_matches(), "test"),
    )
    monkeypatch.setattr(sys, "argv", ["tennis_retrain.py", "--dry-run"])
    rc = tennis_retrain.main()
    assert rc == 0
    assert not (tmp_path / "elo_snapshot.pkl").exists()
    assert not (tmp_path / "elo_meta.json").exists()


def test_retrain_empty_matches_aborts(tmp_path, monkeypatch):
    from scripts import tennis_retrain
    monkeypatch.setattr(tennis_retrain, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(
        tennis_retrain, "load_match_history",
        lambda: (pd.DataFrame(), "empty"),
    )
    monkeypatch.setattr(sys, "argv", ["tennis_retrain.py"])
    rc = tennis_retrain.main()
    assert rc == 1


def test_meta_json_structure():
    """Live-Snapshot muss vollstaendige Meta enthalten."""
    root = Path(__file__).parent.parent.parent
    meta_path = root / "models" / "tennis" / "elo_meta.json"
    if not meta_path.exists():
        # Not yet generated in this env - skip
        return
    m = json.loads(meta_path.read_text())
    for key in ("generated_at", "source", "reference_date", "n_matches",
                "n_players_overall", "top20_overall"):
        assert key in m, f"Missing meta key {key}"
    assert m["n_matches"] > 0
    assert len(m["top20_overall"]) > 0
