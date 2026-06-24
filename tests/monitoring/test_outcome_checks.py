"""Tests für src.monitoring.outcome_checks."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.monitoring import outcome_checks as oc


@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    """Patcht alle Pfade ins tmp-Verzeichnis."""
    led = tmp_path / "ledger.csv"
    sig = tmp_path / "signals.json"
    push = tmp_path / "push_delivery.json"
    settle = tmp_path / "settle.log"
    monkeypatch.setattr(oc, "LEDGER_PATH", led)
    monkeypatch.setattr(oc, "SIGNALS_PATH", sig)
    monkeypatch.setattr(oc, "PUSH_DELIVERY_PATH", push)
    monkeypatch.setattr(oc, "SETTLE_LOG", settle)
    return {"ledger": led, "signals": sig, "push": push, "settle": settle}


def _write_ledger(path: Path, rows: list[dict]) -> None:
    cols = ["match_id", "match_date", "home", "away", "market", "decimal_odds",
            "stake_pct", "stake_amount", "placed_date", "status", "pnl"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({**{c: "" for c in cols}, **r})


def test_stuck_open_bets_detected(tmp_paths):
    old_date = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
    _write_ledger(tmp_paths["ledger"], [
        {"match_id": "m1", "match_date": old_date, "home": "A", "away": "B",
         "market": "home", "status": "open"},
    ])
    sym = oc.check_stuck_open_bets()
    assert sym is not None
    assert sym.id == oc.SYM_STUCK_BETS
    assert sym.suggested_action == "re-run-settle"
    assert sym.payload["count"] == 1


def test_stuck_open_bets_clean(tmp_paths):
    today = datetime.now(timezone.utc).date().isoformat()
    _write_ledger(tmp_paths["ledger"], [
        {"match_id": "m1", "match_date": today, "home": "A", "away": "B",
         "market": "home", "status": "open"},  # heute → nicht stuck
        {"match_id": "m2", "match_date": "2026-06-01", "home": "X", "away": "Y",
         "market": "away", "status": "won"},   # alt aber settled → egal
    ])
    assert oc.check_stuck_open_bets() is None


def test_signals_stale_detected(tmp_paths):
    stale = (datetime.now(timezone.utc) - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp_paths["signals"].write_text(json.dumps({"updated": stale}))
    sym = oc.check_signals_freshness()
    assert sym is not None
    assert sym.id == oc.SYM_SIGNALS_STALE
    assert sym.suggested_action == "force-refresh-signals"


def test_signals_fresh_clean(tmp_paths):
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp_paths["signals"].write_text(json.dumps({"updated": fresh}))
    assert oc.check_signals_freshness() is None


def test_signals_live_window_uses_tighter_threshold(tmp_paths):
    """Wenn ein open bet auf heute steht, gilt 10-min-Grenze statt 30."""
    today = datetime.now(timezone.utc).date().isoformat()
    _write_ledger(tmp_paths["ledger"], [
        {"match_id": "m1", "match_date": today, "home": "A", "away": "B",
         "market": "home", "status": "open"},
    ])
    # 20 min stale: normal noch ok, im Live-Fenster schon Symptom
    twenty_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp_paths["signals"].write_text(json.dumps({"updated": twenty_min_ago}))
    sym = oc.check_signals_freshness()
    assert sym is not None
    assert sym.payload["live_window"] is True
    assert sym.severity == "error"


def test_push_expired_majority(tmp_paths):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tmp_paths["push"].write_text(json.dumps({
        today: {"attempted": 4, "sent": 1, "pruned_410": 3},
        "last_send_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
    sym = oc.check_push_delivery_health()
    assert sym is not None
    assert sym.id == oc.SYM_PUSH_EXPIRED


def test_push_healthy(tmp_paths):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tmp_paths["push"].write_text(json.dumps({
        today: {"attempted": 5, "sent": 5, "pruned_410": 0},
        "last_send_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
    assert oc.check_push_delivery_health() is None


def test_settle_silent_when_log_fresh_but_bets_stuck(tmp_paths):
    old_date = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
    _write_ledger(tmp_paths["ledger"], [
        {"match_id": "m1", "match_date": old_date, "home": "A", "away": "B",
         "market": "home", "status": "open"},
    ])
    tmp_paths["settle"].write_text("[settle] ran ok\n")
    sym = oc.check_settle_silent()
    assert sym is not None
    assert sym.id == oc.SYM_SETTLE_SILENT


def test_run_all_no_crash_when_files_missing(tmp_paths):
    # Alle Pfade existieren nicht → keine Symptome, kein Crash
    assert oc.run_all_checks() == []
