"""Regression tests: _evScore() Top-Picks inclusion/exclusion logic.

Tests the corrected fix that:
1. Uses canonical "odds" field (not "decimal_odds") for all signal types.
2. Fails closed for missing/null/zero/non-positive odds in odds-dependent evaluations.
3. Allows tennis match-winner signals with EV >= 5% regardless of odds magnitude.
4. Football home/away signals require valid odds; odds > 2.5 are excluded.
"""
import base64
import functools
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

DOCS_DIR = Path(__file__).parent.parent.parent / "docs"
_NOW = datetime.now(timezone.utc)
_KICKOFF_3H = (_NOW + timedelta(hours=3)).isoformat()
_KICKOFF_20H = (_NOW + timedelta(hours=20)).isoformat()

_BANKROLL_FREE = {
    "start": 100.0, "free": 52.0, "staked": 0,
    "exposure_pct": 0.0, "max_win": 0, "pnl_closed": -48.0,
}

_BASE: dict = {
    "updated": _NOW.isoformat(),
    "football": [],
    "tennis": [],
    "open_bets": [],
    "settled_bets": [],
    "bankroll_state": _BANKROLL_FREE,
    "schedule": [],
    "all_odds": {},
    "model_tips": {},
    "health": {},
    "build_info": {},
    "wm_results": [],
    "wm_stats": {},
    "portfolio": {},
    "top_elo": [],
    "history": {},
    "odds_history": {},
    "model_evals": {},
}


def _sig_tennis(odds=None, ev_pct=9.1, decimal_odds_field=None, confidence="HIGH"):
    """Build a tennis away signal. odds=None → field absent (missing)."""
    s = {
        "sport": "tennis",
        "match": "Alexandrova vs Svitolina",
        "market": "away",
        "model_prob": 73.7,
        "fair_prob": 62.8,
        "ev_pct": ev_pct,
        "stake_eur": 12.0,
        "stake_pct": 12.0,
        "confidence": confidence,
        "n_models_agree": 1,
        "kickoff": _KICKOFF_20H,
        "tour": "wta",
        "tournament": "Canadian Open",
        "category": "wta1000",
        "surface": "hard",
        "best_of": 3,
        "display_priority": 1,
    }
    if odds is not None:
        s["odds"] = odds
    if decimal_odds_field is not None:
        s["decimal_odds"] = decimal_odds_field
    return s


def _sig_football_away(odds=None, ev_pct=30.0, decimal_odds_field=None):
    """Build a football away signal."""
    s = {
        "sport": "football",
        "match": "TeamA vs TeamB",
        "market": "away",
        "model_prob": 46.0,
        "fair_prob": 42.0,
        "ev_pct": ev_pct,
        "stake_eur": 10.0,
        "stake_pct": 10.0,
        "confidence": "HIGH",
        "n_models_agree": 2,
        "kickoff": _KICKOFF_3H,
        "display_priority": 1,
    }
    if odds is not None:
        s["odds"] = odds
    if decimal_odds_field is not None:
        s["decimal_odds"] = decimal_odds_field
    return s


@pytest.fixture(scope="module")
def server_url():
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(DOCS_DIR))
    srv = HTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _inject(page: Page, payload: dict) -> None:
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    page.add_init_script(f"""
        if ('serviceWorker' in navigator) {{
            navigator.serviceWorker.register = () =>
                Promise.reject(new Error('SW disabled in tests'));
        }}
        localStorage.setItem('sb_seen_onboarding', '1');
        const _body = atob('{b64}');
        const _orig = window.fetch.bind(window);
        window.fetch = (url, opts) => String(url).includes('signals.json')
            ? Promise.resolve(new Response(_body, {{status:200,headers:{{'Content-Type':'application/json'}}}}))
            : _orig(url, opts);
    """)


def _home(page: Page, server_url: str, payload: dict) -> None:
    _inject(page, payload)
    page.goto(server_url, wait_until="domcontentloaded")
    page.wait_for_timeout(600)


# ── Tennis: valid canonical odds ──────────────────────────────────────────────

