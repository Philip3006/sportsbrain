"""Signal lifecycle state management and odds-state sidecar persistence.

The sidecar file (data/cache/odds_state.json) is the single authority for
refreshed odds. It is keyed by signal_id and written only by the odds refresher.
write_signals_json() merges sidecar fields into published signals at publish time.
This design avoids concurrent read-modify-write races between the scanner and
the refresher on signals.json.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import threading

from src.betting.gates import Gate, gate_for
from src.utils.atomic_io import atomic_write_json

ROOT = Path(__file__).resolve().parents[2]
_SIDECAR_PATH = ROOT / "data" / "cache" / "odds_state.json"
_SIDECAR_LOCK = threading.Lock()

SignalStatus = Literal["ACTIVE", "EDGE_LOST", "STALE_ODDS", "STARTED", "EXPIRED", "UNREFRESHABLE"]

_STALE_THRESHOLD_SECONDS = 30 * 60   # 30 minutes hard stale
_PREFERRED_THRESHOLD_SECONDS = 15 * 60  # 15 minutes preferred


def make_signal_id(sport: str, match: str, market: str, kickoff: str) -> str:
    """Deterministic 12-char signal identity from canonical fields.

    Stable across: original scan, subsequent odds refreshes, regenerated
    signals.json, and frontend rendering. A rescan of the same logical event
    generates the same ID.
    """
    kickoff_date = (kickoff or "")[:10]  # only date portion — intra-day rescans stable
    key = f"{sport}:{match}:{market}:{kickoff_date}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Probability unit conversion
# ---------------------------------------------------------------------------

def model_prob_to_decimal(model_prob_pct: float) -> float:
    """Convert model_prob from signals.json (percentage, e.g. 49.5) to decimal (0.495).

    signals.json stores model_prob in percentage units because _signal_to_dict()
    multiplies BetSignal.model_prob (decimal) by 100. The refresh layer must
    consistently use this helper to avoid a 100x error in EV calculation.
    """
    return model_prob_pct / 100.0


def compute_current_ev(model_prob_pct: float, current_odds: float) -> float:
    """Current EV using immutable model probability + refreshed market price."""
    p = model_prob_to_decimal(model_prob_pct)
    return round(p * current_odds - 1.0, 6)


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------

def _resolve_gate(signal: dict) -> Gate:
    sport = signal.get("sport", "football")
    market = signal.get("market", "")
    category = signal.get("category", "")  # tennis tournament category
    return gate_for(sport, market=market, category=category)


def evaluate_signal_status(
    signal: dict,
    current_odds: float | None,
    odds_ts: str | None,
) -> SignalStatus:
    """Compute lifecycle status using canonical production gate.

    Parameters
    ----------
    signal:      signal dict from signals.json (model_prob in pct units)
    current_odds: freshly fetched decimal odds, or None if unavailable
    odds_ts:     ISO-8601 timestamp of the odds fetch, or None

    Returns one of ACTIVE | EDGE_LOST | STALE_ODDS | STARTED | EXPIRED | UNREFRESHABLE
    """
    kickoff = signal.get("kickoff", "")
    now = datetime.now(timezone.utc)

    # EXPIRED: kickoff + 100 min passed (already handled by _drop_finished_signals,
    # but also guard here so refresher doesn't keep refreshing dead signals)
    if kickoff:
        try:
            ko_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            elapsed_min = (now - ko_dt).total_seconds() / 60
            if elapsed_min > 100:
                return "EXPIRED"
            if elapsed_min > -5:  # kickoff started (within pre-match window)
                return "STARTED"
        except ValueError:
            pass

    # UNREFRESHABLE: no current_odds provided by any provider
    if current_odds is None:
        return "UNREFRESHABLE"

    # STALE_ODDS: last fetch was more than 30 minutes ago
    if odds_ts:
        try:
            ts_dt = datetime.fromisoformat(odds_ts.replace("Z", "+00:00"))
            age_seconds = (now - ts_dt).total_seconds()
            if age_seconds > _STALE_THRESHOLD_SECONDS:
                return "STALE_ODDS"
        except ValueError:
            return "STALE_ODDS"
    else:
        return "STALE_ODDS"

    # Canonical gate evaluation
    gate = _resolve_gate(signal)
    model_prob_pct = float(signal.get("model_prob", 0))
    current_ev = compute_current_ev(model_prob_pct, current_odds)

    p = model_prob_to_decimal(model_prob_pct)
    if current_ev < gate.min_edge or p < gate.min_prob or current_odds > gate.max_odds:
        return "EDGE_LOST"

    return "ACTIVE"


# ---------------------------------------------------------------------------
# Sidecar read / write
# ---------------------------------------------------------------------------

_EV_MAX_ABS = 500.0  # W2: EV values outside ±500% are corrupt; matches JS guard


def _sanitize_sidecar_ev(state: dict) -> bool:
    """In-place: null out corrupted current_ev_pct values on load.

    Returns True if any entries were modified (triggers a sidecar rewrite).
    Corrupted = NaN, ±Infinity, or |ev_pct| > 500. Non-ACTIVE signals that
    carry corrupted EV are downgraded to STALE_ODDS so they stay non-actionable.
    """
    import math
    modified = False
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        ev = entry.get("current_ev_pct")
        if ev is None:
            continue
        try:
            fev = float(ev)
            if math.isfinite(fev) and abs(fev) <= _EV_MAX_ABS:
                continue  # value is clean
        except (TypeError, ValueError):
            pass
        entry["current_ev_pct"] = None
        if entry.get("signal_status") == "ACTIVE":
            entry["signal_status"] = "STALE_ODDS"
        modified = True
    return modified


def load_odds_state() -> dict[str, dict]:
    """Load the odds-state sidecar. Sanitizes corrupted EV values on read.

    Returns empty dict if missing or corrupt JSON.
    """
    if not _SIDECAR_PATH.exists():
        return {}
    try:
        import json
        raw = _SIDECAR_PATH.read_text()
        data = json.loads(raw)
        if isinstance(data, dict):
            if _sanitize_sidecar_ev(data):
                # Persist the clean state so bad values don't re-appear next load
                atomic_write_json(_SIDECAR_PATH, data)
            return data
    except Exception:
        pass
    return {}


def _dedup_history(history: list[dict], new_entry: dict) -> list[dict]:
    """Append new_entry only if odds differ from last entry within 5-minute bucket."""
    if not history:
        return [new_entry]
    last = history[-1]
    if last.get("odds") == new_entry.get("odds"):
        # Same price — check if within 5-minute bucket
        try:
            last_ts = datetime.fromisoformat(last["ts"].replace("Z", "+00:00"))
            new_ts = datetime.fromisoformat(new_entry["ts"].replace("Z", "+00:00"))
            if abs((new_ts - last_ts).total_seconds()) < 300:
                return history  # skip duplicate
        except (KeyError, ValueError):
            pass
    return history + [new_entry]


def update_odds_state(
    signal_id: str,
    *,
    current_odds: float,
    odds_ts: str,
    odds_source: str,
    odds_fetch_tier: int,
    signal_status: SignalStatus,
    current_ev_pct: float,
) -> None:
    """Atomically update one signal's entry in the sidecar.

    Preserves initial_odds from first observation. Appends to odds_history
    with deduplication. Thread-safe via module-level lock.
    """
    with _SIDECAR_LOCK:
        state = load_odds_state()
        existing = state.get(signal_id, {})

        # Seed initial_odds from first observation
        initial_odds = existing.get("initial_odds", current_odds)
        initial_ev_pct = existing.get("initial_ev_pct")

        # Build history entry — only append real observations (not None status-only writes)
        history = existing.get("odds_history", [])
        if current_odds is not None and odds_ts is not None:
            new_point = {"ts": odds_ts, "odds": current_odds, "source": odds_source}
            history = _dedup_history(history, new_point)

        # Guard against absurd EV values (historic double-multiplication bug produced
        # 1e+59). Anything with magnitude > 500% is treated as corrupt input and
        # written as None so downstream fail-closed logic excludes it.
        ev_pct_stored: float | None = None
        if current_ev_pct is not None:
            candidate = round(current_ev_pct * 100, 1)
            if abs(candidate) <= 500:
                ev_pct_stored = candidate

        state[signal_id] = {
            "initial_odds":    initial_odds,
            "initial_ev_pct":  initial_ev_pct,
            "current_odds":    current_odds,
            "current_ev_pct":  ev_pct_stored,
            "odds_ts":         odds_ts,
            "odds_source":     odds_source,
            "odds_fetch_tier": odds_fetch_tier,
            "signal_status":   signal_status,
            "odds_history":    history,
        }

        atomic_write_json(_SIDECAR_PATH, state)


def seed_initial_odds(signal_id: str, signal: dict) -> None:
    """Seed a signal's initial_odds from its scan-time values if not yet present.

    Called during migration: existing signals without history get their
    scan-time odds recorded as the first history point.
    """
    with _SIDECAR_LOCK:
        state = load_odds_state()
        if signal_id in state and state[signal_id].get("initial_odds") is not None:
            return  # already seeded

        odds = signal.get("odds")
        if not odds:
            return

        generated_at = signal.get("generated_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        ev_pct = signal.get("ev_pct")

        existing = state.get(signal_id, {})
        history = existing.get("odds_history", [])
        if not history:
            history = [{"ts": generated_at, "odds": odds, "source": "scan"}]

        state[signal_id] = {
            **existing,
            "initial_odds":    odds,
            "initial_ev_pct":  ev_pct,
            "current_odds":    existing.get("current_odds"),
            "current_ev_pct":  existing.get("current_ev_pct"),
            "odds_ts":         existing.get("odds_ts"),
            "odds_source":     existing.get("odds_source"),
            "odds_fetch_tier": existing.get("odds_fetch_tier"),
            "signal_status":   existing.get("signal_status"),
            "odds_history":    history,
        }
        atomic_write_json(_SIDECAR_PATH, state)


def merge_odds_state_into_signal(signal: dict, odds_state: dict[str, dict]) -> dict:
    """Merge sidecar fields into a signal dict at publish time.

    Called by write_signals_json() before writing signals.json. Preserves all
    original scan-time fields (odds, ev_pct, generated_at, model_prob).
    """
    sid = signal.get("signal_id")
    if not sid:
        return signal
    entry = odds_state.get(sid)
    if not entry:
        return signal

    merged = dict(signal)
    # Immutable scan-time fields are never overwritten (odds, ev_pct stay as-is)
    _REFRESH_FIELDS = (
        "initial_odds", "initial_ev_pct",
        "current_odds", "current_ev_pct",
        "odds_ts", "odds_source", "odds_fetch_tier",
        "signal_status", "odds_history",
    )
    for field in _REFRESH_FIELDS:
        val = entry.get(field)
        if val is not None:
            merged[field] = val

    # W2: reject corrupted EV at publish time so it never reaches signals.json
    import math
    cev = merged.get("current_ev_pct")
    if cev is not None:
        try:
            fcev = float(cev)
            if not math.isfinite(fcev) or abs(fcev) > _EV_MAX_ABS:
                merged["current_ev_pct"] = None
                if merged.get("signal_status") == "ACTIVE":
                    merged["signal_status"] = "STALE_ODDS"
        except (TypeError, ValueError):
            merged["current_ev_pct"] = None

    return merged
