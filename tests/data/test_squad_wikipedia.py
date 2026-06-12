"""
Tests for the Wikipedia squad fallback in squad_availability.py.
All HTTP calls are mocked — no network access needed.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.data.squad_availability import (
    _fetch_wikipedia_squad,
    _parse_wikipedia_squad_html,
    squad_report,
)


# ---------------------------------------------------------------------------
# Sample Wikipedia squad HTML (minimal, representative of real WM 2026 pages)
# ---------------------------------------------------------------------------

_SAMPLE_HTML = """
<html><body>
<h2>Squad</h2>
<table class="wikitable">
<tr>
  <th>No.</th><th>Pos.</th><th>Name</th><th>DOB (Age)</th><th>Caps</th><th>Club</th>
</tr>
<tr>
  <td>1</td><td>GK</td><td>Mohammed Al-Owais</td><td>(1991-09-05)5 September 1991 (aged 34)</td>
  <td>82</td><td>Al-Hilal</td>
</tr>
<tr>
  <td>2</td><td>DF</td><td>Saud Abdulhamid</td><td>(1999-07-21)21 July 1999 (aged 26)</td>
  <td>40</td><td>Roma</td>
</tr>
<tr>
  <td>10</td><td>MF</td><td>Sami Al-Najei</td><td>(1997-03-11)11 March 1997 (aged 29)</td>
  <td>52</td><td>Al-Hilal</td>
</tr>
<tr>
  <td>9</td><td>FW</td><td>Saleh Al-Shehri</td><td>(1993-11-01)1 November 1993 (aged 32)</td>
  <td>48</td><td>Al-Hilal</td>
</tr>
</table>
</body></html>
"""

_SAMPLE_HTML_WITH_CAPTAIN = """
<html><body>
<table class="wikitable">
<tr>
  <th>No.</th><th>Pos.</th><th>Name</th><th>DOB (Age)</th><th>Caps</th><th>Club</th>
</tr>
<tr>
  <td>7</td><td>FW</td><td>Mehdi Taremi (c)</td><td>(1992-07-18)18 July 1992 (aged 33)</td>
  <td>97</td><td>Inter Milan</td>
</tr>
<tr>
  <td>1</td><td>GK</td><td>Alireza Beiranvand [1]</td><td>(1992-09-21)21 September 1992 (aged 33)</td>
  <td>64</td><td>Persepolis</td>
