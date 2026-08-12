"""Canonical Tennis event state model (Wave 3B).

Separates two truth dimensions:
  Schedule truth  — initial/current start, when SportsBrain accepted it, source
  Lifecycle truth — current canonical status, source authority, observation timestamp

Consumers must not invent lifecycle truth from timestamps; derive status only
from authoritative observations supplied by this module.

Wave 3A invariant preserved: elapsed time alone NEVER creates LIVE.
"""
from __future__ import annotations

import json
import threading
from copy import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[2]
_SIDECAR_PATH = ROOT / "data" / "cache" / "tennis_event_states.json"
_SIDECAR_LOCK = threading.Lock()


# ── Status enum ───────────────────────────────────────────────────────────────

class TennisEventStatus(str, Enum):
    """Bounded lifecycle states. Transitions follow deterministic rules below.

    Status must only be set when qualified source evidence supports it.
    Prefer UNKNOWN over a falsely-confident state.
    """
    UPCOMING       = "UPCOMING"        # start in future, no authoritative LIVE evidence
    AWAITING_START = "AWAITING_START"  # start passed, no authoritative LIVE evidence yet
    DELAYED        = "DELAYED"         # explicit qualified-source evidence of delay
    LIVE           = "LIVE"            # fresh authoritative in-progress evidence (Wave 3A)
    COMPLETED      = "COMPLETED"       # authoritative terminal evidence
    POSTPONED      = "POSTPONED"       # qualified postponement evidence
    CANCELLED      = "CANCELLED"       # qualified cancellation evidence
    UNKNOWN        = "UNKNOWN"         # cannot safely determine state


# ── Source authority ──────────────────────────────────────────────────────────

class SourceAuthority(str, Enum):
    PRIMARY             = "PRIMARY"
    QUALIFIED_SECONDARY = "QUALIFIED_SECONDARY"
    FALLBACK            = "FALLBACK"
    INFORMATIONAL_ONLY  = "INFORMATIONAL_ONLY"
    NOT_AUTHORIZED      = "NOT_AUTHORIZED"


# Authority matrix: provider → capability → authority level
AUTHORITY_MATRIX: dict[str, dict[str, SourceAuthority]] = {
    "the_odds_api": {
        "fixture_existence": SourceAuthority.PRIMARY,
        "initial_start":     SourceAuthority.PRIMARY,
        "updated_start":     SourceAuthority.PRIMARY,
        "explicit_delay":    SourceAuthority.QUALIFIED_SECONDARY,
        "live":              SourceAuthority.NOT_AUTHORIZED,
        "score":             SourceAuthority.NOT_AUTHORIZED,
        "completion":        SourceAuthority.NOT_AUTHORIZED,
        "cancellation":      SourceAuthority.QUALIFIED_SECONDARY,
    },
    "espn": {
        "fixture_existence": SourceAuthority.QUALIFIED_SECONDARY,
        "initial_start":     SourceAuthority.QUALIFIED_SECONDARY,
        "updated_start":     SourceAuthority.QUALIFIED_SECONDARY,
        "explicit_delay":    SourceAuthority.QUALIFIED_SECONDARY,
        "live":              SourceAuthority.PRIMARY,
        "score":             SourceAuthority.PRIMARY,
        "completion":        SourceAuthority.PRIMARY,
        "cancellation":      SourceAuthority.PRIMARY,
    },
    "tennisexplorer": {
        "fixture_existence": SourceAuthority.QUALIFIED_SECONDARY,
        "initial_start":     SourceAuthority.QUALIFIED_SECONDARY,
        "updated_start":     SourceAuthority.FALLBACK,
        "explicit_delay":    SourceAuthority.INFORMATIONAL_ONLY,
        "live":              SourceAuthority.NOT_AUTHORIZED,
        "score":             SourceAuthority.INFORMATIONAL_ONLY,
        "completion":        SourceAuthority.INFORMATIONAL_ONLY,
        "cancellation":      SourceAuthority.INFORMATIONAL_ONLY,
    },
    "implied_elo": {
        "fixture_existence": SourceAuthority.INFORMATIONAL_ONLY,
        "initial_start":     SourceAuthority.NOT_AUTHORIZED,
        "updated_start":     SourceAuthority.NOT_AUTHORIZED,
        "explicit_delay":    SourceAuthority.NOT_AUTHORIZED,
        "live":              SourceAuthority.NOT_AUTHORIZED,
        "score":             SourceAuthority.NOT_AUTHORIZED,
        "completion":        SourceAuthority.NOT_AUTHORIZED,
        "cancellation":      SourceAuthority.NOT_AUTHORIZED,
    },
}

