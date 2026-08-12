"""Regression tests: _evScore() Top Empfehlungen inclusion/exclusion logic.

Tests the canonical Top Empfehlungen – nächste 24h section:
1. Uses canonical "odds" field (not "decimal_odds") for all signal types.
2. Fails closed for missing/null/zero/non-positive odds in odds-dependent evaluations.
3. Allows tennis match-winner signals with EV >= 5% regardless of odds magnitude.
4. Football home/away signals require valid odds; odds > 2.5 are excluded.
5. Exactly one recommendation section renders on the homepage (no Smart Suggest duplicate).
6. EDGE_LOST/UNREFRESHABLE/shadow signals (signal_status != ACTIVE) are excluded.
7. current_odds/current_ev_pct are used when available.
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


def _sig_tennis(odds=None, ev_pct=9.1, decimal_odds_field=None, confidence="HIGH",
                signal_status=None, current_odds=None, current_ev_pct=None):
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
    if signal_status is not None:
        s["signal_status"] = signal_status
    if current_odds is not None:
        s["current_odds"] = current_odds
    if current_ev_pct is not None:
        s["current_ev_pct"] = current_ev_pct
    return s


def _sig_football_away(odds=None, ev_pct=30.0, decimal_odds_field=None, match="TeamA vs TeamB"):
    """Build a football away signal."""
    s = {
        "sport": "football",
        "match": match,
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


# ── Structural: exactly one recommendation section ───────────────────────────

def test_exactly_one_recommendation_section(page: Page, server_url: str):
    """Homepage must render exactly one canonical recommendation section (.toprec-card)."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=1.48, ev_pct=9.1)]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-card")).to_have_count(1, timeout=5_000)


def test_smart_suggest_section_not_rendered(page: Page, server_url: str):
    """Old Smart Suggest / Top-Picks duplicate section (.suggest-card) must not render."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=1.48, ev_pct=9.1)]}
    _home(page, server_url, payload)
    expect(page.locator(".suggest-card")).to_have_count(0, timeout=5_000)


def test_canonical_title_label(page: Page, server_url: str):
    """Canonical section must display German product label 'TOP EMPFEHLUNGEN'."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=1.48, ev_pct=9.1)]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-title")).to_have_text("TOP EMPFEHLUNGEN", timeout=5_000)


# ── Signal count cases ────────────────────────────────────────────────────────

def test_five_signals_render_five_picks(page: Page, server_url: str):
    """Five qualifying signals → five .toprec-pick elements visible."""
    tennis_sigs = []
    for i in range(5):
        s = _sig_tennis(odds=1.48 + i * 0.05, ev_pct=9.1 + i)
        s["match"] = f"Player{i}A vs Player{i}B"
        tennis_sigs.append(s)
    payload = {**_BASE, "tennis": tennis_sigs}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(5, timeout=5_000)


def test_one_signal_renders_one_pick(page: Page, server_url: str):
    """One qualifying signal → exactly one .toprec-pick."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=1.48, ev_pct=9.1)]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(1, timeout=5_000)


def test_zero_signals_empty_state(page: Page, server_url: str):
    """Zero qualifying signals → .toprec-empty renders, no .toprec-pick."""
    payload = {**_BASE, "tennis": [], "football": []}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-empty")).to_be_visible(timeout=5_000)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


# ── Tennis: valid canonical odds ──────────────────────────────────────────────

def test_tennis_valid_odds_shows_top_empfehlungen(page: Page, server_url: str):
    """Tennis signal with canonical odds=1.48 and EV+9.1% → Top Empfehlungen pick visible."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=1.48, ev_pct=9.1)]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-card")).to_be_visible(timeout=5_000)
    expect(page.locator(".toprec-pick")).to_have_count(1, timeout=5_000)


# ── Tennis: missing odds (field absent) → fail closed ────────────────────────

def test_tennis_missing_odds_excluded(page: Page, server_url: str):
    """Tennis signal with missing odds field → fail closed, excluded from Top Empfehlungen."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=None, ev_pct=9.1)]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)
    expect(page.locator(".toprec-empty")).to_be_visible(timeout=5_000)


# ── Tennis: EV below threshold ────────────────────────────────────────────────

def test_tennis_ev_below_5_excluded(page: Page, server_url: str):
    """Tennis signal with EV+3% (below 5% floor) → NOT in Top Empfehlungen."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=1.48, ev_pct=3.0)]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


