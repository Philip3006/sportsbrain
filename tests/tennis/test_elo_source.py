"""Tests für src/tennis/elo_source.py (Roadmap J2-I)."""
from __future__ import annotations

import pandas as pd
import pytest

from src.tennis import elo_source


@pytest.fixture
def fake_xlsx():
    return pd.DataFrame({
        "Date": pd.to_datetime(["2024-06-01", "2024-06-15"]),
        "Tournament": ["Stuttgart", "Halle"],
        "Winner": ["Alcaraz C.", "Sinner J."],
        "Loser": ["Zverev A.", "Medvedev D."],
        "WRank": [3, 1],
        "LRank": [4, 2],
        "Round": ["F", "F"],
        "surface_std": ["grass", "grass"],
        "tour": ["ATP", "ATP"],
    })


def test_xlsx_to_sackmann_renames(fake_xlsx):
    out = elo_source._xlsx_to_sackmann(fake_xlsx)
    assert "tourney_date" in out.columns
    assert "winner_name" in out.columns
    assert "loser_name" in out.columns
    assert out["surface"].iloc[0] == "Grass"
    assert out["tourney_level"].iloc[0] == "A"
    assert len(out) == 2


def test_xlsx_to_sackmann_empty():
    assert elo_source._xlsx_to_sackmann(pd.DataFrame()).empty


def test_load_match_history_uses_sackmann_when_available(monkeypatch, fake_xlsx):
    monkeypatch.setattr(elo_source, "fetch_atp_matches",
                        lambda: pd.DataFrame({"tourney_date": ["2024-01-01"], "winner_name": ["X"]}))
    monkeypatch.setattr(elo_source, "fetch_wta_matches", lambda: pd.DataFrame())
    monkeypatch.setattr(elo_source, "fetch_full_tour_odds",
                        lambda **kw: pytest.fail("XLSX must not be called"))
    df, source = elo_source.load_match_history()
    assert source == "sackmann"
    assert not df.empty


def test_load_match_history_falls_back_to_xlsx(monkeypatch, fake_xlsx):
    monkeypatch.setattr(elo_source, "fetch_atp_matches", lambda: pd.DataFrame())
    monkeypatch.setattr(elo_source, "fetch_wta_matches", lambda: pd.DataFrame())
    monkeypatch.setattr(elo_source, "fetch_full_tour_odds", lambda **kw: fake_xlsx)
    df, source = elo_source.load_match_history()
    assert source == "xlsx-fallback"
    assert "winner_name" in df.columns
    assert len(df) == 2


def test_load_match_history_empty_when_all_fail(monkeypatch):
    monkeypatch.setattr(elo_source, "fetch_atp_matches", lambda: pd.DataFrame())
    monkeypatch.setattr(elo_source, "fetch_wta_matches", lambda: pd.DataFrame())
    monkeypatch.setattr(elo_source, "fetch_full_tour_odds", lambda **kw: pd.DataFrame())
    df, source = elo_source.load_match_history()
    assert source == "empty"
    assert df.empty


def test_load_match_history_swallows_sackmann_exception(monkeypatch, fake_xlsx):
    def _raise(*a, **kw): raise RuntimeError("404")
    monkeypatch.setattr(elo_source, "fetch_atp_matches", _raise)
    monkeypatch.setattr(elo_source, "fetch_wta_matches", _raise)
    monkeypatch.setattr(elo_source, "fetch_full_tour_odds", lambda **kw: fake_xlsx)
    df, source = elo_source.load_match_history()
    assert source == "xlsx-fallback"
    assert len(df) == 2
