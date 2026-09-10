"""
Tests for the weekly bankroll snapshot used to drive tier-based stake sizing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.betting import ledger as ledger_mod
from src.betting.ledger import (
    _current_iso_week,
    get_bankroll_snapshot,
    peek_bankroll_snapshot,
)


def _write_ledger(path: Path, *, total_pnl: float) -> None:
    """Minimal ledger CSV: one settled bet whose pnl equals `total_pnl`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stake = 10.0
    path.write_text(
        "match_id,match_date,home,away,market,decimal_odds,stake_pct,stake_amount,"
        "placed_date,status,pnl,closing_odds,clv,pinnacle_ref_odds,source,model_prob\n"
        f"x,2026-06-15,A,B,home,2.0,0.1,{stake:.2f},2026-06-15,won,{total_pnl:.2f},,,,value,\n"
    )


@pytest.fixture
def tmp_paths(tmp_path):
    ledger = tmp_path / "ledger.csv"
    snapshot = tmp_path / "bankroll_snapshot.json"
    return ledger, snapshot


def test_snapshot_creates_file_when_missing(tmp_paths):
    ledger, snapshot = tmp_paths
    _write_ledger(ledger, total_pnl=74.52)
    br = get_bankroll_snapshot(snapshot, ledger)
    assert br == 174.52
    assert snapshot.exists()
    data = json.loads(snapshot.read_text())
    year, week = _current_iso_week()
    assert data["iso_year"] == year
    assert data["iso_week"] == week
    assert data["bankroll"] == 174.52


def test_snapshot_persists_across_calls_same_week(tmp_paths):
    ledger, snapshot = tmp_paths
    _write_ledger(ledger, total_pnl=50.0)
    first = get_bankroll_snapshot(snapshot, ledger)
    # Modify ledger: even a big swing must NOT change the snapshot mid-week
    _write_ledger(ledger, total_pnl=-30.0)
    second = get_bankroll_snapshot(snapshot, ledger)
    assert first == second == 150.0


def test_snapshot_refreshes_in_new_iso_week(tmp_paths):
    ledger, snapshot = tmp_paths
    _write_ledger(ledger, total_pnl=50.0)
    # Seed a snapshot with last week's ISO date
    year, week = _current_iso_week()
    stale = {"iso_year": year, "iso_week": max(1, week - 1),
             "snapshot_date": "1970-01-01", "bankroll": 999.99}
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps(stale))
    br = get_bankroll_snapshot(snapshot, ledger)
    # New week → snapshot must recompute from current ledger
    assert br == 150.0
    assert json.loads(snapshot.read_text())["bankroll"] == 150.0


def test_snapshot_uses_100_plus_total_pnl(tmp_paths):
    ledger, snapshot = tmp_paths
    _write_ledger(ledger, total_pnl=0.0)
    assert get_bankroll_snapshot(snapshot, ledger) == 100.0


def test_peek_does_not_create_file(tmp_paths):
    ledger, snapshot = tmp_paths
    _write_ledger(ledger, total_pnl=25.0)
    br = peek_bankroll_snapshot(snapshot, ledger)
    assert br == 125.0
    assert not snapshot.exists()


def test_peek_uses_cache_if_valid(tmp_paths):
    ledger, snapshot = tmp_paths
    _write_ledger(ledger, total_pnl=75.0)
    get_bankroll_snapshot(snapshot, ledger)  # writes 175.0
    # Change ledger; peek must return cached value within same ISO week
    _write_ledger(ledger, total_pnl=-50.0)
    assert peek_bankroll_snapshot(snapshot, ledger) == 175.0


def test_corrupt_snapshot_recovers_gracefully(tmp_paths):
    ledger, snapshot = tmp_paths
    _write_ledger(ledger, total_pnl=20.0)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text("{ not valid json")
    br = get_bankroll_snapshot(snapshot, ledger)
    assert br == 120.0
    # And it overwrites with a valid one
    assert json.loads(snapshot.read_text())["bankroll"] == 120.0


# ───────────── D3 Multi-User-Schema ─────────────

def test_campaign_snapshot_is_not_used_for_weekly_stake_control(tmp_path, monkeypatch):
    """Weekly stake state is external and never overwrites campaign accounting."""
    from src.runtime import paths

    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    campaign = ledger_dir / "bankroll_snapshot_philip.json"
    campaign_payload = {
        "starting_bankroll_eur": 100.0,
        "current_bankroll_eur": 87.67,
        "phase": "campaign",
    }
    campaign.write_text(json.dumps(campaign_payload))
    runtime_state = tmp_path / "runtime-state"
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_STATE_DIR", str(runtime_state))
    monkeypatch.setattr(paths, "DEFAULT_RUNTIME_STATE_DIR", runtime_state)

    ledger = tmp_path / "ledger.csv"
    _write_ledger(ledger, total_pnl=10.0)

    assert ledger_mod.get_bankroll_snapshot(ledger_path=ledger, user="philip") == 110.0
    weekly = runtime_state / "financial" / "weekly_bankroll_snapshot_philip.json"
    assert weekly.exists()
    assert json.loads(campaign.read_text()) == campaign_payload


def test_per_user_snapshots_are_isolated(tmp_path, monkeypatch):
    """Two users receive isolated external weekly stake-control snapshots."""
    runtime_state = tmp_path / "runtime-state"
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_STATE_DIR", str(runtime_state))

    ledger = tmp_path / "ledger.csv"
    _write_ledger(ledger, total_pnl=23.0)

    a = ledger_mod.get_bankroll_snapshot(ledger_path=ledger, user="philip")
    b = ledger_mod.get_bankroll_snapshot(ledger_path=ledger, user="alice")
    assert a == b == 123.0
    assert (runtime_state / "financial" / "weekly_bankroll_snapshot_philip.json").exists()
    alice = runtime_state / "financial" / "weekly_bankroll_snapshot_alice.json"
    assert alice.exists()
    assert json.loads(alice.read_text())["user"] == "alice"


def test_cancellation_refund_uses_external_weekly_snapshot(tmp_path, monkeypatch):
    runtime_state = tmp_path / "runtime-state"
    monkeypatch.setenv("SPORTSBRAIN_RUNTIME_STATE_DIR", str(runtime_state))
    campaign = tmp_path / "bankroll_snapshot_philip.json"
    campaign.write_text(json.dumps({"current_bankroll_eur": 87.67, "phase": "campaign"}))
    original_campaign = campaign.read_text()
    ledger = tmp_path / "ledger.csv"
    ledger.write_text(
        "match_id,match_date,home,away,market,decimal_odds,stake_pct,stake_amount,"
        "placed_date,status,pnl,closing_odds,clv,pinnacle_ref_odds,source,model_prob\n"
        "x,2026-06-15,A,B,home,2.0,0.1,10.00,2026-06-15,open,0.0,,,,value,\n"
    )

    assert ledger_mod.get_bankroll_snapshot(ledger_path=ledger, user="philip") == 100.0
    assert ledger_mod.cancel_bet("A", "B", "home", path=ledger, user="philip") == "ok"

    weekly = runtime_state / "financial" / "weekly_bankroll_snapshot_philip.json"
    assert json.loads(weekly.read_text())["bankroll"] == 110.0
    assert campaign.read_text() == original_campaign
