"""
P0C-001 — Public product serialization boundary.

ALL public static artifacts (docs/data/signals.json, signals_philip.json) and
the Worker public GET /signals.json endpoint MUST derive from
serialize_public_product(). Private financial and identity fields are excluded
by an explicit ALLOWLIST — not a blacklist.

Architecture: public_payload = serialize_public_product(full_internal_snapshot)

The full internal/private snapshot is still written to Cloudflare KV for
Worker POST /pending-bet validation (bankroll cap, open bet count). The public
serializer is applied ONLY at the static-file write boundary and at the Worker
GET response boundary.
"""
from __future__ import annotations

from typing import Any

# ── Top-level public-product allowlist ─────────────────────────────────────
# Only keys in this set are permitted in the public payload.
# Excluded top-level: bankroll_state, open_bets, settled_bets, history (P&L),
# portfolio, wm_stats — all contain private financial state.
# meta and tennis_stats are included but reconstructed (see below).
_PUBLIC_TOP_LEVEL_KEYS: frozenset[str] = frozenset({
    "updated",
    "build_info",
    "schedule",
    "all_odds",
    "model_tips",
    "model_evals",
    "football",
    "tennis",
    "top_elo",
    "wm_results",
    "odds_history",
    "health",
})

# Keys that MUST NOT appear anywhere in a public payload (recursive check).
# Used by assert_no_private_fields() as the security assertion gate.
FORBIDDEN_PRIVATE_KEYS: frozenset[str] = frozenset({
    # Financial state
    "bankroll",
    "bankroll_state",
    "open_bets",
    "pending_bets",
    "settled_bets",
    "bet_history",
    "ledger",
    "ledger_rows",
    "stake_history",
    "pnl_history",
    # Identity / ownership
    "user",
    "user_id",
    "default_user",
    "owner",
    # Credentials
    "auth_token",
    "token",
    "master_token",
    "api_token",
    # Private financial aggregates (from wm_stats/tennis_stats/portfolio)
    "total_staked",
    "total_pnl",
})


def _public_meta(meta: dict | None) -> dict:
    """Return a public-safe meta object — operational flags only, no identity."""
    if not isinstance(meta, dict):
        return {}
    return {"stale_odds": bool(meta.get("stale_odds", False))}


def _public_tennis_stats(ts: dict | None) -> dict:
    """Return public-safe tennis_stats — operational context only, no financial stats.

    ts["stats"] contains total_staked, total_pnl, roi_pct — these are private.
    Only the tournament/gate operational fields are public.
    """
    if not isinstance(ts, dict):
        return {}
    result: dict = {}
    for key in ("updated", "active_tournaments", "live_gate_status"):
        if key in ts:
            result[key] = ts[key]
    return result


def serialize_public_product(snapshot: dict | None) -> dict:
    """Return a public-safe product payload derived from a full internal snapshot.

    Implements an explicit ALLOWLIST: every top-level key must be on the list.
    Nested objects with mixed public/private fields (meta, tennis_stats) are
    reconstructed using their own allowlists.

    Private state excluded:
      - bankroll_state (free, staked, exposure_pct, max_win, pnl_closed,
        published_at)
      - open_bets (active bet list)
      - settled_bets (bet history)
      - history (per-day P&L derived from ledger)
      - portfolio (per-market P&L derived from ledger)
      - wm_stats (WM betting performance stats: drawdown, CLV, ROI …)
      - meta.user / meta.default_user (default-user identity)
      - tennis_stats.stats (total_staked, total_pnl, roi_pct …)
    """
    if not isinstance(snapshot, dict):
        return {}
    pub: dict = {}
    for key in _PUBLIC_TOP_LEVEL_KEYS:
        if key in snapshot:
            pub[key] = snapshot[key]
    # Reconstruct objects that contain a mix of public and private fields.
    if "meta" in snapshot:
        pub["meta"] = _public_meta(snapshot["meta"])
    if "tennis_stats" in snapshot:
        pub["tennis_stats"] = _public_tennis_stats(snapshot["tennis_stats"])
    return pub


def assert_no_private_fields(obj: Any, _path: str = "root") -> None:
    """Recursively assert that no forbidden private key appears in a public payload.

    Raises AssertionError with the offending path when a private key is found.
    Walks dicts, lists, and nested combinations to any depth.

    Used as the deterministic security gate in tests/privacy/test_public_serializer.py
    and can be called at static-file write time to enforce the invariant.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_PRIVATE_KEYS:
                raise AssertionError(
                    f"P0C-001 PRIVACY VIOLATION — forbidden key '{k}' found at {_path}.{k}"
                )
            assert_no_private_fields(v, _path=f"{_path}.{k}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            assert_no_private_fields(item, _path=f"{_path}[{i}]")
