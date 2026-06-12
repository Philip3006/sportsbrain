"""
Tests for yellow-card/suspension tracking in squad_availability.py.
All file I/O is patched — no real suspensions.json needed.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.data.squad_availability import (
    PlayerStatus,
    SquadReport,
    _apply_suspension_overlay_to_statuses,
    get_suspended_players,
    load_suspensions,
    squad_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_player(name: str, status: str = "fit") -> PlayerStatus:
    avail = 1.0 if status == "fit" else 0.0
    return PlayerStatus(
        name=name,
        position="MID",
        availability=avail,
        status=status,
        key_player=True,
        p_plays=avail,
    )


def _suspensions_file_content(data: dict) -> str:
    return json.dumps(data)


# ---------------------------------------------------------------------------
# load_suspensions
# ---------------------------------------------------------------------------

class TestLoadSuspensions:

    def test_returns_empty_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.data.squad_availability._SUSPENSIONS_FILE",
            tmp_path / "nonexistent.json",
        )
        assert load_suspensions() == {}

    def test_returns_empty_on_invalid_json(self, tmp_path, monkeypatch):
        bad_file = tmp_path / "suspensions.json"
        bad_file.write_text("not valid json {{{")
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", bad_file)
        assert load_suspensions() == {}

    def test_filters_comment_keys(self, tmp_path, monkeypatch):
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({
            "_comment": "ignore me",
            "_format": "also ignore",
            "Brazil": ["Rodrygo"],
        }))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)
        result = load_suspensions()
        assert "_comment" not in result
        assert "_format" not in result
        assert "Brazil" in result

    def test_returns_all_non_comment_teams(self, tmp_path, monkeypatch):
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({
            "_comment": "...",
            "Germany": ["Musiala"],
            "France": ["Tchouameni", "Kante"],
        }))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)
        result = load_suspensions()
        assert result == {"Germany": ["Musiala"], "France": ["Tchouameni", "Kante"]}

    def test_returns_empty_dict_for_empty_suspensions_file(self, tmp_path, monkeypatch):
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({"_comment": "no suspensions yet"}))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)
        result = load_suspensions()
        assert result == {}


# ---------------------------------------------------------------------------
# get_suspended_players
# ---------------------------------------------------------------------------

class TestGetSuspendedPlayers:

    def test_exact_match(self, tmp_path, monkeypatch):
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({"Brazil": ["Rodrygo"]}))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)
        assert get_suspended_players("Brazil") == ["Rodrygo"]

    def test_case_insensitive_match(self, tmp_path, monkeypatch):
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({"brazil": ["Rodrygo"]}))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)
        assert get_suspended_players("Brazil") == ["Rodrygo"]

    def test_returns_empty_for_unknown_team(self, tmp_path, monkeypatch):
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({"Germany": ["Musiala"]}))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)
        assert get_suspended_players("Argentina") == []

    def test_returns_empty_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.data.squad_availability._SUSPENSIONS_FILE",
            tmp_path / "missing.json",
        )
        assert get_suspended_players("Brazil") == []

    def test_multiple_suspended_players(self, tmp_path, monkeypatch):
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({
            "France": ["Tchouameni", "Kante", "Camavinga"],
        }))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)
        result = get_suspended_players("France")
        assert len(result) == 3
        assert "Kante" in result


# ---------------------------------------------------------------------------
# _apply_suspension_overlay_to_statuses
# ---------------------------------------------------------------------------

class TestApplySuspensionOverlay:

    def test_marks_matching_player_as_suspended(self, tmp_path, monkeypatch):
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({"Brazil": ["Rodrygo"]}))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)

        players = [_make_player("Rodrygo Goes"), _make_player("Vinicius Junior")]
        updated, count = _apply_suspension_overlay_to_statuses(players, "Brazil")

        assert count == 1
        rodrygo = next(p for p in updated if "Rodrygo" in p.name)
        assert rodrygo.status == "suspended"
        assert rodrygo.availability == 0.0

    def test_non_matching_player_stays_fit(self, tmp_path, monkeypatch):
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({"Brazil": ["Rodrygo"]}))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)

        players = [_make_player("Rodrygo Goes"), _make_player("Vinicius Junior")]
        updated, _ = _apply_suspension_overlay_to_statuses(players, "Brazil")

        vini = next(p for p in updated if "Vinicius" in p.name)
        assert vini.status == "fit"
        assert vini.availability == 1.0

    def test_no_suspensions_returns_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.data.squad_availability._SUSPENSIONS_FILE",
            tmp_path / "missing.json",
        )
        players = [_make_player("Musiala"), _make_player("Kimmich")]
        updated, count = _apply_suspension_overlay_to_statuses(players, "Germany")
        assert count == 0
        assert all(p.status == "fit" for p in updated)

    def test_partial_name_match_suspension_name_in_player_name(self, tmp_path, monkeypatch):
        """Suspension entry 'Musiala' matches player 'Jamal Musiala'."""
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({"Germany": ["Musiala"]}))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)

        players = [_make_player("Jamal Musiala"), _make_player("Joshua Kimmich")]
        updated, count = _apply_suspension_overlay_to_statuses(players, "Germany")

        assert count == 1
        musiala = next(p for p in updated if "Musiala" in p.name)
        assert musiala.status == "suspended"

    def test_count_reflects_number_of_suspensions_applied(self, tmp_path, monkeypatch):
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({"France": ["Tchouameni", "Kante"]}))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)

        players = [
            _make_player("Aurelien Tchouameni"),
            _make_player("N'Golo Kante"),
            _make_player("Kylian Mbappe"),
        ]
        _, count = _apply_suspension_overlay_to_statuses(players, "France")
        assert count == 2


# ---------------------------------------------------------------------------
# squad_report integration — suspended_count field
# ---------------------------------------------------------------------------

class TestSquadReportSuspendedCount:

    @patch("src.data.squad_availability._fetch_wikipedia_squad")
    @patch("src.data.squad_availability._fetch_wc_squads_page")
    @patch("src.data.squad_availability._fetch_covers_squad")
    @patch("src.data.squad_availability.fetch_transfermarkt_squad")
    def test_suspended_count_in_tm_report(self, mock_tm, mock_covers, mock_wc, mock_wiki, tmp_path, monkeypatch):
        monkeypatch.setattr("src.data.squad_availability._CACHE_DIR", tmp_path / "squad")
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({"Germany": ["Musiala"]}))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)

        mock_covers.return_value = []
        mock_tm.return_value = [
            _make_player("Jamal Musiala"),
            _make_player("Joshua Kimmich"),
        ]
        mock_wc.return_value = []
        mock_wiki.return_value = []

        report = squad_report("Germany", pd.Timestamp("2026-07-10"))
        assert report.data_source == "transfermarkt"
        assert report.suspended_count == 1

    @patch("src.data.squad_availability._fetch_wikipedia_squad")
    @patch("src.data.squad_availability._fetch_wc_squads_page")
    @patch("src.data.squad_availability._fetch_covers_squad")
    @patch("src.data.squad_availability.fetch_transfermarkt_squad")
    def test_suspended_count_in_wiki_report(self, mock_tm, mock_covers, mock_wc, mock_wiki, tmp_path, monkeypatch):
        monkeypatch.setattr("src.data.squad_availability._CACHE_DIR", tmp_path / "squad")
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({"Brazil": ["Rodrygo"]}))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)

        mock_covers.return_value = []
        mock_tm.return_value = []
        mock_wc.return_value = []
        mock_wiki.return_value = [
            _make_player("Rodrygo Goes"),
            _make_player("Vinicius Junior"),
        ]

        report = squad_report("Brazil", pd.Timestamp("2026-07-10"))
        assert report.data_source == "wikipedia"
        assert report.suspended_count == 1

    @patch("src.data.squad_availability._fetch_wikipedia_squad")
    @patch("src.data.squad_availability._fetch_wc_squads_page")
    @patch("src.data.squad_availability._fetch_covers_squad")
    @patch("src.data.squad_availability.fetch_transfermarkt_squad")
    def test_suspended_count_zero_when_no_suspensions(
        self, mock_tm, mock_covers, mock_wc, mock_wiki, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("src.data.squad_availability._CACHE_DIR", tmp_path / "squad")
        monkeypatch.setattr(
            "src.data.squad_availability._SUSPENSIONS_FILE",
            tmp_path / "missing.json",
        )
        mock_covers.return_value = []
        mock_tm.return_value = [_make_player("Kimmich")]
        mock_wc.return_value = []
        mock_wiki.return_value = []

        report = squad_report("Germany", pd.Timestamp("2026-07-10"))
        assert report.suspended_count == 0

    @patch("src.data.squad_availability._fetch_wikipedia_squad")
    @patch("src.data.squad_availability._fetch_wc_squads_page")
    @patch("src.data.squad_availability._fetch_covers_squad")
    @patch("src.data.squad_availability.fetch_transfermarkt_squad")
    def test_suspended_count_in_default_report(self, mock_tm, mock_covers, mock_wc, mock_wiki, tmp_path, monkeypatch):
        monkeypatch.setattr("src.data.squad_availability._CACHE_DIR", tmp_path / "squad")
        f = tmp_path / "suspensions.json"
        f.write_text(json.dumps({"Argentina": ["Messi", "De Paul"]}))
        monkeypatch.setattr("src.data.squad_availability._SUSPENSIONS_FILE", f)

        mock_covers.return_value = []
        mock_tm.return_value = []
        mock_wc.return_value = []
        mock_wiki.return_value = []

        report = squad_report("Argentina", pd.Timestamp("2026-07-10"))
        assert report.data_source == "default"
        assert report.suspended_count == 2
