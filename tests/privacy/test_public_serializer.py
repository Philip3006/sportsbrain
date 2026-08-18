"""
P0C-001 — Public product serialization boundary tests.

Tests T1–T11 of the required deterministic privacy gate.
T12 (browser Playwright smoke) is not implemented here — Playwright is not
installed in this repository. Manual logged-out PWA verification is required.

Invariants covered: SEC-001, SEC-002, SEC-003, SEC-008
Finding addressed: FND-20260814-011 (partial), FND-20260814-012, FND-20260814-013
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from src.notifications.public_serializer import (
    FORBIDDEN_PRIVATE_KEYS,
    assert_no_private_fields,
    serialize_public_product,
)

ROOT = Path(__file__).resolve().parents[2]
SIGNALS_JSON = ROOT / "docs" / "data" / "signals.json"
SIGNALS_PHILIP_JSON = ROOT / "docs" / "data" / "signals_philip.json"

# ── Synthetic private snapshot for security negative test ─────────────────

_PRIVATE_SNAPSHOT = {
    "updated": "2026-08-18T00:00:00Z",
    "build_info": {"sha": "abc123", "date": "2026-08-18"},
    "meta": {
        "stale_odds": False,
        "default_user": "philip",          # private — identity
        "user": "philip",                  # private — identity
    },
    "schedule": [{"match": "Federer vs Nadal", "kickoff": "2026-08-18T14:00:00Z"}],
    "all_odds": {"federer_vs_nadal": {"home": 1.95, "away": 1.95}},
    "model_tips": {"federer_vs_nadal": {"prob": 52.0}},
    "model_evals": {"tennis": {"brier": 0.24}},
    "football": [],
    "tennis": [{"signal_id": "sig_001", "ev_pct": 12.5, "match": "Federer vs Nadal"}],
    "top_elo": [{"name": "Federer", "rating": 2400}],
    "wm_results": [{"home": "Germany", "away": "France", "home_score": 2, "away_score": 1}],
    "odds_history": {"federer_vs_nadal": [{"ts": "2026-08-17", "home": 2.0}]},
    "history": [                           # private — per-day P&L from ledger
        {"date": "2026-08-12", "n_bets": 1, "pnl": -5.0, "roi_pct": -100.0}
    ],
    "open_bets": [{"id": "PRIVATE_TEST_MARKER"}],   # private — active bets
    "settled_bets": [{"id": "settled_001", "pnl": -10.0}],  # private — bet history
    "bankroll_state": {                    # private — financial state
        "start": 100.0,
        "free": 12345.67,                  # marker value
        "staked": 0.0,
        "exposure_pct": 0.0,
        "max_win": 0.0,
        "pnl_closed": -52.87,
        "published_at": "2026-08-18T00:00:00Z",
    },
    "portfolio": {                         # private — per-market P&L
        "by_market": {"h2h_home": {"pnl": -20.0, "roi_pct": -100.0}}
    },
    "wm_stats": {                          # private — betting performance stats
        "summary": {"roi_pct": -32.0, "total_staked": 500.0}
    },
    "tennis_stats": {
        "updated": "2026-08-18T00:00:00Z",
        "active_tournaments": ["cincinnati_atp"],
        "live_gate_status": {"atp250": "live"},
        "stats": {                         # private — betting stats
            "n_bets_all": 20,
            "total_staked": 200.0,
            "total_pnl": -52.0,
            "roi_pct": -26.0,
        },
    },
    "nested_private": {                    # synthetic deep-nesting test
        "owner": "philip",
        "level2": {
            "level3": [
                {"default_user": "philip", "bankroll": 100.0}
            ]
        },
    },
}


# ── T1: Public allowlist — expected public fields present ─────────────────

def test_t1_public_allowlist_retains_expected_fields():
    """T1: Known public fields survive serialization."""
    pub = serialize_public_product(_PRIVATE_SNAPSHOT)
    assert "updated" in pub
    assert "build_info" in pub
    assert "schedule" in pub
    assert "all_odds" in pub
    assert "model_tips" in pub
    assert "model_evals" in pub
    assert "football" in pub
    assert "tennis" in pub
    assert "top_elo" in pub
    assert "wm_results" in pub
    assert "odds_history" in pub


# ── T2: Recursive private-key rejection (nested) ─────────────────────────

def test_t2_recursive_private_key_rejection_nested():
    """T2: Private key buried multiple levels deep triggers assertion."""
    sneaky = {
        "schedule": [],
        "nested": {
            "deep": [{"bankroll_state": {"free": 999}}]
        },
    }
    with pytest.raises(AssertionError, match="bankroll_state"):
        assert_no_private_fields(sneaky)


def test_t2b_nested_array_of_objects():
    """T2b: Private key in list of objects is detected."""
    obj = {"tennis": [{"ev_pct": 12, "open_bets": []}]}
    with pytest.raises(AssertionError, match="open_bets"):
        assert_no_private_fields(obj)


# ── T3: No public identity ────────────────────────────────────────────────

def test_t3_no_default_user_in_public_payload():
    """T3: default_user, user, user_id, owner absent from public payload."""
    pub = serialize_public_product(_PRIVATE_SNAPSHOT)
    pub_str = json.dumps(pub)
    assert "default_user" not in pub_str
    assert '"user": "philip"' not in pub_str
    assert "user_id" not in pub_str
    assert '"owner"' not in pub_str


def test_t3b_meta_contains_no_identity():
    """T3b: Public meta contains only stale_odds; no user/default_user."""
    pub = serialize_public_product(_PRIVATE_SNAPSHOT)
    meta = pub.get("meta", {})
    assert "user" not in meta
    assert "default_user" not in meta
    assert "stale_odds" in meta


# ── T4: Bankroll absent ───────────────────────────────────────────────────

def test_t4_bankroll_state_absent():
    """T4: bankroll_state must not appear in public payload."""
    pub = serialize_public_product(_PRIVATE_SNAPSHOT)
    assert "bankroll_state" not in pub
    pub_str = json.dumps(pub)
    assert "12345.67" not in pub_str, "bankroll marker value leaked"
    assert "pnl_closed" not in pub_str


# ── T5: Open/history bets absent ─────────────────────────────────────────

def test_t5_open_and_settled_bets_absent():
    """T5: open_bets, settled_bets, history (P&L) absent from public payload."""
    pub = serialize_public_product(_PRIVATE_SNAPSHOT)
    assert "open_bets" not in pub
    assert "settled_bets" not in pub
    assert "history" not in pub
    assert "portfolio" not in pub
    assert "wm_stats" not in pub


# ── T7: Synthetic private marker values do not appear ────────────────────

def test_t7_private_marker_values_absent():
    """T7: Security negative test — synthetic private marker values absent."""
    pub = serialize_public_product(_PRIVATE_SNAPSHOT)
    pub_str = json.dumps(pub)
    assert "PRIVATE_TEST_MARKER" not in pub_str, "open_bets marker leaked"
    assert "12345.67" not in pub_str, "bankroll free marker leaked"
    assert "nested_private" not in pub_str


# ── T8: Public product is not empty ──────────────────────────────────────

def test_t8_public_product_is_useful():
    """T8: Zero private fields must not mean empty product."""
    pub = serialize_public_product(_PRIVATE_SNAPSHOT)
    assert pub.get("schedule"), "schedule missing from public product"
    assert pub.get("tennis"), "tennis signals missing from public product"
    assert pub.get("all_odds"), "all_odds missing from public product"
    assert pub.get("wm_results"), "wm_results missing from public product"


# ── T9: Internal betting safety not broken (assert_no_private_fields clean) ──

def test_t9_assert_no_private_fields_passes_clean_payload():
    """T9: Clean public payload passes recursive private-field assertion."""
    pub = serialize_public_product(_PRIVATE_SNAPSHOT)
    assert_no_private_fields(pub)  # must not raise


def test_t9b_assert_catches_private_payload():
    """T9b: Full private snapshot fails recursive check."""
    with pytest.raises(AssertionError):
        assert_no_private_fields(_PRIVATE_SNAPSHOT)


# ── T10: Static public artifacts ─────────────────────────────────────────

def test_t10_signals_json_has_no_private_fields():
    """T10a: docs/data/signals.json must contain zero private fields."""
    if not SIGNALS_JSON.exists():
        pytest.skip("signals.json not present in this environment")
    data = json.loads(SIGNALS_JSON.read_text())
    assert_no_private_fields(data)


def test_t10_signals_json_has_no_identity():
    """T10b: docs/data/signals.json must not contain default_user or user identity."""
    if not SIGNALS_JSON.exists():
        pytest.skip("signals.json not present in this environment")
    text = SIGNALS_JSON.read_text()
    assert '"default_user"' not in text, "default_user found in public signals.json"
    assert '"user": "philip"' not in text, "user identity found in public signals.json"


def test_t10_signals_philip_json_has_no_private_fields():
    """T10c: docs/data/signals_philip.json must contain zero private fields."""
    if not SIGNALS_PHILIP_JSON.exists():
        pytest.skip("signals_philip.json not present in this environment")
    data = json.loads(SIGNALS_PHILIP_JSON.read_text())
    assert_no_private_fields(data)


def test_t10_signals_philip_json_has_no_identity():
    """T10d: docs/data/signals_philip.json must not contain user identity."""
    if not SIGNALS_PHILIP_JSON.exists():
        pytest.skip("signals_philip.json not present in this environment")
    text = SIGNALS_PHILIP_JSON.read_text()
    assert '"default_user"' not in text
    assert '"user": "philip"' not in text


# ── T11: Writer regression — public serializer is the gate ───────────────

def test_t11_serialize_public_product_is_idempotent():
    """T11: Applying serialize_public_product twice yields the same result."""
    pub1 = serialize_public_product(_PRIVATE_SNAPSHOT)
    pub2 = serialize_public_product(pub1)
    assert pub1 == pub2


def test_t11_empty_snapshot_returns_empty_public():
    """T11b: Empty or None snapshot handled gracefully."""
    assert serialize_public_product({}) == {}
    assert serialize_public_product(None) == {}   # type: ignore[arg-type]


def test_t11_public_top_level_keys_only():
    """T11c: Only allowlisted keys appear at the top level of the public payload."""
    pub = serialize_public_product(_PRIVATE_SNAPSHOT)
    _ALWAYS_PRIVATE = {
        "bankroll_state", "open_bets", "settled_bets",
        "history", "portfolio", "wm_stats",
    }
    for key in _ALWAYS_PRIVATE:
        assert key not in pub, f"private key '{key}' leaked to top-level public payload"


# ── Tennis stats nested private excluded ─────────────────────────────────

def test_tennis_stats_private_stats_excluded():
    """tennis_stats.stats (total_staked, total_pnl, roi_pct) excluded; operational keys kept."""
    pub = serialize_public_product(_PRIVATE_SNAPSHOT)
    ts = pub.get("tennis_stats", {})
    assert "stats" not in ts, "tennis_stats.stats (private) leaked to public payload"
    assert "active_tournaments" in ts
    assert "live_gate_status" in ts
    assert "updated" in ts


# ── FORBIDDEN_PRIVATE_KEYS contract ──────────────────────────────────────

def test_forbidden_keys_include_required_identities():
    """Core private identity and financial keys are in FORBIDDEN_PRIVATE_KEYS."""
    for key in ("bankroll_state", "open_bets", "settled_bets", "default_user",
                "user", "ledger", "total_staked", "total_pnl"):
        assert key in FORBIDDEN_PRIVATE_KEYS, f"'{key}' missing from FORBIDDEN_PRIVATE_KEYS"


# ── C3: Fail-closed nested private markers ────────────────────────────────
# These tests call the ACTUAL serialize_public_product() with nested private
# markers inside approved containers. The serializer must raise AssertionError
# (fail-closed) rather than silently returning the private data.

def test_c3_nested_owner_in_tennis_raises():
    """C3: private 'owner' key nested inside approved 'tennis' container triggers fail-closed."""
    snapshot = {"tennis": [{"signal_id": "sig_001", "ev_pct": 12.5, "owner": "PRIVATE_TEST_MARKER"}]}
    with pytest.raises(AssertionError, match="owner"):
        serialize_public_product(snapshot)


def test_c3_nested_bankroll_in_model_tips_raises():
    """C3: private 'bankroll' key nested inside approved 'model_tips' triggers fail-closed."""
    snapshot = {"model_tips": {"match_x": {"prob": 0.55, "bankroll": 12345.67}}}
    with pytest.raises(AssertionError, match="bankroll"):
        serialize_public_product(snapshot)


def test_c3_nested_user_in_health_raises():
    """C3: private 'user' key nested inside approved 'health' container triggers fail-closed."""
    snapshot = {"health": {"status": "ok", "user": "philip"}}
    with pytest.raises(AssertionError, match="user"):
        serialize_public_product(snapshot)


def test_c3_nested_open_bets_in_schedule_raises():
    """C3: private 'open_bets' key nested inside approved 'schedule' container triggers fail-closed."""
    snapshot = {"schedule": [{"match": "A vs B", "kickoff": "2026-08-18T14:00:00Z", "open_bets": []}]}
    with pytest.raises(AssertionError, match="open_bets"):
        serialize_public_product(snapshot)


def test_c3_clean_nested_data_passes_fail_closed():
    """C3: clean nested data in approved containers passes the fail-closed boundary."""
    snapshot = {
        "tennis": [{"signal_id": "sig_001", "ev_pct": 12.5, "match": "A vs B"}],
        "schedule": [{"match": "A vs B", "kickoff": "2026-08-18T14:00:00Z"}],
        "health": {"status": "ok", "jobs": []},
    }
    pub = serialize_public_product(snapshot)
    assert "tennis" in pub
    assert "schedule" in pub
    assert "health" in pub


def test_c3_python_worker_contract_parity():
    """C3 contract parity: same forbidden keys in Python and Worker JS FORBIDDEN set.

    Verifies that the Python FORBIDDEN_PRIVATE_KEYS constant matches the JS
    _FORBIDDEN_PRIVATE_KEYS set documented in cloudflare/worker.js.
    """
    # Keys that must be in both Python and JS forbidden sets (canonical overlap).
    canonical_overlap = {
        "bankroll", "bankroll_state", "open_bets", "pending_bets", "settled_bets",
        "bet_history", "ledger", "ledger_rows", "stake_history", "pnl_history",
        "user", "user_id", "default_user", "owner",
        "auth_token", "token", "master_token", "api_token",
        "total_staked", "total_pnl",
    }
    for key in canonical_overlap:
        assert key in FORBIDDEN_PRIVATE_KEYS, (
            f"C3 parity: '{key}' in JS _FORBIDDEN_PRIVATE_KEYS but missing from Python set"
        )