def test_tennis_valid_odds_shows_top_picks(page: Page, server_url: str):
    """Tennis signal with canonical odds=1.48 and EV+9.1% → Top-Picks visible."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=1.48, ev_pct=9.1)]}
    _home(page, server_url, payload)
    expect(page.locator(".suggest-card")).to_be_visible(timeout=5_000)
    expect(page.locator(".suggest-title")).to_have_text("⚡ Top-Picks")


# ── Tennis: missing odds (field absent) → fail closed ────────────────────────

def test_tennis_missing_odds_excluded(page: Page, server_url: str):
    """Tennis signal with missing odds field → fail closed, excluded from Top-Picks.

    All signals require valid market odds (> 1.0, finite, numeric) to be safely
    renderable and to pass the inclusion gate.
    """
    payload = {**_BASE, "tennis": [_sig_tennis(odds=None, ev_pct=9.1)]}
    _home(page, server_url, payload)
    expect(page.locator(".suggest-card")).not_to_be_visible(timeout=5_000)


# ── Tennis: EV below threshold ────────────────────────────────────────────────

def test_tennis_ev_below_5_excluded(page: Page, server_url: str):
    """Tennis signal with EV+3% (below 5% floor) → NOT in Top-Picks."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=1.48, ev_pct=3.0)]}
    _home(page, server_url, payload)
    expect(page.locator(".suggest-card")).not_to_be_visible(timeout=5_000)


# ── Tennis: decimal_odds field must NOT be used as fallback ──────────────────

def test_tennis_decimal_odds_legacy_field_not_used_as_fallback(page: Page, server_url: str):
    """Signal has decimal_odds=2.0 (old field, never written by scanner) but no odds key.

    The old code did `s.decimal_odds || 2.0` which would fabricate odds=2.0 and
    potentially include the signal. The corrected code must exclude it because
    the canonical odds field is absent → fail closed.
    """
    payload = {**_BASE, "tennis": [_sig_tennis(odds=None, ev_pct=9.1, decimal_odds_field=2.0)]}
    _home(page, server_url, payload)
    # Must be excluded: decimal_odds=2.0 must never substitute for missing canonical odds
    expect(page.locator(".suggest-card")).not_to_be_visible(timeout=5_000)


# ── Football: valid odds ≤ 2.5 ───────────────────────────────────────────────

def test_football_away_valid_odds_shows_top_picks(page: Page, server_url: str):
    """Football away with odds=2.2 and EV+30% → included (odds ≤ 2.5, EV ≥ 25%)."""
    payload = {**_BASE, "football": [_sig_football_away(odds=2.2, ev_pct=30.0)]}
    _home(page, server_url, payload)
    expect(page.locator(".suggest-card")).to_be_visible(timeout=5_000)


# ── Football: missing odds → fail closed ─────────────────────────────────────

def test_football_away_missing_odds_excluded(page: Page, server_url: str):
    """Football away signal with no odds field → fail closed, excluded from Top-Picks."""
    payload = {**_BASE, "football": [_sig_football_away(odds=None, ev_pct=30.0)]}
    _home(page, server_url, payload)
    expect(page.locator(".suggest-card")).not_to_be_visible(timeout=5_000)


# ── Football: null odds → fail closed ────────────────────────────────────────

def test_football_away_null_odds_excluded(page: Page, server_url: str):
    """Football away signal with odds=null → fail closed, excluded."""
    sig = _sig_football_away(ev_pct=30.0)
    sig["odds"] = None
    payload = {**_BASE, "football": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".suggest-card")).not_to_be_visible(timeout=5_000)


# ── Football: zero odds → fail closed ────────────────────────────────────────

def test_football_away_zero_odds_excluded(page: Page, server_url: str):
    """Football away signal with odds=0 → fail closed (not positive), excluded."""
    payload = {**_BASE, "football": [_sig_football_away(odds=0, ev_pct=30.0)]}
    _home(page, server_url, payload)
    expect(page.locator(".suggest-card")).not_to_be_visible(timeout=5_000)


# ── Football: odds > 2.5 → excluded ──────────────────────────────────────────

def test_football_away_high_odds_excluded(page: Page, server_url: str):
    """Football away with odds=2.8 (> 2.5) → excluded by market filter."""
    payload = {**_BASE, "football": [_sig_football_away(odds=2.8, ev_pct=30.0)]}
    _home(page, server_url, payload)
    expect(page.locator(".suggest-card")).not_to_be_visible(timeout=5_000)


# ── Football: decimal_odds field present but no odds → fail closed ────────────

def test_football_decimal_odds_legacy_field_not_used(page: Page, server_url: str):
    """Football signal has decimal_odds=2.0 (old field) but no odds key → fail closed.

    Previously decimal_odds||2.0 would fabricate 2.0 and INCLUDE the signal.
    The corrected code must exclude it because odds is missing.
    """
    sig = _sig_football_away(ev_pct=30.0)
    # No odds key, but has old decimal_odds field
    sig["decimal_odds"] = 2.0
    payload = {**_BASE, "football": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".suggest-card")).not_to_be_visible(timeout=5_000)
