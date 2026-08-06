"""Pipeline-Guards: D2 CSV-Loader, Team-Universe, ESPN-Mapping."""
import json
from pathlib import Path

import pytest

from src.data.football_data import AVAILABLE_LEAGUES
from src.data.football_live import ESPN_LEAGUE_CODES, canonical_match_key
from src.data.results_router import _season_code


def test_d2_in_available_leagues():
    """D2 muss im football-data-Loader registriert sein."""
    assert "D2" in AVAILABLE_LEAGUES
    assert "German Bundesliga 2" in AVAILABLE_LEAGUES["D2"]


def test_espn_mapping_2bl():
    """ESPN-Code für 2.BL ist ger.2 (verifiziert 2026-08-06)."""
    assert ESPN_LEAGUE_CODES["soccer_germany_bundesliga2"] == "ger.2"


def test_season_code_derivation():
    """Registry.start_date=2026-08-01 → football-data Season-Code = 2627."""
    assert _season_code("2026-08-01") == "2627"
    assert _season_code("2027-08-01") == "2728"
    assert _season_code(None) is None
    assert _season_code("garbage") is None


def test_canonical_match_key_symmetric_on_case():
    """canonical_match_key ist deterministisch für gleiche Team-Paare."""
    a = canonical_match_key("VfL Bochum", "Hertha Berlin")
    b = canonical_match_key("VfL Bochum", "Hertha Berlin")
    assert a == b
    # Case-insensitive
    assert canonical_match_key("VFL BOCHUM", "HERTHA BERLIN") == a


def test_universe_file_present_if_built():
    """Wenn Universe-Datei existiert, muss sie mind. 30 Teams enthalten (10 Saisons)."""
    p = Path(__file__).resolve().parents[2] / "data" / "cache" / "bundesliga2_universe.json"
    if not p.exists():
        pytest.skip("Universe noch nicht gebaut (build_bundesliga2_universe.py)")
    universe = json.loads(p.read_text())
    assert len(universe) >= 30, f"Universe enthält nur {len(universe)} Teams — Erwartung 30+"