</tr>
</table>
</body></html>
"""

_EMPTY_HTML = "<html><body><p>No squad table here.</p></body></html>"

_WRONG_TABLE_HTML = """
<html><body>
<table class="wikitable">
<tr><th>Date</th><th>Opponent</th><th>Result</th></tr>
<tr><td>2026-06-15</td><td>Mexico</td><td>1-2</td></tr>
</table>
</body></html>
"""


# ---------------------------------------------------------------------------
# Unit tests for _parse_wikipedia_squad_html
# ---------------------------------------------------------------------------

class TestParseWikipediaSquadHtml:

    def test_parses_four_players(self):
        players = _parse_wikipedia_squad_html(_SAMPLE_HTML, "Saudi Arabia")
        assert len(players) == 4

    def test_correct_positions(self):
        players = _parse_wikipedia_squad_html(_SAMPLE_HTML, "Saudi Arabia")
        pos_map = {p.name: p.position for p in players}
        assert pos_map["Mohammed Al-Owais"] == "GK"
        assert pos_map["Saud Abdulhamid"] == "DEF"
        assert pos_map["Sami Al-Najei"] == "MID"
        assert pos_map["Saleh Al-Shehri"] == "FWD"

    def test_all_players_available(self):
        players = _parse_wikipedia_squad_html(_SAMPLE_HTML, "Saudi Arabia")
        assert all(p.availability == 1.0 for p in players)
        assert all(p.status == "fit" for p in players)

    def test_all_players_key_player(self):
        players = _parse_wikipedia_squad_html(_SAMPLE_HTML, "Saudi Arabia")
        assert all(p.key_player for p in players)

    def test_strips_captain_marker(self):
        players = _parse_wikipedia_squad_html(_SAMPLE_HTML_WITH_CAPTAIN, "Iran")
        names = [p.name for p in players]
        assert "Mehdi Taremi" in names
        assert any("(c)" not in n for n in names)

    def test_strips_footnote_references(self):
        players = _parse_wikipedia_squad_html(_SAMPLE_HTML_WITH_CAPTAIN, "Iran")
        names = [p.name for p in players]
        assert "Alireza Beiranvand" in names
        assert all("[1]" not in n for n in names)

    def test_returns_empty_for_no_squad_table(self):
        players = _parse_wikipedia_squad_html(_EMPTY_HTML, "SomeTeam")
        assert players == []

    def test_returns_empty_for_wrong_table_structure(self):
        players = _parse_wikipedia_squad_html(_WRONG_TABLE_HTML, "SomeTeam")
        assert players == []


# ---------------------------------------------------------------------------
# Integration tests for _fetch_wikipedia_squad (mocked HTTP)
# ---------------------------------------------------------------------------

class TestFetchWikipediaSquad:

    @patch("src.data.squad_availability.requests.get")
    def test_returns_players_on_200(self, mock_get, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.data.squad_availability._CACHE_DIR", tmp_path / "squad"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _SAMPLE_HTML
        mock_get.return_value = mock_resp

        result = _fetch_wikipedia_squad("Saudi Arabia", pd.Timestamp("2026-06-15"))
        assert len(result) == 4
        assert result[0].status == "fit"

    @patch("src.data.squad_availability.requests.get")
    def test_returns_empty_on_404(self, mock_get, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.data.squad_availability._CACHE_DIR", tmp_path / "squad"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = _fetch_wikipedia_squad("Saudi Arabia", pd.Timestamp("2026-06-15"))
        assert result == []

    @patch("src.data.squad_availability.requests.get")
    def test_returns_empty_on_request_exception(self, mock_get, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.data.squad_availability._CACHE_DIR", tmp_path / "squad"
        )
        mock_get.side_effect = ConnectionError("network unreachable")

        result = _fetch_wikipedia_squad("Iran", pd.Timestamp("2026-06-15"))
        assert result == []

    @patch("src.data.squad_availability.requests.get")
    def test_writes_cache_on_success(self, mock_get, tmp_path, monkeypatch):
        cache_dir = tmp_path / "squad"
        monkeypatch.setattr("src.data.squad_availability._CACHE_DIR", cache_dir)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _SAMPLE_HTML
        mock_get.return_value = mock_resp

        _fetch_wikipedia_squad("Saudi Arabia", pd.Timestamp("2026-06-15"))
        cache_files = list(cache_dir.glob("*_wiki.json"))
        assert len(cache_files) == 1

    @patch("src.data.squad_availability.requests.get")
    def test_uses_cache_when_fresh(self, mock_get, tmp_path, monkeypatch):
        cache_dir = tmp_path / "squad"
        monkeypatch.setattr("src.data.squad_availability._CACHE_DIR", cache_dir)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _SAMPLE_HTML
        mock_get.return_value = mock_resp

        # First call populates cache
        _fetch_wikipedia_squad("Saudi Arabia", pd.Timestamp("2026-06-15"))
        assert mock_get.call_count == 1

        # Second call should use cache — no extra HTTP call
        result = _fetch_wikipedia_squad("Saudi Arabia", pd.Timestamp("2026-06-15"))
        assert mock_get.call_count == 1  # still 1
        assert len(result) == 4

    @patch("src.data.squad_availability.requests.get")
    def test_url_slug_built_correctly(self, mock_get, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.data.squad_availability._CACHE_DIR", tmp_path / "squad"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        _fetch_wikipedia_squad("Saudi Arabia", pd.Timestamp("2026-06-15"))
        called_url = mock_get.call_args[0][0]
        assert "Saudi_Arabia" in called_url
        assert "2026_FIFA_World_Cup" in called_url

    @patch("src.data.squad_availability.requests.get")
    def test_dr_congo_slug_override(self, mock_get, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.data.squad_availability._CACHE_DIR", tmp_path / "squad"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        _fetch_wikipedia_squad("DR Congo", pd.Timestamp("2026-06-15"))
        called_url = mock_get.call_args[0][0]
        assert "DR_Congo" in called_url


# ---------------------------------------------------------------------------
# Integration test: squad_report falls back to Wikipedia when TM is empty
# ---------------------------------------------------------------------------

class TestSquadReportWikipediaFallback:

    @patch("src.data.squad_availability._fetch_wikipedia_squad")
    @patch("src.data.squad_availability._fetch_wc_squads_page")
    @patch("src.data.squad_availability._fetch_covers_squad")
    @patch("src.data.squad_availability.fetch_transfermarkt_squad")
    def test_uses_wikipedia_when_tm_empty(self, mock_tm, mock_covers, mock_wc, mock_wiki, tmp_path, monkeypatch):
        from src.data.squad_availability import PlayerStatus

        monkeypatch.setattr(
            "src.data.squad_availability._CACHE_DIR", tmp_path / "squad"
        )
        mock_tm.return_value = []      # TM blocked
        mock_covers.return_value = []  # covers.com: no injuries
        mock_wc.return_value = []      # WC squads page: empty
        mock_wiki.return_value = [
            PlayerStatus(name="Test Player", position="GK", availability=1.0,
                         status="fit", key_player=True, p_plays=1.0)
        ]

        report = squad_report("Saudi Arabia", pd.Timestamp("2026-06-15"))
        assert report.data_source == "wikipedia"
        assert len(report.players) == 1
        assert report.availability_score == 1.0

    @patch("src.data.squad_availability._fetch_wikipedia_squad")
    @patch("src.data.squad_availability._fetch_covers_squad")
    @patch("src.data.squad_availability.fetch_transfermarkt_squad")
    def test_uses_tm_when_available(self, mock_tm, mock_covers, mock_wiki, tmp_path, monkeypatch):
        from src.data.squad_availability import PlayerStatus

        monkeypatch.setattr(
            "src.data.squad_availability._CACHE_DIR", tmp_path / "squad"
        )
        mock_covers.return_value = []  # no covers data for this test
        mock_tm.return_value = [
            PlayerStatus(name="TM Player", position="FWD", availability=0.0,
                         status="injured", key_player=True, p_plays=0.0)
        ]

        report = squad_report("Germany", pd.Timestamp("2026-06-15"))
        assert report.data_source == "transfermarkt"
        mock_wiki.assert_not_called()

    @patch("src.data.squad_availability._fetch_wikipedia_squad")
    @patch("src.data.squad_availability.fetch_transfermarkt_squad")
    def test_falls_back_to_default_when_both_empty(
        self, mock_tm, mock_wiki, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "src.data.squad_availability._CACHE_DIR", tmp_path / "squad"
        )
        mock_tm.return_value = []
        mock_wiki.return_value = []

        report = squad_report("Unknown FC", pd.Timestamp("2026-06-15"))
        assert report.data_source == "default"
        assert report.availability_score == 1.0


# ---------------------------------------------------------------------------
# _TM_TEAMS coverage: newly added WM 2026 qualifier nations
# ---------------------------------------------------------------------------

class TestTmTeamsCoverage:
    """Regression guard: teams added in Round 9 must stay in _TM_TEAMS."""

    _NEW_TEAMS = {
        "Bosnia and Herzegovina",
        "Sweden",
        "Norway",
        "Haiti",
        "Curacao",
        "Cape Verde",
        "Iraq",
        "Jordan",
        "Czechia",
    }

    def test_all_new_wm2026_teams_present(self):
        from src.data.squad_availability import _TM_TEAMS
        missing = self._NEW_TEAMS - set(_TM_TEAMS)
        assert not missing, f"Teams missing from _TM_TEAMS: {missing}"

    def test_tm_teams_has_slug_and_id(self):
        """Every entry must be a (slug, numeric_id) tuple with non-empty strings."""
        from src.data.squad_availability import _TM_TEAMS
        for team, (slug, team_id) in _TM_TEAMS.items():
            assert slug, f"Empty slug for {team}"
            assert team_id, f"Empty team_id for {team}"
