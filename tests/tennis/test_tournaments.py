"""Tests für src/tennis/tournaments.py — Registry-Konsistenz + Lookup."""
from __future__ import annotations

from src.config import TENNIS_CATEGORY_MODE, TENNIS_MIN_EDGE_BY_CATEGORY
from src.tennis.tournaments import (
    TENNIS_REGISTRY,
    Tournament,
    all_sport_keys,
    get_tournament,
    tournaments_by_category,
    tournaments_for_month,
    unknown_sport_key,
)


def test_registry_minimum_size():
    """Registry muss alle Slams + Masters + WTA 1000 + relevante 500/250 enthalten."""
    assert len(TENNIS_REGISTRY) >= 40, f"Registry zu klein: {len(TENNIS_REGISTRY)}"


def test_registry_has_all_grand_slams():
    slams = tournaments_by_category("grand_slam")
    # 4 Slams × 2 Tours = 8
    assert len(slams) == 8, f"Erwarte 8 Slam-Einträge, gefunden {len(slams)}"
    slam_names = {t.name for t in slams}
    assert slam_names == {"Australian Open", "French Open", "Wimbledon", "US Open"}


def test_registry_has_nine_atp_masters():
    masters = tournaments_by_category("m1000")
    assert len(masters) == 9, f"ATP Masters 1000 sind 9 Events, gefunden {len(masters)}"
    assert all(t.tour == "atp" for t in masters)


def test_wta_1000_present():
    wta1k = tournaments_by_category("wta1000")
    # 4 mandatory + Cincinnati + China + Wuhan = mind. 5
    assert len(wta1k) >= 5
    assert all(t.tour == "wta" for t in wta1k)


def test_atp_slams_are_bo5_wta_bo3():
    for t in tournaments_by_category("grand_slam"):
        if t.tour == "atp":
            assert t.best_of == 5, f"{t.slug} ATP-Slam muss BO5 sein"
        else:
            assert t.best_of == 3, f"{t.slug} WTA-Slam muss BO3 sein"


def test_non_slams_are_bo3():
    for t in TENNIS_REGISTRY:
        if not t.is_grand_slam:
            assert t.best_of == 3, f"{t.slug} ({t.category}) muss BO3 sein"


def test_sport_keys_unique():
    """Kein sport_key darf zwei Tournaments zugeordnet sein (sonst Lookup-Konflikt)."""
    seen: dict[str, str] = {}
    for t in TENNIS_REGISTRY:
        for key in t.sport_keys:
            assert key not in seen, f"sport_key {key} doppelt: {seen[key]} vs {t.slug}"
            seen[key] = t.slug


def test_slugs_unique():
    slugs = [t.slug for t in TENNIS_REGISTRY]
    assert len(slugs) == len(set(slugs)), "Slug-Duplikate in Registry"


def test_get_tournament_by_slug():
    t = get_tournament("wimbledon_atp")
    assert t is not None
    assert t.tour == "atp"
    assert t.surface == "grass"
    assert t.best_of == 5


def test_get_tournament_by_sport_key():
    t = get_tournament("tennis_atp_wimbledon")
    assert t is not None
    assert t.slug == "wimbledon_atp"

    # WTA-Variante
    t2 = get_tournament("tennis_wta_wimbledon")
    assert t2 is not None
    assert t2.slug == "wimbledon_wta"


def test_get_tournament_unknown_returns_none():
    assert get_tournament("not_a_real_slug") is None


def test_tournaments_for_month_wimbledon_july():
    july = tournaments_for_month(7)
    july_slugs = {t.slug for t in july}
    assert "wimbledon_atp" in july_slugs
    assert "wimbledon_wta" in july_slugs


def test_tournaments_for_month_atp_filter():
    july_atp = tournaments_for_month(7, tour="atp")
    assert all(t.tour == "atp" for t in july_atp)
    # Wimbledon ATP muss dabei sein
    assert any(t.slug == "wimbledon_atp" for t in july_atp)


def test_all_sport_keys_nonempty():
    keys = all_sport_keys()
    assert len(keys) >= 40
    assert "tennis_atp_wimbledon" in keys
    assert "tennis_wta_wimbledon" in keys


def test_unknown_sport_key_wrap_atp():
    t = unknown_sport_key("tennis_atp_some_new_250")
    assert t.tour == "atp"
    assert t.category == "atp250"
    assert t.best_of == 3
    assert t.surface == "unknown"


def test_unknown_sport_key_wrap_wta():
    t = unknown_sport_key("tennis_wta_new_event")
    assert t.tour == "wta"
    assert t.category == "wta250"


def test_min_edge_by_category_covers_all_categories():
    """Jede Kategorie in Registry muss in MIN_EDGE-Map sein."""
    registry_cats = {t.category for t in TENNIS_REGISTRY}
    config_cats = set(TENNIS_MIN_EDGE_BY_CATEGORY.keys())
    missing = registry_cats - config_cats
    assert not missing, f"Kategorien ohne min_edge-Eintrag: {missing}"


def test_category_mode_covers_all_categories():
    registry_cats = {t.category for t in TENNIS_REGISTRY}
    mode_cats = set(TENNIS_CATEGORY_MODE.keys())
    missing = registry_cats - mode_cats
    assert not missing, f"Kategorien ohne mode-Eintrag: {missing}"


def test_category_mode_values_valid():
    for cat, mode in TENNIS_CATEGORY_MODE.items():
        assert mode in {"live", "shadow", "blacklist"}, f"{cat}: ungültiger Mode {mode}"


def test_min_edge_values_in_sane_range():
    for cat, edge in TENNIS_MIN_EDGE_BY_CATEGORY.items():
        assert 0.0 < edge <= 0.20, f"{cat}: min_edge {edge} außerhalb (0, 0.20]"


def test_tournament_is_frozen():
    """Tournament-Dataclass muss frozen sein (Registry-Immutability)."""
    import dataclasses
    assert getattr(Tournament, "__dataclass_params__").frozen


def test_typical_months_in_valid_range():
    for t in TENNIS_REGISTRY:
        for m in t.typical_months:
            assert 1 <= m <= 12, f"{t.slug}: ungültiger Monat {m}"
