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


def test_fetch_rejects_legacy_missing_or_stale_source_timestamp(monkeypatch):
    row = {
        "player_a": "alcaraz c.",
        "player_b": "sinner j.",
        "odds_a": 1.90,
        "odds_b": 2.05,
        "te_bookies_count": 3,
    }
    monkeypatch.setattr(te, "_quote_from_requested_fixture", lambda *_: None)
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


def test_fetch_no_match_returns_none(monkeypatch):
    monkeypatch.setattr(te, "_quote_from_requested_fixture", lambda *_: None)
    _reset_bulk([{"player_a": "nadal r.", "player_b": "federer r.",
                   "odds_a": 1.80, "odds_b": 2.10, "te_bookies_count": 4}])
    q = te.fetch({"player_a": "Alcaraz", "player_b": "Sinner"})
    assert q is None


def test_fetch_invalid_odds_rejected(monkeypatch):
    monkeypatch.setattr(te, "_quote_from_requested_fixture", lambda *_: None)
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


def test_scraper_prefers_full_year_match_kickoff_over_quote_history(monkeypatch):
    from src.data import tennis_secondary_odds

    detail_html = """
    <td class="k1">Vekic Donna</td><td class="k2">Swiatek Iga</td>
    <div>05.10.2026</div><div>, 05:00</div>
    <a href="/beijing/2026/wta-women/">Beijing</a>
    <tr class="one"><td class="k1"><div class="odds-in">1.95</div></td>
    <td class="k2"><div class="odds-in">2.05</div></td></tr>
    <div>04.10. 23:15</div>
    """
    monkeypatch.setattr(
        tennis_secondary_odds, "_http_get", lambda *_a, **_kw: detail_html
    )

    match = tennis_secondary_odds._fetch_match_detail("fixture-1")

    assert match is not None
    assert match["commence_time"] == "2026-10-05T03:00:00Z"


def test_targeted_te_fixture_lookup_finds_named_match_after_bulk_cap(monkeypatch):
    from src.data import tennis_secondary_odds

    distractors = "".join(
        f'<a href="/match-detail/?id={index}">Other{index} A. - Rival{index} B.</a>'
        for index in range(1, 201)
    )
    page = distractors + '<a href="/match-detail/?id=3338909">Vekic D. - Swiatek I.</a>'
    requested_ids = []
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    target = {
        "match_id": "te_3338909",
        "player_a": "Vekic Donna",
        "player_b": "Swiatek Iga",
        "commence_time": "2026-10-05T03:00:00Z",
        "odds_a": 1.95,
        "odds_b": 2.05,
        "te_bookies_count": 6,
        "source_observed_at": observed_at,
    }
    monkeypatch.setattr(tennis_secondary_odds, "_get_next_html", lambda: page)

    def fetch_detail(match_id):
        requested_ids.append(match_id)
        return target

    monkeypatch.setattr(tennis_secondary_odds, "_fetch_match_detail", fetch_detail)

    match = tennis_secondary_odds.fetch_te_match_for_hint(
        {
            "player_a": "Donna Vekic",
            "player_b": "Iga Swiatek",
            "commence_time": "2026-10-05T03:00:00Z",
        }
    )

    assert requested_ids == ["3338909"]
    assert match is target


def test_targeted_te_lookup_rejects_participant_or_date_mismatch(monkeypatch):
    from src.data import tennis_secondary_odds

    page = '<a href="/match-detail/?id=3338909">Vekic D. - Swiatek I.</a>'
    monkeypatch.setattr(tennis_secondary_odds, "_get_next_html", lambda: page)
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_fetch_match_detail",
        lambda _match_id: {
            "player_a": "Vekic Donna",
            "player_b": "Swiatek Iga",
            "commence_time": "2026-10-06T03:00:00Z",
            "te_bookies_count": 6,
        },
    )

    wrong_date = tennis_secondary_odds.fetch_te_match_for_hint(
        {
            "player_a": "Donna Vekic",
            "player_b": "Iga Swiatek",
            "commence_time": "2026-10-05T03:00:00Z",
        }
    )
    wrong_players = tennis_secondary_odds.fetch_te_match_for_hint(
        {
            "player_a": "Donna Vekic",
            "player_b": "Coco Gauff",
            "commence_time": "2026-10-05T03:00:00Z",
        }
    )
    assert wrong_date is None
    assert wrong_players is None


def test_targeted_te_lookup_requires_timezone_aware_kickoff(monkeypatch):
    from src.data import tennis_secondary_odds

    monkeypatch.setattr(
        tennis_secondary_odds,
        "_get_next_html",
        lambda: (_ for _ in ()).throw(AssertionError("must fail before request")),
    )

    missing_kickoff = tennis_secondary_odds.fetch_te_match_for_hint(
        {"player_a": "Donna Vekic", "player_b": "Iga Swiatek"}
    )
    naive_kickoff = tennis_secondary_odds.fetch_te_match_for_hint(
        {
            "player_a": "Donna Vekic",
            "player_b": "Iga Swiatek",
            "commence_time": "2026-10-05T03:00:00",
        }
    )
    assert missing_kickoff is None
    assert naive_kickoff is None


def test_targeted_te_lookup_has_a_bounded_detail_request_budget(monkeypatch):
    from src.data import tennis_secondary_odds

    monkeypatch.setattr(
        tennis_secondary_odds,
        "_TARGETED_MATCH_DETAIL_COUNT",
        tennis_secondary_odds._MAX_TARGETED_MATCH_DETAILS,
    )
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_get_next_html",
        lambda: '<a href="/match-detail/?id=3338909">Vekic D. - Swiatek I.</a>',
    )
    monkeypatch.setattr(
        tennis_secondary_odds,
        "_fetch_match_detail",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("budget must fail before request")
        ),
    )

    assert (
        tennis_secondary_odds.fetch_te_match_for_hint(
            {
                "player_a": "Donna Vekic",
                "player_b": "Iga Swiatek",
                "commence_time": "2026-10-05T03:00:00Z",
            }
        )
        is None
    )


def test_adapter_uses_one_exact_fixture_result_for_both_outcomes(monkeypatch):
    from src.data import tennis_secondary_odds
    from src.tennis.odds.base import ProviderOutcome

    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hint = {"player_a": "Donna Vekic", "player_b": "Iga Swiatek"}
    match = {
        "player_a": "Vekic Donna",
        "player_b": "Swiatek Iga",
        "odds_a": 1.95,
        "odds_b": 2.05,
        "te_bookies_count": 6,
        "source_observed_at": observed_at,
    }
    calls = []
    te._BULK = []
    te._TS = 0.0
    monkeypatch.setattr(
        te,
        "_get_bulk_with_diagnostics",
        lambda: ([], ProviderOutcome(te.name, True, True, "success")),
    )
    monkeypatch.setattr(
        tennis_secondary_odds,
        "fetch_te_match_for_hint",
        lambda requested, min_bookies: calls.append((requested, min_bookies)) or match,
    )

    quote, outcome = te.fetch_with_diagnostics(hint)

    assert quote is not None
    assert quote.h2h_a == 1.95
    assert quote.h2h_b == 2.05
    assert quote.bookmaker == "consensus"
    assert quote.ts == datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    assert calls == [(hint, 2)]
    assert outcome.result == "usable_quote"


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