_AUTHORITY_RANK: dict[SourceAuthority, int] = {
    SourceAuthority.PRIMARY:             4,
    SourceAuthority.QUALIFIED_SECONDARY: 3,
    SourceAuthority.FALLBACK:            2,
    SourceAuthority.INFORMATIONAL_ONLY:  1,
    SourceAuthority.NOT_AUTHORIZED:      0,
}


def authority_rank(provider: str, capability: str) -> int:
    """Numeric rank for a provider/capability pair. 0 = not authorized."""
    entry = AUTHORITY_MATRIX.get(provider, {})
    auth = entry.get(capability, SourceAuthority.NOT_AUTHORIZED)
    return _AUTHORITY_RANK.get(auth, 0)


# ── Provider status normalization ─────────────────────────────────────────────

_ESPN_STATUS_MAP: dict[str, TennisEventStatus] = {
    "scheduled":   TennisEventStatus.UPCOMING,
    "not_started": TennisEventStatus.UPCOMING,
    "delayed":     TennisEventStatus.DELAYED,
    "in_progress": TennisEventStatus.LIVE,
    "suspended":   TennisEventStatus.LIVE,     # suspended still in Live tab (Wave 3A)
    "postponed":   TennisEventStatus.POSTPONED,
    "completed":   TennisEventStatus.COMPLETED,
    "retired":     TennisEventStatus.COMPLETED,
    "walkover":    TennisEventStatus.COMPLETED,
    "abandoned":   TennisEventStatus.CANCELLED,
    "cancelled":   TennisEventStatus.CANCELLED,
}


def normalize_provider_status(raw: str, provider: str) -> TennisEventStatus:
    """Map provider-specific status string to TennisEventStatus.

    Provider strings must not leak into application logic.
    Returns UNKNOWN when the raw value cannot be mapped confidently.
    """
    if not raw:
        return TennisEventStatus.UNKNOWN
    s = raw.lower().strip()
    if provider in ("espn", "tennis_live_push"):
        return _ESPN_STATUS_MAP.get(s, TennisEventStatus.UNKNOWN)
    if provider == "the_odds_api":
        # TheOddsAPI is pre-match only; it has no live status authority
        return TennisEventStatus.UPCOMING
    return TennisEventStatus.UNKNOWN


# ── Canonical event state ──────────────────────────────────────────────────────

@dataclass
class TennisEventState:
    """Canonical representation of a Tennis fixture lifecycle.

    Schedule truth and lifecycle truth are kept in separate fields so that
    a schedule update (e.g. delay) does not silently overwrite status, and
    a live provider saying in_progress does not rewrite the expected start.
    """

    # ── Schedule truth ────────────────────────────────────────────────────────
    # First qualified start time SportsBrain accepted — immutable after first valid assignment.
    scheduled_start_initial: str = ""
    # Latest qualified expected start — mutable on schedule updates.
    scheduled_start_current: str = ""
    # When SportsBrain last accepted a schedule update (not necessarily provider timestamp).
    schedule_updated_at: str = ""
    # Provider of the current schedule truth.
    schedule_source: str = ""

    # ── Lifecycle truth ───────────────────────────────────────────────────────
    event_status: TennisEventStatus = TennisEventStatus.UNKNOWN
    # Provider responsible for the current canonical status.
    status_source: str = ""
    # When the status-bearing observation was made.
    status_updated_at: str = ""

    # ── Metadata ──────────────────────────────────────────────────────────────
    fixture_key: str = ""    # canonical_match_key(home, away)
    accepted_at: str = ""    # when SportsBrain first created this record

    def to_dict(self) -> dict:
        d = asdict(self)
        d["event_status"] = self.event_status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TennisEventState":
        raw_status = d.get("event_status", TennisEventStatus.UNKNOWN.value)
        try:
            status = TennisEventStatus(raw_status)
        except ValueError:
            status = TennisEventStatus.UNKNOWN
        return cls(
            scheduled_start_initial=d.get("scheduled_start_initial", ""),
            scheduled_start_current=d.get("scheduled_start_current", ""),
            schedule_updated_at=d.get("schedule_updated_at", ""),
            schedule_source=d.get("schedule_source", ""),
            event_status=status,
            status_source=d.get("status_source", ""),
            status_updated_at=d.get("status_updated_at", ""),
            fixture_key=d.get("fixture_key", ""),
            accepted_at=d.get("accepted_at", ""),
        )


