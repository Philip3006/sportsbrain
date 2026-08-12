"""W2: EV sanitization — corrupted sidecar values must be nulled on load.

Covers:
* NaN / ±Infinity rejected
* |ev_pct| > 500 rejected
* valid high EV within domain preserved
* corrupted ACTIVE signal downgraded to STALE_ODDS
* valid non-ACTIVE signal status preserved
* future valid refresh repairs a previously-nulled signal
"""
from __future__ import annotations

import math

import pytest

from src.signals.signal_status import _sanitize_sidecar_ev, _EV_MAX_ABS


def _entry(ev, status="ACTIVE"):
    return {"current_ev_pct": ev, "signal_status": status}


# ── Rejection cases ──────────────────────────────────────────────────────────

def test_nan_ev_rejected():
    state = {"sig1": _entry(float("nan"))}
    modified = _sanitize_sidecar_ev(state)
    assert modified
    assert state["sig1"]["current_ev_pct"] is None


def test_positive_infinity_rejected():
    state = {"sig1": _entry(float("inf"))}
    modified = _sanitize_sidecar_ev(state)
    assert modified
    assert state["sig1"]["current_ev_pct"] is None


def test_negative_infinity_rejected():
    state = {"sig1": _entry(float("-inf"))}
    modified = _sanitize_sidecar_ev(state)
    assert modified
    assert state["sig1"]["current_ev_pct"] is None


def test_huge_finite_positive_rejected():
    state = {"sig1": _entry(2.1e59)}
    modified = _sanitize_sidecar_ev(state)
    assert modified
    assert state["sig1"]["current_ev_pct"] is None


def test_huge_finite_negative_rejected():
    state = {"sig1": _entry(-2.1e59)}
    modified = _sanitize_sidecar_ev(state)
    assert modified
    assert state["sig1"]["current_ev_pct"] is None


def test_over_bound_positive_rejected():
    state = {"sig1": _entry(_EV_MAX_ABS + 1)}
    modified = _sanitize_sidecar_ev(state)
    assert modified
    assert state["sig1"]["current_ev_pct"] is None


def test_over_bound_negative_rejected():
    state = {"sig1": _entry(-(_EV_MAX_ABS + 1))}
    modified = _sanitize_sidecar_ev(state)
    assert modified
    assert state["sig1"]["current_ev_pct"] is None


# ── Valid-value preservation ──────────────────────────────────────────────────

def test_null_ev_unchanged():
    state = {"sig1": _entry(None)}
    modified = _sanitize_sidecar_ev(state)
    assert not modified
    assert state["sig1"]["current_ev_pct"] is None


def test_valid_small_ev_preserved():
    state = {"sig1": _entry(12.5)}
    modified = _sanitize_sidecar_ev(state)
    assert not modified
    assert state["sig1"]["current_ev_pct"] == pytest.approx(12.5)


def test_valid_high_ev_within_domain_preserved():
    state = {"sig1": _entry(_EV_MAX_ABS)}
    modified = _sanitize_sidecar_ev(state)
    assert not modified
    assert state["sig1"]["current_ev_pct"] == pytest.approx(_EV_MAX_ABS)


def test_valid_negative_ev_preserved():
    state = {"sig1": _entry(-15.0)}
    modified = _sanitize_sidecar_ev(state)
    assert not modified
    assert state["sig1"]["current_ev_pct"] == pytest.approx(-15.0)


# ── Status downgrade ─────────────────────────────────────────────────────────

def test_corrupted_active_signal_downgraded_to_stale_odds():
    state = {"sig1": _entry(2.1e59, status="ACTIVE")}
    _sanitize_sidecar_ev(state)
    assert state["sig1"]["signal_status"] == "STALE_ODDS"
    assert state["sig1"]["current_ev_pct"] is None


def test_corrupted_non_active_status_preserved():
    state = {"sig1": _entry(2.1e59, status="EDGE_LOST")}
    _sanitize_sidecar_ev(state)
    assert state["sig1"]["signal_status"] == "EDGE_LOST"
    assert state["sig1"]["current_ev_pct"] is None


# ── Repairability ────────────────────────────────────────────────────────────

def test_future_valid_refresh_repairs_nulled_signal():
    state = {"sig1": _entry(2.1e59, status="ACTIVE")}
    _sanitize_sidecar_ev(state)
    assert state["sig1"]["current_ev_pct"] is None
    # Simulate a valid refresh writing a clean value
    state["sig1"]["current_ev_pct"] = 18.0
    state["sig1"]["signal_status"] = "ACTIVE"
    modified2 = _sanitize_sidecar_ev(state)
    assert not modified2
    assert state["sig1"]["current_ev_pct"] == pytest.approx(18.0)


# ── Mixed state ──────────────────────────────────────────────────────────────

def test_mixed_state_only_corrupted_touched():
    state = {
        "clean": _entry(14.0),
        "corrupt": _entry(2.1e73, status="ACTIVE"),
        "null_entry": _entry(None),
    }
    _sanitize_sidecar_ev(state)
    assert state["clean"]["current_ev_pct"] == pytest.approx(14.0)
    assert state["corrupt"]["current_ev_pct"] is None
    assert state["corrupt"]["signal_status"] == "STALE_ODDS"
    assert state["null_entry"]["current_ev_pct"] is None
