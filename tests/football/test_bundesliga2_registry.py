"""LEAGUE_REGISTRY-Contracts für 2.BL — verhindert stille Regressionen
bei Sport-Key, Registry-Feldern, Active-Window."""
from datetime import date

from src.config import LEAGUE_REGISTRY, league_config, active_leagues


def test_bl2_registered_with_correct_sport_key():
    """Der korrekte TheOddsAPI-Sport-Key ist soccer_germany_bundesliga2
    (nicht soccer_germany_2_bundesliga — der lieferte 404)."""
    assert "soccer_germany_bundesliga2" in LEAGUE_REGISTRY
    assert "soccer_germany_2_bundesliga" not in LEAGUE_REGISTRY


def test_bl2_registry_has_required_fields():
    cfg = league_config("soccer_germany_bundesliga2")
    assert cfg is not None
    for field in ("name", "short", "sport", "start_date", "end_date",
                  "result_source", "fbdata_code", "min_edge", "elo_k", "category_mode"):
        assert field in cfg, f"missing field {field} in bl2 registry"
    assert cfg["short"] == "bl2"
    assert cfg["sport"] == "football"
    assert cfg["result_source"] == "football_data"
    assert cfg["fbdata_code"] == "D2"


def test_bl2_active_at_season_start():
    """Spieltag 1 laut Registry: ab 2026-08-01. Test-Datum: mid-Saison."""
    active = active_leagues(date(2026, 10, 15))
    assert "soccer_germany_bundesliga2" in active


def test_bl2_inactive_off_season():
    """Off-Season (Juni/Juli): 2.BL nicht aktiv."""
    active = active_leagues(date(2026, 6, 20))
    assert "soccer_germany_bundesliga2" not in active


def test_wm_still_registered_and_bounded():
    """Regression-Guard: WM darf nicht aus Registry entfernt worden sein."""
    cfg = league_config("soccer_fifa_world_cup")
    assert cfg is not None
    assert cfg["short"] == "wm2026"
    # WM 2026 endete am 19.07.2026
    active_july = active_leagues(date(2026, 7, 15))
    active_aug = active_leagues(date(2026, 8, 15))
    assert "soccer_fifa_world_cup" in active_july
    assert "soccer_fifa_world_cup" not in active_aug
