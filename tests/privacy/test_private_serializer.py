"""
P0C-002 — Authenticated private state serialization boundary tests.

Mirrors the Node test suite (Suite 16 of tests/js/worker.test.mjs). These are
allowlist unit tests for src/notifications/private_serializer.py.

Invariants covered:
  • serialize_private_state returns ONLY allowlisted private keys.
  • Public product keys (schedule, tennis, model_tips, …) are stripped.
  • owner and schema_version are always present.
  • owner must be a non-empty string (fail closed).
"""
from __future__ import annotations

import pytest

from src.notifications.private_serializer import (
    PRIVATE_STATE_KEYS,
    PUBLIC_PRODUCT_KEYS,
    assert_no_public_product_fields,
    serialize_private_state,
)


_PRIVATE_SNAPSHOT = {
    "updated": "2026-08-18T00:00:00Z",
    # Public product keys — must be stripped
    "schedule": [{"match": "A vs B"}],
    "tennis": [{"signal_id": "sig_001"}],
    "football": [],
    "model_tips": {"x": {}},
    "all_odds": {"a": 1.95},
    "top_elo": [],
    "wm_results": [],
    "odds_history": {},
    "health": {"overall": "ok"},
    "build_info": {"sha": "abc"},
    "tennis_stats": {"updated": "..."},
    # Private state keys — must survive
    "bankroll_state": {"free": 100, "staked": 0},
    "open_bets": [{"id": "OPEN_MARKER"}],
    "settled_bets": [{"id": "SETTLED_MARKER"}],
    "history": [{"date": "2026-08-12", "pnl": -5}],
    "portfolio": {"h2h_home": {"pnl": 1}},
    "wm_stats": {"total_pnl": -32.0},
    # Random extra keys — must NOT appear (allowlist enforced)
    "some_random_thing": {"foo": "bar"},
}


def test_owner_field_present_and_schema_version() -> None:
    out = serialize_private_state({}, "alice")
    assert out["owner"] == "alice"
    assert out["schema_version"] == "1"


def test_owner_must_be_nonempty_string() -> None:
    with pytest.raises(ValueError):
        serialize_private_state({"open_bets": []}, "")
    with pytest.raises(ValueError):
        serialize_private_state({"open_bets": []}, None)  # type: ignore[arg-type]


def test_allowlist_enforcement_all_private_keys_survive() -> None:
    out = serialize_private_state(_PRIVATE_SNAPSHOT, "philip")
    for k in PRIVATE_STATE_KEYS:
        assert k in out, f"expected private allowlisted key '{k}' present"


def test_public_product_keys_are_stripped() -> None:
    out = serialize_private_state(_PRIVATE_SNAPSHOT, "philip")
    for k in PUBLIC_PRODUCT_KEYS:
        assert k not in out, f"public product key '{k}' leaked into /me payload"


def test_non_allowlisted_keys_are_stripped() -> None:
    out = serialize_private_state(_PRIVATE_SNAPSHOT, "philip")
    assert "some_random_thing" not in out
    # Only allowlist + schema_version + owner allowed.
    allowed = PRIVATE_STATE_KEYS | {"schema_version", "owner"}
    for key in out.keys():
        assert key in allowed, f"unexpected key '{key}' in /me payload"


def test_none_snapshot_returns_only_meta_fields() -> None:
    out = serialize_private_state(None, "alice")
    assert out == {"schema_version": "1", "owner": "alice"}


def test_non_dict_snapshot_treated_as_empty() -> None:
    out = serialize_private_state("not a dict", "alice")  # type: ignore[arg-type]
    assert out == {"schema_version": "1", "owner": "alice"}


def test_assert_no_public_product_fields_passes_on_valid() -> None:
    out = serialize_private_state(_PRIVATE_SNAPSHOT, "philip")
    # Must not raise — public product keys were already stripped.
    assert_no_public_product_fields(out)


def test_assert_no_public_product_fields_raises_on_violation() -> None:
    bad = {"schema_version": "1", "owner": "alice", "schedule": [1, 2]}
    with pytest.raises(AssertionError, match=r"schedule"):
        assert_no_public_product_fields(bad)


def test_owner_marker_never_replaced_with_default() -> None:
    """P0C-002 core: owner is what the caller passed, never coerced to 'philip'."""
    out_alice = serialize_private_state({"bankroll_state": {"free": 1}}, "alice")
    assert out_alice["owner"] == "alice"
    out_bob = serialize_private_state({"bankroll_state": {"free": 1}}, "bob")
    assert out_bob["owner"] == "bob"


def test_marker_values_preserved() -> None:
    out = serialize_private_state(_PRIVATE_SNAPSHOT, "philip")
    assert out["open_bets"][0]["id"] == "OPEN_MARKER"
    assert out["settled_bets"][0]["id"] == "SETTLED_MARKER"
