"""
Canonical actionability contract for value bet signals (P0-A).

Single authoritative source of truth for whether a signal can be acted upon.
All bet placement paths (PWA → Worker → consumer) must respect these checks.
Fail closed: any missing/invalid field returns False + reason.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta

from src.config import MAX_ACTIVE_BETS

# ── Public constants ─────────────────────────────────────────────────────────

TERMINAL_STATUSES = {"FINISHED", "CANCELLED", "POSTPONED", "ABANDONED", "TERMINATED"}
LIVE_STATUSES = {"LIVE", "IN_PROGRESS"}

# Hard 5% bankroll cap — mirrors BANKROLL_RISK_CAP_PCT in config.py
MAX_STAKE_PCT = 0.05

# Maximum odds_ts age before a signal is treated as stale (seconds)
_MAX_ODDS_AGE_SECONDS = 14400  # 4 hours


def is_actionable_value_signal(
    signal: dict,
    bankroll: float,
    active_bet_count: int,
) -> tuple[bool, str]:
    """Return (can_place, reason).

    Enforces ALL conditions for a canonical value bet — fail closed: returns
    (False, reason) if any single condition fails.

    Parameters
    ----------
    signal:
        The signal dict as stored in signals.json / passed from the scanner.
    bankroll:
        Current authoritative bankroll in EUR.
    active_bet_count:
        Number of currently open bets in the ledger.
    """
    # 1. signal_id present and non-empty
    sid = signal.get("signal_id")
    if not sid or not str(sid).strip():
        return False, "signal_id missing or empty"

    # 2. signal_status must be exactly "ACTIVE"
    status = signal.get("signal_status")
    if status != "ACTIVE":
        return False, f"signal_status must be 'ACTIVE', got {status!r}"

    # 3. Not a shadow signal
    if signal.get("shadow") is True or signal.get("is_shadow") is True:
        return False, "shadow signal — not actionable"

    # 4. Not unsupported
    if signal.get("unsupported") is True:
        return False, "unsupported signal"

    # 5. Edge not lost
    if signal.get("edge_lost") is True:
        return False, "edge_lost — signal no longer has value"

    # 6. Not stale
    if signal.get("stale") is True:
        return False, "stale signal"

    # 7. no_bet_flag not set
    if signal.get("no_bet_flag") is True:
        return False, "no_bet_flag — risk cap or minimum stake violation"

    # 8. current_odds must be a finite float > 1.0
    current_odds_raw = signal.get("current_odds")
    try:
        current_odds = float(current_odds_raw)
    except (TypeError, ValueError):
        return False, f"current_odds invalid: {current_odds_raw!r}"
    if not math.isfinite(current_odds) or current_odds <= 1.0:
        return False, f"current_odds must be finite and > 1.0, got {current_odds}"

    # 9. current_ev_pct must be a finite float
    current_ev_raw = signal.get("current_ev_pct")
    try:
        current_ev_pct = float(current_ev_raw)
    except (TypeError, ValueError):
        return False, f"current_ev_pct invalid: {current_ev_raw!r}"
    if not math.isfinite(current_ev_pct):
        return False, f"current_ev_pct must be finite, got {current_ev_pct}"

    # 10. Reject absurd EV values
    if not (-100 < current_ev_pct < 500):
        return False, f"current_ev_pct out of plausible range (-100, 500): {current_ev_pct}"

    # 11. EV must be positive for a value bet
    if current_ev_pct <= 0:
        return False, f"current_ev_pct must be > 0 for a value bet, got {current_ev_pct}"

    # 12. odds_ts must be present
    odds_ts_raw = signal.get("odds_ts")
    if not odds_ts_raw:
        return False, "odds_ts missing"

    # 13. odds_ts must be within 4 hours of now
    try:
        if isinstance(odds_ts_raw, str):
            # Handle ISO strings with or without Z suffix
            ts_str = odds_ts_raw.replace("Z", "+00:00")
            odds_ts = datetime.fromisoformat(ts_str)
            if odds_ts.tzinfo is None:
                odds_ts = odds_ts.replace(tzinfo=timezone.utc)
        else:
            odds_ts = datetime.fromtimestamp(float(odds_ts_raw), tz=timezone.utc)
        now = datetime.now(timezone.utc)
        age_seconds = (now - odds_ts).total_seconds()
        if age_seconds > _MAX_ODDS_AGE_SECONDS:
            return False, f"odds_ts stale: {age_seconds:.0f}s old (max {_MAX_ODDS_AGE_SECONDS}s)"
    except Exception as exc:
        return False, f"odds_ts parse error: {exc}"

    # 14. event_status must not be LIVE or IN_PROGRESS
    event_status = signal.get("event_status")
    if event_status in LIVE_STATUSES:
        return False, f"event_status is live: {event_status!r}"

    # 15. event_status must not be terminal
    if event_status in TERMINAL_STATUSES:
        return False, f"event_status is terminal: {event_status!r}"

    # 16. bankroll must be a finite float > 0
    try:
        bankroll_f = float(bankroll)
    except (TypeError, ValueError):
        return False, f"bankroll invalid: {bankroll!r}"
    if not math.isfinite(bankroll_f) or bankroll_f <= 0:
        return False, f"bankroll must be > 0, got {bankroll_f}"

    # 17. active_bet_count must be < MAX_ACTIVE_BETS
    try:
        count = int(active_bet_count)
    except (TypeError, ValueError):
        return False, f"active_bet_count invalid: {active_bet_count!r}"
    if count >= MAX_ACTIVE_BETS:
        return False, f"max active bets ({MAX_ACTIVE_BETS}) reached — {count} already open"

    # 18. sport must be present and non-empty
    sport = signal.get("sport")
    if not sport or not str(sport).strip():
        return False, "sport field missing or empty"

    return True, "ok"


def compute_safe_stake(bankroll: float, requested_eur: float) -> tuple[float, bool]:
    """Return (capped_stake, cap_was_applied).

    Hard cap: bankroll * MAX_STAKE_PCT (5%).
    The cap can only reduce the requested stake, never increase it.
    """
    try:
        br = float(bankroll)
        req = float(requested_eur)
    except (TypeError, ValueError):
        return 0.0, True

    ceiling = br * MAX_STAKE_PCT
    if req > ceiling:
        return ceiling, True
    return req, False
