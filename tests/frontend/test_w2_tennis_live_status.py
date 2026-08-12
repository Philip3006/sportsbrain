"""W2 regression: Tennis LIVE/COMPLETED must not be inferred from elapsed time.

Hard invariant: scheduled time is NEVER authoritative evidence for LIVE or
COMPLETED state. Tennis fixtures with past kickoffs and no live evidence must
render a neutral state, not LIVE.

Tests use the Today-section in renderHome(), which is visible at page load.
Schedule entries with past kickoffs are injected via the signals.json payload.
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


def _ko_past(hours: float) -> str:
    """Return an ISO kickoff timestamp that is `hours` in the past."""
    return (_NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ko_future(hours: float) -> str:
    return (_NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


_BASE: dict = {
    "updated": _NOW.isoformat(),
    "football": [],
    "tennis": [],
    "open_bets": [],
    "settled_bets": [],
    "bankroll_state": {"start": 100.0, "free": 100.0, "staked": 0, "exposure_pct": 0, "max_win": 0, "pnl_closed": 0},
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
    page.wait_for_timeout(800)


def _sched_entry(sport: str, kickoff: str) -> dict:
    return {"sport": sport, "home": "Player A", "away": "Player B", "kickoff": kickoff}


# ── 1. Tennis kickoff passed → NOT LIVE ──────────────────────────────────────

def test_tennis_kickoff_passed_no_live_evidence_not_live(page: Page, server_url: str):
    """W2-1: Tennis with kickoff just passed (30 min ago) must NOT show LIVE badge."""
    schedule = [_sched_entry("tennis", _ko_past(0.5))]
    payload = {**_BASE, "schedule": schedule}
    _home(page, server_url, payload)
    # No LIVE badge must appear
    expect(page.locator(".today-live-badge")).to_have_count(0, timeout=5_000)
    # Truthful neutral state should appear instead
    expect(page.locator("text=Status unbekannt")).to_be_visible(timeout=3_000)


# ── 2. Tennis kickoff 90 min ago → NOT LIVE ──────────────────────────────────

def test_tennis_kickoff_90min_ago_not_live(page: Page, server_url: str):
    """W2-2: Tennis 90 min past kickoff, no live evidence → NOT LIVE."""
    schedule = [_sched_entry("tennis", _ko_past(1.5))]
    payload = {**_BASE, "schedule": schedule}
    _home(page, server_url, payload)
    expect(page.locator(".today-live-badge")).to_have_count(0, timeout=5_000)


# ── 3. Tennis kickoff 3h ago → NOT COMPLETED ─────────────────────────────────

def test_tennis_kickoff_3h_ago_not_completed(page: Page, server_url: str):
    """W2-3: Tennis 3h past kickoff is outside Today window → no today-row shown.

    The Today section window is kickoff within [-1h, +24h]. An entry 3h in the past
    is excluded entirely, so no completed/LIVE inference can surface for it.
    """
    schedule = [_sched_entry("tennis", _ko_past(3.0))]
    payload = {**_BASE, "schedule": schedule}
    _home(page, server_url, payload)
    # Entry must not appear in Today section at all
    expect(page.locator(".today-row")).to_have_count(0, timeout=3_000)
    # The countdown ticker "✓ Abgeschlossen" must not appear in any countdown element
    expect(page.locator(".match-countdown:has-text('Abgeschlossen')")).to_have_count(0, timeout=3_000)


# ── 4. Football kickoff passed → LIVE badge shown ────────────────────────────

def test_football_kickoff_passed_shows_live_badge(page: Page, server_url: str):
    """W2-4: Football match with past kickoff MAY still show LIVE (time-inference OK for football)."""
    schedule = [_sched_entry("football", _ko_past(0.5))]
    payload = {**_BASE, "schedule": schedule}
    _home(page, server_url, payload)
    expect(page.locator(".today-live-badge")).to_have_count(1, timeout=5_000)


# ── 5. Tennis future kickoff → normal countdown, no live badge ───────────────

def test_tennis_future_kickoff_no_live_badge(page: Page, server_url: str):
    """W2-5: Tennis match starting in 2h must show countdown, not LIVE."""
    schedule = [_sched_entry("tennis", _ko_future(2.0))]
    payload = {**_BASE, "schedule": schedule}
    _home(page, server_url, payload)
    expect(page.locator(".today-live-badge")).to_have_count(0, timeout=5_000)
    # Should show an "in Xh Ymin" countdown
    expect(page.locator(".today-countdown")).to_be_visible(timeout=3_000)


# ── 6. Today section window: past kickoff > 1h excluded entirely ─────────────

def test_tennis_kickoff_far_past_excluded_from_today(page: Page, server_url: str):
    """W2-6: Tennis kickoff 4h ago is outside the Today window → no entry shown."""
    schedule = [_sched_entry("tennis", _ko_past(4.0))]
    payload = {**_BASE, "schedule": schedule}
    _home(page, server_url, payload)
    # No today-row at all (window is -1h to +24h from now)
    expect(page.locator(".today-row")).to_have_count(0, timeout=5_000)
