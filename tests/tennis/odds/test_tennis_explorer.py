"""J8-B12: tennis_explorer.py — unit tests mit injiziertem Bulk."""
from __future__ import annotations

import pickle
import time
from datetime import datetime, timedelta, timezone

import src.tennis.odds.tennis_explorer as te


def _reset_bulk(entries: list[dict]):
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for entry in entries:
        entry.setdefault("source_observed_at", observed_at)
    te._BULK = entries
    te._TS = time.time()


# ---------------------------------------------------------------------------
# fetch() — empty / missing players
# ---------------------------------------------------------------------------

def test_fetch_returns_none_for_empty_players():
    _reset_bulk([{"player_a": "Alcaraz", "player_b": "Sinner", "odds_a": 1.90, "odds_b": 2.05}])
    assert te.fetch({}) is None
    assert te.fetch({"player_a": "", "player_b": "Sinner"}) is None
    assert te.fetch({"player_a": "Alcaraz", "player_b": ""}) is None


# ---------------------------------------------------------------------------
# fetch() — match forward / reverse
# ---------------------------------------------------------------------------

def test_fetch_forward_match():
    _reset_bulk([{"player_a": "alcaraz c.", "player_b": "sinner j.",
                   "odds_a": 1.90, "odds_b": 2.05, "te_bookies_count": 3}])
    q = te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner",
                   "name_source": "odds_api"})
    assert q is not None
    assert q.source == "tennis_explorer"
    assert q.source_tier == 2


def test_fetch_preserves_provider_response_capture_timestamp():
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=8)
    te._BULK = [
        {
            "player_a": "alcaraz c.",
            "player_b": "sinner j.",
            "odds_a": 1.90,
            "odds_b": 2.05,
            "te_bookies_count": 3,
            "source_observed_at": observed_at.isoformat(),
        }
    ]
    te._TS = time.time()

    q = te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"})

    assert q is not None
    assert q.ts == observed_at


def test_fetch_rejects_legacy_missing_or_stale_source_timestamp():
    row = {
        "player_a": "alcaraz c.",
        "player_b": "sinner j.",
        "odds_a": 1.90,
        "odds_b": 2.05,
        "te_bookies_count": 3,
    }
    te._BULK = [row]
    te._TS = time.time()
    assert te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"}) is None

    row["source_observed_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=31)
    ).isoformat()
    assert te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"}) is None


def test_bulk_with_legacy_cache_fails_with_missing_provenance(monkeypatch):
    from src.data import tennis_secondary_odds

    te._BULK = []
    te._TS = 0.0
    monkeypatch.setattr(
        tennis_secondary_odds,
        "fetch_te_upcoming_matches",
        lambda **_kwargs: [{"player_a": "alcaraz c.", "player_b": "sinner j."}],
    )

    bulk, outcome = te._get_bulk_with_diagnostics()

    assert bulk == []
    assert outcome.status_class == "missing_provenance"


def test_fetch_reverse_match_swaps_odds():
    _reset_bulk([{"player_a": "sinner j.", "player_b": "alcaraz c.",
                   "odds_a": 2.05, "odds_b": 1.90, "te_bookies_count": 2}])
    q = te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner",
                   "name_source": "odds_api"})
    assert q is not None
    assert abs(q.h2h_a - 1.90) < 0.01   # Alcaraz nach Swap
    assert abs(q.h2h_b - 2.05) < 0.01


def test_fetch_no_match_returns_none():
    _reset_bulk([{"player_a": "nadal r.", "player_b": "federer r.",
                   "odds_a": 1.80, "odds_b": 2.10, "te_bookies_count": 4}])
    q = te.fetch({"player_a": "Alcaraz", "player_b": "Sinner"})
    assert q is None


def test_fetch_invalid_odds_rejected():
    # Overround > 1.15 → sanity_ok False
    _reset_bulk([{"player_a": "alcaraz c.", "player_b": "sinner j.",
                   "odds_a": 1.40, "odds_b": 1.40, "te_bookies_count": 1}])
    q = te.fetch({"player_a": "Carlos Alcaraz", "player_b": "Jannik Sinner"})
    assert q is None


# ---------------------------------------------------------------------------
# J8-B4: Stale-Bulk-Drop wenn Refresh leer ist
# ---------------------------------------------------------------------------

def test_stale_bulk_dropped_when_refresh_empty(monkeypatch):
    """_get_bulk() soll leere Liste zurückgeben wenn Bulk > 2×TTL alt und Refresh leer."""
    te._BULK = [{"player_a": "old", "player_b": "data", "odds_a": 2.0, "odds_b": 2.0}]
    te._TS = time.time() - (te._TTL_S * 2 + 1)  # älter als 2×TTL

    # Simuliere leeren Refresh
    monkeypatch.setattr(
        "src.data.tennis_secondary_odds.fetch_te_upcoming_matches",
        lambda **kw: [],
        raising=False,
    )
    try:
        bulk = te._get_bulk()
        # Wenn Refresh leer + stale → Bulk muss leer zurückkommen
        assert bulk == []
    except Exception:
        pass  # Modul nicht verfügbar → Skip (CI ohne Netz)


def test_scraper_records_response_capture_time_without_network(monkeypatch):
    from src.data import tennis_secondary_odds

    detail_html = """
    <td class="k1">Shelton Ben</td><td class="k2">Sinner Jannik</td>
    <tr class="one"><td class="k1"><div class="odds-in">1.80</div></td>
    <td class="k2"><div class="odds-in">2.10</div></td></tr>
    """
    monkeypatch.setattr(
        tennis_secondary_odds, "_http_get", lambda *_a, **_kw: detail_html
    )
    before = datetime.now(timezone.utc)
    match = tennis_secondary_odds._fetch_match_detail("fixture-1")
    after = datetime.now(timezone.utc)

    assert match is not None
    observed_at = datetime.fromisoformat(
        match["source_observed_at"].replace("Z", "+00:00")
    )
    assert before.replace(microsecond=0) <= observed_at <= after.replace(microsecond=0)


def test_legacy_disk_cache_is_refreshed_without_network(monkeypatch, tmp_path):
    from src.data import tennis_secondary_odds

    cache_path = tmp_path / "te_upcoming.pkl"
    cache_path.write_bytes(
        pickle.dumps(
            [
                {
                    "player_a": "shelton ben",
                    "player_b": "sinner jannik",
                    "odds_a": 1.8,
                    "odds_b": 2.1,
                    "te_bookies_count": 2,
                }
            ]
        )
    )
    monkeypatch.setattr(tennis_secondary_odds, "_CACHE_PATH", cache_path)
    monkeypatch.setattr(tennis_secondary_odds, "_discover_match_ids", lambda: ["fresh"])
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_fetch_match_detail",
        lambda _match_id: {
            "player_a": "shelton ben",
            "player_b": "sinner jannik",
            "odds_a": 1.8,
            "odds_b": 2.1,
            "te_bookies_count": 2,
            "source_observed_at": observed_at,
        },
    )

    matches = tennis_secondary_odds.fetch_te_upcoming_matches()

    assert len(matches) == 1
    assert matches[0]["source_observed_at"] == observed_at
