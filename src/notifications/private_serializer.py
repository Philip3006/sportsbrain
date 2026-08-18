"""
P0C-002 — Authenticated private state serialization boundary.

The Cloudflare Worker GET /me endpoint returns the payload produced by
serialize_private_state(). It is the mirror-image of P0C-001's public
serializer: an explicit ALLOWLIST of private-state top-level keys.

Public product fields (schedule, model_tips, football, tennis, all_odds, …)
are intentionally EXCLUDED from /me — the PWA fetches those from the public
GET /signals.json channel via the dual-fetch architecture.

Owner routing:
  The `owner` parameter is the authenticated per-user identity (never a
  fallback). Serialization does not verify ownership itself — the Worker
  handler enforces owner assertion before calling serialize_private_state().
"""
from __future__ import annotations

from typing import Any

# ── Private-state allowlist ────────────────────────────────────────────────
# Only these top-level keys are propagated into the /me payload.
PRIVATE_STATE_KEYS: frozenset[str] = frozenset({
    "bankroll_state",
    "open_bets",
    "settled_bets",
    "history",
    "portfolio",
    "wm_stats",
})

# Public product keys — must NEVER appear in a /me payload. If any of these
# were included, /me would duplicate public data behind auth, breaking the
# dual-channel invariant and inflating the private response surface.
PUBLIC_PRODUCT_KEYS: frozenset[str] = frozenset({
    "schedule", "all_odds", "model_tips", "model_evals",
    "football", "tennis", "top_elo", "wm_results", "odds_history",
    "tennis_stats", "health", "build_info",
})


def serialize_private_state(snapshot: dict | None, owner: str) -> dict:
    """Return the authenticated private payload for `owner`.

    ALLOWLIST-based: only keys in PRIVATE_STATE_KEYS survive. The result
    always contains `schema_version` and `owner`, even for an empty snapshot,
    so downstream consumers can distinguish "no state" from "not authenticated".

    Args:
      snapshot: Parsed KV snapshot for this owner (or None).
      owner:    Authenticated per-user identity (must be non-empty).

    Returns:
      dict with keys: schema_version, owner, and any subset of
      PRIVATE_STATE_KEYS present in `snapshot`.
    """
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner must be a non-empty string")
    result: dict[str, Any] = {"schema_version": "1", "owner": owner}
    if not isinstance(snapshot, dict):
        return result
    for key in PRIVATE_STATE_KEYS:
        if key in snapshot:
            result[key] = snapshot[key]
    return result


def assert_no_public_product_fields(payload: Any, _path: str = "root") -> None:
    """Assert the /me payload contains no public product fields.

    Raises AssertionError with the offending path when a public key is found
    at the top level. Public data must be served via GET /signals.json only —
    duplicating it into /me would inflate the private response and complicate
    caching/auth semantics.
    """
    if not isinstance(payload, dict):
        return
    for k in payload:
        if k in PUBLIC_PRODUCT_KEYS:
            raise AssertionError(
                f"P0C-002 PRIVATE-BOUNDARY VIOLATION — public product key "
                f"'{k}' found at {_path}.{k}"
            )
