"""
CEO-Roadmap O1-1 — TheOddsAPI Sport-Key + fail-safe post-Turnier.

Root Cause (verifiziert 2026-08-09 im lokalen prematch_scan_cron.log):
  "Scores fetch failed: 401 Client Error: Unauthorized for url:
   https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/scores?apiKey=..."

Der historische Sport-Key `soccer_fifa_world_cup` (2022er Ausgabe) wurde
nach Turnier-Ende bei TheOddsAPI deaktiviert. WM 2026 nutzt den Suffix
`_2026`. Zusätzlich muss der Endpunkt nach Turnier-Ende `WC2026_END`
fail-safe [] returnen statt Exception zu werfen.

Diese Tests sichern gegen die Regression, die den 401 erzeugt hat.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestWmSportKeyConstant:
    def test_wm_sport_key_uses_2026_suffix(self):
        from src.config import WM_SPORT_KEY
        assert WM_SPORT_KEY == "soccer_fifa_world_cup_2026", (
            "Legacy 2022er Key soccer_fifa_world_cup liefert seit Turnier-Ende "
            "401. WM 2026 verlangt _2026-Suffix."
        )

    def test_wc2026_end_date_present(self):
        from src.config import WC2026_END
        assert WC2026_END == "2026-07-19"


class TestFetchWmScoresPostSeason:
    """Post-Turnier: kein API-Call, direktes []-Return."""

    def test_returns_empty_post_end_date(self, monkeypatch):
        """Wenn heutiges Datum > WC2026_END: kein API-Call, direktes []."""
        import src.data.odds_api as m
        # Setze WC2026_END künstlich in die Vergangenheit, dann triggert der Guard
        # deterministisch — vermeidet fragiles Patching von datetime.date.
        import src.config
        monkeypatch.setattr(src.config, "WC2026_END", "1970-01-01")
        # ensure no API call happens
        monkeypatch.setattr(m, "get_api_key",
                            lambda k=None: (_ for _ in ()).throw(
                                AssertionError("must not call API post-tournament")))
        assert m.fetch_wm_scores() == []


class TestFetchWmScoresFailSafe401:
    """Wenn Endpunkt aktiv ist aber 401 liefert: [] statt Exception."""

    def test_returns_empty_on_401(self, monkeypatch):
        """401 vom Endpunkt: fail-safe [] statt Exception."""
        import src.data.odds_api as m
        import src.config
        # Aktiv-Turnier simulieren (Guard darf NICHT vor API-Call greifen)
        monkeypatch.setattr(src.config, "WC2026_END", "9999-12-31")
        fake_resp = MagicMock()
        fake_resp.status_code = 401
        monkeypatch.setattr(m, "get_api_key", lambda k=None: "fake_key")
        with patch("scripts._http_retry.retry_request", return_value=fake_resp):
            result = m.fetch_wm_scores()
            assert result == [], "401 muss fail-safe [] returnen (O1-1)"

    def test_returns_empty_on_network_exception(self, monkeypatch):
        """Netzwerk-Ausfall: fail-safe []."""
        import src.data.odds_api as m
        import src.config
        monkeypatch.setattr(src.config, "WC2026_END", "9999-12-31")
        monkeypatch.setattr(m, "get_api_key", lambda k=None: "fake_key")
        with patch("scripts._http_retry.retry_request",
                   side_effect=ConnectionError("network down")):
            assert m.fetch_wm_scores() == []


class TestNoLegacySportKeyReferences:
    """Statische Absicherung: nirgends im Prod-Code darf noch der bare Legacy-Key stehen."""

    def test_no_hardcoded_legacy_key_in_odds_api(self):
        from pathlib import Path
        src = Path(__file__).resolve().parents[2] / "src" / "data" / "odds_api.py"
        content = src.read_text()
        # der bare Legacy-Key darf nicht mehr fest-verdrahtet sein
        # (Suffix `_2026` ist ok — es ist Substring von Legacy nicht)
        bare_occurrences = content.count("soccer_fifa_world_cup\"") \
                          + content.count("soccer_fifa_world_cup/")
        assert bare_occurrences == 0, (
            "O1-1 Regression: bare `soccer_fifa_world_cup` (ohne _2026) "
            f"noch {bare_occurrences}× hart-kodiert. Verwende WM_SPORT_KEY."
        )

    def test_no_hardcoded_legacy_key_in_settle_bets(self):
        from pathlib import Path
        src = Path(__file__).resolve().parents[2] / "scripts" / "settle_bets.py"
        content = src.read_text()
        bare_occurrences = content.count("soccer_fifa_world_cup/scores/\"") \
                          + content.count("soccer_fifa_world_cup/scores?")
        # WM_SPORT_KEY-Konstante ist erlaubt, hart-kodierter Legacy nicht
        assert bare_occurrences == 0, (
            "O1-1 Regression: settle_bets.py hat noch bare Legacy-Sport-Key."
        )


class TestSettleBetsPostTournament:
    """settle_bets.fetch_scores() darf nach WC2026_END keine API-Calls machen."""

    def test_returns_empty_without_api_call_post_wm(self, monkeypatch):
        """Nach WC2026_END: kein API-Call, direktes {} ohne ESPN-Fallback."""
        import scripts.settle_bets as sb
        import src.config
        monkeypatch.setattr(src.config, "WC2026_END", "1970-01-01")
        # Patch requests.Session.request — must NOT be called
        import requests
        monkeypatch.setattr(
            requests.Session, "request",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("API called — post-tournament guard did not fire")),
        )
        result = sb.fetch_scores()
        assert result == {}, f"expected empty dict, got {result}"
        assert sb.LAST_SCORES_SOURCE == "none"
