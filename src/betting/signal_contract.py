"""
Canonical actionability contract for value bet signals (P0-A).

Single authoritative source of truth for whether a signal can be acted upon.
All bet placement paths (PWA → Worker → consumer) must respect these checks.
Fail closed: any missing/invalid field returns False + reason.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from src.config import MAX_ACTIVE_BETS, MAX_EV

# ── Public constants ─────────────────────────────────────────────────────────

TERMINAL_STATUSES = {"FINISHED", "CANCELLED", "POSTPONED", "ABANDONED", "TERMINATED", "COMPLETED"}
LIVE_STATUSES = {"LIVE", "IN_PROGRESS"}

# P0-A (Blocker-1 / parity with contract.js ALLOWED_PREMATCH_STATUSES):
# Explicit allowlist of pre-match event states derived from actual production models.
# Tennis (TennisEventStatus): UPCOMING, AWAITING_START, DELAYED accepted.
# Football scanner does NOT emit event_status → None/missing accepted (not rejected).
# Any explicitly-set value outside this set is rejected fail-closed.
ALLOWED_PREMATCH_STATUSES = {
    "UPCOMING",       # canonical Tennis pre-match (TennisEventStatus.UPCOMING)
    "AWAITING_START", # start passed, no authoritative LIVE evidence
    "DELAYED",        # qualified delay; pre-match odds still valid
    "PREMATCH",       # retained for football / legacy compatibility
    "SCHEDULED",      # retained for football / legacy compatibility
}

# Hard 5% bankroll cap — mirrors BANKROLL_RISK_CAP_PCT in config.py
MAX_STAKE_PCT = 0.05

# Maximum odds_ts age before a signal is treated as stale (seconds).
# P1.5-H canonical freshness threshold: 30 minutes.
_MAX_ODDS_AGE_SECONDS = 1800  # 30 minutes

# P0-A (item D / parity with contract.js MAX_CLOCK_SKEW_MS): maximum allowed future skew.
_MAX_CLOCK_SKEW_SECONDS = 60  # 60 seconds

# EV upper bound in percentage points (MAX_EV from config is a fraction: 0.40 → 40 pp)
_MAX_EV_PCT = MAX_EV * 100  # 40.0


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

    # 10. Reject artifact EV values (SportsBrain canonical MAX_EV = 40%)
    if current_ev_pct > _MAX_EV_PCT:
        return False, f"current_ev_pct exceeds canonical MAX_EV ({_MAX_EV_PCT}%): {current_ev_pct}"
    if current_ev_pct <= -100:
        return False, f"current_ev_pct implausibly low: {current_ev_pct}"

    # 11. EV must be positive for a value bet
    if current_ev_pct <= 0:
        return False, f"current_ev_pct must be > 0 for a value bet, got {current_ev_pct}"

    # 12. odds_ts must be present
    odds_ts_raw = signal.get("odds_ts")
    if not odds_ts_raw:
        return False, "odds_ts missing"

    # 13. odds_ts must be within canonical freshness window and not materially future
    try:
        if isinstance(odds_ts_raw, str):
            ts_str = odds_ts_raw.replace("Z", "+00:00")
            odds_ts = datetime.fromisoformat(ts_str)
            if odds_ts.tzinfo is None:
                odds_ts = odds_ts.replace(tzinfo=timezone.utc)
        else:
            odds_ts = datetime.fromtimestamp(float(odds_ts_raw), tz=timezone.utc)
        now = datetime.now(timezone.utc)
        skew_seconds = (odds_ts - now).total_seconds()
        # P0-A (item D / parity): reject materially future odds_ts
        if skew_seconds > _MAX_CLOCK_SKEW_SECONDS:
            return False, (
                f"odds_ts is {skew_seconds:.0f}s in the future — fail closed "
                f"(max skew {_MAX_CLOCK_SKEW_SECONDS}s)"
            )
        age_seconds = -skew_seconds  # positive = in the past
        if age_seconds > _MAX_ODDS_AGE_SECONDS:
            return False, f"odds_ts stale: {age_seconds:.0f}s old (max {_MAX_ODDS_AGE_SECONDS}s)"
    except Exception as exc:  # noqa: BLE001
        return False, f"odds_ts parse error: {exc}"

    # 14. event_status must not be LIVE or IN_PROGRESS
    event_status = signal.get("event_status")
    if event_status in LIVE_STATUSES:
        return False, f"event_status is live: {event_status!r}"

    # 15. event_status must not be terminal (includes COMPLETED, POSTPONED)
    if event_status in TERMINAL_STATUSES:
        return False, f"event_status is terminal: {event_status!r}"

    # 15b. Parity with contract.js (Blocker-1): explicit allowlist check.
    # None/missing = sport does not emit event_status (e.g. football) → accepted.
    # Explicitly set to a non-allowlisted value (e.g. UNKNOWN) → rejected fail-closed.
    if event_status is not None and event_status != "" and event_status not in ALLOWED_PREMATCH_STATUSES:
        return False, f"event_status {event_status!r} not in allowed pre-match set — fail closed"

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


def validate_stake_cap(bankroll: float, stake_eur: float) -> tuple[bool, str]:
    """Return (ok, reason) — REJECTS if stake exceeds the 5% bankroll cap.

    Use at security boundaries (Worker / consumer): an incoming confirmed stake
    that exceeds the cap is rejected, not silently altered.
    The UI uses compute_safe_stake() for proactive pre-confirmation capping.
    """
    try:
        br = float(bankroll)
        stake = float(stake_eur)
    except (TypeError, ValueError):
        return False, f"invalid bankroll ({bankroll!r}) or stake ({stake_eur!r})"
    if not math.isfinite(br) or br <= 0:
        return False, f"bankroll must be > 0, got {br}"
    if not math.isfinite(stake) or stake <= 0:
        return False, f"stake must be > 0, got {stake}"
    ceiling = br * MAX_STAKE_PCT
    if stake > ceiling + 0.001:  # 0.001 EUR tolerance for floating-point rounding
        return False, (
            f"stake €{stake:.2f} exceeds 5% cap (€{ceiling:.2f}) of bankroll €{br:.2f}"
        )
    return True, "ok"


def compute_safe_stake(bankroll: float, requested_eur: float) -> tuple[float, bool]:
    """Return (capped_stake, cap_was_applied) — for UI/proactive pre-confirmation capping.

    Hard cap: bankroll * MAX_STAKE_PCT (5%).
    Cap can only reduce the stake, never increase it.
    Use at security boundaries (Worker / consumer) use validate_stake_cap() instead.
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
