"""Tests fuer _build_tennis_stats() in web_dashboard (Roadmap TENNIS P1.5)."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.notifications.web_dashboard import _build_tennis_stats


LEDGER_FIELDS = [
    "match_id", "match_date", "home", "away", "market", "decimal_odds",
    "stake_pct", "stake_amount", "placed_date", "status", "pnl",
    "closing_odds", "clv", "pinnacle_ref_odds", "source", "model_prob",
    "stake_reason",
]


def _write_ledger(path: Path, rows: list[dict]):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in LEDGER_FIELDS})


def _bet(**k) -> dict:
    """Bet-Zeilenhelfer mit vernuenftigen Defaults."""
    return {
        "match_id": k.get("match_id", "m1"),
        "home": k.get("home", "PlayerA"),
        "away": k.get("away", "PlayerB"),
        "market": k.get("market", "home"),
        "decimal_odds": str(k.get("odds", 1.80)),
        "stake_amount": str(k.get("stake", 10.0)),
        "placed_date": "2026-08-01",
        "status": k.get("status", "open"),
        "pnl": str(k.get("pnl", 0)),
        "closing_odds": str(k.get("closing_odds", 0)),
        "clv": str(k.get("clv", "")),
        "source": k.get("source", "tennis_scanner"),
        "stake_reason": k.get("stake_reason", ""),
    }


def test_empty_ledger_returns_dict_with_zeros(tmp_path):
    p = tmp_path / "ledger.csv"
    _write_ledger(p, [])
    stats = _build_tennis_stats(ledger_path=p)
    assert "stats" in stats
    assert stats["stats"]["n_bets_all"] == 0
    assert stats["stats"]["total_pnl"] == 0.0
    assert "active_tournaments" in stats
    assert "live_gate_status" in stats


def test_missing_ledger_returns_empty(tmp_path):
    p = tmp_path / "does_not_exist.csv"
    assert _build_tennis_stats(ledger_path=p) == {}


def test_counts_only_tennis_bets(tmp_path):
    p = tmp_path / "ledger.csv"
    _write_ledger(p, [
        _bet(market="first_set_a", status="won", pnl=8.5, stake=10),   # tennis
        _bet(market="ah-1.5_a",    status="lost", pnl=-10, stake=10),  # tennis
        _bet(market="btts_yes",    status="won", pnl=9, stake=10, source="football_scan"),
        _bet(market="o/u2.5_over", status="lost", pnl=-10, stake=10, source="football_scan"),
    ])
    stats = _build_tennis_stats(ledger_path=p)["stats"]
    assert stats["n_bets_all"] == 2   # nur tennis (nach market prefix)
    assert stats["n_won"] == 1
    assert stats["n_lost"] == 1


def test_home_bet_identified_by_source_hint(tmp_path):
    """'home'/'away' sind mehrdeutig - via source-hint als Tennis flaggen."""
    p = tmp_path / "ledger.csv"
    _write_ledger(p, [
        _bet(market="home", status="won", pnl=7, stake=10, source="tennis_scan"),
        _bet(market="home", status="won", pnl=7, stake=10, source="football_scan"),
    ])
    stats = _build_tennis_stats(ledger_path=p)["stats"]
    assert stats["n_bets_all"] == 1  # nur der tennis-source ist tennis


def test_roi_and_pnl_calculation(tmp_path):
    p = tmp_path / "ledger.csv"
    _write_ledger(p, [
        _bet(market="first_set_a", status="won", pnl=8.5, stake=10),
        _bet(market="first_set_b", status="lost", pnl=-10, stake=10),
        _bet(market="first_set_a", status="won", pnl=5.0, stake=10),
    ])
    stats = _build_tennis_stats(ledger_path=p)["stats"]
    assert stats["total_staked"] == 30.0
    assert stats["total_pnl"] == pytest.approx(3.5)
    assert stats["roi_pct"] == pytest.approx(11.667, abs=0.01)


def test_by_market_breakdown(tmp_path):
    p = tmp_path / "ledger.csv"
    _write_ledger(p, [
        _bet(market="first_set_a", status="won", pnl=8, stake=10),
        _bet(market="first_set_a", status="lost", pnl=-10, stake=10),
        _bet(market="ah-1.5_a",    status="won", pnl=15, stake=10),
    ])
    stats = _build_tennis_stats(ledger_path=p)["stats"]
    by_m = stats["by_market"]
    assert "first_set_a" in by_m and "ah-1.5_a" in by_m
    assert by_m["first_set_a"]["n"] == 2
    assert by_m["first_set_a"]["won"] == 1
    assert by_m["first_set_a"]["pnl"] == pytest.approx(-2.0)
    assert by_m["ah-1.5_a"]["n"] == 1
    assert by_m["ah-1.5_a"]["won"] == 1


def test_by_tour_split_atp_wta(tmp_path):
    p = tmp_path / "ledger.csv"
    _write_ledger(p, [
        _bet(market="first_set_a", status="won", pnl=8, stake=10, source="tennis_atp"),
        _bet(market="first_set_a", status="won", pnl=8, stake=10, stake_reason="wta_scanner"),
        _bet(market="first_set_a", status="won", pnl=8, stake=10, source="tennis_wta_wimbledon"),
    ])
    stats = _build_tennis_stats(ledger_path=p)["stats"]
    assert stats["by_tour"]["atp"]["n"] == 1
    assert stats["by_tour"]["wta"]["n"] == 2


def test_clv_mean(tmp_path):
    p = tmp_path / "ledger.csv"
    _write_ledger(p, [
        _bet(market="first_set_a", status="won", pnl=8, stake=10, clv=0.05),
        _bet(market="first_set_a", status="lost", pnl=-10, stake=10, clv=0.15),
        _bet(market="first_set_a", status="won", pnl=8, stake=10, clv=-0.02),
    ])
    stats = _build_tennis_stats(ledger_path=p)["stats"]
    assert stats["clv_mean"] == pytest.approx(0.06, abs=0.01)


def test_open_bets_counted_but_not_settled(tmp_path):
    p = tmp_path / "ledger.csv"
    _write_ledger(p, [
        _bet(market="first_set_a", status="open"),
        _bet(market="first_set_a", status="open"),
        _bet(market="first_set_a", status="won", pnl=8, stake=10),
    ])
    stats = _build_tennis_stats(ledger_path=p)["stats"]
    assert stats["n_open"] == 2
    assert stats["n_bets_settled"] == 1
    assert stats["n_bets_all"] == 3


def test_push_status_counted(tmp_path):
    """Retirement -> push. Muss n_push zaehlen, aber PnL 0."""
    p = tmp_path / "ledger.csv"
    _write_ledger(p, [
        _bet(market="ah-1.5_a", status="push", pnl=0, stake=10),
    ])
    stats = _build_tennis_stats(ledger_path=p)["stats"]
    assert stats["n_push"] == 1
    assert stats["total_pnl"] == 0.0
    assert stats["total_staked"] == 10.0


def test_returns_active_tournaments_list(tmp_path, monkeypatch):
    """Discovery-Failure schluckt gracefully, active_tournaments bleibt []."""
    p = tmp_path / "ledger.csv"
    _write_ledger(p, [])
    def _fail():
        raise Exception("no api")
    monkeypatch.setattr(
        "src.tennis.discovery.discover_active_tournaments",
        _fail,
    )
    stats = _build_tennis_stats(ledger_path=p)
    assert isinstance(stats["active_tournaments"], list)


def test_live_gate_status_shape(tmp_path):
    p = tmp_path / "ledger.csv"
    _write_ledger(p, [])
    gate = _build_tennis_stats(ledger_path=p)["live_gate_status"]
    assert "category_mode" in gate
    assert "surface_overrides" in gate
    assert isinstance(gate["surface_overrides"], list)


# needed by pytest.approx
import pytest  # noqa: E402