# ── Status derivation (schedule-only, no live authority) ─────────────────────

def derive_schedule_status(
    state: TennisEventState,
    now_utc: datetime,
) -> TennisEventStatus:
    """Derive a status from schedule truth only (no live observation).

    Used for display and scheduling decisions.
    Wave 3A invariant: this function NEVER returns LIVE or COMPLETED.
    Those states require authoritative live/completion evidence.
    """
    terminal = {
        TennisEventStatus.COMPLETED,
        TennisEventStatus.CANCELLED,
        TennisEventStatus.POSTPONED,
    }
    if state.event_status in terminal:
        return state.event_status

    current = state.scheduled_start_current
    if not current:
        return TennisEventStatus.UNKNOWN
    try:
        ko = datetime.fromisoformat(current.replace("Z", "+00:00"))
        if ko.tzinfo is None:
            ko = ko.replace(tzinfo=timezone.utc)
        elapsed_sec = (now_utc - ko).total_seconds()
        return TennisEventStatus.UPCOMING if elapsed_sec < 0 else TennisEventStatus.AWAITING_START
    except (ValueError, TypeError):
        return TennisEventStatus.UNKNOWN


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Merge semantics ──────────────────────────────────────────────────────────

def merge_schedule_update(
    existing: TennisEventState,
    new_start: str,
    source: str,
    observed_at: str = "",
) -> TennisEventState:
    """Apply a schedule observation using authority and recency precedence.

    Rules:
    - scheduled_start_initial is immutable after first valid assignment.
    - A lower-authority source must not overwrite a higher-authority accepted update.
    - When authority is equal, the newer observation wins.
    """
    updated = copy(existing)
    now_iso = observed_at or _now_iso()

    # Initial start: immutable after first valid assignment
    if not updated.scheduled_start_initial and new_start:
        updated.scheduled_start_initial = new_start
        if not updated.accepted_at:
            updated.accepted_at = now_iso

    # Current start: update when incoming authority >= existing authority
    incoming_rank = authority_rank(source, "updated_start")
    existing_rank = authority_rank(updated.schedule_source, "updated_start")

    if incoming_rank >= existing_rank and new_start:
        # At equal authority: only update if observation is not older
        if incoming_rank == existing_rank:
            existing_ts = _parse_ts(updated.schedule_updated_at)
            incoming_ts = _parse_ts(now_iso)
            if existing_ts and incoming_ts and incoming_ts < existing_ts:
                return updated  # stale equal-authority source — do not overwrite

        updated.scheduled_start_current = new_start
        updated.schedule_source = source
        updated.schedule_updated_at = now_iso

    return updated


def apply_status_observation(
    existing: TennisEventState,
    new_status: TennisEventStatus,
    source: str,
    observed_at: str = "",
) -> TennisEventState:
    """Apply a status observation using authority precedence.

    Terminal states (COMPLETED, CANCELLED) set by a PRIMARY source are sticky
    and cannot be overwritten by lower-authority sources.
    """
    updated = copy(existing)
    now_iso = observed_at or _now_iso()

    incoming_rank = authority_rank(source, "live")  # live authority governs status updates
    existing_rank = authority_rank(updated.status_source, "live")

    terminal = {TennisEventStatus.COMPLETED, TennisEventStatus.CANCELLED}
    if (
        updated.event_status in terminal
        and existing_rank >= _AUTHORITY_RANK[SourceAuthority.PRIMARY]
    ):
        return updated  # sticky terminal — do not overwrite

    if incoming_rank > 0 and (incoming_rank >= existing_rank or new_status in terminal):
        updated.event_status = new_status
        updated.status_source = source
        updated.status_updated_at = now_iso

    return updated


# ── Sidecar persistence ───────────────────────────────────────────────────────

def _sidecar_key(fixture_key: str, kickoff: str) -> str:
    """Stable sidecar key: fixture identity + date (aligns with signal_id semantics)."""
    date = (kickoff or "")[:10]
    return f"{fixture_key}:{date}"