# ── Tennis: decimal_odds field must NOT be used as fallback ──────────────────

def test_tennis_decimal_odds_legacy_field_not_used_as_fallback(page: Page, server_url: str):
    """Signal has decimal_odds=2.0 (old field) but no odds key → excluded (fail closed)."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=None, ev_pct=9.1, decimal_odds_field=2.0)]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


# ── signal_status gate: EDGE_LOST/UNREFRESHABLE/shadow excluded ───────────────

def test_edge_lost_signal_excluded(page: Page, server_url: str):
    """EDGE_LOST signal must be excluded even if EV score is positive."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=1.48, ev_pct=9.1, signal_status="EDGE_LOST")]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_unrefreshable_signal_excluded(page: Page, server_url: str):
    """UNREFRESHABLE signal must be excluded from Top Empfehlungen."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=1.48, ev_pct=9.1, signal_status="UNREFRESHABLE")]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_active_signal_status_passes(page: Page, server_url: str):
    """ACTIVE signal_status passes through the gate correctly."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=1.48, ev_pct=9.1, signal_status="ACTIVE")]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(1, timeout=5_000)


def test_no_signal_status_passes_for_backward_compat(page: Page, server_url: str):
    """Signals without signal_status field pass through (backward compatibility)."""
    s = _sig_tennis(odds=1.48, ev_pct=9.1)
    assert "signal_status" not in s
    payload = {**_BASE, "tennis": [s]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(1, timeout=5_000)


# ── current_odds / current_ev_pct (O1-7 refreshed odds) ──────────────────────

def test_current_odds_used_when_available(page: Page, server_url: str):
    """When current_odds is set, Top Empfehlungen must display it (not scan-time odds)."""
    payload = {**_BASE, "tennis": [_sig_tennis(odds=1.48, ev_pct=9.1,
                                                current_odds=1.62, current_ev_pct=14.5)]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(1, timeout=5_000)
    # current_odds=1.62 must be displayed, not scan-time 1.48
    expect(page.locator(".toprec-odds")).to_have_text("1.62", timeout=5_000)


# ── Football signals ──────────────────────────────────────────────────────────

def test_football_away_valid_odds_shows_top_empfehlungen(page: Page, server_url: str):
    """Football away with odds=2.2 and EV+30% → included (odds ≤ 2.5, EV ≥ 25%)."""
    payload = {**_BASE, "football": [_sig_football_away(odds=2.2, ev_pct=30.0)]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(1, timeout=5_000)


def test_football_away_missing_odds_excluded(page: Page, server_url: str):
    """Football away signal with no odds field → fail closed, excluded."""
    payload = {**_BASE, "football": [_sig_football_away(odds=None, ev_pct=30.0)]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_football_away_null_odds_excluded(page: Page, server_url: str):
    """Football away signal with odds=null → fail closed, excluded."""
    sig = _sig_football_away(ev_pct=30.0)
    sig["odds"] = None
    payload = {**_BASE, "football": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_football_away_zero_odds_excluded(page: Page, server_url: str):
    """Football away signal with odds=0 → fail closed (not positive), excluded."""
    payload = {**_BASE, "football": [_sig_football_away(odds=0, ev_pct=30.0)]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_football_away_high_odds_excluded(page: Page, server_url: str):
    """Football away with odds=2.8 (> 2.5) → excluded by market filter."""
    payload = {**_BASE, "football": [_sig_football_away(odds=2.8, ev_pct=30.0)]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)


def test_football_decimal_odds_legacy_field_not_used(page: Page, server_url: str):
    """Football signal has decimal_odds=2.0 (old field) but no odds key → fail closed."""
    sig = _sig_football_away(ev_pct=30.0)
    sig["decimal_odds"] = 2.0
    payload = {**_BASE, "football": [sig]}
    _home(page, server_url, payload)
    expect(page.locator(".toprec-pick")).to_have_count(0, timeout=5_000)
