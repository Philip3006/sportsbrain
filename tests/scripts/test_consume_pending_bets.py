"""P0-A regression tests for consume_pending_bets.py (V5 micro-pass).

Covers three merge blockers:
  Blocker-1  Ledger persistence path is reachable when added > 0
  Blocker-2  sport-aware event_status (via signal_contract.py)
  Blocker-3  source=value model_prob fails closed when null/out-of-range
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _fresh_odds_ts() -> str:
    """Return an odds_ts 5 minutes in the past (within the 30-min freshness window)."""
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _valid_bet(overrides: dict | None = None) -> dict:
    """Minimal valid pending bet as stored by the Worker (model_prob is fraction)."""
    base = {
        "id": "bet_001",
        "match": "Federer vs Nadal",
        "market": "home",
        "odds": 2.10,
        "stake_eur": 5.0,
        "source": "value",
        "signal_id": "sig_001",
        "sport": "tennis",
        "league": "wimbledon",
        "kickoff": "2026-08-14T14:00:00Z",
        "model_prob": 0.52,  # fraction (already normalized by Worker)
        "ev_pct": 15.2,
        "fixture_key": "federer_vs_nadal_20260814",
    }
    if overrides:
        base.update(overrides)
    return base


def _run_row_from_bet(bet: dict, bankroll: float = 100.0) -> tuple:
    """Call _row_from_bet from scripts.consume_pending_bets."""
    from scripts.consume_pending_bets import _row_from_bet
    import pandas as pd
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    return _row_from_bet(bet, today, bankroll)


# ── Blocker-1: consumer durability control-flow ───────────────────────────────


def test_blocker1_valid_bet_produces_row():
    """A valid pending bet produces a ledger row (the happy path)."""
    row, _ = _run_row_from_bet(_valid_bet())
    assert row is not None, "valid bet must produce a ledger row"
    assert row["model_prob"] == "0.520000"
    assert row["source"] == "value"
    assert row["status"] == "open"


def test_blocker1_added_zero_path_no_ledger_row_for_invalid():
    """Invalid bet produces (None, reason) — added count stays 0."""
    row, reason = _run_row_from_bet(_valid_bet({"odds": 0.5}))
    assert row is None
    assert reason  # some rejection reason


def test_blocker1_valid_manual_bet_produces_row():
    """Manual bets (model_prob absent) produce a row — model_prob empty."""
    bet = _valid_bet({"source": "manual", "signal_id": "", "model_prob": None})
    row, _ = _run_row_from_bet(bet)
    assert row is not None, "valid manual bet must produce a ledger row"
    assert row["model_prob"] == ""
    assert row["source"] == "manual"


# ── Blocker-2: sport-aware event_status ──────────────────────────────────────


def _tennis_sig(**overrides) -> dict:
    base = {
        "signal_id": "sig_001",
        "signal_status": "ACTIVE",
        "shadow": False, "is_shadow": False,
        "unsupported": False, "edge_lost": False,
        "stale": False, "no_bet_flag": False,
        "current_odds": 2.10,
        "current_ev_pct": 15.2,
        "odds_ts": _fresh_odds_ts(),
        "sport": "tennis",
        "event_status": "UPCOMING",
    }
    base.update(overrides)
    return base


def _football_sig(**overrides) -> dict:
    base = {
        "signal_id": "sig_002",
        "signal_status": "ACTIVE",
        "shadow": False, "is_shadow": False,
        "unsupported": False, "edge_lost": False,
        "stale": False, "no_bet_flag": False,
        "current_odds": 2.10,
        "current_ev_pct": 12.0,
        "odds_ts": _fresh_odds_ts(),
        "sport": "football",
    }
    base.update(overrides)
    return base


def test_blocker2_tennis_missing_event_status_rejected():
    """Tennis signal without event_status must be rejected fail-closed."""
    from src.betting.signal_contract import is_actionable_value_signal
    sig = _tennis_sig()
    del sig["event_status"]
    ok, reason = is_actionable_value_signal(sig, bankroll=100.0, active_bet_count=0)
    assert not ok, f"tennis with missing event_status must be rejected; got ok=True"
    assert "tennis" in reason.lower() or "fail closed" in reason.lower(), reason


def test_blocker2_tennis_null_event_status_rejected():
    """Tennis signal with event_status=None must be rejected."""
    from src.betting.signal_contract import is_actionable_value_signal
    ok, reason = is_actionable_value_signal(_tennis_sig(event_status=None), bankroll=100.0, active_bet_count=0)
    assert not ok, "tennis with null event_status must be rejected"
    assert "tennis" in reason.lower() or "fail closed" in reason.lower(), reason


def test_blocker2_tennis_unknown_event_status_rejected():
    """Tennis signal with event_status=UNKNOWN must be rejected."""
    from src.betting.signal_contract import is_actionable_value_signal
    ok, _ = is_actionable_value_signal(_tennis_sig(event_status="UNKNOWN"), bankroll=100.0, active_bet_count=0)
    assert not ok, "tennis UNKNOWN event_status must be rejected"


def test_blocker2_tennis_upcoming_accepted():
    """Tennis signal with event_status=UPCOMING must be accepted."""
    from src.betting.signal_contract import is_actionable_value_signal
    ok, reason = is_actionable_value_signal(_tennis_sig(event_status="UPCOMING"), bankroll=100.0, active_bet_count=0)
    assert ok, f"tennis UPCOMING must be accepted: {reason}"


def test_blocker2_football_missing_event_status_accepted():
    """Football signal without event_status is accepted (scanner does not emit it)."""
    from src.betting.signal_contract import is_actionable_value_signal
    sig = _football_sig()
    ok, reason = is_actionable_value_signal(sig, bankroll=100.0, active_bet_count=0)
    assert ok, f"football with missing event_status must be accepted: {reason}"


def test_blocker2_football_null_event_status_accepted():
    """Football signal with event_status=None is accepted."""
    from src.betting.signal_contract import is_actionable_value_signal
    ok, reason = is_actionable_value_signal(_football_sig(event_status=None), bankroll=100.0, active_bet_count=0)
    assert ok, f"football with null event_status must be accepted: {reason}"


# ── Blocker-3: source=value model_prob fails closed ──────────────────────────


def test_blocker3_value_null_model_prob_rejected():
    """source=value with null model_prob must be rejected (calibration fail-closed)."""
    row, reason = _run_row_from_bet(_valid_bet({"model_prob": None}))
    assert row is None, "source=value with null model_prob must be rejected"
    assert "model_prob" in reason.lower() or "null" in reason.lower() or "fail" in reason.lower(), reason


def test_blocker3_value_missing_model_prob_rejected():
    """source=value with model_prob key absent must be rejected."""
    bet = _valid_bet()
    del bet["model_prob"]
    row, reason = _run_row_from_bet(bet)
    assert row is None, "source=value with missing model_prob must be rejected"
    assert "model_prob" in reason.lower() or "null" in reason.lower(), reason


def test_blocker3_value_out_of_range_model_prob_rejected():
    """source=value with model_prob > 1.0 (out of fraction range) must be rejected."""
    # Worker normalizes percent→fraction (52.0→0.52). If normalization was skipped
    # and the raw percent arrived in the pending entry, 52.0 > 1.0 → reject.
    row, reason = _run_row_from_bet(_valid_bet({"model_prob": 52.0}))
    assert row is None, "source=value with model_prob=52.0 (un-normalized percent) must be rejected"
    assert "model_prob" in reason.lower() or "calibration" in reason.lower(), reason


def test_blocker3_value_negative_model_prob_rejected():
    """source=value with negative model_prob must be rejected."""
    row, reason = _run_row_from_bet(_valid_bet({"model_prob": -0.1}))
    assert row is None, "source=value with negative model_prob must be rejected"
    assert "model_prob" in reason.lower() or "calibration" in reason.lower(), reason


def test_blocker3_valid_model_prob_fraction_accepted():
    """source=value with valid model_prob fraction (0,1) is accepted."""
    row, _ = _run_row_from_bet(_valid_bet({"model_prob": 0.52}))
    assert row is not None, "valid model_prob fraction 0.52 must be accepted"
    assert row["model_prob"] == "0.520000"


def test_blocker3_calibration_regression_model_prob_in_range():
    """Accepted source=value row must have 0 < model_prob < 1 for Brier/ECE/LogLoss."""
    row, reason = _run_row_from_bet(_valid_bet({"model_prob": 0.508}))
    assert row is not None, f"valid bet rejected: {reason}"
    mp = float(row["model_prob"])
    assert 0 < mp < 1, f"ledger model_prob must be in (0,1) for calibration; got {mp}"


def test_blocker3_manual_null_model_prob_accepted():
    """source=manual with null model_prob is accepted (manual bets may omit it)."""
    bet = _valid_bet({"source": "manual", "signal_id": "", "model_prob": None})
    row, _ = _run_row_from_bet(bet)
    assert row is not None, "manual bet with null model_prob must be accepted"
    assert row["model_prob"] == ""
