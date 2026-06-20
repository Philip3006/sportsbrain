"""Tests for tennis data fetching."""
from unittest.mock import patch, MagicMock
import io
import pandas as pd
import pytest

from src.data.tennis_data import (
    fetch_atp_matches, fetch_wta_matches, fetch_matches,
    grass_matches, wimbledon_matches, _KEEP_COLS,
)

# Minimales CSV für Mocks
_MINI_CSV = """tourney_date,tourney_name,tourney_level,surface,winner_name,loser_name,score,round,winner_rank,loser_rank
20240701,Wimbledon,G,Grass,Carlos Alcaraz,Novak Djokovic,6-2 6-2,F,3,2
20240110,Australian Open,G,Hard,Jannik Sinner,Daniil Medvedev,3-6 3-6 6-4 6-4 6-3,F,4,3
20240601,Roland Garros,G,Clay,Carlos Alcaraz,Alexander Zverev,6-3 2-6 5-7 6-1 6-2,F,3,4
"""

def _mock_resp(text: str):
    r = MagicMock()
    r.ok = True
    r.text = text
    return r


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Redirect DATA_CACHE to a temp dir so tests never corrupt the real cache."""
    monkeypatch.setattr("src.data.cache.DATA_CACHE", tmp_path)


def test_grass_matches_filters_surface():
    df = pd.DataFrame({
        "surface": ["grass", "clay", "hard", "grass"],
        "tourney_date": pd.to_datetime(["2024-01-01"]*4),
        "winner_name": ["A","B","C","D"],
        "loser_name": ["E","F","G","H"],
    })
    result = grass_matches(df)
    assert list(result["surface"].unique()) == ["grass"]
    assert len(result) == 2


def test_wimbledon_matches_filters_tournament():
    df = pd.DataFrame({
        "tourney_name": ["Wimbledon", "Roland Garros", "The Championships Wimbledon"],
        "tourney_date": pd.to_datetime(["2024-01-01"]*3),
        "surface": ["grass","clay","grass"],
    })
    result = wimbledon_matches(df)
    assert len(result) == 2
    assert all("Wimbledon" in n for n in result["tourney_name"])


@patch("src.data.tennis_data.retry_request")
def test_fetch_atp_returns_dataframe(mock_get):
    mock_get.return_value = _mock_resp(_MINI_CSV)
    df = fetch_atp_matches(force=True)
    assert isinstance(df, pd.DataFrame)
    assert "winner_name" in df.columns
    assert "surface" in df.columns
    assert len(df) > 0


@patch("src.data.tennis_data.retry_request")
def test_fetch_wta_returns_dataframe(mock_get):
    mock_get.return_value = _mock_resp(_MINI_CSV)
    df = fetch_wta_matches(force=True)
    assert isinstance(df, pd.DataFrame)
    assert "winner_name" in df.columns
    assert len(df) > 0


@patch("src.data.tennis_data.retry_request")
def test_fetch_matches_routes_to_wta(mock_get):
    mock_get.return_value = _mock_resp(_MINI_CSV)
    df = fetch_matches("wta", force=True)
    called_url = mock_get.call_args[0][1]
    assert "tennis_wta" in called_url


@patch("src.data.tennis_data.retry_request")
def test_fetch_matches_routes_to_atp(mock_get):
    mock_get.return_value = _mock_resp(_MINI_CSV)
    df = fetch_matches("atp", force=True)
    called_url = mock_get.call_args[0][1]
    assert "tennis_atp" in called_url


@patch("src.data.tennis_data.retry_request")
def test_surface_normalized_to_lowercase(mock_get):
    mock_get.return_value = _mock_resp(_MINI_CSV)
    df = fetch_atp_matches(force=True)
    assert all(s == s.lower() for s in df["surface"].dropna())