def load_event_states() -> dict[str, TennisEventState]:
    """Load persisted canonical event states from sidecar. Returns empty dict on error."""
    with _SIDECAR_LOCK:
        if not _SIDECAR_PATH.exists():
            return {}
        try:
            raw = json.loads(_SIDECAR_PATH.read_text())
            return {k: TennisEventState.from_dict(v) for k, v in raw.items()}
        except Exception:
            return {}


def save_event_states(states: dict[str, TennisEventState]) -> None:
    """Persist canonical event states to sidecar. Silently drops on error."""
    with _SIDECAR_LOCK:
        try:
            _SIDECAR_PATH.parent.mkdir(parents=True, exist_ok=True)
            _SIDECAR_PATH.write_text(
                json.dumps({k: v.to_dict() for k, v in states.items()}, indent=2)
            )
        except Exception:
            pass


# ── Schedule enrichment (called from tennis_scan.py) ─────────────────────────

def enrich_schedule_with_canonical_state(
    schedule: list[dict],
    source: str = "the_odds_api",
    now_utc: datetime | None = None,
) -> list[dict]:
    """Add canonical event-state fields to each schedule entry in-place.

    Loads the sidecar, merges new observations, saves the sidecar, then
    writes canonical fields onto each entry so downstream consumers and the
    published signals.json see them.

    Published canonical fields added to each entry:
      scheduled_start_initial  — immutable first accepted start
      scheduled_start_current  — latest accepted start (= kickoff for most matches)
      schedule_updated_at      — when SportsBrain accepted this update
      event_status             — canonical TennisEventStatus value string
      status_source            — which provider last set the status
      status_updated_at        — when status was last updated
    """
    if not schedule:
        return schedule

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    states = load_event_states()
    observed_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    for entry in schedule:
        if entry.get("sport", "football") != "tennis":
            continue

        home = entry.get("home", "")
        away = entry.get("away", "")
        kickoff = entry.get("kickoff", "")

        # Derive fixture key using same logic as canonical_match_key in tennis_scores.py
        try:
            from src.data.tennis_scores import canonical_match_key
            fk = canonical_match_key(home, away)
        except Exception:
            import unicodedata, re as _re
            def _clean(s: str) -> str:
                s = unicodedata.normalize("NFD", s or "")
                s = "".join(c for c in s if unicodedata.category(c) != "Mn")
                s = _re.sub(r"[^a-z0-9]+", "", s.lower())
                return s
            ca, cb = _clean(home), _clean(away)
            fk = f"{ca}|{cb}" if ca <= cb else f"{cb}|{ca}"

        sk = _sidecar_key(fk, kickoff)

        # Load or create canonical state
        state = states.get(sk, TennisEventState(fixture_key=fk, accepted_at=observed_at))

        # Merge schedule observation
        entry_source = entry.get("odds_source", source)
        # Normalize TE source name for authority lookup
        if str(entry_source).startswith("te:") or "tennisexplorer" in str(entry_source):
            entry_source = "tennisexplorer"
        elif entry_source not in AUTHORITY_MATRIX:
            entry_source = "the_odds_api"

        state = merge_schedule_update(state, kickoff, entry_source, observed_at)

        # Derive schedule-based status when lifecycle is not yet authoritative.
        # Set directly — apply_status_observation requires provider authority, but
        # schedule-derived status is an internal computation, not a provider claim.
        non_schedule_statuses = {
            TennisEventStatus.LIVE,
            TennisEventStatus.COMPLETED,
            TennisEventStatus.CANCELLED,
            TennisEventStatus.POSTPONED,
        }
        if state.event_status not in non_schedule_statuses:
            derived = derive_schedule_status(state, now_utc)
            if derived != TennisEventStatus.UNKNOWN:
                state.event_status = derived
                state.status_source = "sportsbrain_schedule"
                state.status_updated_at = observed_at

        states[sk] = state

        # Write canonical fields onto the schedule entry
        entry["scheduled_start_initial"] = state.scheduled_start_initial
        entry["scheduled_start_current"] = state.scheduled_start_current
        entry["schedule_updated_at"] = state.schedule_updated_at
        entry["event_status"] = state.event_status.value
        entry["status_source"] = state.status_source
        entry["status_updated_at"] = state.status_updated_at

    save_event_states(states)
    return schedule
