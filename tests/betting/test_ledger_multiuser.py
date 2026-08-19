"""Tests for D4 Multi-User-Ledger-Schema.

P0D-002: All ledger paths now route through SPORTSBRAIN_LEDGER_DIR (private repo).
Tests patch the env var + reload affected modules for isolation.
"""
from __future__ import annotations

import importlib
import os

import pandas as pd
import pytest

from src.betting.value_detector import BetSignal


def _patch_ledger_dir(tmp_path, monkeypatch):
    """Route all ledger I/O to an isolated tmp directory via SPORTSBRAIN_LEDGER_DIR.

    P0D-002: ledger_path_for() reads _resolve_ledger_dir() which reads the env var.
    We also patch _LEGACY_LEDGER_PATH + LEDGER_PATH module attrs so the migration
    sentinel set stays consistent with the patched dir.
    """
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    monkeypatch.setenv("SPORTSBRAIN_LEDGER_DIR", str(ledger_dir))

    # Reload config so _resolve_ledger_dir() picks up the new env var.
    import src.config as cfg
    importlib.reload(cfg)

    # Reload ledger module so LEDGER_PATH + sentinel set re-resolve.
    import src.betting.ledger as lm
    importlib.reload(lm)
    # Point legacy path into the same ledger_dir for migration tests.
    monkeypatch.setattr(lm, "_LEGACY_LEDGER_PATH", ledger_dir / "ledger.csv")

    return ledger_dir


def test_legacy_ledger_migrates_into_default_user_slot(tmp_path, monkeypatch):
    """Legacy `ledger.csv` is renamed to `ledger_philip.csv` on first
    `_resolve_ledger_path()` call without an explicit path.

    P0D-002: both legacy and new paths are inside SPORTSBRAIN_LEDGER_DIR.
    """
    ledger_dir = _patch_ledger_dir(tmp_path, monkeypatch)
    import src.betting.ledger as lm

    legacy = ledger_dir / "ledger.csv"
    legacy.write_text(
        "match_id,match_date,home,away,market,decimal_odds,stake_pct,stake_amount,"
        "placed_date,status,pnl,closing_odds,clv,pinnacle_ref_odds,source,model_prob\n"
        "x,2026-06-15,A,B,home,2.0,0.1,10,2026-06-15,won,10,,,,value,\n"
    )

    resolved = lm._resolve_ledger_path(None, user="philip")
    assert resolved == ledger_dir / "ledger_philip.csv"
    assert resolved.exists()
    assert not legacy.exists()


def test_per_user_ledgers_are_isolated(tmp_path, monkeypatch):
    """philip and alice get independent ledger files inside SPORTSBRAIN_LEDGER_DIR."""
    _patch_ledger_dir(tmp_path, monkeypatch)
    from src.betting.ledger import append_bets, count_open_bets

    sig = BetSignal(
        match_id="m1", home="A", away="B", market="home",
        model_prob=0.6, fair_prob=0.5, decimal_odds=2.0,
        ev=0.2, kelly_f=0.1, stake_pct=0.1, confidence="MED",
    )
    append_bets([sig], 100.0, match_date="2026-06-22", user="philip")
    append_bets([sig], 100.0, match_date="2026-06-22", user="alice")

    assert count_open_bets(user="philip") == 1
    assert count_open_bets(user="alice") == 1

    # Cross-user isolation: appending only for alice doesn't show on philip
    sig2 = BetSignal(
        match_id="m2", home="C", away="D", market="away",
        model_prob=0.4, fair_prob=0.33, decimal_odds=3.0,
        ev=0.2, kelly_f=0.05, stake_pct=0.05, confidence="MED",
    )
    append_bets([sig2], 100.0, match_date="2026-06-22", user="alice")
    assert count_open_bets(user="philip") == 1
    assert count_open_bets(user="alice") == 2


def test_explicit_path_bypasses_user_resolution(tmp_path, monkeypatch):
    """A test path different from the sentinels is used as-is and is NOT
    swallowed by the per-user migration."""
    _patch_ledger_dir(tmp_path, monkeypatch)
    from src.betting.ledger import append_bets, count_open_bets

    custom = tmp_path / "explicit_ledger.csv"

    sig = BetSignal(
        match_id="m1", home="A", away="B", market="home",
        model_prob=0.6, fair_prob=0.5, decimal_odds=2.0,
        ev=0.2, kelly_f=0.1, stake_pct=0.1, confidence="MED",
    )
    append_bets([sig], 100.0, path=custom, match_date="2026-06-22")
    assert custom.exists()
    assert count_open_bets(custom) == 1


def test_alice_does_not_migrate_legacy(tmp_path, monkeypatch):
    """Migration is gated on user == DEFAULT_USER. A new user's first
    access must NOT consume the legacy file."""
    ledger_dir = _patch_ledger_dir(tmp_path, monkeypatch)
    import src.betting.ledger as lm

    legacy = ledger_dir / "ledger.csv"
    legacy.write_text(
        "match_id,match_date,home,away,market,decimal_odds,stake_pct,stake_amount,"
        "placed_date,status,pnl,closing_odds,clv,pinnacle_ref_odds,source,model_prob\n"
    )

    resolved = lm._resolve_ledger_path(None, user="alice")
    assert resolved == ledger_dir / "ledger_alice.csv"
    assert legacy.exists(), "legacy file must remain untouched for non-default user"


def test_ledger_summary_routes_per_user(tmp_path, monkeypatch):
    """`ledger_summary(user='alice')` reads only alice's ledger."""
    _patch_ledger_dir(tmp_path, monkeypatch)
    from src.betting.ledger import append_bets, ledger_summary

    sig = BetSignal(
        match_id="m1", home="A", away="B", market="home",
        model_prob=0.6, fair_prob=0.5, decimal_odds=2.0,
        ev=0.2, kelly_f=0.1, stake_pct=0.1, confidence="MED",
    )
    append_bets([sig], 100.0, match_date="2026-06-22", user="alice")
    s = ledger_summary(user="alice")
    assert s["n_bets"] == 1
    s_default = ledger_summary(user="philip")
    assert s_default["n_bets"] == 0
