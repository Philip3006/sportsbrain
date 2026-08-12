"""Stable Tennis fixture identity registry (Wave 3B.1).

Maps (sport_key, player_a, player_b, market) → stable signal_id.

Signal ID is assigned on first encounter and never changes even when kickoff
crosses midnight UTC, the date shifts, or timezone formatting changes.

Public API:
    make_fixture_key(sport_key, player_a, player_b) -> str
        Canonical fixture identity (no market, no date).

    get_or_register(sport_key, player_a, player_b, market, kickoff) -> str
        Return stable signal_id for this fixture+market combination.
        First call: computes from original formula and registers.
        Subsequent calls: returns same ID regardless of kickoff changes.

    lookup_signal_id(sport_key, player_a, player_b, market) -> str | None
        Non-mutating lookup. Returns None if not yet registered.

Registry sidecar: data/cache/tennis_fixture_registry.json
Registry key format: "{normalized_sport_key}:{canonical_player_pair}:{market}"
Fixture key format:  "{normalized_sport_key}:{canonical_player_pair}"
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_REGISTRY_PATH = ROOT / "data" / "cache" / "tennis_fixture_registry.json"
_LOCK = threading.Lock()


# ── Canonical player-pair key ─────────────────────────────────────────────────

def _clean_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def canonical_player_pair(player_a: str, player_b: str) -> str:
    """Sort-normalized player pair. Player A vs B == Player B vs A."""
    ca, cb = _clean_name(player_a), _clean_name(player_b)
    return f"{ca}|{cb}" if ca <= cb else f"{cb}|{ca}"


# ── Sport-key normalization ───────────────────────────────────────────────────

def _normalize_sport_key(sport_key: str) -> str:
    """Stable tournament identity from raw sport_key.

    Strips leading/trailing whitespace and lowercases. Preserves tournament
    differentiators (e.g. 'tennis_atp_washington' ≠ 'tennis_atp_toronto').
    Returns 'tennis' for empty/unknown values so a key is always formed.
    """
    if not sport_key or not sport_key.strip():
        return "tennis"
    return sport_key.lower().strip()


# ── Fixture key (stable, no date, no market) ──────────────────────────────────

def make_fixture_key(sport_key: str, player_a: str, player_b: str) -> str:
    """Canonical fixture identity independent of schedule and market.

    Components:
      - normalized sport_key (tournament differentiator)
      - canonical player pair (sort-normalized, diacritic-stripped)

    Explicitly excluded: kickoff time, date, timezone, odds, lifecycle status.

    Same players in different tournaments → different fixture_key ✓
    Same players in same tournament, player order reversed → same key ✓
    Reschedule from 23:45→00:30 UTC → same key ✓
    """
    sk = _normalize_sport_key(sport_key)
    pair = canonical_player_pair(player_a, player_b)
    return f"{sk}:{pair}"


def _registry_key(sport_key: str, player_a: str, player_b: str, market: str) -> str:
    """Registry lookup key = fixture_key + market."""
    fk = make_fixture_key(sport_key, player_a, player_b)
    return f"{fk}:{market.lower().strip()}"


# ── Signal ID computation (original formula — first-assignment only) ──────────

def _compute_signal_id(sport: str, match: str, market: str, kickoff: str) -> str:
    """Original date-based formula. Used ONLY for first registration."""
    kickoff_date = (kickoff or "")[:10]
    key = f"{sport}:{match}:{market}:{kickoff_date}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


# ── Sidecar persistence ───────────────────────────────────────────────────────

def load_registry() -> dict[str, dict]:
    """Load registry sidecar. Returns empty dict on missing/corrupt file."""
    if not _REGISTRY_PATH.exists():
        return {}
    try:
        raw = _REGISTRY_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _save_registry(registry: dict[str, dict]) -> None:
    """Write registry sidecar. Silently drops on error."""
    try:
        _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REGISTRY_PATH.write_text(
            json.dumps(registry, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError):
        return


# ── Collision detection ───────────────────────────────────────────────────────

def _collision_detected(
    existing_entry: dict,
    sport_key: str,
    player_a: str,
    player_b: str,
) -> bool:
    """True if registry entry has a different player pair than supplied."""
    pair = canonical_player_pair(player_a, player_b)
    stored_pair = canonical_player_pair(
        existing_entry.get("player_a", ""),
        existing_entry.get("player_b", ""),
    )
    # Different players on same registry key = genuine collision
    return pair != stored_pair


# ── Public API ────────────────────────────────────────────────────────────────

def get_or_register(
    sport_key: str,
    player_a: str,
    player_b: str,
    market: str,
    kickoff: str,
    *,
    sport: str = "tennis",
) -> str:
    """Return stable signal_id for this fixture+market.

    First call: computes signal_id from original date formula (preserving
    backward compatibility for signals already in signals.json with the same
    kickoff), registers under (sport_key, player_pair, market), returns it.

    Subsequent calls: returns the registered ID regardless of kickoff changes.

    Cross-midnight reschedule:
      First call with kickoff="2026-08-13T23:45Z" → registers "abc123"
      Later call with kickoff="2026-08-14T00:30Z" → returns "abc123" (stable)

    Collision safety:
      If two genuinely different player pairs hash to the same registry key
      (which cannot happen with current key design), logs a warning and falls
      back to the original formula rather than silently returning wrong ID.
    """
    rk = _registry_key(sport_key, player_a, player_b, market)
    match_str = f"{player_a} vs {player_b}"

    with _LOCK:
        registry = load_registry()
        existing = registry.get(rk)

        if existing:
            if _collision_detected(existing, sport_key, player_a, player_b):
                # Collision: same key, different players. Fail safe: compute fresh.
                print(
                    f"[fixture_registry] COLLISION on key {rk!r}: "
                    f"stored=({existing.get('player_a')}/{existing.get('player_b')}), "
                    f"new=({player_a}/{player_b}) — using date-based ID"
                )
                return _compute_signal_id(sport, match_str, market, kickoff)
            return existing["signal_id"]

        # First registration: compute from original formula with current kickoff.
        # If the fixture already existed in signals.json before 3B.1, it would have
        # had the same kickoff at registration time → same ID → backward compatible.
        sid = _compute_signal_id(sport, match_str, market, kickoff)
        registry[rk] = {
            "signal_id":      sid,
            "sport_key":      sport_key,
            "player_a":       player_a,
            "player_b":       player_b,
            "market":         market,
            "first_kickoff":  kickoff,
            "registered_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _save_registry(registry)
        return sid


def lookup_signal_id(
    sport_key: str,
    player_a: str,
    player_b: str,
    market: str,
) -> str | None:
    """Non-mutating lookup. Returns None if the fixture is not yet registered."""
    rk = _registry_key(sport_key, player_a, player_b, market)
    registry = load_registry()
    entry = registry.get(rk)
    return entry.get("signal_id") if entry else None


def get_all_market_ids(
    sport_key: str,
    player_a: str,
    player_b: str,
) -> dict[str, str]:
    """Return {market: signal_id} for all registered markets of a fixture."""
    fk = make_fixture_key(sport_key, player_a, player_b)
    prefix = f"{fk}:"
    registry = load_registry()
    return {
        k[len(prefix):]: v["signal_id"]
        for k, v in registry.items()
        if k.startswith(prefix) and isinstance(v, dict) and v.get("signal_id")
    }
