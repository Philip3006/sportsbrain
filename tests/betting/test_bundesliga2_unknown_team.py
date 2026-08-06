"""Tests für Unknown-Team-Gate in BL2-Scanner."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _make_universe(team: str, seasons: int) -> dict:
    return {team: {"seasons_played": [f"season_{i}" for i in range(seasons)], "last_season": "2526"}}


def test_unknown_team_gate_blocks_signal() -> None:
    """Team mit <5 Matches (0 Saisons) → kein Signal."""
    from scripts.bundesliga2_scan import _scan_match, _team_matches_in_universe

    home = "Unbekannter FC"
    away = "Hamburg"
    universe = _make_universe(home, 0)  # 0 Saisons = 0 matches
    universe.update(_make_universe(away, 3))

    assert _team_matches_in_universe(home, universe) == 0

    mock_params = MagicMock()
    mock_params.attack = {"Hamburg": 1.0}
    mock_elo = {"Hamburg": 1500.0}

    match = {"match_id": "test_1", "home_team": home, "away_team": away, "bookmakers": []}
    signals, meta = _scan_match(match, mock_params, mock_elo, universe, 100.0, 0.06)

    assert signals == []
    assert meta.get("skip") == "unknown_team_gate"


def test_known_team_passes_gate() -> None:
    """Team mit ≥1 Saison (≥34 Matches äquivalent) → Gate passiert."""
    from scripts.bundesliga2_scan import _team_matches_in_universe

    universe = _make_universe("Hamburg", 2)  # 2 Saisons = 68 Matches
    assert _team_matches_in_universe("Hamburg", universe) >= 5


def test_elo_default_not_used_when_unknown(monkeypatch) -> None:
    """Wenn Team nicht im Elo-Dict → ELO_DEFAULT 1500, aber Unknown-Gate fängt es vorher ab."""
    from scripts.bundesliga2_scan import _scan_match
    from src.models.elo import ELO_DEFAULT

    home = "Aufsteiger FC"
    away = "Hamburg"
    universe = _make_universe(home, 0)  # blocked by gate
    universe.update(_make_universe(away, 3))

    mock_params = MagicMock()
    mock_params.attack = {home: 1.0, away: 1.0}
    mock_elo = {}  # Aufsteiger nicht im Elo

    match = {"match_id": "test_2", "home_team": home, "away_team": away, "bookmakers": []}
    signals, meta = _scan_match(match, mock_params, mock_elo, universe, 100.0, 0.06)

    # Gate blockt bevor Elo-Default in EV-Rechnung fließt
    assert signals == []
    assert meta.get("skip") == "unknown_team_gate"


def test_universe_matches_count_per_season() -> None:
    """Universe zählt ~34 Matches pro Saison."""
    from scripts.bundesliga2_scan import _team_matches_in_universe

    universe = _make_universe("Schalke 04", 1)
    count = _team_matches_in_universe("Schalke 04", universe)
    assert count == 34  # 1 Saison × 34

    universe2 = _make_universe("Schalke 04", 3)
    assert _team_matches_in_universe("Schalke 04", universe2) == 102
